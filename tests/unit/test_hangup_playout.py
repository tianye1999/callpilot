"""预定挂断等播放：假时钟与手动 Timer，不启动线程、不真实睡眠。"""

import asyncio
import logging
from types import SimpleNamespace

import pytest
from fakes import FakeAgent, FakeAudioBridge, FakeModem

from agentcall import call_agent
from agentcall.call_agent import CallAgentService


class ControlledBridge(FakeAudioBridge):
    def __init__(self):
        super().__init__()
        self.progress = 0

    def output_progress_bytes(self):
        return self.progress

    def play(self, count):
        self.pending_bytes -= count
        self.progress += count

    def write_modem_chunks(self, chunks):
        for chunk in chunks:
            self.pending_bytes += len(chunk)

    def silence_for_hangup(self):
        return self.discard_pending_output()


class ManualTimer:
    timers = []

    def __init__(self, interval, function, args=()):
        self.interval = interval
        self.function = function
        self.args = args
        self.cancelled = False
        self.timers.append(self)

    def start(self):
        pass

    def cancel(self):
        self.cancelled = True

    def fire(self):
        # 故意允许 cancel 后触发，模拟已越过取消点的回调。
        self.function(*self.args)


@pytest.fixture
def rig(monkeypatch):
    clock = SimpleNamespace(now=100.0)
    monkeypatch.setattr(call_agent.time, "monotonic", lambda: clock.now)
    ManualTimer.timers = []
    monkeypatch.setattr(call_agent.threading, "Timer", ManualTimer)
    service = CallAgentService("unused", "unused", provider="local", modem=FakeModem())
    session = service.session
    session._set_active(True)
    session._hangup_delay_seconds = 4.5
    bridge = ControlledBridge()
    session._start_hangup_bridge(bridge)
    events = []
    session._record = SimpleNamespace(log_event=lambda kind, **fields: events.append((kind, fields)))
    stops = []
    original_stop = session.stop

    def stop():
        stops.append(clock.now)
        original_stop()

    monkeypatch.setattr(session, "stop", stop)
    return SimpleNamespace(
        clock=clock, service=service, session=session, bridge=bridge, events=events, stops=stops,
    )


def tool_deadline(rig):
    rig.session._schedule_deferred_hangup()
    assert ManualTimer.timers[-1].interval == 4.5
    rig.clock.now += 4.5
    ManualTimer.timers[-1].fire()


def test_tool_waits_for_playout_and_tail(rig, caplog):
    caplog.set_level(logging.INFO)
    rig.bridge.pending_bytes = 16000
    tool_deadline(rig)
    assert rig.session.is_active
    assert rig.stops == []
    rig.clock.now += 0.5
    rig.bridge.play(16000)
    ManualTimer.timers[-1].fire()
    assert rig.stops == []
    rig.clock.now += 0.299
    ManualTimer.timers[-1].fire()
    assert rig.stops == []
    rig.clock.now += 0.002
    ManualTimer.timers[-1].fire()
    assert len(rig.stops) == 1
    assert rig.events == [("hangup_playout_wait", {
        "reason": "hangup_tool", "pending_bytes": 16000, "queued_bytes": 0,
        "generating": False, "media_busy": False, "wait_ms": 801.0, "end_reason": "played_out",
    })]
    assert "挂断前等待播放" in caplog.text


def test_stalled_backlog_hangs_up_without_waiting_for_max(rig):
    rig.bridge.pending_bytes = 16000
    tool_deadline(rig)
    rig.clock.now += 1.499
    ManualTimer.timers[-1].fire()
    assert rig.stops == []
    rig.clock.now += 0.002
    ManualTimer.timers[-1].fire()
    assert len(rig.stops) == 1
    assert rig.events[0][1]["end_reason"] == "stalled"
    assert rig.events[0][1]["wait_ms"] == 1501.0


def test_continuous_writes_cannot_extend_max_wait(rig):
    rig.bridge.pending_bytes = 16000
    tool_deadline(rig)
    for _ in range(19):
        rig.clock.now += 0.5
        rig.bridge.progress += 8000  # 边生成边播放，积压长度始终不减。
        ManualTimer.timers[-1].fire()
        assert rig.stops == []
    rig.clock.now += 0.5
    rig.bridge.progress += 8000
    ManualTimer.timers[-1].fire()
    assert len(rig.stops) == 1
    assert rig.events[0][1]["end_reason"] == "max_wait"
    assert rig.events[0][1]["wait_ms"] == 10000.0


