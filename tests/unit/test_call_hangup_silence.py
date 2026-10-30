"""挂断停写入口及兼容性，仅使用假桥/假模组。"""

import asyncio
import logging
from types import SimpleNamespace

import pytest
from fakes import FakeAgent, FakeAudioBridge, FakeModem

from agentcall.call_agent import CallAgentService, CallSession


@pytest.mark.parametrize("entry", ["shutdown", "remote", "remote_owner"])
@pytest.mark.parametrize("method", ["supported", "missing", "failing"])
def test_hangup_silences_before_cleanup_and_drains_queue(monkeypatch, caplog, entry, method):
    modem = FakeModem()
    service = CallAgentService("unused", "unused", provider="openai", modem=modem)
    session = service.session
    bridge = FakeAudioBridge()
    events = []

    def silence():
        events.append("silence")
        if method == "failing":
            raise OSError("fake bridge error")
        return 1280

    if method != "missing":
        bridge.silence_for_hangup = silence
    session._start_hangup_bridge(bridge)
    session._outgoing_audio.put(b"\x01\x00" * 320)
    session._outgoing_audio.put(b"\x02\x00" * 320)

    def check_cleanup(name):
        assert session._outgoing_audio.empty()
        assert events == ([] if method == "missing" else ["silence"])
        events.append(name)

    monkeypatch.setattr(modem, "hangup", lambda: events.append("hangup"))
    caplog.set_level(logging.DEBUG)
    if entry == "shutdown":
        async def stop_resources(*args, **kwargs):
            check_cleanup("resources")
        monkeypatch.setattr(session, "_stop_agent_resources", stop_resources)
        asyncio.run(session._shutdown_agent(None, bridge))
        assert events[-2:] == ["resources", "hangup"]
    else:
        monkeypatch.setattr(session, "stop", lambda: check_cleanup("stop"))
        if entry == "remote_owner":
            service._remote_call_owner = SimpleNamespace(
                _bridge=bridge, request_call_stop=lambda reason: check_cleanup(reason),
            )
        modem.trigger_hangup()
        assert events[-1] == ("remote_party_hangup" if entry == "remote_owner" else "hangup")
    assert "挂断停写下行" in caplog.text
    assert "队列=1280 bytes/2 块" in caplog.text
    if method == "failing":
        assert "挂断停写失败" in caplog.text


def test_bridge_start_after_hangup_is_also_silenced_and_late_audio_is_fenced(monkeypatch):
    session = CallSession(FakeModem(), "unused", "openai", "nmea", "unused", 115200, 1.0)
    session._set_active(True)
    session._silence_for_hangup(None, "remote_party_hangup")
    bridge = FakeAudioBridge()
    calls = []
    bridge.silence_for_hangup = lambda: calls.append("silence") or 0
    session._start_hangup_bridge(bridge)
    assert calls == ["silence"]
    assert not bridge.started
    handler = session._make_agent_audio_handler(FakeAgent(), bridge, None)
    handler(b"\x01\x00" * 320)
    assert session._outgoing_audio.empty()
    session._outgoing_audio.put(b"\x01\x00" * 320)
    session._drain_agent_audio(bridge)
    assert session._outgoing_audio.empty()
    assert bridge.downlink == []
    # 下一通 start 必须解除会话级停写闸门，不实际启动通话线程。
    monkeypatch.setattr("agentcall.call_agent.threading.Thread.start", lambda _self: None)
    session._set_active(False)
    session.start()
    assert not session._hangup_silenced
    session._set_active(False)


def test_shutdown_also_silences_current_bridge_when_caller_holds_old_bridge():
    session = CallSession(FakeModem(), "unused", "openai", "nmea", "unused", 115200, 1.0)
    old_bridge = FakeAudioBridge()
    current_bridge = FakeAudioBridge()
    calls = []
    old_bridge.silence_for_hangup = lambda: calls.append("old") or 0
    current_bridge.silence_for_hangup = lambda: calls.append("current") or 0
    session._start_hangup_bridge(current_bridge)
    asyncio.run(session._shutdown_agent(None, old_bridge))
    assert calls == ["old", "current"]
