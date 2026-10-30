"""PCM 死路保护：只用假串口、假时钟，不访问模组。"""

import asyncio
import json
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import serial
from fakes import FakeAgent, FakeAudioBridge, FakeModem

from agentcall import audio_bridge, call_agent, config
from agentcall import modem as modem_module
from agentcall.audio_bridge import SerialPcmAudioBridge
from agentcall.call_agent import CallSession
from agentcall.call_log import CallLogger
from agentcall.modem import Sim7600Modem


@pytest.fixture
def pcm_clock(monkeypatch):
    clock = SimpleNamespace(now=100.0)
    monkeypatch.setattr(audio_bridge, "time", SimpleNamespace(monotonic=lambda: clock.now))
    monkeypatch.setenv("PCM_STALL_SECONDS", "5")
    bridge = SerialPcmAudioBridge("unused-fake-pcm")
    port = Mock()
    port.in_waiting = 0
    port.read.return_value = b""
    monkeypatch.setattr(bridge, "_open_serial", lambda: port)
    monkeypatch.setattr(audio_bridge.threading.Thread, "start", lambda self: None)
    return clock, bridge, port


def test_pcm_stall_clock_starts_at_bridge_start_not_construction(pcm_clock):
    clock, bridge, _port = pcm_clock
    clock.now += 100
    assert bridge.pcm_stalled() is None
    bridge.start()
    clock.now += 4.9
    assert bridge.pcm_stalled() is None
    clock.now += 0.2
    assert bridge.pcm_stalled() is not None


def test_pcm_stall_counts_any_received_byte_and_reset_restarts_window(pcm_clock):
    clock, bridge, port = pcm_clock
    bridge.start()
    clock.now += 4
    port.read.return_value = b"\x00"
    bridge._read_serial_chunk()
    clock.now += 4
    assert bridge.pcm_stalled() is None
    clock.now += 1.1
    assert bridge.pcm_stalled() is not None
    bridge._write_timeouts = 4
    bridge.reset_stall_clock()
    assert bridge._write_timeouts == 0
    clock.now += 4.9
    assert bridge.pcm_stalled() is None
    clock.now += 0.2
    assert bridge.pcm_stalled() is not None


def test_pcm_stall_requires_three_consecutive_write_timeouts(pcm_clock):
    _clock, bridge, port = pcm_clock
    bridge.start()
    port.write.side_effect = serial.SerialTimeoutException("fake timeout")
    for _ in range(2):
        bridge._write_frame(b"\x00\x00", _clock.now)
        assert bridge.pcm_stalled() is None
    bridge._write_frame(b"\x00\x00", _clock.now)
    assert bridge.pcm_stalled() is not None
    port.write.side_effect = None
    port.write.return_value = 2
    bridge._write_frame(b"\x00\x00", _clock.now)
    assert bridge.pcm_stalled() is None


@pytest.fixture
def stall_modem(monkeypatch, tmp_path):
    monkeypatch.setenv("MODEM_PCM_PIN_8K", "false")
    monkeypatch.setenv("MODEM_RESET_AFTER_CALL", "false")
    monkeypatch.setenv("MODEM_LOG_RELEASE_CAUSE", "false")
    modem = Sim7600Modem("unused-fake-at")
    commands = []
    monkeypatch.setattr(modem, "_send", lambda cmd: commands.append(cmd) or "OK")
    monkeypatch.setattr(modem, "_CPCMREG_ENABLE_RETRY_DELAY", 0)
    monkeypatch.setattr(modem, "RECOVERY_REQUEST_PATH", str(tmp_path / "recover"))
    return modem, commands, tmp_path / "recover"


def test_pcm_stall_rearm_uses_bounded_enable_and_defers_recovery(stall_modem, monkeypatch):
    modem, commands, request = stall_modem
    modem.answer()
    commands.clear()
    replies = iter(["ERROR", "OK"])

    def send(cmd):
        commands.append(cmd)
        return next(replies) if cmd == "AT+CPCMREG=1" else "OK"

    monkeypatch.setattr(modem, "_send", send)
    assert modem.rearm_voice_channel()
    assert commands == ["AT+CPCMREG=0", "AT+CPCMREG=1", "AT+CPCMREG=1"]
    modem.mark_pcm_degraded()
    assert modem._pcm_degraded
    assert not request.exists()
    modem.hangup()
    assert request.exists()
    assert not modem._voice_call_armed