@pytest.mark.parametrize("disabled", [False, True])
def test_empty_or_disabled_keeps_original_deadline(rig, monkeypatch, disabled):
    if disabled:
        monkeypatch.setenv("HANGUP_PLAYOUT_MAX_SECONDS", "0")
        rig.session._load_session_config()
        rig.bridge.pending_bytes = 16000
        rig.session._outgoing_audio.put(b"\x01\x00" * 160)
    tool_deadline(rig)
    assert rig.stops == [104.5]
    assert rig.events == []
    assert len(ManualTimer.timers) == 1


def test_queue_without_bridge_backlog_also_waits(rig):
    rig.session._outgoing_audio.put(b"\x01\x00" * 160)
    tool_deadline(rig)
    assert rig.stops == []
    rig.session._drain_agent_audio(rig.bridge)
    rig.clock.now += 0.5
    rig.bridge.play(320)
    ManualTimer.timers[-1].fire()
    rig.clock.now += 0.301
    ManualTimer.timers[-1].fire()
    assert len(rig.stops) == 1
    assert rig.events[0][1]["pending_bytes"] == 0
    assert rig.events[0][1]["queued_bytes"] == 320


@pytest.mark.parametrize("interruption", ["remote", "manual", "new_session", "detach"])
def test_wait_interrupted_never_stops_again_or_touches_new_session(rig, monkeypatch, interruption):
    rig.bridge.pending_bytes = 16000
    tool_deadline(rig)
    late_timer = ManualTimer.timers[-1]
    rig.clock.now += 0.5
    if interruption == "remote":
        rig.service.modem.trigger_hangup()
        assert rig.bridge.pending_bytes == 0
        assert rig.events[0][1]["end_reason"] == "remote_hangup"
    elif interruption == "manual":
        rig.session.stop()
    elif interruption == "detach":
        asyncio.run(rig.session.detach_agent(FakeAgent(), rig.bridge))
        assert rig.session.is_active
        assert rig.events[0][1]["end_reason"] == "session_changed"
    else:
        monkeypatch.setattr(call_agent.threading.Thread, "start", lambda self: None)
        rig.session._set_active(False)
        rig.session.start()
        assert rig.session.is_active
        assert rig.events[0][1]["end_reason"] == "session_changed"
        rig.session._record = SimpleNamespace(log_event=lambda *args, **kw: pytest.fail("写入了新会话"))
    stops = list(rig.stops)
    rig.clock.now += 20
    late_timer.fire()
    late_timer.fire()
    assert rig.stops == stops
    assert rig.session.is_active == (interruption in {"new_session", "detach"})
    assert len(rig.events) == (0 if interruption == "manual" else 1)


def test_event_write_failure_does_not_prevent_hangup(rig, caplog):
    def fail(*args, **kwargs):
        raise OSError("fake disk failure")

    rig.session._record = SimpleNamespace(log_event=fail)
    rig.bridge.pending_bytes = 16000
    tool_deadline(rig)
    rig.clock.now += 1.5
    ManualTimer.timers[-1].fire()
    assert len(rig.stops) == 1
    assert "事件落盘失败（不影响挂断）" in caplog.text


@pytest.mark.parametrize("path", ["wrap_up_judge", "outbound_deadline"])
@pytest.mark.parametrize("mode", ["drain", "empty", "disabled"])
def test_main_loop_winddown_drains_audio_before_hangup(rig, monkeypatch, path, mode):
    agent = FakeAgent()
    rig.session._outbound_number = "10000"
    rig.session._wrap_up_requested = path == "wrap_up_judge"
    rig.session._profile_max_call_seconds = 1 if path == "outbound_deadline" else 0
    rig.session._profile_wrap_up_judge = False
    if mode == "disabled":
        rig.session._hangup_playout_max_seconds = 0
    said_at = []
    drained_at = []

    async def say(instructions):
        said_at.append(rig.clock.now)
        if mode != "empty":
            rig.session._outgoing_audio.put(b"\x01\x00" * 64000)  # 8s，基础延迟放不完。

    async def tick(_seconds):
        assert rig.clock.now < 120, "主循环未按假时钟结束"
        rig.clock.now = round(rig.clock.now + 0.1, 3)
        if rig.bridge.pending_bytes:
            rig.bridge.play(min(1600, rig.bridge.pending_bytes))
            if not rig.bridge.pending_bytes:
                drained_at.append(rig.clock.now)

    monkeypatch.setattr(agent, "say", say)
    monkeypatch.setattr(call_agent.asyncio, "sleep", tick)
    asyncio.run(rig.session._run_agent_loop(agent, rig.bridge, rig.session._record, []))
    wait_events = [fields for kind, fields in rig.events if kind == "hangup_playout_wait"]
    if mode != "drain":
        assert rig.clock.now == pytest.approx(said_at[0] + 4.5)
        assert wait_events == []
        assert bool(rig.bridge.pending_bytes) == (mode == "disabled")
        return
    assert len(said_at) == len(drained_at) == 1
    assert rig.clock.now >= drained_at[0] + 0.3
    assert rig.clock.now < drained_at[0] + 0.5
    assert len(wait_events) == 1
    assert wait_events[0]["reason"] == path
    assert wait_events[0]["end_reason"] == "played_out"
    assert wait_events[0]["pending_bytes"] > 0
    assert rig.bridge.pending_bytes == 0
    asyncio.run(rig.session._shutdown_agent(agent, rig.bridge))
    assert ("hangup", ()) in rig.service.modem.calls


