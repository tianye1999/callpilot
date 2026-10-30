"""Call startup uses only fake modems/bridges/providers, never real devices."""

from __future__ import annotations

import asyncio
import json
import threading
import time
import wave

import pytest
from fakes import FakeAgent, FakeAudioBridge, FakeModem

from agentcall.audio_bridge import apply_pcm_gain
from agentcall.call_agent import CallSession
from agentcall.call_log import CallLogger
from agentcall.modem import Sim7600Modem


@pytest.fixture
def setup_call(monkeypatch, tmp_path):
    monkeypatch.setenv("PARALLEL_AGENT_CONNECT", "true")
    monkeypatch.setenv("RECORDING_ENABLED", "true")
    monkeypatch.setenv("PROMPT_GEN_ENABLED", "false")
    modem = FakeModem()
    modem.POST_ANSWER_SETTLE_SECONDS = 0
    bridge = FakeAudioBridge()
    agent = FakeAgent()
    session = CallSession(
        modem, "unused", "openai", "nmea", "unused", 115200, 1.0,
        call_logger=CallLogger(tmp_path / "calls", recording_enabled=True),
    )
    session._set_active(True)
    monkeypatch.setattr("agentcall.call_agent.create_agent", lambda _provider: agent)
    monkeypatch.setattr("agentcall.call_agent.create_audio_bridge", lambda **_kw: bridge)

    async def finish(_agent, active_bridge, _record, _transcripts):
        session._drain_agent_audio(active_bridge)
        session._drain_agent_audio(active_bridge)
        session._set_active(False)
        return active_bridge

    monkeypatch.setattr(session, "_run_agent_loop", finish)
    return session, modem, bridge, agent, tmp_path / "calls"


def events(base):
    path = next(base.iterdir()) / "events.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_product_defaults_local_parallel_startup_and_cleanup(setup_call, monkeypatch, product_latency_defaults):
    from test_local_segmented import SegmentedPipeline

    from agentcall import config
    from agentcall.agents.local_agent import LocalPipelineAgent

    session, _modem, bridge, _agent, base = setup_call
    agents = []

    class Local(LocalPipelineAgent):
        def __init__(self):
            super().__init__(pipeline_factory=SegmentedPipeline,
                             llm_chat=lambda *a: {"role": "assistant", "content": "您好。"})
            agents.append(self)

    monkeypatch.setattr("agentcall.call_agent.LocalPipelineAgent", Local)
    session.provider = "local"
    assert config.get_bool("PARALLEL_AGENT_CONNECT")
    asyncio.run(session._handle_call())
    assert len(agents) == 1 and not agents[0]._running
    assert session._local_prepared_agent is None and session._local_prepare_task is None
    assert bridge.started and bridge.stopped
    snapshot = next(e for e in events(base) if e["type"] == "config_snapshot")
    assert snapshot["parallel_agent_connect"] is True
    assert snapshot["local_tts_streaming"] and snapshot["local_asr_segmented"]
    assert snapshot["local_fast_opening"] and snapshot["local_speculative_llm"]


@pytest.fixture
def native_call(setup_call, monkeypatch):
    session, _old_modem, bridge, agent, base = setup_call
    monkeypatch.setenv("MODEM_ANSWER_GREETING", "喂")
    monkeypatch.setenv("MODEM_ANSWER_GREETING_DELAY_MS", "50")
    monkeypatch.setenv("MODEM_PCM_PIN_8K", "false")
    monkeypatch.setenv("MODEM_RESET_AFTER_CALL", "false")
    modem = Sim7600Modem("unused-fake-port")
    modem.POST_ANSWER_SETTLE_SECONDS = 0
    sent = []
    monkeypatch.setattr(modem, "_send", lambda cmd: sent.append(cmd) or "OK")
    monkeypatch.setattr(modem, "_write_command", lambda cmd: sent.append(cmd) or "OK")
    modem._init_answer_greeting()
    sent.clear()
    session.modem = modem
    return session, modem, bridge, agent, base, sent


@pytest.mark.parametrize("ok", [True, False])
def test_native_greeting_sequence_parallel_connect_and_conditional_hint(native_call, monkeypatch, ok):
    session, modem, bridge, agent, base, sent = native_call
    connected = threading.Event()
    said = threading.Event()
    order = []
    answered_at = 0.0
    tts_at = 0.0
    instructions = []

    def send(cmd):
        nonlocal answered_at
        sent.append(cmd)
        order.append(cmd)
        if cmd == "ATA":
            answered_at = time.monotonic()
        elif cmd == "AT+CPCMREG=1":
            assert said.wait(timeout=1)  # Generated opening must still cache before PCM starts.
        return "OK"

    def tts(cmd):
        nonlocal tts_at
        tts_at = time.monotonic()
        assert connected.wait(timeout=1), "AI connection must overlap native greeting preparation"
        assert not said.is_set(), "Opening cannot precede the greeting result"
        order.append("CTTS")
        time.sleep(0.03)
        order.append("CTTS_return")
        return "OK" if ok else "ERROR"

    async def connect(callback):
        order.append("agent_connected")
        connected.set()
        await FakeAgent.start(agent, callback)

    original_say = agent.say

    async def say(text):
        instructions.append(text)
        assert "CTTS_return" in order
        assert not bridge.started
        order.append("say")
        await original_say(text)
        said.set()

    monkeypatch.setattr(modem, "_send", send)
    monkeypatch.setattr(modem, "_write_command", tts)
    monkeypatch.setattr(agent, "start", connect)
    monkeypatch.setattr(agent, "say", say)
    asyncio.run(session._handle_call())
    # Sampled intervals can lose one monotonic tick; allow 1ms for metric rounding.
    tick_ms = time.get_clock_info("monotonic").resolution * 1000
    assert tts_at - answered_at >= (50 - tick_ms - 1) / 1000
    assert order.index("ATA") < order.index("CTTS") < order.index("CTTS_return") < order.index("AT+CPCMREG=1")
    assert order.index("agent_connected") < order.index("CTTS_return")
    assert ("系统已代为说过" in instructions[0]) is ok
    if ok:
        assert "喂" in instructions[0] and "不要再重复打招呼" in instructions[0]
        assert "喂，你好。" not in instructions[0]
    assert agent.stopped and bridge.stopped
    expected = bridge.agent_to_modem(agent.reply_pcm, agent.output_rate)
    assert b"".join(bridge.downlink) == expected
    marks = events(base)
    module = next(event for event in marks if event["type"] == "module_greeting")
    assert module["ok"] is ok
    assert module["duration_ms"] >= 30 - tick_ms - 1
    assert module["started_after_answer_ms"] >= 50 - tick_ms - 1
    assert "text" not in module and "喂" not in json.dumps(module, ensure_ascii=False)
    metrics = json.loads((next(base.iterdir()) / "metrics.json").read_text(encoding="utf-8"))
    assert metrics["module_greeting"]["ok"] is ok
    assert metrics["first_playout_boundary"] == "pcm_written"