def test_pcm_stall_rearm_idle_does_not_send(stall_modem):
    modem, commands, _request = stall_modem
    assert not modem.rearm_voice_channel()
    assert commands == []


@pytest.mark.parametrize("ended", [True, False])
def test_pcm_stall_rearm_stops_when_generation_changes(stall_modem, monkeypatch, ended):
    modem, commands, request = stall_modem
    modem.answer()
    commands.clear()

    def send(cmd):
        commands.append(cmd)
        if cmd == "AT+CPCMREG=1":
            if ended:
                modem._voice_call_armed = False
            else:
                modem._voice_call_generation += 1
            return "ERROR"
        return "OK"

    monkeypatch.setattr(modem, "_send", send)
    assert not modem.rearm_voice_channel()
    assert commands == ["AT+CPCMREG=0", "AT+CPCMREG=1"]
    assert not request.exists()


def test_pcm_stall_safe_config_defaults():
    assert config.get_spec("PCM_STALL_RECOVERY").default == "false"
    assert config.get_spec("PCM_STALL_SECONDS").default == "5"


@pytest.mark.parametrize("last_ok", [False, True])
def test_pcm_stall_old_rearm_result_cannot_overwrite_next_call(stall_modem, monkeypatch, last_ok):
    modem, _commands, request = stall_modem
    modem.answer()
    monkeypatch.setattr(modem, "_CPCMREG_ENABLE_ATTEMPTS", 3)
    replies = iter(["ERROR", "ERROR", "OK" if last_ok else "ERROR"])
    monkeypatch.setattr(modem, "_send", lambda cmd: next(replies) if cmd == "AT+CPCMREG=1" else "OK")
    new_snapshot = {"ok": True, "attempts": 1, "degraded": False}

    def log(message, *args):
        if "CPCMREG 第" in message and args[0] == 3:
            modem.hangup()
            modem.answer()
            modem.last_voice_enable = new_snapshot

    monkeypatch.setattr(modem_module.logger, "info", log)
    assert not modem.rearm_voice_channel()
    assert modem.last_voice_enable == new_snapshot
    assert not modem._pcm_degraded
    assert not request.exists()


def test_pcm_stall_expired_generation_does_not_clear_snapshot(stall_modem):
    modem, commands, _request = stall_modem
    modem.answer()
    old_generation = modem._voice_call_generation
    modem.hangup()
    modem.answer()
    snapshot = {"ok": True, "attempts": 1, "degraded": False}
    modem.last_voice_enable = snapshot
    commands.clear()
    modem.initialize_for_voice(expected_generation=old_generation)
    assert commands == []
    assert modem.last_voice_enable == snapshot


def test_pcm_stall_rearm_failure_is_bounded_without_recovery_request(stall_modem, monkeypatch):
    modem, commands, request = stall_modem
    modem.answer()
    commands.clear()
    monkeypatch.setattr(modem, "_send", lambda cmd: commands.append(cmd) or "ERROR")
    assert not modem.rearm_voice_channel()
    assert commands == ["AT+CPCMREG=0"] + ["AT+CPCMREG=1"] * modem._CPCMREG_ENABLE_ATTEMPTS
    assert not request.exists()


def test_pcm_stall_reader_thread_updates_clock_even_for_digital_silence(pcm_clock):
    clock, bridge, port = pcm_clock
    bridge.start()
    clock.now += 10

    def read(_size):
        bridge._running = False
        return b"\x00\x00"

    port.read.side_effect = read
    bridge._read_loop()
    bridge._running = True
    clock.now += 4.9
    assert bridge.pcm_stalled() is None
    clock.now += 0.2
    assert bridge.pcm_stalled() == "no_uplink_bytes"