@pytest.mark.parametrize("provider", ["local_brain", "local_tts", "openai_active", "openai_requested"])
def test_existing_generation_signals_wait_before_first_audio(rig, provider):
    from agentcall.agents.local_agent import LocalPipelineAgent
    from agentcall.agents.openai_agent import OpenAIVoiceAgent

    if provider.startswith("local"):
        agent = LocalPipelineAgent()
        work = agent._brain_queue if provider == "local_brain" else agent._tts_queue
        work.put(object())
        work.get_nowait()  # 队列已空，但 LLM/TTS 仍在 worker 中处理。
        complete = work.task_done
    else:
        agent = OpenAIVoiceAgent(api_key="test-key", model="test-model", model_display_name="Test")
        field = "_response_active" if provider == "openai_active" else "_response_requested"
        setattr(agent, field, True)
        def complete():
            setattr(agent, field, False)
    rig.session._hangup_agent = agent
    tool_deadline(rig)
    assert rig.stops == []
    rig.clock.now += 2.0
    ManualTimer.timers[-1].fire()
    assert rig.stops == []  # 纯生成尚无积压，不误判下行停滞。
    complete()
    rig.session._outgoing_audio.put(b"\x01\x00" * 160)
    rig.session._drain_agent_audio(rig.bridge)
    rig.clock.now += 0.1
    ManualTimer.timers[-1].fire()
    assert rig.stops == []
    rig.bridge.play(320)
    rig.clock.now += 0.1
    ManualTimer.timers[-1].fire()
    rig.clock.now += 0.301
    ManualTimer.timers[-1].fire()
    assert len(rig.stops) == 1
    assert rig.events[0][1]["generating"] is True


def test_new_audio_during_tail_restarts_tail_without_extending_cap(rig):
    rig.bridge.pending_bytes = 16000
    tool_deadline(rig)
    for _ in range(19):
        rig.clock.now += 0.25
        rig.bridge.play(rig.bridge.pending_bytes)
        ManualTimer.timers[-1].fire()
        assert rig.stops == []
        rig.clock.now += 0.25
        rig.bridge.pending_bytes = 16000
        ManualTimer.timers[-1].fire()
        assert rig.stops == []
    rig.clock.now += 0.5
    rig.bridge.play(16000)
    ManualTimer.timers[-1].fire()
    assert len(rig.stops) == 1
    assert rig.events[0][1]["end_reason"] == "max_wait"


def test_generation_without_audio_still_has_a_hard_cap(rig):
    from agentcall.agents.local_agent import LocalPipelineAgent

    agent = LocalPipelineAgent()
    agent._brain_queue.put(object())
    rig.session._hangup_agent = agent
    tool_deadline(rig)
    rig.clock.now += 10
    ManualTimer.timers[-1].fire()
    assert len(rig.stops) == 1
    assert rig.events[0][1]["end_reason"] == "max_wait"


def test_small_max_wait_does_not_schedule_beyond_cap(rig, monkeypatch):
    monkeypatch.setenv("HANGUP_PLAYOUT_MAX_SECONDS", "0.01")
    rig.session._load_session_config()
    rig.bridge.pending_bytes = 16000
    tool_deadline(rig)
    assert ManualTimer.timers[-1].interval == pytest.approx(0.01)


def test_audio_arriving_as_generation_completes_is_not_missed(rig, monkeypatch):
    def completed_with_audio(_agent):
        rig.session._outgoing_audio.put(b"\x01\x00" * 160)
        return False

    monkeypatch.setattr(rig.session, "_hangup_reply_pending", completed_with_audio)
    tool_deadline(rig)
    assert rig.stops == []
    assert rig.session._hangup_playout_wait.queued_bytes == 320