@pytest.mark.parametrize("disabled", ["outbound", "serial", "empty"])
def test_native_greeting_not_played_outside_enabled_parallel_inbound(native_call, monkeypatch, disabled):
    session, _modem, _bridge, agent, base, sent = native_call
    if disabled == "serial":
        monkeypatch.setenv("PARALLEL_AGENT_CONNECT", "false")
    elif disabled == "empty":
        monkeypatch.setenv("MODEM_ANSWER_GREETING", "")
    else:
        session._outbound_number = "10086"

        async def connected(**_kw):
            return True

        monkeypatch.setattr(session, "_wait_connected", connected)
    opening = []
    original_say = agent.say

    async def say(text):
        opening.append(text)
        await original_say(text)

    monkeypatch.setattr(agent, "say", say)
    asyncio.run(session._handle_call())
    assert not any(cmd.startswith("AT+CTTS") for cmd in sent)
    assert "系统已代为说过" not in opening[0]
    assert not any(event["type"] == "module_greeting" for event in events(base))


def test_native_greeting_failed_route_keeps_original_opening(native_call, monkeypatch):
    session, modem, _bridge, agent, base, sent = native_call
    monkeypatch.setattr(modem, "_send", lambda cmd: "ERROR" if cmd == "AT+CDTAM=1" else "OK")
    modem._init_answer_greeting()
    monkeypatch.setattr(modem, "_write_command", lambda _cmd: "ERROR")
    instructions = []
    opening_done = asyncio.Event()
    original_say = agent.say
    original_loop = session._run_agent_loop

    async def say(text):
        instructions.append(text)
        await original_say(text)
        opening_done.set()

    async def finish_after_opening(*args):
        # The fixture's simulated hangup must not cancel the opening being tested.
        await asyncio.wait_for(opening_done.wait(), timeout=2)
        return await original_loop(*args)

    monkeypatch.setattr(agent, "say", say)
    monkeypatch.setattr(session, "_run_agent_loop", finish_after_opening)
    asyncio.run(session._handle_call())
    assert "系统已代为说过" not in instructions[0]
    assert not any(cmd.startswith("AT+CTTS") for cmd in sent)
    assert next(event for event in events(base) if event["type"] == "module_greeting")["ok"] is False


@pytest.mark.parametrize("phase", ["delay", "tts"])
def test_native_greeting_hangup_or_cancel_joins_worker_without_pcm(native_call, monkeypatch, phase):
    session, modem, bridge, agent, _base, sent = native_call
    monkeypatch.setenv("MODEM_ANSWER_GREETING_DELAY_MS", "200" if phase == "delay" else "0")
    connecting = asyncio.Event()
    tts_entered = threading.Event()
    release_tts = threading.Event()

    async def connect(callback):
        await FakeAgent.start(agent, callback)
        connecting.set()

    def tts(cmd):
        sent.append(cmd)
        tts_entered.set()
        assert release_tts.wait(timeout=1)
        assert "AT+CHUP" not in sent  # Cleanup must join the blocking CTTS before hangup/reset.
        return "OK"

    monkeypatch.setattr(agent, "start", connect)
    monkeypatch.setattr(modem, "_write_command", tts)

    async def run():
        task = asyncio.create_task(session._handle_call())
        await asyncio.wait_for(connecting.wait(), timeout=1)
        if phase == "delay":
            session._set_active(False)  # Peer hangup during cancellable ATA->CTTS delay.
            await asyncio.wait_for(task, timeout=1)
        else:
            assert await asyncio.to_thread(tts_entered.wait, 1)
            task.cancel()
            await asyncio.sleep(0.02)
            assert "AT+CHUP" not in sent
            release_tts.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=1)

    asyncio.run(run())
    assert "AT+CPCMREG=1" not in sent
    if phase == "delay":
        assert not tts_entered.is_set()
    assert agent.stopped and not bridge.started
    assert "AT+CHUP" in sent


@pytest.mark.parametrize("phase", ["answer", "agent", "pcm"])
def test_native_greeting_startup_failures_cleanup(native_call, monkeypatch, phase):
    session, modem, bridge, agent, _base, sent = native_call

    def fail(*_args):
        raise RuntimeError("fake startup failure")

    if phase == "answer":
        monkeypatch.setattr(modem, "answer", fail)
    elif phase == "agent":
        async def fail_connect(_callback):
            fail()
        monkeypatch.setattr(agent, "start", fail_connect)
    else:
        monkeypatch.setattr(modem, "initialize_for_voice", fail)
    with pytest.raises(RuntimeError, match="fake startup failure"):
        asyncio.run(session._handle_call())
    assert agent.stopped and not bridge.started
    assert "AT+CHUP" in sent
    if phase in {"answer", "agent"}:
        assert not any(cmd.startswith("AT+CTTS") for cmd in sent)


