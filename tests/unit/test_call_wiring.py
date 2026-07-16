"""核心接线单测：通话记录/摘要/批量外呼/本地监听接进 CallAgentService 主流程。

用 FakeModem/FakeAudioBridge/FakeAgent 驱动完整会话，
验证 call_log、summarizer、dial_queue、monitor_playback 的接线点。
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from pathlib import Path

import numpy as np
import pytest
from fakes import FakeAgent, FakeAudioBridge, FakeModem

from agentcall import config
from agentcall.call_agent import CallAgentService
from agentcall.call_log import CallLogger
from agentcall.events import EventHub


def make_service(
    modem: FakeModem,
    hub: EventHub | None = None,
    call_logger: CallLogger | None = None,
) -> CallAgentService:
    return CallAgentService(
        modem_port="unused",
        audio_keyword="unused",
        provider="qwen",
        hub=hub,
        modem=modem,  # type: ignore[arg-type]  # FakeModem 与 Eg25Modem 同形
        call_logger=call_logger,
    )


def make_hub() -> EventHub:
    return EventHub(asyncio.new_event_loop())


def wait_until(cond, timeout: float = 5.0) -> bool:
    """轮询等待条件成立，超时返回 False。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.02)
    return False


def run_single_agent_uplink_frame(
    monkeypatch, raw: bytes, *, gain: str
) -> tuple[bytes, list[bytes]]:
    """直接跑一帧模组→模型上行，返回模型 PCM 与录音收到的原始 PCM。"""
    monkeypatch.setenv("AGENT_UPLINK_GAIN", gain)
    service = make_service(FakeModem())
    bridge = FakeAudioBridge()
    bridge.feed_uplink(raw)

    class RecordSpy:
        def __init__(self) -> None:
            self.uplink: list[bytes] = []

        def write_uplink(self, pcm: bytes) -> None:
            self.uplink.append(pcm)

    record = RecordSpy()
    agent = FakeAgent()

    async def stop_after_first_frame(pcm: bytes) -> None:
        agent.received_audio.append(pcm)
        service.session._active = False

    monkeypatch.setattr(agent, "send_audio", stop_after_first_frame)
    service.session._active = True
    service.session._hangover_seconds = 0.0
    asyncio.run(
        service.session._run_agent_loop(
            agent,
            bridge,
            record,  # type: ignore[arg-type]
            [],
        )
    )
    assert len(agent.received_audio) == 1
    return agent.received_audio[0], record.uplink


def run_inbound_call(
    monkeypatch,
    call_logger: CallLogger,
    hub: EventHub | None = None,
    agent: FakeAgent | None = None,
    monitor=None,
) -> tuple[CallAgentService, FakeModem, FakeAudioBridge, FakeAgent]:
    """跑一通完整来电（接听→开场白→上行音频→挂断收尾），返回参与对象。"""
    modem = FakeModem()
    bridge = FakeAudioBridge()
    agent = agent or FakeAgent()
    monkeypatch.setattr(
        "agentcall.call_agent.create_audio_bridge", lambda **kw: bridge
    )
    monkeypatch.setattr("agentcall.call_agent.create_agent", lambda provider: agent)

    service = make_service(modem, hub=hub, call_logger=call_logger)
    if monitor is not None:
        service.session.monitor = monitor
    bridge.feed_uplink(b"\x02\x00" * 160)  # 模拟对方说话（上行 8kHz PCM）
    modem.trigger_ring("13800000000")

    assert wait_until(lambda: bridge.downlink and not bridge.uplink), "会话主循环未跑起来"

    service.session.stop()
    assert service.session._thread is not None
    service.session._thread.join(timeout=5)
    return service, modem, bridge, agent


def sole_call_dir(base: Path) -> Path:
    dirs = [p for p in base.iterdir() if p.is_dir()]
    assert len(dirs) == 1, f"应只有一条通话记录，实际 {dirs}"
    return dirs[0]