@pytest.fixture
def stall_call(stall_modem, monkeypatch, tmp_path):
    modem, commands, request = stall_modem
    modem.POST_ANSWER_SETTLE_SECONDS = 0
    monkeypatch.setenv("PCM_STALL_SECONDS", "5")
    monkeypatch.setenv("PCM_STALL_RECOVERY", "true")
    monkeypatch.setenv("PROMPT_GEN_ENABLED", "false")
    monkeypatch.setenv("WRAP_UP_JUDGE_ENABLED", "false")
    # 只测卡死状态机：固定关掉平台相关的启动/收尾分支。Linux 默认的模组接听问候
    # 会在假时钟上空转掉 100 多秒启动时间，撞上下面「120 秒结束」；通话后重启
    # 模组则让每个用例多等十几秒。
    monkeypatch.setenv("MODEM_ANSWER_GREETING", "")
    monkeypatch.setenv("MODEM_RESET_AFTER_CALL", "false")
    clock = SimpleNamespace(now=100.0)
    monkeypatch.setattr(call_agent, "time", SimpleNamespace(monotonic=lambda: clock.now, time=time.time))

    async def tick(seconds):
        clock.now += max(seconds, 0.1)
        await asyncio.sleep(0)

    monkeypatch.setattr(call_agent, "asyncio", SimpleNamespace(**{**vars(asyncio), "sleep": tick}))
    bridge = FakeAudioBridge()
    agent = FakeAgent()
    base = tmp_path / "calls"
    session = CallSession(modem, "unused", "openai", "nmea", "unused", 115200, 1.0,
                          call_logger=CallLogger(base, recording_enabled=True))
    session._set_active(True)
    checks = []
    resets = []
    rearm_threads = []
    state = SimpleNamespace(recovered=False, rearm_ok=True)

    def stalled():
        checks.append(clock.now)
        return None if state.recovered else "consecutive_write_timeouts"

    def reset():
        resets.append(clock.now)

    original_rearm = modem.rearm_voice_channel

    def rearm():
        rearm_threads.append(threading.get_ident())
        assert not request.exists()
        return original_rearm() if state.rearm_ok else False

    def read():
        if clock.now >= 120:
            session._set_active(False)
        return b""

    bridge.pcm_stalled = stalled
    bridge.reset_stall_clock = reset
    monkeypatch.setattr(bridge, "read_modem_chunk", read)
    monkeypatch.setattr(modem, "rearm_voice_channel", rearm)
    monkeypatch.setattr(call_agent, "create_agent", lambda _provider: agent)
    monkeypatch.setattr(call_agent, "create_audio_bridge", lambda **_kw: bridge)
    return SimpleNamespace(session=session, modem=modem, commands=commands, request=request,
                           agent=agent, bridge=bridge, clock=clock, checks=checks,
                           resets=resets, rearm_threads=rearm_threads, state=state, base=base)