@pytest.mark.parametrize("parallel", [False, True])
def test_startup_order_and_overlap(setup_call, monkeypatch, parallel):
    session, modem, bridge, agent, _base = setup_call
    monkeypatch.setenv("PARALLEL_AGENT_CONNECT", str(parallel).lower())
    order = []
    spans: dict[str, list[float]] = {}
    pcm_entered = threading.Event()
    connecting = threading.Event()
    loop_thread = threading.get_ident()

    def answer():
        order.append("answer")
        if parallel:
            assert threading.get_ident() != loop_thread

    def pcm(_mode):
        order.append("pcm_begin")
        pcm_entered.set()
        if parallel:
            assert threading.get_ident() != loop_thread
            assert connecting.wait(timeout=1)
        spans["pcm"] = [time.monotonic()]
        time.sleep(0.25)
        spans["pcm"].append(time.monotonic())
        order.append("pcm_end")

    async def connect(callback):
        order.append("agent_connect")
        connecting.set()
        spans["connect"] = [time.monotonic()]
        await asyncio.sleep(0.25)
        spans["connect"].append(time.monotonic())
        await FakeAgent.start(agent, callback)
        order.append("agent_ready")

    original_start = bridge.start

    def start_bridge():
        order.append("bridge_start")
        original_start()

    original_say = agent.say

    async def say(text):
        order.append("say")
        await original_say(text)

    monkeypatch.setattr(modem, "answer", answer)
    monkeypatch.setattr(modem, "initialize_for_voice", pcm)
    monkeypatch.setattr(agent, "start", connect)
    monkeypatch.setattr(agent, "say", say)
    monkeypatch.setattr(bridge, "start", start_bridge)
    started = time.monotonic()
    asyncio.run(session._handle_call())
    elapsed = time.monotonic() - started

    if parallel:
        assert order.index("agent_connect") < order.index("pcm_end")
        # The two 0.25s waits must overlap in time. Comparing spans instead of the
        # whole call's wall time keeps scheduler overhead (slow Windows runners)
        # out of the verdict; a serial path cannot produce an overlap.
        (pcm_start, pcm_stop), (connect_start, connect_stop) = spans["pcm"], spans["connect"]
        assert max(pcm_start, connect_start) < min(pcm_stop, connect_stop), spans
    else:
        assert order == ["answer", "pcm_begin", "pcm_end", "bridge_start", "agent_connect", "agent_ready", "say"]
        assert spans["pcm"][1] <= spans["connect"][0], spans
        assert elapsed >= 0.5 - time.get_clock_info("monotonic").resolution
    assert agent.stopped and bridge.stopped


def test_early_greeting_is_queued_once_and_measured_at_playout(setup_call, monkeypatch):
    session, modem, bridge, agent, base = setup_call
    release_pcm = threading.Event()
    original_say = agent.say

    def pcm(_mode):
        assert release_pcm.wait(timeout=1)

    async def say(text):
        assert not bridge.started
        # Silence trimming must happen once, before caching, not again on replay.
        agent._on_audio_out(b"\x00\x00" * 240)
        await original_say(text)
        agent._on_audio_out(agent.reply_pcm)
        assert not session._outgoing_audio.empty()
        assert bridge.downlink == []
        assert session._record._downlink == []  # Record actual output, not generation.
        release_pcm.set()

    monkeypatch.setattr(modem, "initialize_for_voice", pcm)
    monkeypatch.setattr(agent, "say", say)
    asyncio.run(session._handle_call())

    expected = bridge.agent_to_modem(agent.reply_pcm, agent.output_rate) * 2
    assert b"".join(bridge.downlink) == expected
    with wave.open(str(next(base.iterdir()) / "downlink.wav"), "rb") as recording:
        assert recording.readframes(recording.getnframes()) == expected
    marks = events(base)
    types = [event["type"] for event in marks]
    assert types.index("greeting_requested") < types.index("bridge_started") < types.index("first_audio")
    first = [event for event in marks if event["type"] == "first_audio"]
    assert len(first) == 1
    assert first[0]["boundary"] == "pcm_written"
    assert first[0]["t_ms"] >= next(event["t_ms"] for event in marks if event["type"] == "bridge_started")


@pytest.mark.parametrize("failure", ["answer", "pcm", "pcm_not_ok", "bridge", "agent", "say"])
def test_startup_failure_closes_every_resource(setup_call, monkeypatch, failure):
    session, modem, bridge, agent, _base = setup_call

    def fail(*_args):
        raise RuntimeError("startup failed")

    async def async_fail(*_args):
        raise RuntimeError("startup failed")

    if failure == "answer":
        monkeypatch.setattr(modem, "answer", fail)
    elif failure == "pcm":
        monkeypatch.setattr(modem, "initialize_for_voice", fail)
    elif failure == "pcm_not_ok":
        modem.last_voice_enable = {"ok": False, "attempts": 6, "degraded": True}
    elif failure == "bridge":
        monkeypatch.setattr(bridge, "start", fail)
    else:
        monkeypatch.setattr(agent, "start" if failure == "agent" else "say", async_fail)

    async def run():
        with pytest.raises(RuntimeError):
            await session._handle_call()
        assert not [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]

    asyncio.run(run())
    assert agent.stopped
    assert "hangup" in modem.call_names()
    assert not session.is_active
    if failure == "bridge":
        assert bridge.stopped


@pytest.mark.parametrize("outbound", [False, True])
def test_serial_startup_pcm_failure_hangs_up_before_bridge(setup_call, monkeypatch, outbound):
    session, modem, bridge, agent, base = setup_call
    monkeypatch.setenv("PARALLEL_AGENT_CONNECT", "false")
    modem.last_voice_enable = {"ok": False, "attempts": 6, "degraded": True}
    if outbound:
        session._outbound_number = "123"

        async def connected(mark):
            return True

        monkeypatch.setattr(session, "_connect_outbound", connected)
    asyncio.run(session._handle_call())
    assert modem.call_names().count("hangup") == 1
    assert not bridge.started and not agent.started
    assert not session.is_active
    assert any(e["type"] == "pcm_enable_failed" for e in events(base))
    assert next(e for e in events(base) if e["type"] == "ended")["status"] == "failed"


def test_loop_close_failure_still_notifies_queue_once(setup_call, monkeypatch, caplog):
    session, _modem, _bridge, _agent, _base = setup_call
    loop = asyncio.new_event_loop()
    close = loop.close
    ended = []
    session._on_ended = lambda: ended.append(session._resources_released)

    async def finish():
        pass

    def failed_close():
        raise RuntimeError("Cannot close a running event loop")

    monkeypatch.setattr(session, "_handle_call", finish)
    monkeypatch.setattr("agentcall.call_agent.asyncio.new_event_loop", lambda: loop)
    monkeypatch.setattr(loop, "close", failed_close)
    try:
        session._run()
    finally:
        close()
        asyncio.set_event_loop(None)
    assert ended == [True]
    assert "事件循环关闭失败" in caplog.text