def test_media_sampling_never_holds_generation_lock(rig):
    class CheckLockOrder:
        def acquire(self, *, blocking):
            assert not blocking
            # 同步 UAC 写出回调需要此锁；不能形成 hangup -> media 反序。
            assert rig.session._hangup_lock.acquire(blocking=False)
            rig.session._hangup_lock.release()
            return True

        def release(self):
            pass

    rig.session._media_lock = CheckLockOrder()
    rig.bridge.pending_bytes = 16000
    tool_deadline(rig)
    assert rig.stops == []


@pytest.mark.parametrize("progressing", [False, True])
def test_media_lock_contention_cannot_block_timer_or_hide_limits(rig, progressing):
    # 用同线程持锁模拟下行 write 未返回；若 Timer 阻塞拿锁，此用例会死锁。
    assert rig.session._media_lock.acquire(blocking=False)
    try:
        tool_deadline(rig)
        assert rig.stops == []
        assert rig.session._hangup_playout_wait.media_busy
        for _ in range(20 if progressing else 3):
            rig.clock.now += 0.5
            if progressing:
                rig.bridge.progress += 8000
            ManualTimer.timers[-1].fire()
        assert len(rig.stops) == 1
        assert rig.events[0][1]["end_reason"] == ("max_wait" if progressing else "stalled")
    finally:
        rig.session._media_lock.release()


def test_generation_changes_during_sampling_cannot_stop_new_call(rig, monkeypatch):
    def changed():
        rig.session._session_generation += 1
        return 0

    monkeypatch.setattr(rig.bridge, "pending_output_bytes", changed)
    tool_deadline(rig)
    assert rig.stops == []
    assert rig.session.is_active
    assert rig.events == []
    assert len(ManualTimer.timers) == 1


@pytest.mark.parametrize("interruption", ["remote", "new_session"])
def test_main_loop_wait_exits_on_remote_hangup_or_new_generation(rig, monkeypatch, interruption):
    rig.session._wrap_up_requested = True
    rig.session._hangup_delay_seconds = 0
    rig.bridge.pending_bytes = 16000
    monkeypatch.setattr(call_agent.threading.Thread, "start", lambda self: None)

    async def tick(_seconds):
        assert rig.session._hangup_playout_wait is not None
        rig.clock.now += 0.5
        if interruption == "remote":
            rig.service.modem.trigger_hangup()
        else:
            rig.session._set_active(False)
            rig.session.start()

    monkeypatch.setattr(call_agent.asyncio, "sleep", tick)
    asyncio.run(rig.session._run_agent_loop(FakeAgent(), rig.bridge, rig.session._record, []))
    assert len(rig.stops) == (1 if interruption == "remote" else 0)
    assert rig.session.is_active == (interruption == "new_session")
    waits = [fields for kind, fields in rig.events if kind == "hangup_playout_wait"]
    assert len(waits) == 1
    assert waits[0]["end_reason"] == ("remote_hangup" if interruption == "remote" else "session_changed")


def test_parallel_stall_ends_wait_without_recovery_discard(rig, monkeypatch):
    rig.session._parallel_agent_connect = True
    rig.session._wrap_up_requested = True
    rig.session._hangup_delay_seconds = 0
    rig.bridge.pending_bytes = 16000

    async def tick(_seconds):
        rig.clock.now = round(rig.clock.now + 0.1, 3)
        assert rig.clock.now <= 102

    monkeypatch.setattr(call_agent.asyncio, "sleep", tick)
    asyncio.run(rig.session._run_agent_loop(FakeAgent(), rig.bridge, rig.session._record, []))
    assert rig.clock.now == 101.5
    assert rig.bridge.discarded_bytes == 0
    waits = [fields for kind, fields in rig.events if kind == "hangup_playout_wait"]
    assert waits[0]["end_reason"] == "stalled"


def test_triage_rejection_keeps_its_original_deadline(rig, monkeypatch):
    rig.bridge.pending_bytes = 16000
    rig.session._triage_reject_deadline = rig.clock.now

    async def reject(*args):
        return "reject"

    async def unexpected_tick(_seconds):
        pytest.fail("分诊拒绝被额外等待拖延")

    monkeypatch.setattr(rig.session, "_consume_triage_results", reject)
    monkeypatch.setattr(call_agent.asyncio, "sleep", unexpected_tick)
    asyncio.run(rig.session._run_agent_loop(FakeAgent(), rig.bridge, rig.session._record, []))
    assert rig.session._hangup_playout_wait is None
    assert rig.clock.now == 100
