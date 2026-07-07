"""CallAgentService / CallSession 行为单测（FakeModem 驱动，无硬件）。"""

from __future__ import annotations

import asyncio
import time

from fakes import FakeAgent, FakeAudioBridge, FakeModem

from agentcall.call_agent import CallAgentService, CallSession
from agentcall.events import EventHub


def make_service(modem: FakeModem, hub: EventHub | None = None) -> CallAgentService:
    return CallAgentService(
        modem_port="unused",
        audio_keyword="unused",
        provider="qwen",
        hub=hub,
        modem=modem,  # type: ignore[arg-type]  # FakeModem 与 Eg25Modem 同形
    )


def make_hub() -> EventHub:
    return EventHub(asyncio.new_event_loop())


# ---- RING 去重 ----

def test_ring_starts_session_once(monkeypatch):
    modem = FakeModem()
    service = make_service(modem)
    starts: list[str | None] = []

    def fake_start(outbound_number: str | None = None) -> None:
        starts.append(outbound_number)
        service.session._active = True  # 模拟会话进行中

    monkeypatch.setattr(service.session, "start", fake_start)

    modem.trigger_ring("13800000000")
    modem.trigger_ring("13800000000")  # RING 与 CLCC 轮询重复触发

    assert starts == [None]


# ---- 外呼互斥与等待接通 ----

def test_dial_rejected_when_session_active(monkeypatch):
    modem = FakeModem()
    service = make_service(modem)
    monkeypatch.setattr(
        service.session, "start",
        lambda outbound_number=None: setattr(service.session, "_active", True),
    )

    ok, err = service.dial("13900000000")
    assert ok

    ok2, err2 = service.dial("13911111111")
    assert not ok2
    assert err2


def test_dial_empty_number_rejected():
    service = make_service(FakeModem())
    ok, err = service.dial("  ")
    assert not ok


def test_wait_connected_times_out():
    modem = FakeModem()
    session = make_service(modem).session
    session._active = True

    assert asyncio.run(session._wait_connected(timeout=0.2)) is False


def test_wait_connected_success():
    modem = FakeModem()
    session = make_service(modem).session
    session._active = True
    modem.trigger_call_connected()

    assert asyncio.run(session._wait_connected(timeout=0.2)) is True


# ---- 工具处理 ----

def test_tool_send_sms_uses_current_caller():
    modem = FakeModem()
    session = make_service(modem).session
    session.current_caller = "13800000000"

    result = session._tool_send_sms({"content": "你好"})

    assert result["success"] is True
    assert ("send_sms", ("13800000000", "你好")) in modem.calls


def test_tool_send_sms_failure_reported():
    modem = FakeModem()
    modem.sms_should_succeed = False
    session = make_service(modem).session
    session.current_caller = "13800000000"

    result = session._tool_send_sms({"content": "hi"})
    assert result["success"] is False


def test_tool_query_code_finds_keyword_sms():
    hub = make_hub()
    hub.publish({"type": "sms_in", "sender": "10086", "text": "余额 1000 元"})
    hub.publish({"type": "sms_in", "sender": "95588", "text": "您的验证码是 482913，5分钟内有效"})
    session = make_service(FakeModem(), hub=hub).session

    result = session._tool_query_code({})

    assert result["success"] is True
    assert result["code"] == "482913"


def test_tool_query_code_no_sms():
    session = make_service(FakeModem(), hub=make_hub()).session
    assert session._tool_query_code({})["success"] is False


# ---- 来电全链路：接听 → 开场白 → 下行音频 → 挂断收尾 ----

def test_inbound_call_full_lifecycle(monkeypatch):
    monkeypatch.setenv("OWNER_NAME", "田野")
    monkeypatch.setenv("AGENT_PERSONA", "数字分身")
    modem = FakeModem()
    hub = make_hub()
    bridge = FakeAudioBridge()
    agent = FakeAgent()

    monkeypatch.setattr("agentcall.call_agent.create_audio_bridge", lambda **kw: bridge)
    monkeypatch.setattr("agentcall.call_agent.create_agent", lambda provider: agent)

    service = make_service(modem, hub=hub)
    modem.trigger_ring("13800000000")

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not bridge.downlink:
        time.sleep(0.05)

    service.session.stop()
    assert service.session._thread is not None
    service.session._thread.join(timeout=5)

    assert "answer" in modem.call_names()  # ATA 接听
    assert agent.started and agent.said  # 开场白已发
    assert "田野的数字分身" in agent._session_instructions
    assert "不方便接" in agent.said[0]
    assert bridge.downlink  # 开场白 PCM 写回模组
    assert agent.stopped and bridge.stopped  # 会话收尾
    assert "hangup" in modem.call_names()  # 物理挂断兜底

    statuses = [e["status"] for e in hub.history() if e.get("type") == "call"]
    assert statuses[0] == "ringing"
    assert "answered" in statuses
    assert statuses[-1] == "ended"


def test_outbound_call_uses_digital_twin_prompt(monkeypatch):
    monkeypatch.setenv("OWNER_NAME", "田野")
    monkeypatch.setenv("AGENT_PERSONA", "数字分身")
    modem = FakeModem()
    bridge = FakeAudioBridge()
    agent = FakeAgent()

    monkeypatch.setenv("AGENT_OUTBOUND_TASK", "查询本机套餐和剩余流量")
    monkeypatch.setattr("agentcall.call_agent.create_audio_bridge", lambda **kw: bridge)
    monkeypatch.setattr("agentcall.call_agent.create_agent", lambda provider: agent)

    service = make_service(modem)
    ok, err = service.dial("10000")
    assert ok, err
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and ("dial", ("10000",)) not in modem.calls:
        time.sleep(0.05)
    modem.trigger_call_connected("10000")

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not bridge.downlink:
        time.sleep(0.05)

    service.session.stop()
    assert service.session._thread is not None
    service.session._thread.join(timeout=5)

    assert ("dial", ("10000",)) in modem.calls
    assert "田野的数字分身" in agent._session_instructions
    assert "查询本机套餐和剩余流量" in agent._session_instructions
    assert "不要问对方“有什么可以帮您”" in agent._session_instructions
    assert "田野让我打这个电话" in agent.said[0]
    assert "查询本机套餐和剩余流量" in agent.said[0]