@pytest.mark.parametrize("phase", ["pcm", "connect", "say"])
@pytest.mark.parametrize("cancel", [False, True])
def test_hangup_or_cancel_during_startup_joins_workers(setup_call, monkeypatch, phase, cancel):
    session, modem, bridge, agent, _base = setup_call
    entered = threading.Event()
    pcm_entered = threading.Event()
    release = threading.Event()
    order = []

    def pcm(_mode):
        pcm_entered.set()
        if phase == "pcm":
            entered.set()
            assert release.wait(timeout=5)
        order.append("pcm_finished")

    async def blocked(*_args):
        entered.set()
        await asyncio.Event().wait()

    async def stop():
        order.append("agent_stopped")
        agent.stopped = True
        release.set()

    def hangup():
        order.append("hangup")

    async def run_loop(_agent, active_bridge, _record, _transcripts):
        await asyncio.Event().wait()  # The startup owner cancels the loop on hangup.
        return active_bridge

    monkeypatch.setattr(modem, "initialize_for_voice", pcm)
    monkeypatch.setattr(modem, "hangup", hangup)
    monkeypatch.setattr(agent, "stop", stop)
    monkeypatch.setattr(session, "_run_agent_loop", run_loop)
    if phase != "pcm":
        monkeypatch.setattr(agent, "start" if phase == "connect" else "say", blocked)

    async def run():
        task = asyncio.create_task(session._handle_call())
        assert await asyncio.to_thread(entered.wait, 5)
        # Agent connection/opening can run before the executor starts PCM at all.
        assert await asyncio.to_thread(pcm_entered.wait, 5)
        if cancel:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=2)
        else:
            session.stop()
            await asyncio.wait_for(task, timeout=2)
        assert not [other for other in asyncio.all_tasks() if other is not asyncio.current_task()]

    asyncio.run(run())
    assert agent.stopped
    assert order[-1] == "hangup"
    assert order.index("pcm_finished") < order.index("hangup")
    if phase == "pcm":
        assert not bridge.started  # A late PCM return must never resurrect media.
        assert order.index("agent_stopped") < order.index("pcm_finished")


@pytest.mark.parametrize("opening_mode", ["say", "wait"])
def test_outbound_starts_agent_only_after_connect_and_preserves_wait(setup_call, monkeypatch, opening_mode):
    session, modem, bridge, agent, base = setup_call
    session._outbound_number = "10086"
    session._prompt_gen_opening_mode = opening_mode
    monkeypatch.setattr(session, "_build_agent_instructions", lambda _direction: "test")
    loop_thread = threading.get_ident()

    def dial(_number):
        assert threading.get_ident() != loop_thread
        modem.connected_flag.set()

    original_start = agent.start

    async def connect(callback):
        assert modem.is_call_connected()
        await original_start(callback)

    monkeypatch.setattr(modem, "dial", dial)
    monkeypatch.setattr(agent, "start", connect)
    asyncio.run(session._handle_call())

    types = [event["type"] for event in events(base)]
    assert types.index("connected") < types.index("agent_connect_started")
    assert bool(agent.said) is (opening_mode == "say")
    assert bool(bridge.downlink) is (opening_mode == "say")
    assert ("first_audio" in types) is (opening_mode == "say")
    assert agent.stopped and bridge.stopped


def test_dtmf_guard_and_tones_do_not_consume_first_ai_playout(setup_call, monkeypatch):
    session, _modem, bridge, agent, base = setup_call
    tone = b"\x01\x02" * 80

    async def say(_text):
        session._dtmf_guard_until = time.monotonic() + 10
        agent._on_audio_out(agent.reply_pcm)  # Dropped by the ordinary guard.
        session._outgoing_audio.put(tone)
        session._record.write_downlink(tone)  # The existing DTMF recording owner.
        session._dtmf_guard_until = 0
        agent._on_audio_out(agent.reply_pcm)

    original_write = bridge.write_modem_chunks

    def write(chunks):
        for chunk in chunks:
            original_write([chunk])
            if chunk is tone:
                assert not any('"type": "first_audio"' in line for line in session._record._event_lines)

    monkeypatch.setattr(agent, "say", say)
    monkeypatch.setattr(bridge, "write_modem_chunks", write)
    asyncio.run(session._handle_call())
    expected = tone + bridge.agent_to_modem(agent.reply_pcm, agent.output_rate)
    assert b"".join(bridge.downlink) == expected
    with wave.open(str(next(base.iterdir()) / "downlink.wav"), "rb") as recording:
        assert recording.readframes(recording.getnframes()) == expected
    assert sum(event["type"] == "first_audio" for event in events(base)) == 1


def test_service_defers_hangup_and_rejects_next_call_until_worker_joins(setup_call, monkeypatch):
    from agentcall.call_agent import CallAgentService

    _session, modem, _bridge, agent, _base = setup_call
    entered = threading.Event()
    release = threading.Event()
    order = []
    service = CallAgentService("unused", "unused", provider="openai", modem=modem)

    def pcm(_mode):
        entered.set()
        assert release.wait(timeout=2)
        order.append("pcm_finished")

    monkeypatch.setattr(modem, "initialize_for_voice", pcm)
    monkeypatch.setattr(modem, "hangup", lambda: order.append("hangup"))
    service.session.start()
    assert entered.wait(timeout=1)
    old_thread = service.session._thread
    try:
        modem.trigger_hangup()
        assert service.line_busy()
        modem.trigger_ring("different-caller")
        service.session.start(outbound_number="10086")
        assert service.session._thread is old_thread
        assert order == []  # The callback must not race the live PCM operation.
    finally:
        release.set()
        old_thread.join(timeout=3)
    assert not old_thread.is_alive()
    assert order == ["pcm_finished", "hangup"]
    assert not service.line_busy()
    assert agent.stopped