def call_events(base: Path) -> list[dict]:
    call_dir = sole_call_dir(base)
    return [
        json.loads(line)
        for line in (call_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
    ]


class MultiChunkGreetingAgent(FakeAgent):
    """开场白一次吐两块音频，用来验证首音频只打点一次。"""

    async def say(self, instructions: str) -> None:
        self.said.append(instructions)
        self._emit_transcript("agent", instructions)
        if self._on_audio_out:
            self._on_audio_out(self.reply_pcm)
            self._on_audio_out(self.reply_pcm)


# ---- P0-2 通话记录：events.jsonl 全生命周期 + 录音落盘 ----

def test_inbound_call_writes_events_and_recordings(tmp_path, monkeypatch):
    monkeypatch.setenv("RECORDING_ENABLED", "true")
    base = tmp_path / "rec"
    clog = CallLogger(base, recording_enabled=True)
    run_inbound_call(monkeypatch, clog)

    call_dir = sole_call_dir(base)
    events = [
        json.loads(line)
        for line in (call_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    types = [e["type"] for e in events]

    assert types[0] == "call_started"
    for expected in (
        "answered",
        "bridge_started",
        "agent_started",
        "greeting_sent",
        "transcript",
        "ended",
        "call_finished",
    ):
        assert expected in types, f"缺少节点事件 {expected}: {types}"
    # 节点事件带相对会话开始的耗时字段
    answered = next(e for e in events if e["type"] == "answered")
    assert answered["t_ms"] >= 0
    ended = next(e for e in events if e["type"] == "ended")
    assert ended["status"] == "completed"
    # 转写事件带角色与文本
    transcript = next(e for e in events if e["type"] == "transcript")
    assert transcript["role"] == "agent" and transcript["text"]

    meta = json.loads((call_dir / "meta.json").read_text(encoding="utf-8"))
    assert meta["direction"] == "inbound"
    assert meta["number"] == "13800000000"
    assert meta["status"] == "completed"
    # 上下行录音都有数据并落盘为 wav
    assert meta["uplink_bytes"] > 0 and meta["downlink_bytes"] > 0
    assert (call_dir / "uplink.wav").exists()
    assert (call_dir / "downlink.wav").exists()


def test_first_audio_latency_logged_once_after_greeting(tmp_path, monkeypatch, caplog):
    base = tmp_path / "rec"
    with caplog.at_level("INFO", logger="agentcall.call_agent"):
        run_inbound_call(
            monkeypatch,
            CallLogger(base),
            agent=MultiChunkGreetingAgent(),
        )

    events = call_events(base)
    types = [event["type"] for event in events]
    first_audio = [event for event in events if event["type"] == "first_audio"]

    assert len(first_audio) == 1
    assert first_audio[0]["ms"] >= 0
    assert types.index("greeting_sent") < types.index("first_audio")
    assert "首音频延迟:" in caplog.text


def test_first_audio_latency_logged_for_outbound_call(tmp_path, monkeypatch):
    monkeypatch.setenv("PROMPT_GEN_ENABLED", "false")
    base = tmp_path / "rec"
    modem = FakeModem()
    bridge = FakeAudioBridge()
    agent = FakeAgent()
    monkeypatch.setattr("agentcall.call_agent.create_audio_bridge", lambda **kw: bridge)
    monkeypatch.setattr("agentcall.call_agent.create_agent", lambda provider: agent)

    service = make_service(modem, call_logger=CallLogger(base))
    ok, err = service.dial("10000")
    assert ok, err
    assert wait_until(lambda: ("dial", ("10000",)) in modem.calls)
    modem.trigger_call_connected("10000")
    assert wait_until(lambda: bridge.downlink)

    service.session.stop()
    assert service.session._thread is not None
    service.session._thread.join(timeout=5)

    events = call_events(base)
    first_audio = [event for event in events if event["type"] == "first_audio"]
    call_started = next(event for event in events if event["type"] == "call_started")

    assert call_started["direction"] == "outbound"
    assert len(first_audio) == 1
    assert first_audio[0]["ms"] >= 0


def test_recording_disabled_skips_wav_but_keeps_events(tmp_path, monkeypatch):
    base = tmp_path / "rec"
    clog = CallLogger(base, recording_enabled=False)
    run_inbound_call(monkeypatch, clog)

    call_dir = sole_call_dir(base)
    assert (call_dir / "events.jsonl").exists()
    assert (call_dir / "meta.json").exists()
    assert not (call_dir / "uplink.wav").exists()
    assert not (call_dir / "downlink.wav").exists()
    meta = json.loads((call_dir / "meta.json").read_text(encoding="utf-8"))
    assert meta["recording_enabled"] is False


@pytest.mark.parametrize("direction", ["inbound", "outbound"])
def test_recording_config_change_applies_to_next_call_only(tmp_path, monkeypatch, direction):
    logger = CallLogger(tmp_path / "rec", recording_enabled=True)
    service = make_service(FakeModem(), call_logger=logger)

    monkeypatch.setenv("RECORDING_ENABLED", "false")
    current = service.session._begin_record(direction, "10000")
    assert current is not None
    assert current.recording_enabled is False

    monkeypatch.setenv("RECORDING_ENABLED", "true")
    following = service.session._begin_record(direction, "10000")
    assert following is not None
    assert current.recording_enabled is False
    assert following.recording_enabled is True


def test_service_start_purges_expired_once(tmp_path, monkeypatch):
    clog = CallLogger(tmp_path / "rec")
    calls: list[bool] = []
    monkeypatch.setattr(clog, "purge_expired", lambda: calls.append(True) or 0)

    service = make_service(FakeModem(), call_logger=clog)
    service.start()

    assert calls == [True]


# ---- P0-4 延迟挂断 Timer：会话复用时不得误伤下一通 ----

def test_stale_hangup_timer_does_not_stop_next_call(tmp_path, monkeypatch):
    """第一通排下延迟挂断后对方先挂断，第二通开始后旧 Timer 不得停掉它。"""
    monkeypatch.setenv("HANGUP_TOOL_DELAY_SECONDS", "0.4")
    modem = FakeModem()
    bridges: list[FakeAudioBridge] = []

    def new_bridge(**kw):
        bridge = FakeAudioBridge()
        bridges.append(bridge)
        return bridge

    monkeypatch.setattr("agentcall.call_agent.create_audio_bridge", new_bridge)
    monkeypatch.setattr(
        "agentcall.call_agent.create_agent", lambda provider: FakeAgent()
    )
    service = make_service(modem, call_logger=CallLogger(tmp_path / "rec"))
    session = service.session

    # 第一通：接起后 Agent 调用挂断工具，排下延迟挂断
    modem.trigger_ring("13800000000")
    assert wait_until(lambda: bridges and bridges[0].downlink), "第一通未跑起来"
    session._schedule_deferred_hangup()  # hangup 工具经此回调排定延迟挂断
    timer_fires_at = time.monotonic() + 0.4

    # 延迟窗口内对方先挂断，第一通收尾
    modem.trigger_hangup()
    assert session._thread is not None
    session._thread.join(timeout=5)

    # 第二通随即开始；越过旧 Timer 的触发点后会话必须仍在进行
    modem.trigger_ring("13900000000")
    assert wait_until(lambda: len(bridges) >= 2 and bridges[1].downlink), "第二通未跑起来"
    time.sleep(max(0.0, timer_fires_at - time.monotonic()) + 0.2)
    assert session.is_active, "上一通遗留的挂断 Timer 停掉了新会话"

    session.stop()
    session._thread.join(timeout=5)
    assert not session.is_active


def test_deferred_hangup_ignores_stale_generation(monkeypatch):
    """cancel 挡不住已在执行的 Timer 回调，世代号校验必须兜底。"""
    session = make_service(FakeModem()).session
    monkeypatch.setattr(session, "_run", lambda: None)  # 只做状态切换，不跑真实会话

    session.start()
    stale = session._session_generation
    session._active = False  # 本通结束
    session.start()  # 新会话开始，世代号推进

    session._deferred_hangup(stale)  # 旧 Timer 回调此刻才执行
    assert session.is_active, "过期的延迟挂断回调不得停掉新会话"


def test_scheduled_hangup_stops_current_session(monkeypatch):
    """延迟挂断对本通会话仍然生效（防止修复过度）。"""
    session = make_service(FakeModem()).session
    monkeypatch.setattr(session, "_run", lambda: None)
    session._hangup_delay_seconds = 0.05

    session.start()
    session._schedule_deferred_hangup()
    assert wait_until(lambda: not session.is_active), "延迟挂断未停掉本通会话"


def test_detach_agent_invalidates_late_effects_without_hanging_up(monkeypatch):
    modem = FakeModem()
    service = make_service(modem)
    session = service.session
    agent = FakeAgent()
    bridge = FakeAudioBridge()
    transcripts: list[tuple[str, str]] = []

    class RecordSpy:
        def __init__(self) -> None:
            self.events: list[tuple[str, dict]] = []
            self.downlink: list[bytes] = []

        def log_event(self, event_type: str, **fields) -> None:
            self.events.append((event_type, fields))

        def write_downlink(self, pcm: bytes) -> None:
            self.downlink.append(pcm)

    record = RecordSpy()
    session._active = True
    session._session_generation = 4
    session._hangup_delay_seconds = 0.01
    session.current_caller = "13800000000"
    audio_handler = session._make_agent_audio_handler(
        agent,
        bridge,
        record,  # type: ignore[arg-type]
    )
    transcript_handler = session._make_transcript_handler(
        record,  # type: ignore[arg-type]
        transcripts,
        agent,
    )
    tools = session._build_tools()

    asyncio.run(session.detach_agent(agent, bridge))

    audio_handler(b"\x01\x00" * 160)
    transcript_handler("agent", "迟到转写")
    hangup = tools.dispatch("hangup_call", {})
    dtmf = tools.dispatch("send_dtmf", {"digits": "1"})
    time.sleep(0.03)

    assert agent.stopped
    assert bridge.stopped
    assert session.is_active
    assert hangup["code"] == "STALE_AGENT_GENERATION"
    assert dtmf["code"] == "STALE_AGENT_GENERATION"
    assert session._outgoing_audio.empty()
    assert transcripts == []
    assert record.events == []
    assert record.downlink == []
    assert modem.calls == []


def test_repeat_suppression_stuck_requests_winddown(monkeypatch, tmp_path):
    """连续复读抑制判卡死后，CallSession 走既有收尾路径说告别并挂断。"""

    class StuckAfterStartAgent(FakeAgent):
        async def start(self, on_audio_out):
            await super().start(on_audio_out)
            self._emit_repeat_stuck("复读抑制连续触发，判定模型卡死")

    monkeypatch.setenv("HANGUP_TOOL_DELAY_SECONDS", "0.05")
    modem = FakeModem()
    bridge = FakeAudioBridge()
    agent = StuckAfterStartAgent()
    monkeypatch.setattr("agentcall.call_agent.create_audio_bridge", lambda **kw: bridge)
    monkeypatch.setattr("agentcall.call_agent.create_agent", lambda provider: agent)

    service = make_service(modem, call_logger=CallLogger(tmp_path / "rec"))
    bridge.feed_uplink(b"\x02\x00" * 160)
    modem.trigger_ring("13800000000")

    assert wait_until(lambda: any("再见" in s for s in agent.said), timeout=5), agent.said
    assert service.session._thread is not None
    service.session._thread.join(timeout=5)
    assert ("hangup", ()) in modem.calls


# ---- P1-4 通话摘要：后台线程 + summary.json + hub 推送 ----

class TalkativeCallerAgent(FakeAgent):
    """开场白后模拟对方说话，产生 user 转写（触发摘要条件）。"""

    async def say(self, instructions: str) -> None:
        await super().say(instructions)
        self._emit_transcript("user", "你好，我是快递员，有个件放门口了")


def test_summary_thread_writes_summary_and_publishes(tmp_path, monkeypatch):
    monkeypatch.setenv("SUMMARY_ENABLED", "true")
    seen: dict = {}

    def fake_summarize(transcripts, direction, number, *, timeout=15.0):
        seen["transcripts"] = list(transcripts)
        seen["direction"] = direction
        seen["number"] = number
        return {
            "ok": True,
            "caller_identity": "快递员",
            "intent": "快递放门口",
            "urgency": "低",
            "callback_needed": False,
            "summary": "快递员来电，件已放门口。",
            "error": None,
        }

    monkeypatch.setattr("agentcall.call_agent.summarize_call", fake_summarize)

    base = tmp_path / "rec"
    hub = make_hub()
    service, *_ = run_inbound_call(
        monkeypatch, CallLogger(base), hub=hub, agent=TalkativeCallerAgent()
    )
    thread = service.session._summary_thread
    assert thread is not None, "应已启动摘要线程"
    thread.join(timeout=5)

    assert seen["direction"] == "inbound"
    assert seen["number"] == "13800000000"
    assert ("user", "你好，我是快递员，有个件放门口了") in seen["transcripts"]

    call_dir = sole_call_dir(base)
    summary = json.loads((call_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["caller_identity"] == "快递员"
    meta = json.loads((call_dir / "meta.json").read_text(encoding="utf-8"))
    assert meta["summary_state"] == "READY"

    summary_events = [e for e in hub.history() if e.get("type") == "call_summary"]
    assert len(summary_events) == 1
    assert summary_events[0]["call_id"] == call_dir.name
    assert summary_events[0]["summary"] == "快递员来电，件已放门口。"


def test_summary_skipped_when_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("SUMMARY_ENABLED", "false")
    monkeypatch.setattr(
        "agentcall.call_agent.summarize_call",
        lambda *a, **kw: pytest.fail("摘要关闭时不应调用 summarize_call"),
    )
    base = tmp_path / "rec"
    service, *_ = run_inbound_call(
        monkeypatch, CallLogger(base), agent=TalkativeCallerAgent()
    )
    assert service.session._summary_thread is None
    meta = json.loads((sole_call_dir(base) / "meta.json").read_text(encoding="utf-8"))
    assert meta["summary_state"] == "UNAVAILABLE"


def test_summary_skipped_without_user_speech(tmp_path, monkeypatch):
    monkeypatch.setenv("SUMMARY_ENABLED", "true")
    monkeypatch.setattr(
        "agentcall.call_agent.summarize_call",
        lambda *a, **kw: pytest.fail("对方没说话时不应调用 summarize_call"),
    )
    base = tmp_path / "rec"
    service, *_ = run_inbound_call(monkeypatch, CallLogger(base))
    assert service.session._summary_thread is None  # FakeAgent 只有 agent 转写
    meta = json.loads((sole_call_dir(base) / "meta.json").read_text(encoding="utf-8"))
    assert meta["summary_state"] == "UNAVAILABLE"


def test_summary_failure_is_terminal_not_permanently_pending(tmp_path, monkeypatch):
    monkeypatch.setenv("SUMMARY_ENABLED", "true")
    monkeypatch.setattr(
        "agentcall.call_agent.summarize_call",
        lambda *args, **kwargs: {
            "ok": False,
            "summary": "",
            "error": "model_timeout",
        },
    )
    service = make_service(FakeModem(), call_logger=CallLogger(tmp_path / "rec"))
    record = service.session._begin_record("inbound", "13800000000")
    assert record is not None
    record.finish("completed")

    service.session._maybe_summarize(
        record,
        [("user", "hello")],
        "inbound",
        "13800000000",
    )
    assert service.session._summary_thread is not None
    service.session._summary_thread.join(timeout=1.0)

    meta = json.loads((record.path / "meta.json").read_text(encoding="utf-8"))
    summary = json.loads((record.path / "summary.json").read_text(encoding="utf-8"))
    assert meta["summary_state"] == "FAILED"
    assert summary["ok"] is False


def test_summary_thread_start_failure_is_terminal(tmp_path, monkeypatch):
    monkeypatch.setenv("SUMMARY_ENABLED", "true")
    service = make_service(FakeModem(), call_logger=CallLogger(tmp_path / "rec"))
    record = service.session._begin_record("inbound", "13800000000")
    assert record is not None
    record.finish("completed")
    monkeypatch.setattr(
        "agentcall.call_agent.threading.Thread.start",
        lambda _self: (_ for _ in ()).throw(RuntimeError("cannot start")),
    )

    service.session._maybe_summarize(
        record,
        [("user", "hello")],
        "inbound",
        "13800000000",
    )

    meta = json.loads((record.path / "meta.json").read_text(encoding="utf-8"))
    summary = json.loads((record.path / "summary.json").read_text(encoding="utf-8"))
    assert meta["summary_state"] == "FAILED"
    assert summary["error"] == "summary_worker_start_failed"


def test_carrier_sms_profile_replaces_misheard_summary_with_official_result(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("SUMMARY_ENABLED", "true")
    monkeypatch.setenv("SMS_VERIFICATION_WAIT_SECONDS", "0.05")
    monkeypatch.setattr(
        "agentcall.call_agent.summarize_call",
        lambda *args, **kwargs: {
            "ok": True,
            "caller_identity": "中国移动客服",
            "intent": "查询话费",
            "urgency": "低",
            "callback_needed": False,
            "summary": "听写金额是19.00元。",
            "error": None,
        },
    )
    hub = make_hub()
    service = make_service(FakeModem(), hub=hub, call_logger=CallLogger(tmp_path / "rec"))
    record = service.session._begin_record("outbound", "10086")
    assert record is not None
    record.finish("completed")
    hub.publish(
        {
            "type": "sms_in",
            "sender": "10086",
            "text": "当月累计话费29.00元。",
        }
    )

    service.session._summarize_worker(
        record,
        [("user", "上月话费十九元")],
        "outbound",
        "10086",
        "carrier_sms",
        "10086",
    )

    summary = json.loads((record.path / "summary.json").read_text(encoding="utf-8"))
    assert summary["result_verification"] == "verified"
    assert "29.00" in summary["summary"]
    assert "19.00" not in summary["summary"]


def test_carrier_sms_profile_summarizes_without_transcript_speech(tmp_path, monkeypatch):
    monkeypatch.setenv("SUMMARY_ENABLED", "true")
    monkeypatch.setenv("SMS_VERIFICATION_WAIT_SECONDS", "0.05")
    monkeypatch.setattr(
        "agentcall.call_agent.summarize_call",
        lambda *args, **kwargs: {"ok": False, "summary": "", "error": "no transcript"},
    )
    hub = make_hub()
    service = make_service(FakeModem(), hub=hub, call_logger=CallLogger(tmp_path / "rec"))
    record = service.session._begin_record("outbound", "10086")
    assert record is not None
    record.finish("completed")
    hub.publish({"type": "sms_in", "sender": "10086", "text": "余额41.40元。"})

    service.session._maybe_summarize(
        record,
        [],
        "outbound",
        "10086",
        "carrier_sms",
        "10086",
    )
    assert service.session._summary_thread is not None
    service.session._summary_thread.join(timeout=1.0)

    summary = json.loads((record.path / "summary.json").read_text(encoding="utf-8"))
    assert summary["result_verification"] == "verified"
    assert summary["summary"] == "已由官方运营商短信核实：余额41.40元。"


def test_carrier_sms_mode_does_not_attach_service_sms_to_ordinary_call(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("SMS_VERIFICATION_WAIT_SECONDS", "0")
    monkeypatch.setattr(
        "agentcall.call_agent.summarize_call",
        lambda *args, **kwargs: {
            "ok": True,
            "summary": "普通通话听写。",
            "error": None,
        },
    )
    hub = make_hub()
    service = make_service(FakeModem(), hub=hub, call_logger=CallLogger(tmp_path / "rec"))
    record = service.session._begin_record("outbound", "13900000000")
    assert record is not None
    record.finish("completed")
    hub.publish({"type": "sms_in", "sender": "10086", "text": "余额41.40元。"})

    service.session._summarize_worker(
        record,
        [("user", "普通通话")],
        "outbound",
        "13900000000",
        "carrier_sms",
        "10086",
    )

    summary = json.loads((record.path / "summary.json").read_text(encoding="utf-8"))
    assert summary["result_verification"] == "unverified"
    assert summary["evidence"] == []


def test_prompt_profile_applies_carrier_sms_verification_mode():
    session = make_service(FakeModem()).session
    session._prompt_gen_result = {
        "ok": True,
        "scenario": "查费后以官方短信为准",
        "result_verification": "carrier_sms",
    }

    assert session._apply_prompt_gen_result() == "查费后以官方短信为准"
    assert session._result_verification_mode == "carrier_sms"


# ---- P2-1 批量外呼：入队顺序拨打 + 白名单 + 状态查询 ----

def test_batch_dial_dials_in_order(tmp_path, monkeypatch):
    monkeypatch.setenv("DIAL_INTERVAL_SECONDS", "0.05")
    monkeypatch.delenv("DIAL_WHITELIST", raising=False)
    # 持久化经 _remember_outbound_task 写 os.environ；
    # 先让 monkeypatch 登记该变量，测试结束自动恢复原状。
    monkeypatch.delenv("AGENT_OUTBOUND_TASK", raising=False)
    monkeypatch.setattr(config, "update_env_file", lambda updates: list(updates))
    modem = FakeModem()
    bridges: list[FakeAudioBridge] = []
    agents: list[FakeAgent] = []

    def new_bridge(**kw):
        bridge = FakeAudioBridge()
        bridges.append(bridge)
        return bridge

    def new_agent(provider):
        agent = FakeAgent()
        agents.append(agent)
        return agent

    monkeypatch.setattr("agentcall.call_agent.create_audio_bridge", new_bridge)
    monkeypatch.setattr("agentcall.call_agent.create_agent", new_agent)
    service = make_service(modem, call_logger=CallLogger(tmp_path / "rec"))

    result = service.batch_dial(["10000", " 10001 ", ""], task="催一下快递进度")
    assert result["accepted"] == ["10000", "10001"]
    assert result["rejected"] == [""]

    def dials() -> list[str]:
        return [args[0] for name, args in modem.calls if name == "dial"]

    # 第一通：拨号 → 接通 → 对端挂断
    assert wait_until(lambda: "10000" in dials()), "第一通未拨出"
    assert os.environ["AGENT_OUTBOUND_TASK"] == "催一下快递进度"  # 持久化为下次默认
    modem.trigger_call_connected("10000")
    assert wait_until(lambda: bridges and bridges[0].downlink)
    # 队列的 task 显式传进会话，注入本通提示词
    assert "你要办的事：催一下快递进度" in agents[0]._session_instructions
    modem.trigger_hangup()

    # 第二通：间隔后自动拨下一个
    assert wait_until(lambda: "10001" in dials()), "第二通未自动拨出"
    modem.trigger_call_connected("10001")
    assert wait_until(lambda: len(bridges) >= 2 and bridges[1].downlink)
    modem.trigger_hangup()

    assert wait_until(lambda: not service.dial_queue_status()["active"])
    assert dials() == ["10000", "10001"]

    status = service.dial_queue_status()
    assert status["pending"] == []
    assert status["current"] is None
    assert [d["number"] for d in status["done"]] == ["10000", "10001"]
    assert all(d["ok"] for d in status["done"])


def test_outbound_prompt_generation_injects_scenario_and_logs_event(tmp_path, monkeypatch):
    monkeypatch.setenv("PROMPT_GEN_ENABLED", "true")
    monkeypatch.setenv("NUMBER_PROFILES_ENABLED", "false")
    monkeypatch.setenv("PROMPT_GEN_WAIT_SECONDS", "1")
    monkeypatch.setenv("AGENT_OUTBOUND_TASK", "查询流量")
    monkeypatch.setattr(config, "validate_provider_credentials", lambda provider: [])
    modem = FakeModem()
    bridge = FakeAudioBridge()
    agent = FakeAgent()

    monkeypatch.setattr("agentcall.call_agent.create_audio_bridge", lambda **kw: bridge)
    monkeypatch.setattr("agentcall.call_agent.create_agent", lambda provider: agent)
    monkeypatch.setattr(
        "agentcall.call_agent.generate_prompt_scenario",
        lambda number, task, lang, **kwargs: {
            "ok": True,
            "scenario": "开场直接说查询流量，遇到菜单用短词。",
            "opening": "查一下本机流量",
            "error": None,
            "provider": "qwen",
            "model": "qwen-plus",
            "cached": False,
        },
    )

    service = make_service(modem, call_logger=CallLogger(tmp_path / "rec"))
    ok, err = service.dial("10000")
    assert ok, err
    assert wait_until(lambda: ("dial", ("10000",)) in modem.calls)
    modem.trigger_call_connected("10000")
    assert wait_until(lambda: bridge.downlink)
    service.session.stop()
    assert service.session._thread is not None
    service.session._thread.join(timeout=5)

    assert "本通场景与开场策略：开场直接说查询流量" in agent._session_instructions
    call_dir = sole_call_dir(tmp_path / "rec")
    events = [
        json.loads(line)
        for line in (call_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    prompt_events = [event for event in events if event["type"] == "prompt_gen"]
    assert prompt_events
    assert prompt_events[0]["ok"] is True
    assert prompt_events[0]["scenario"] == "开场直接说查询流量，遇到菜单用短词。"
    assert prompt_events[0]["opening"] == "查一下本机流量"
    assert prompt_events[0]["source"] == "generated"
    assert agent.said[0] == "请直接说：查一下本机流量"


def test_outbound_prompt_generation_uses_number_profile_without_thread_or_model(
    tmp_path, monkeypatch
):
    profile_file = tmp_path / "number_profiles.json"
    profile_file.write_text(
        json.dumps(
            {
                "profiles": [
                    {
                        "number": "10000",
                        "task": "查询流量",
                        "scenario": "预设策略：只说需求，菜单用短词。",
                        "opening": "查流量",
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("PROMPT_GEN_ENABLED", "true")
    monkeypatch.setenv("NUMBER_PROFILES_ENABLED", "true")
    monkeypatch.setenv("NUMBER_PROFILES_FILE", str(profile_file))
    monkeypatch.setenv("AGENT_OUTBOUND_TASK", "查询流量")
    calls: list[tuple] = []
    monkeypatch.setattr(
        "agentcall.call_agent.generate_prompt_scenario",
        lambda *args, **kwargs: calls.append((args, kwargs)) or {
            "ok": True,
            "scenario": "不应生成",
            "opening": "不应开场",
            "error": None,
            "provider": "qwen",
            "model": "qwen-plus",
            "cached": False,
        },
    )
    modem = FakeModem()
    bridge = FakeAudioBridge()
    agent = FakeAgent()
    monkeypatch.setattr("agentcall.call_agent.create_audio_bridge", lambda **kw: bridge)
    monkeypatch.setattr("agentcall.call_agent.create_agent", lambda provider: agent)

    service = make_service(modem, call_logger=CallLogger(tmp_path / "rec"))
    ok, err = service.dial("10000")
    assert ok, err
    assert wait_until(lambda: ("dial", ("10000",)) in modem.calls)
    assert service.session._prompt_gen_thread is None
    modem.trigger_call_connected("10000")
    assert wait_until(lambda: bridge.downlink)
    service.session.stop()
    assert service.session._thread is not None
    service.session._thread.join(timeout=5)

    assert calls == []
    assert "本通场景与开场策略：预设策略：只说需求" in agent._session_instructions
    assert agent.said[0] == "请直接说：查流量"
    call_dir = sole_call_dir(tmp_path / "rec")
    events = [
        json.loads(line)
        for line in (call_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    prompt_event = next(event for event in events if event["type"] == "prompt_gen")
    assert prompt_event["source"] == "profile"
    assert prompt_event["number"] == "10000"
    assert prompt_event["task"] == "查询流量"
    assert prompt_event["scenario"] == "预设策略：只说需求，菜单用短词。"


def test_number_profiles_disabled_falls_back_to_dynamic_generation(tmp_path, monkeypatch):
    profile_file = tmp_path / "number_profiles.json"
    profile_file.write_text(
        json.dumps(
            {
                "profiles": [
                    {
                        "number": "10000",
                        "task": "查询流量",
                        "scenario": "禁用时不应使用",
                        "opening": "不应开场",
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("PROMPT_GEN_ENABLED", "true")
    monkeypatch.setenv("NUMBER_PROFILES_ENABLED", "false")
    monkeypatch.setenv("NUMBER_PROFILES_FILE", str(profile_file))
    monkeypatch.setenv("AGENT_OUTBOUND_TASK", "查询流量")
    monkeypatch.setattr(config, "validate_provider_credentials", lambda provider: [])
    monkeypatch.setattr(
        "agentcall.call_agent.generate_prompt_scenario",
        lambda number, task, lang, **kwargs: {
            "ok": True,
            "scenario": "动态生成策略",
            "opening": "动态开场",
            "error": None,
            "provider": "qwen",
            "model": "qwen-plus",
            "cached": False,
        },
    )

    service = make_service(FakeModem(), call_logger=CallLogger(tmp_path / "rec"))
    service.session._outbound_number = "10000"
    service.session._outbound_task_value = "查询流量"
    service.session._start_prompt_generation()
    assert service.session._prompt_gen_thread is not None
    service.session._prompt_gen_thread.join(timeout=1)

    assert service.session._take_prompt_scenario() == "动态生成策略"
    assert service.session._prompt_gen_opening == "动态开场"


def test_number_profile_still_used_when_dynamic_generation_disabled(
    tmp_path, monkeypatch
):
    profile_file = tmp_path / "number_profiles.json"
    profile_file.write_text(
        json.dumps(
            {
                "profiles": [
                    {
                        "number": "10000",
                        "task": "查询流量",
                        "scenario": "关动态生成也应使用预设",
                        "opening": "查流量",
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("PROMPT_GEN_ENABLED", "false")
    monkeypatch.setenv("NUMBER_PROFILES_ENABLED", "true")
    monkeypatch.setenv("NUMBER_PROFILES_FILE", str(profile_file))
    calls: list[tuple] = []
    monkeypatch.setattr(
        "agentcall.call_agent.generate_prompt_scenario",
        lambda *args, **kwargs: calls.append((args, kwargs)) or {
            "ok": True,
            "scenario": "不应生成",
            "opening": "不应开场",
            "error": None,
            "provider": "qwen",
            "model": "qwen-plus",
            "cached": False,
        },
    )

    service = make_service(FakeModem(), call_logger=CallLogger(tmp_path / "rec"))
    service.session._outbound_number = "10000"
    service.session._outbound_task_value = "查询流量"
    service.session._start_prompt_generation()

    assert service.session._prompt_gen_thread is None
    assert calls == []
    assert service.session._take_prompt_scenario() == "关动态生成也应使用预设"
    assert service.session._prompt_gen_opening == "查流量"


def test_selected_profile_id_survives_custom_subtopic(tmp_path, monkeypatch):
    profile_file = tmp_path / "number_profiles.json"
    profile_file.write_text(
        json.dumps(
            {
                "profiles": [
                    {
                        "id": "telecom_data",
                        "number": "10000",
                        "task": "查询流量",
                        "scenario": "稳定预设策略",
                        "opening": "查流量",
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("PROMPT_GEN_ENABLED", "false")
    monkeypatch.setenv("NUMBER_PROFILES_ENABLED", "true")
    monkeypatch.setenv("NUMBER_PROFILES_FILE", str(profile_file))

    service = make_service(FakeModem(), call_logger=CallLogger(tmp_path / "rec"))
    service.session._outbound_number = "10000"
    service.session._outbound_task_value = "只查本月剩余量"
    service.session._preset_hint = "不匹配的旧任务文案"
    service.session._preset_id = "telecom_data"
    service.session._start_prompt_generation()

    result = service.session._prompt_gen_result
    assert result is not None
    assert result["profile_id"] == "telecom_data"
    assert result["task"] == "只查本月剩余量"
    assert service.session._take_prompt_scenario() == "稳定预设策略"


def test_dynamic_generation_disabled_and_profile_miss_falls_back_to_template(
    tmp_path, monkeypatch
):
    profile_file = tmp_path / "number_profiles.json"
    profile_file.write_text(
        json.dumps(
            {
                "profiles": [
                    {
                        "number": "10000",
                        "task": "查询流量",
                        "scenario": "不匹配时不应使用",
                        "opening": "不应开场",
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("PROMPT_GEN_ENABLED", "false")
    monkeypatch.setenv("NUMBER_PROFILES_ENABLED", "true")
    monkeypatch.setenv("NUMBER_PROFILES_FILE", str(profile_file))
    calls: list[tuple] = []
    monkeypatch.setattr(
        "agentcall.call_agent.generate_prompt_scenario",
        lambda *args, **kwargs: calls.append((args, kwargs)) or {
            "ok": True,
            "scenario": "不应生成",
            "opening": "不应开场",
            "error": None,
            "provider": "qwen",
            "model": "qwen-plus",
            "cached": False,
        },
    )

    service = make_service(FakeModem(), call_logger=CallLogger(tmp_path / "rec"))
    service.session._outbound_number = "10001"
    service.session._outbound_task_value = "查询流量"
    service.session._start_prompt_generation()

    assert service.session._prompt_gen_thread is None
    assert calls == []
    assert service.session._take_prompt_scenario() is None
    assert service.session._prompt_gen_opening == ""


def test_outbound_prompt_generation_timeout_falls_back_to_template(tmp_path, monkeypatch):
    monkeypatch.setenv("PROMPT_GEN_ENABLED", "true")
    monkeypatch.setenv("NUMBER_PROFILES_ENABLED", "false")
    monkeypatch.setenv("PROMPT_GEN_WAIT_SECONDS", "0.01")
    monkeypatch.setenv("AGENT_OUTBOUND_TASK", "查询流量")
    monkeypatch.setattr(config, "validate_provider_credentials", lambda provider: [])
    modem = FakeModem()
    bridge = FakeAudioBridge()
    agent = FakeAgent()
    release = threading.Event()

    def slow_generate(number, task, lang, **kwargs):
        release.wait(timeout=2)
        return {
            "ok": True,
            "scenario": "这段太晚了，本通不应使用。",
            "opening": "这句也太晚了",
            "error": None,
            "provider": "qwen",
            "model": "qwen-plus",
            "cached": False,
        }

    monkeypatch.setattr("agentcall.call_agent.create_audio_bridge", lambda **kw: bridge)
    monkeypatch.setattr("agentcall.call_agent.create_agent", lambda provider: agent)
    monkeypatch.setattr("agentcall.call_agent.generate_prompt_scenario", slow_generate)

    service = make_service(modem, call_logger=CallLogger(tmp_path / "rec"))
    ok, err = service.dial("10000")
    assert ok, err
    assert wait_until(lambda: ("dial", ("10000",)) in modem.calls)
    modem.trigger_call_connected("10000")
    assert wait_until(lambda: bridge.downlink)
    service.session.stop()
    assert service.session._thread is not None
    service.session._thread.join(timeout=5)

    assert "本通场景与开场策略" not in agent._session_instructions
    call_dir = sole_call_dir(tmp_path / "rec")
    events = [
        json.loads(line)
        for line in (call_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    timeout_events = [event for event in events if event["type"] == "prompt_gen"]
    assert timeout_events
    assert timeout_events[0]["ok"] is False
    assert "超时" in timeout_events[0]["error"]
    release.set()
    if service.session._prompt_gen_thread is not None:
        service.session._prompt_gen_thread.join(timeout=1)


def test_outbound_prompt_generation_skips_when_credentials_missing(
    tmp_path, monkeypatch, caplog
):
    monkeypatch.setenv("PROMPT_GEN_ENABLED", "true")
    monkeypatch.setenv("NUMBER_PROFILES_ENABLED", "false")
    calls: list[tuple] = []
    monkeypatch.setattr(
        config,
        "validate_provider_credentials",
        lambda provider: [f"缺少环境变量 DASHSCOPE_API_KEY（{provider} 必需）"],
    )
    monkeypatch.setattr(
        "agentcall.call_agent.generate_prompt_scenario",
        lambda *args, **kwargs: calls.append((args, kwargs)) or {
            "ok": True,
            "scenario": "不应生成",
            "opening": "不应开场",
            "error": None,
            "provider": "qwen",
            "model": "qwen-plus",
            "cached": False,
        },
    )

    service = make_service(FakeModem(), call_logger=CallLogger(tmp_path / "rec"))
    service.session._outbound_number = "10000"
    service.session._outbound_task_value = "查询流量"

    with caplog.at_level("DEBUG", logger="agentcall.call_agent"):
        service.session._start_prompt_generation()

    assert service.session._prompt_gen_thread is None
    assert calls == []
    assert service.session._take_prompt_scenario() is None
    assert service.session._prompt_gen_opening == ""
    assert "跳过动态场景提示词生成" in caplog.text


def test_batch_dial_respects_whitelist_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv("DIAL_WHITELIST", "1000*, 13800000000")
    modem = FakeModem()
    service = make_service(modem, call_logger=CallLogger(tmp_path / "rec"))
    # 不真正起会话：start 置 active，队列停在第一通
    monkeypatch.setattr(
        service.session,
        "start",
        lambda outbound_number=None, task=None, preset_hint=None: setattr(service.session, "_active", True),
    )

    result = service.batch_dial(["10000", "13900001111", "13800000000"])
    assert result["accepted"] == ["10000", "13800000000"]
    assert result["rejected"] == ["13900001111"]

    assert wait_until(lambda: service.dial_queue_status()["current"] == "10000")
    assert service.dial_queue_status()["pending"] == ["13800000000"]


def test_dial_queue_does_not_touch_environ(tmp_path, monkeypatch):
    """队列的 task 显式传参，不再经 os.environ 中转（除持久化写点外）。"""
    monkeypatch.setenv("DIAL_INTERVAL_SECONDS", "0.05")
    monkeypatch.delenv("DIAL_WHITELIST", raising=False)
    monkeypatch.delenv("AGENT_OUTBOUND_TASK", raising=False)
    service = make_service(FakeModem(), call_logger=CallLogger(tmp_path / "rec"))
    received: list[tuple[str, str | None]] = []

    def fake_dial(number: str, task: str | None = None) -> tuple[bool, str | None]:
        received.append((number, task))
        return False, "测试拒绝"  # 立即失败，驱动队列继续

    monkeypatch.setattr(service.dial_queue, "_dial_fn", fake_dial)
    service.dial_queue.enqueue(["10000", "10001"], task="催一下快递进度")

    assert wait_until(lambda: len(received) == 2)
    # task 随每次拨号显式传给 dial_fn，而 DialQueue 全程不碰 os.environ
    assert received == [("10000", "催一下快递进度"), ("10001", "催一下快递进度")]
    assert "AGENT_OUTBOUND_TASK" not in os.environ


# ---- P2-7 本地监听：feed 接线 + 按配置构造并启动 ----

class FakeMonitor:
    def __init__(self) -> None:
        self.fed: list[bytes] = []
        self.started = False
        self.stopped = False

    def start(self) -> None:
        self.started = True

    def feed(self, pcm: bytes) -> None:
        self.fed.append(pcm)

    def stop(self) -> None:
        self.stopped = True


def test_monitor_feed_receives_raw_agent_audio(tmp_path, monkeypatch):
    monitor = FakeMonitor()
    _, _, _, agent = run_inbound_call(
        monkeypatch, CallLogger(tmp_path / "rec"), monitor=monitor
    )
    # 喂给监听的是 Agent 原始 24k PCM（未经 8k 重采样）
    assert monitor.fed
    assert monitor.fed[0] == agent.reply_pcm


def test_service_creates_and_starts_monitor_from_config(monkeypatch):
    """监听开启时创建两个实例：AI 下行(24k, AI_GAIN) + 对方上行(8k, UPLINK_GAIN)。"""
    created: list[dict] = []

    class SpyMonitor:
        def __init__(self, device_keyword, *, sample_rate=24000, gain=1.0):
            self.info = {"device": device_keyword, "rate": sample_rate,
                         "gain": gain, "started": False}
            created.append(self.info)

        def start(self) -> None:
            self.info["started"] = True

    monkeypatch.setattr("agentcall.call_agent.MonitorPlayback", SpyMonitor)
    monkeypatch.setenv("MONITOR_AI_PLAYBACK", "true")
    monkeypatch.setenv("MONITOR_OUTPUT_DEVICE", "外接音箱")
    monkeypatch.setenv("MONITOR_AI_GAIN", "0.5")
    monkeypatch.setenv("MONITOR_UPLINK_GAIN", "4.0")

    service = make_service(FakeModem())

    assert created == [
        {"device": "外接音箱", "rate": 24000, "gain": 0.5, "started": True},
        {"device": "外接音箱", "rate": 8000, "gain": 4.0, "started": True},
    ]
    assert service.session.monitor is service.monitor
    assert service.session.uplink_monitor is service.uplink_monitor


def test_monitor_disabled_by_default(monkeypatch):
    monkeypatch.delenv("MONITOR_AI_PLAYBACK", raising=False)
    service = make_service(FakeModem())
    assert service.monitor is None
    assert service.session.monitor is None


# ---- P2-6 会话参数：每通会话开始从 config 重读 ----

def test_session_reloads_tunables_from_env(monkeypatch):
    service = make_service(FakeModem())
    monkeypatch.setenv("HALF_DUPLEX_HANGOVER_SECONDS", "0.9")
    monkeypatch.setenv("HANGUP_TOOL_DELAY_SECONDS", "1.5")

    service.session._load_session_config()

    assert service.session._hangover_seconds == 0.9
    assert service.session._hangup_delay_seconds == 1.5


def test_agent_uplink_gain_only_changes_audio_sent_to_model(monkeypatch):
    """#80-E:模型上行可放大，但通话录音仍保留模组原始 PCM。"""
    raw = np.full(160, 1000, dtype=np.int16).tobytes()
    sent_pcm, recorded = run_single_agent_uplink_frame(monkeypatch, raw, gain="2.0")

    assert recorded == [raw]
    sent = np.frombuffer(sent_pcm, dtype=np.int16)
    assert np.all(sent == 2000)


def test_agent_uplink_gain_records_pre_and_post_flow_stats(monkeypatch):
    """#80-E:校准日志观测 gain 前后峰值，不保存 PCM 内容。"""
    class StatsSpy:
        def __init__(self, label: str) -> None:
            self.frames: list[bytes] = []
            created[label] = self

        def add(self, pcm: bytes) -> None:
            self.frames.append(pcm)

        def maybe_log(self, **_extra: object) -> bool:
            return False

    created: dict[str, StatsSpy] = {}
    monkeypatch.setattr("agentcall.call_agent.PcmFlowStats", StatsSpy)
    raw = np.full(160, 500, dtype=np.int16).tobytes()
    run_single_agent_uplink_frame(monkeypatch, raw, gain="2.0")

    pre = created["agent_uplink_pre_gain"]
    post = created["agent_uplink_post_gain"]
    assert np.max(np.frombuffer(pre.frames[0], dtype=np.int16)) == 500
    assert np.max(np.frombuffer(post.frames[0], dtype=np.int16)) == 1000


@pytest.mark.parametrize("invalid_gain", ["0", "-2", "nan", "inf"])
def test_agent_uplink_invalid_gain_falls_back_to_identity(monkeypatch, invalid_gain):
    """手工 .env 绕过面板校验时，非法增益也不能破坏模型音频。"""
    applied: list[float] = []

    def capture_gain(pcm: bytes, gain: float) -> bytes:
        applied.append(gain)
        return pcm

    monkeypatch.setattr("agentcall.call_agent.apply_pcm_gain", capture_gain)
    raw = np.full(160, 500, dtype=np.int16).tobytes()
    run_single_agent_uplink_frame(monkeypatch, raw, gain=invalid_gain)

    assert applied == [1.0]


# ---- 外呼主题：持久化为下次默认 ----


def test_dial_with_task_persists_as_default(monkeypatch, tmp_path):
    monkeypatch.delenv("AGENT_OUTBOUND_TASK", raising=False)
    written = {}
    monkeypatch.setattr(config, "update_env_file", lambda updates: written.update(updates) or list(updates))
    service = make_service(FakeModem())
    starts: list[tuple[str | None, str | None]] = []
    monkeypatch.setattr(
        service.session,
        "start",
        lambda outbound_number=None, task=None, preset_hint=None: starts.append((outbound_number, task)),
    )

    ok, _ = service.dial("10000", task="查询本月话费")
    assert ok
    assert os.environ["AGENT_OUTBOUND_TASK"] == "查询本月话费"
    assert written == {"AGENT_OUTBOUND_TASK": "查询本月话费"}
    # task 同时显式传给会话（不依赖 env 中转）
    assert starts == [("10000", "查询本月话费")]


def test_dial_without_task_keeps_previous(monkeypatch):
    monkeypatch.setenv("AGENT_OUTBOUND_TASK", "上次的主题")
    called = []
    monkeypatch.setattr(config, "update_env_file", lambda updates: called.append(updates))
    service = make_service(FakeModem())
    starts: list[tuple[str | None, str | None]] = []
    monkeypatch.setattr(
        service.session,
        "start",
        lambda outbound_number=None, task=None, preset_hint=None: starts.append((outbound_number, task)),
    )

    ok, _ = service.dial("10000")
    assert ok
    assert os.environ["AGENT_OUTBOUND_TASK"] == "上次的主题"
    assert called == []  # 未持久化任何变更
    assert starts == [("10000", None)]  # 未传 task：会话回退 env 默认


# ---- 韧性启动：模组不在也起服务，后台 supervisor 反复重连 ----


def test_start_does_not_raise_when_modem_absent():
    """模组 connect 抛错时，start() 不得抛出（Web 服务照常起）。"""
    modem = FakeModem()

    def boom() -> None:
        raise OSError("could not open port /tmp/ec20-at")

    modem.connect = boom  # type: ignore[method-assign]
    service = make_service(modem)
    service.modem_connected = False  # 注入 fake 默认视为已连；此处模拟设备尚未接入
    service.start()  # 不抛
    service._service_running = False  # 停 supervisor
    assert service.modem_connected is False


def test_supervisor_connects_then_publishes_status():
    """模组可连时，supervisor 应完成 connect/init/listen 并广播 modem_status=connected。"""
    modem = FakeModem()
    hub = make_hub()
    service = make_service(modem, hub=hub)
    service.modem_connected = False  # 注入 fake 默认视为已连；此处模拟设备尚未接入
    service.start()

    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and not service.modem_connected:
        time.sleep(0.02)

    service._service_running = False
    assert service.modem_connected is True
    assert "connect" in modem.call_names()
    assert "initialize_for_voice" in modem.call_names()
    assert "start_listener" in modem.call_names()
    statuses = [e for e in hub.history() if e.get("type") == "modem_status"]
    assert statuses and statuses[-1]["connected"] is True


def test_supervisor_retries_until_modem_available(monkeypatch):
    """首次 connect 失败后 supervisor 重试，最终连上。"""
    monkeypatch.setattr("agentcall.call_agent.time.sleep", lambda s: None)
    modem = FakeModem()
    calls = {"n": 0}
    orig_connect = modem.connect

    def flaky_connect() -> None:
        calls["n"] += 1
        if calls["n"] < 3:
            raise OSError("I/O error")
        orig_connect()

    modem.connect = flaky_connect  # type: ignore[method-assign]
    service = make_service(modem)
    service.modem_connected = False  # 注入 fake 默认视为已连；此处模拟设备尚未接入
    service.start()

    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and not service.modem_connected:
        time.sleep(0.02)
    service._service_running = False
    assert service.modem_connected is True
    assert calls["n"] >= 3


def test_supervisor_ready_transition_serializes_ring_and_manual_dial(monkeypatch):
    """重连刚完成时 RING 与用户外呼并发，只允许一个会话抢占成功。"""
    modem = FakeModem()
    service = make_service(modem)
    service.modem_connected = False
    monkeypatch.setattr(service, "_credential_errors", lambda: [])
    starts: list[str | None] = []

    def fake_start(outbound_number=None, **kwargs) -> None:
        starts.append(outbound_number)
        service.session._active = True

    monkeypatch.setattr(service.session, "start", fake_start)
    connected = threading.Event()
    release_supervisor = threading.Event()
    original_set_connected = service._set_modem_connected

    def pause_when_connected(value: bool, error: str | None = None) -> None:
        original_set_connected(value, error)
        if value:
            connected.set()
            release_supervisor.wait(timeout=2)

    monkeypatch.setattr(service, "_set_modem_connected", pause_when_connected)
    service.start()
    assert connected.wait(timeout=2)

    race = threading.Barrier(3)
    dial_result: list[tuple[bool, str | None]] = []

    def ring() -> None:
        race.wait(timeout=2)
        modem.trigger_ring("10086")

    def dial() -> None:
        race.wait(timeout=2)
        dial_result.append(service.dial("10000"))

    ring_thread = threading.Thread(target=ring, daemon=True)
    dial_thread = threading.Thread(target=dial, daemon=True)
    ring_thread.start()
    dial_thread.start()
    race.wait(timeout=2)
    ring_thread.join(timeout=2)
    dial_thread.join(timeout=2)
    release_supervisor.set()
    if service._supervisor_thread:
        service._supervisor_thread.join(timeout=2)
    service._service_running = False
    service.session._active = False

    assert not ring_thread.is_alive() and not dial_thread.is_alive()
    assert len(starts) == 1
    assert len(dial_result) == 1
    if starts[0] is None:
        assert dial_result[0][0] is False
    else:
        assert starts[0] == "10000"
        assert dial_result[0][0] is True


def test_stop_service_halts_supervisor():
    modem = FakeModem()

    def boom() -> None:
        raise OSError("nope")

    modem.connect = boom  # type: ignore[method-assign]
    service = make_service(modem)
    service.modem_connected = False  # 注入 fake 默认视为已连；此处模拟设备尚未接入
    service.start()
    service.stop_service()
    assert service._service_running is False
    assert "close" in modem.call_names()


def test_modem_status_not_spammed_on_repeated_failure():
    """重连期多次失败不重复广播 disconnect（仅状态翻转才发事件）。"""
    hub = make_hub()
    modem = FakeModem()
    service = make_service(modem, hub=hub)
    service.modem_connected = False  # 注入 fake 默认视为已连；此处从未连接状态起测
    # 直接驱动内部状态转换函数，绕过真实线程
    service._set_modem_connected(False)
    service._set_modem_connected(False)
    service._set_modem_connected(False)
    events = [e for e in hub.history() if e.get("type") == "modem_status"]
    assert events == []  # 起始即 False，无翻转，不发
    service._set_modem_connected(True)
    service._set_modem_connected(True)
    events = [e for e in hub.history() if e.get("type") == "modem_status"]
    assert len(events) == 1 and events[0]["connected"] is True


def test_modem_callbacks_drive_truthful_status_and_privacy_safe_sim_events():
    from agentcall.sim_identity import identify

    hub = make_hub()
    modem = FakeModem()
    callbacks: dict[str, object] = {}
    modem.on_connection_state = (  # type: ignore[attr-defined]
        lambda callback: callbacks.__setitem__("connection", callback)
    )
    modem.on_sim_identity = (  # type: ignore[attr-defined]
        lambda callback: callbacks.__setitem__("sim", callback)
    )
    service = make_service(modem, hub=hub)

    on_connection = callbacks["connection"]
    on_sim = callbacks["sim"]
    on_connection(False)  # type: ignore[operator]
    on_connection(False)  # type: ignore[operator]
    assert service.remote_dialer_status()["modem_online"] is False
    on_connection(True)  # type: ignore[operator]
    on_sim(identify("460000123456789\r\nOK", "+CREG: 1"))  # type: ignore[operator]

    statuses = [event for event in hub.history() if event.get("type") == "modem_status"]
    assert [event["connected"] for event in statuses] == [False, True]
    assert "disconnected_at" in statuses[0]
    assert statuses[1]["recovery_seconds"] >= 0
    sim_event = next(event for event in hub.history() if event.get("type") == "sim_status")
    assert sim_event["carrier"] == "中国移动"
    assert sim_event["service_number"] == "10086"
    assert "460000123456789" not in str(sim_event)


def test_dial_rejected_when_modem_not_connected(monkeypatch):
    """模组未连接时拨打必须立即拒绝，而不是假装"已发起呼叫"。"""
    from agentcall.call_agent import CallAgentService

    service = CallAgentService(
        modem_port="unused", audio_keyword="unused", provider="qwen",
    )  # 不注入 modem：等同真机上桥没起、supervisor 还没连上
    assert service.modem_connected is False

    ok, err = service.dial("10000")
    assert not ok
    assert "模组未连接" in (err or "")


def test_dial_rejected_with_clear_error_when_provider_key_missing(monkeypatch):
    from fakes import FakeModem

    monkeypatch.setenv("AGENT_PROVIDER", "qwen")
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    hub = make_hub()
    service = make_service(FakeModem(), hub=hub)

    ok, err = service.dial("10000")

    assert not ok
    assert "DASHSCOPE_API_KEY" in (err or "")
    events = hub.history()
    assert any(
        event.get("type") == "config_error"
        and any("DASHSCOPE_API_KEY" in msg for msg in event.get("errors", []))
        for event in events
    )


def test_dial_rejects_malformed_number():
    """非号码输入直接拒绝，不占用会话等 45s 超时。"""
    from fakes import FakeModem

    from agentcall.call_agent import CallAgentService

    service = CallAgentService(
        modem_port="unused", audio_keyword="unused", provider="qwen",
        modem=FakeModem(),  # type: ignore[arg-type]
    )
    for bad in ("invalid-abc", "123; DROP", "１００００", "+"):
        ok, err = service.dial(bad)
        assert not ok and "格式不合法" in (err or ""), bad
    # 合法形态不受影响（不真正拨出：session.start 打桩）
    service.session.start = lambda *a, **kw: None  # type: ignore[method-assign]
    for good in ("10000", "+8613800138000", "*57#"):
        ok, _ = service.dial(good)
        assert ok, good


# ---- 外呼硬时限：模型不自觉收尾时，自动道别并挂断 ----

def test_outbound_auto_winddown_hangs_up(monkeypatch):
    """外呼超过 OUTBOUND_MAX_SECONDS：AI 说收尾告别 + 物理挂断（不依赖模型自觉）。"""
    from fakes import FakeAgent, FakeAudioBridge, FakeModem

    monkeypatch.setenv("OUTBOUND_MAX_SECONDS", "1")
    monkeypatch.setenv("HANGUP_TOOL_DELAY_SECONDS", "0.2")
    modem = FakeModem()
    bridge = FakeAudioBridge()
    agent = FakeAgent()
    monkeypatch.setattr("agentcall.call_agent.create_audio_bridge", lambda **kw: bridge)
    monkeypatch.setattr("agentcall.call_agent.create_agent", lambda provider: agent)
    # 外呼一拨号即视为接通（覆盖 FakeModem.dial 清除接通标志的行为）
    monkeypatch.setattr(modem, "dial", lambda number: modem.connected_flag.set() or "OK")

    service = make_service(modem)
    ok, _ = service.dial("10000")
    assert ok

    # 到点自动收尾：agent 说了告别语（含“再见”），且模组被挂断
    assert wait_until(lambda: any("再见" in s for s in agent.said), timeout=6), agent.said
    assert service.session._thread is not None
    service.session._thread.join(timeout=6)
    assert ("hangup", ()) in modem.calls