def call_result(call):
    folder = next(call.base.iterdir())
    events = [json.loads(line) for line in (folder / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    return json.loads((folder / "meta.json").read_text(encoding="utf-8")), events


def test_pcm_stall_disabled_only_records_once_and_keeps_call(stall_call, monkeypatch):
    call = stall_call
    monkeypatch.setenv("PCM_STALL_RECOVERY", "false")
    call.session.call_started_at = time.time() - 12
    asyncio.run(call.session._handle_call())
    meta, events = call_result(call)
    assert meta["status"] == "completed"
    detected = [e for e in events if e["type"] == "pcm_stall_detected"]
    assert len(detected) == 1
    assert detected[0]["call_seconds"] >= 11.98
    assert not any(e["type"] in {"pcm_rearm", "pcm_stall_unrecovered"} for e in events)
    assert not call.resets and not call.rearm_threads and not call.request.exists()
    assert call.clock.now >= 120
    assert len(call.agent.said) == 1


def test_pcm_stall_rearm_recovers_and_continues(stall_call, monkeypatch):
    call = stall_call
    reset = call.bridge.reset_stall_clock

    def recovered():
        reset()
        call.state.recovered = True

    monkeypatch.setattr(call.bridge, "reset_stall_clock", recovered)
    asyncio.run(call.session._handle_call())
    meta, events = call_result(call)
    assert meta["status"] == "completed"
    assert len(call.resets) == 1
    assert call.rearm_threads == [call.rearm_threads[0]]
    assert call.rearm_threads[0] != threading.get_ident()
    rearm = next(e for e in events if e["type"] == "pcm_rearm")
    assert rearm["ok"] is True and rearm["elapsed_seconds"] >= 0
    assert not any(e["type"] == "pcm_stall_unrecovered" for e in events)
    assert call.clock.now >= 120
    assert all(b - a >= 0.98 for a, b in zip(call.checks, call.checks[1:]))
    assert not call.request.exists()


@pytest.mark.parametrize("parallel", [False, True])
@pytest.mark.parametrize("rearm_ok", [False, True])
def test_pcm_stall_unrecovered_says_scenario_hangs_up_and_records_failed(stall_call, monkeypatch, parallel, rearm_ok):
    call = stall_call
    monkeypatch.setenv("PARALLEL_AGENT_CONNECT", str(parallel).lower())
    call.state.rearm_ok = rearm_ok
    asyncio.run(call.session._handle_call())
    meta, events = call_result(call)
    assert meta["status"] == "failed"
    assert len(call.resets) == 1 and len(call.rearm_threads) == 1
    rearm = next(e for e in events if e["type"] == "pcm_rearm")
    assert rearm["ok"] is rearm_ok
    assert any(e["type"] == "pcm_stall_unrecovered" for e in events)
    assert call.checks[1] - call.resets[0] >= 4.98
    assert call.request.exists()
    assert "AT+CHUP" in call.commands
    assert call.agent.stopped and call.bridge.stopped
    assert len(call.agent.said) == 2
    assert "一句" in call.agent.said[-1] or "one" in call.agent.said[-1]
    assert len(call.bridge.downlink) >= 2
    assert call.clock.now < 115


@pytest.mark.parametrize("case", ["other_modem", "other_bridge"])
def test_pcm_stall_does_not_affect_other_transports(stall_call, case, monkeypatch):
    call = stall_call
    if case == "other_modem":
        call.session.modem = FakeModem()
    else:
        del call.bridge.pcm_stalled
        del call.bridge.reset_stall_clock
    asyncio.run(call.session._handle_call())
    meta, events = call_result(call)
    assert meta["status"] == "completed"
    assert not any(e["type"].startswith("pcm_stall") for e in events)
    assert not call.checks and not call.resets and not call.rearm_threads


def test_pcm_stall_failure_result_survives_remote_hangup_during_explanation(stall_call, monkeypatch):
    call = stall_call
    monkeypatch.setenv("PARALLEL_AGENT_CONNECT", "true")
    say = call.agent.say

    async def remote_hangup(instructions):
        await say(instructions)
        if len(call.agent.said) == 2:
            call.session.stop()

    monkeypatch.setattr(call.agent, "say", remote_hangup)
    asyncio.run(call.session._handle_call())
    meta, events = call_result(call)
    assert any(e["type"] == "pcm_stall_unrecovered" for e in events)
    assert meta["status"] == "failed"


def test_pcm_stall_cancel_waits_for_rearm_thread_before_hangup(stall_call, monkeypatch):
    call = stall_call
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def blocked_rearm():
        entered.set()
        assert release.wait(timeout=5)
        finished.set()
        return True

    monkeypatch.setattr(call.modem, "rearm_voice_channel", blocked_rearm)

    async def run():
        task = asyncio.create_task(call.session._handle_call())
        try:
            assert await asyncio.to_thread(entered.wait, 3)
            task.cancel()
            await asyncio.sleep(0)
            assert "AT+CHUP" not in call.commands
            assert not task.done()
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
        assert finished.is_set()
        assert "AT+CHUP" in call.commands
        assert call.bridge.stopped and call.agent.stopped

    asyncio.run(run())