def test_bridge_plays_while_greeting_request_is_still_streaming(setup_call, monkeypatch):
    session, _modem, bridge, agent, _base = setup_call

    async def run():
        played = asyncio.Event()
        greeting_done = asyncio.Event()
        original_write = bridge.write_modem_chunks

        def write(chunks):
            original_write(chunks)
            played.set()

        async def say(_text):
            agent._on_audio_out(agent.reply_pcm)
            await asyncio.wait_for(played.wait(), timeout=1)
            greeting_done.set()

        async def run_loop(_agent, active_bridge, _record, _transcripts):
            session._drain_agent_audio(active_bridge)
            await greeting_done.wait()
            session._set_active(False)
            return active_bridge

        monkeypatch.setattr(bridge, "write_modem_chunks", write)
        monkeypatch.setattr(agent, "say", say)
        monkeypatch.setattr(session, "_run_agent_loop", run_loop)
        await asyncio.wait_for(session._handle_call(), timeout=2)

    asyncio.run(run())
    assert len(bridge.downlink) == 1


@pytest.mark.parametrize("mode", ["nmea", "uac", "uac_ffmpeg"])
def test_early_audio_has_same_gain_as_ready_bridge(setup_call, mode):
    session, _modem, bridge, agent, _base = setup_call
    session.audio_mode = mode
    session.tx_gain = 2
    if mode != "uac":
        bridge.amplify_for_modem = lambda pcm: apply_pcm_gain(pcm, 2)
    current = None
    handler = session._make_agent_audio_handler(
        agent, None, None, bridge_getter=lambda: current, record_at_playout=True,
    )
    handler(agent.reply_pcm)
    early = session._outgoing_audio.get_nowait()
    current = bridge
    handler(agent.reply_pcm)
    ready = session._outgoing_audio.get_nowait()
    assert early == ready
    expected = bridge.agent_to_modem(agent.reply_pcm, agent.output_rate)
    assert early == apply_pcm_gain(expected, 1 if mode == "uac" else 2)


@pytest.mark.parametrize("cut", ["barge_in", "late_cut"])
def test_prebridge_audio_uses_existing_interrupt_queue(setup_call, monkeypatch, cut):
    session, _modem, _bridge, agent, _base = setup_call
    monkeypatch.setenv("BARGE_IN_ENABLED", "true")

    async def say(_text):
        agent._on_audio_out(agent.reply_pcm)
        assert not session._outgoing_audio.empty()
        if cut == "barge_in":
            assert agent._emit_user_interrupt()
        else:
            assert agent._emit_late_cut()
        assert session._outgoing_audio.empty()

    monkeypatch.setattr(agent, "say", say)
    asyncio.run(session._handle_call())


@pytest.mark.parametrize("barge_in", [False, True])
def test_last_successful_playout_preserves_half_duplex_hangover(setup_call, monkeypatch, barge_in):
    session, _modem, bridge, agent, _base = setup_call
    session._parallel_agent_connect = True
    session._last_downlink_play_at = time.monotonic()
    session._hangover_seconds = 1
    session._barge_in = barge_in

    def read():
        session._set_active(False)
        return b"\x00\x10" * 160

    monkeypatch.setattr(bridge, "read_modem_chunk", read)
    asyncio.run(CallSession._run_agent_loop(session, agent, bridge, None, []))
    assert bool(agent.received_audio) is barge_in


def test_hangup_cancels_main_loop_blocked_in_response_admission(setup_call, monkeypatch):
    session, modem, bridge, agent, _base = setup_call
    entered = asyncio.Event()

    async def blocked_loop(_agent, active_bridge, _record, _transcripts):
        entered.set()
        await asyncio.Event().wait()  # e.g. a say() queued behind a provider response.
        return active_bridge

    monkeypatch.setattr(session, "_run_agent_loop", blocked_loop)

    async def run():
        task = asyncio.create_task(session._handle_call())
        await asyncio.wait_for(entered.wait(), 1)
        session.stop()
        await asyncio.wait_for(task, 0.4)

    asyncio.run(run())
    assert agent.stopped and bridge.stopped
    assert "hangup" in modem.call_names()


def test_parallel_stop_does_not_submit_a_redundant_shutdown_coroutine(setup_call, monkeypatch):
    session, _modem, _bridge, _agent, _base = setup_call
    session._parallel_agent_connect = True

    def unexpected(coroutine, _loop):
        coroutine.close()
        pytest.fail("Parallel owner polls the stop flag; no redundant coroutine may escape loop shutdown")

    monkeypatch.setattr(asyncio, "run_coroutine_threadsafe", unexpected)

    async def run():
        session._loop = asyncio.get_running_loop()
        session.stop()
        assert not session.is_active

    asyncio.run(run())


@pytest.mark.parametrize("dtmf_guard", [False, True])
def test_stalled_playout_cannot_permanently_suppress_uplink(setup_call, monkeypatch, dtmf_guard):
    session, _modem, bridge, agent, _base = setup_call
    session._parallel_agent_connect = True
    session._hangover_seconds = 0.01
    monkeypatch.setattr("agentcall.call_agent._PARALLEL_PLAYOUT_STALL_SECONDS", 0.03, raising=False)
    dropped = []
    started = time.monotonic()
    if dtmf_guard:
        session._dtmf_guard_until = started + 1

    def discard():
        dropped.append(True)
        return 320

    def read():
        if time.monotonic() - started > 0.15:
            session._set_active(False)
        return b"\x00\x10" * 160

    monkeypatch.setattr(bridge, "pending_output_bytes", lambda: 0 if dropped else 320)
    monkeypatch.setattr(bridge, "discard_pending_output", discard)
    monkeypatch.setattr(bridge, "read_modem_chunk", read)
    asyncio.run(CallSession._run_agent_loop(session, agent, bridge, None, []))
    assert bool(dropped) is not dtmf_guard
    assert bool(agent.received_audio) is not dtmf_guard


def test_uplink_gate_diagnoses_ownership_without_bypassing_it(setup_call, monkeypatch, caplog):
    import logging

    session, _modem, bridge, agent, _base = setup_call
    session._parallel_agent_connect = True
    session._triage_terminal = True
    session._hangover_seconds = 0

    def read():
        session._set_active(False)
        return b"\x00\x10" * 160

    monkeypatch.setattr(bridge, "read_modem_chunk", read)
    with caplog.at_level(logging.INFO):
        asyncio.run(CallSession._run_agent_loop(session, agent, bridge, None, []))
    assert not agent.received_audio
    assert "ownership" in caplog.text and "triage_terminal" in caplog.text


def test_stall_recovery_rechecks_dtmf_after_acquiring_media_lock(setup_call, monkeypatch):
    session, _modem, bridge, agent, _base = setup_call
    session._parallel_agent_connect = True
    monkeypatch.setattr("agentcall.call_agent._PARALLEL_PLAYOUT_STALL_SECONDS", 0)
    monkeypatch.setattr(bridge, "pending_output_bytes", lambda: 320)
    dropped = []
    monkeypatch.setattr(bridge, "discard_pending_output", lambda: dropped.append(True) or 320)
    original_discard = session._discard_agent_playback
    saved_lock = session._media_lock

    class RaceLock:
        def __enter__(self):
            session._dtmf_guard_until = time.monotonic() + 1
            session._outgoing_audio.put(b"protected-tone")

        def __exit__(self, *_args):
            pass

    def race(active_bridge, reason):
        session._media_lock = RaceLock()
        try:
            return original_discard(active_bridge, reason)
        finally:
            session._media_lock = saved_lock

    def read():
        session._set_active(False)
        return b"\x00\x10" * 160

    monkeypatch.setattr(session, "_discard_agent_playback", race)
    monkeypatch.setattr(bridge, "read_modem_chunk", read)
    asyncio.run(CallSession._run_agent_loop(session, agent, bridge, None, []))
    assert not dropped
    assert session._outgoing_audio.get_nowait() == b"protected-tone"


def test_parallel_session_cleanup_with_unresponsive_websocket_is_under_three_seconds(setup_call, monkeypatch):
    from agentcall.agents.openai_agent import OpenAIVoiceAgent

    session, modem, bridge, _old_agent, base = setup_call
    agent = OpenAIVoiceAgent("fake-key", "fake-model", "fake-display")
    aborted = []

    class Transport:
        def abort(self):
            aborted.append(True)

    class Socket:
        transport = Transport()

        async def send(self, _data):
            pass

        async def close(self):
            await asyncio.Event().wait()

        def __aiter__(self):
            return self

        async def __anext__(self):
            await asyncio.Event().wait()

    async def connect(*_args, **_kwargs):
        return Socket()

    monkeypatch.setattr("agentcall.agents.openai_agent.websockets.connect", connect)
    monkeypatch.setattr("agentcall.call_agent.create_agent", lambda _provider: agent)
    started = time.monotonic()
    asyncio.run(session._handle_call())
    assert time.monotonic() - started < 3
    assert aborted and bridge.stopped and "hangup" in modem.call_names()
    assert agent._ws is None and agent._recv_task is None and not agent._background_tasks
    stages = [event["stage"] for event in events(base) if event["type"] == "cleanup_stage"]
    assert stages == ["agent_tasks", "agent_stop", "modem_prepare", "bridge_stop", "modem_hangup"]


def test_wait_mode_logs_first_actual_reply_without_greeting_duration(setup_call, monkeypatch):
    session, modem, bridge, agent, base = setup_call
    session._outbound_number = "10086"
    session._prompt_gen_opening_mode = "wait"
    monkeypatch.setattr(session, "_build_agent_instructions", lambda _direction: "test")
    monkeypatch.setattr(modem, "dial", lambda _number: modem.connected_flag.set())

    async def reply(_agent, active_bridge, _record, _transcripts):
        agent._on_audio_out(agent.reply_pcm)
        session._drain_agent_audio(active_bridge)
        session._set_active(False)
        return active_bridge

    monkeypatch.setattr(session, "_run_agent_loop", reply)
    asyncio.run(session._handle_call())
    first = [event for event in events(base) if event["type"] == "first_audio"]
    assert len(first) == 1 and first[0]["boundary"] == "pcm_written"
    assert "ms" not in first[0]  # No greeting was requested; do not fabricate a duration.
    assert bridge.downlink and not agent.said


def test_takeover_fence_makes_pending_greeting_failure_nonfatal(setup_call, monkeypatch):
    session, modem, bridge, agent, _base = setup_call

    async def run():
        detached = asyncio.Event()

        async def say(_text):
            await detached.wait()
            raise RuntimeError("AI socket closed by takeover")

        async def mobile_loop(_agent, active_bridge, _record, _transcripts):
            session._session_generation += 1  # detach_agent fences AI, not the physical call.
            detached.set()
            await asyncio.sleep(0.02)
            assert session.is_active
            assert "hangup" not in modem.call_names()
            session._set_active(False)
            return active_bridge

        monkeypatch.setattr(agent, "say", say)
        monkeypatch.setattr(session, "_run_agent_loop", mobile_loop)
        await session._handle_call()

    asyncio.run(run())
    assert bridge.stopped and agent.stopped


def test_audio_already_written_during_stop_is_still_recorded(setup_call, monkeypatch):
    session, _modem, bridge, agent, base = setup_call

    async def finish(_agent, active_bridge, _record, _transcripts):
        session._set_active(False)
        session._drain_agent_audio(active_bridge)
        return active_bridge

    monkeypatch.setattr(session, "_run_agent_loop", finish)
    asyncio.run(session._handle_call())
    expected = bridge.agent_to_modem(agent.reply_pcm, agent.output_rate)
    assert b"".join(bridge.downlink) == expected
    with wave.open(str(next(base.iterdir()) / "downlink.wav"), "rb") as recording:
        assert recording.readframes(recording.getnframes()) == expected


def test_completed_mobile_loop_result_is_closed_if_owner_cancelled(setup_call, monkeypatch):
    session, _modem, old_bridge, _agent, _base = setup_call
    replacement = FakeAudioBridge()

    async def run():
        owner = None

        async def handoff(_agent, active_bridge, _record, _transcripts):
            active_bridge.stop()
            replacement.start()
            session._session_generation += 1
            owner.cancel()
            return replacement

        monkeypatch.setattr(session, "_run_agent_loop", handoff)
        owner = asyncio.create_task(session._handle_call())
        with pytest.raises(asyncio.CancelledError):
            await owner

    asyncio.run(run())
    assert old_bridge.stopped
    assert replacement.started and replacement.stopped


@pytest.mark.parametrize("briefing_delay", [0.01, 0.12])
def test_moss_outbound_briefing_finishes_before_dial(setup_call, monkeypatch, briefing_delay):
    session, modem, bridge, agent, base = setup_call
    session.provider = "moss"
    session._outbound_number = "10086"
    monkeypatch.setenv("PARALLEL_AGENT_CONNECT", "false")  # MOSS alone requires early startup.
    monkeypatch.setattr(session, "_build_agent_instructions", lambda _direction: "shared scenario and task")
    order = []
    original_dial = modem.dial

    def dial(number):
        assert "briefing_ready" in order
        order.append("dial")
        return original_dial(number)

    monkeypatch.setattr(modem, "dial", dial)
    monkeypatch.setattr(session, "_start_prompt_generation", lambda: order.append("prompt_prepare"))

    async def start(callback):
        order.append("briefing_start")
        assert order.count("prompt_prepare") == 1
        assert "dial" not in modem.call_names()
        assert not modem.is_call_connected()
        assert agent._session_instructions == "shared scenario and task"
        await asyncio.sleep(briefing_delay)
        await FakeAgent.start(agent, callback)
        order.append("briefing_ready")

    async def connected(timeout):
        await asyncio.sleep(0.05)
        modem.connected_flag.set()
        order.append("answered")
        return True

    async def run_loop(_agent, active_bridge, _record, _transcripts):
        assert "briefing_ready" in order
        order.append("phone_loop")
        session._set_active(False)
        return active_bridge

    monkeypatch.setattr(agent, "start", start)
    monkeypatch.setattr(session, "_wait_connected", connected)
    monkeypatch.setattr(session, "_run_agent_loop", run_loop)
    asyncio.run(session._handle_call())
    assert order.index("briefing_ready") < order.index("dial") < order.index("answered") < order.index("phone_loop")
    assert order.count("prompt_prepare") == 1
    assert not agent.said  # No spoken briefing confirmation or synthetic opening in phone.
    wait = next(event["ms"] for event in events(base)
                if event["type"] == "latency" and event["stage"] == "moss_answered_wait")
    assert wait == 0
    prebrief = next(event["ms"] for event in events(base)
                   if event["type"] == "latency" and event["stage"] == "moss_prebrief")
    # asyncio 的定时回调最多提前一个时钟精度触发，计时本身再差一个精度；
    # Windows 单调时钟精度约 15.6ms（曾实测 109 < 119），macOS/Linux 接近 0。
    clock_slack_ms = 2 * time.get_clock_info("monotonic").resolution * 1000
    assert prebrief >= briefing_delay * 1000 - 1 - clock_slack_ms
    metrics = json.loads((next(base.iterdir()) / "metrics.json").read_text())
    assert metrics["latency"]["moss_prebrief_ms"]["n"] == 1
    assert agent.stopped and bridge.stopped


@pytest.mark.parametrize("end", ["failure", "timeout", "stop", "cancel"])
def test_moss_prebrief_failure_or_cancel_never_touches_modem(setup_call, monkeypatch, end):
    session, modem, bridge, agent, base = setup_call
    session.provider = "moss"
    session._outbound_number = "10086"
    monkeypatch.setattr(session, "_build_agent_instructions", lambda _direction: "task")
    monkeypatch.setattr("agentcall.call_agent._MOSS_PREBRIEF_TIMEOUT", 0.05)
    published = []
    monkeypatch.setattr(session, "_publish", published.append)

    async def run():
        entered = asyncio.Event()

        async def start(_callback):
            entered.set()
            if end == "failure":
                raise RuntimeError("fake briefing failure")
            await asyncio.Event().wait()

        monkeypatch.setattr(agent, "start", start)
        task = asyncio.create_task(session._handle_call())
        await asyncio.wait_for(entered.wait(), 1)
        if end == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        elif end == "stop":
            session.stop()
            await asyncio.wait_for(task, 1)
        else:
            with pytest.raises(RuntimeError, match="MOSS"):
                await asyncio.wait_for(task, 1)
        assert not [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]

    asyncio.run(run())
    assert not modem.calls
    assert agent.stopped and not bridge.started and not agent.said
    if end in {"failure", "timeout"}:
        assert any(event.get("status") == "error" for event in published)
        assert any(event["type"] == "moss_prebrief_failed" for event in events(base))


def test_moss_inbound_still_answers_while_briefing_is_pending(setup_call, monkeypatch):
    session, modem, bridge, agent, base = setup_call
    session.provider = "moss"
    original_start = agent.start

    async def start(callback):
        await asyncio.sleep(0.06)
        assert "answer" in modem.call_names()
        await original_start(callback)

    monkeypatch.setattr(agent, "start", start)
    asyncio.run(session._handle_call())
    recorded = events(base)
    assert "dial" not in modem.call_names()
    assert not any(event.get("stage") == "moss_prebrief" for event in recorded)
    assert next(event["ms"] for event in recorded if event.get("stage") == "moss_answered_wait") > 0
    assert agent.stopped and bridge.stopped


@pytest.mark.parametrize("provider", ["local", "moss"])
def test_response_playout_metric_same_tail_and_hardware_boundary(setup_call, monkeypatch, provider):
    session, _modem, bridge, agent, _base = setup_call
    session.provider = provider
    clock = [101.0]
    monkeypatch.setattr("agentcall.call_agent.time.monotonic", lambda: clock[0])
    session._last_voiced_at = 100.0
    session._max_turn_seconds = 0
    bridge.set_output_observer(lambda _pcm: None)
    latencies = []

    class Record:
        def log_latency(self, stage, ms):
            latencies.append((stage, ms))

    handler = session._make_agent_audio_handler(agent, bridge, Record(), record_at_playout=True)
    handler(agent.reply_pcm)
    assert ("local_response", 1000) in latencies
    assert not any(stage == "local_response_playout" for stage, _ms in latencies)
    clock[0] = 102.0
    session._drain_agent_audio(bridge)
    assert latencies.count(("local_response_playout", 2000)) == 1


def test_moss_loopback_call_writes_metrics_and_transcripts(setup_call, monkeypatch):
    from test_moss_agent import action, finish_briefing, moss_server

    from agentcall.agents.moss_agent import MossVoiceAgent

    session, modem, bridge, _fake_agent, base = setup_call
    session.provider = "moss"
    session._outbound_number = "10086"
    session._outbound_task_value = "test balance inquiry"
    monkeypatch.setenv("DTMF_JUDGE_MODE", "off")
    monkeypatch.setenv("OWNER_NAME", "Test Owner")
    monkeypatch.setenv("BARGE_IN_ENABLED", "false")
    monkeypatch.setenv("WRAP_UP_JUDGE_GRACE_SECONDS", "9999")
    # Restore the real phone loop; only modem, bridge, TTS and remote server are fake.
    monkeypatch.setattr(session, "_run_agent_loop", CallSession._run_agent_loop.__get__(session))

    async def synthesize(text):
        assert text.startswith("接下来你要替我给10086打电话test balance in。")
        assert "Test Owner" not in text and "test balance inquiry" not in text
        return b"\x01\x02" * 320

    monkeypatch.setattr("agentcall.agents.moss_agent.synthesize_briefing", synthesize)

    async def run():
        async with moss_server() as (url, connected, received):
            agent = MossVoiceAgent("fake-moss-key", url)

            def dial(number):
                assert agent._ready and agent._brief_done.done()
                modem.calls.append(("dial", (number,)))
                modem.connected_flag.set()

            monkeypatch.setattr(modem, "dial", dial)
            monkeypatch.setattr("agentcall.call_agent.create_agent", lambda _provider: agent)
            call = asyncio.create_task(session._handle_call())
            ws = await connected
            try:
                update = await action(received, "session.update")
                instructions = update["session"]["instructions"]
                assert instructions == update["session"]["persona"]["prompt"]
                assert "Test Owner" in instructions and "test balance inquiry" in instructions
                assert not modem.calls and not bridge.started
                # start() is owned by the CallSession, so wait on readiness separately.
                async def ready():
                    while not agent._ready:
                        await asyncio.sleep(0.001)
                await finish_briefing(ws, received, asyncio.create_task(ready()))
                bridge.feed_uplink(b"\x00\x10" * 160)
                async with asyncio.timeout(2):
                    while session._last_voiced_at == 0:
                        await asyncio.sleep(0.001)
                # 真实通话的回复至少几百 ms；假服务端同一时钟格内就回，Windows 单调时钟
                # 约 15.6ms 才走一格，量出 0 会被延迟指标的 0 < elapsed 护栏丢弃。
                await asyncio.sleep(0.05)
                await ws.send_json({"action": "input.speech.stopped", "data": {}})
                await ws.send_json({"action": "input.transcript.done", "data": {"text": "test question"}})
                context = {"chunk_id": "c", "run_id": "r", "turn_id": "t"}
                await ws.send_json({"action": "response.audio.context", "data": context})
                await ws.send_bytes(b"\x10\x20" * 960)
                await ws.send_json({"action": "output.transcript.done", "data": {"text": "test answer"}})
                await ws.send_json({"action": "response.audio.done", "data": {}})
                await action(received, "playback.chunk.completed")
                await action(received, "playback.completed")
                session.stop()
                await asyncio.wait_for(call, 2)
            finally:
                call.cancel()
                await asyncio.gather(call, return_exceptions=True)

    asyncio.run(run())
    metrics = json.loads((next(base.iterdir()) / "metrics.json").read_text())
    for name in ("local_response_latency_ms", "local_response_playout_ms", "moss_input_stop_to_audio_ms",
                 "moss_briefing_ms", "moss_answered_wait_ms", "moss_prebrief_ms"):
        assert metrics["latency"][name]["n"] == 1
    assert metrics["answered_to_first_playout_ms"] >= 0
    assert metrics["latency"]["moss_answered_wait_ms"]["median"] == 0
    assert session._barge_in  # MOSS remains full duplex even with the generic switch off.
    transcript_events = [event for event in events(base) if event["type"] == "transcript"]
    assert [event["text"] for event in transcript_events] == ["test question", "test answer"]
    assert b"".join(bridge.downlink) == b"\x10\x20" * 160


@pytest.mark.parametrize("failure", ["update", "response", "timeout"])
def test_moss_loopback_prebrief_failure_never_dials(setup_call, monkeypatch, failure):
    from test_moss_agent import moss_server, next_matching

    from agentcall.agents.moss_agent import MossVoiceAgent

    session, modem, bridge, _agent, base = setup_call
    session.provider = "moss"
    session._outbound_number = "10086"
    monkeypatch.setattr("agentcall.call_agent._MOSS_PREBRIEF_TIMEOUT", 0.15)

    async def synthesize(_text):
        return b"\x01\x02" * 320

    monkeypatch.setattr("agentcall.agents.moss_agent.synthesize_briefing", synthesize)

    async def run():
        async with moss_server(update_error="rejected" if failure == "update" else None) as (url, connected, received):
            agent = MossVoiceAgent("fake-moss-key", url)
            monkeypatch.setattr("agentcall.call_agent.create_agent", lambda _provider: agent)
            call = asyncio.create_task(session._handle_call())
            try:
                ws = await asyncio.wait_for(connected, 1)
                if failure == "response":
                    await next_matching(received, lambda value: isinstance(value, bytes) and any(value))
                    await ws.send_json({"action": "response.ended", "data": {"status": "failed"}})
                with pytest.raises(RuntimeError, match="MOSS"):
                    await asyncio.wait_for(call, 1)
                assert not agent._tasks and agent._ws is None
            finally:
                call.cancel()
                await asyncio.gather(call, return_exceptions=True)

    asyncio.run(run())
    assert not modem.calls and not bridge.started
    recorded = events(base)
    assert any(event["type"] == "moss_prebrief_failed" for event in recorded)
    assert next(event for event in recorded if event["type"] == "ended")["status"] == "failed"
