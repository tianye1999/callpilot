"""B4：固定话术直接合成，不经 LLM（一字不差、少一次模型往返）。"""

from __future__ import annotations

import asyncio
import time

from fakes import FakeAgent, FakeModem

from agentcall import call_agent
from agentcall.agents.local_agent import LocalPipelineAgent


class Pipeline:
    sample_rate = 16000

    def __init__(self) -> None:
        self.synthesized: list[str] = []

    def vad_push(self, pcm16: bytes) -> list[bytes]:
        return []

    def vad_flush(self) -> list[bytes]:
        return []

    def is_speech_detected(self) -> bool:
        return False

    def transcribe(self, segment_pcm16: bytes) -> str:
        return ""

    def synthesize(self, text: str) -> bytes:
        self.synthesized.append(text)
        return b"\x01\x02" * 160


def _wait(cond, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.02)
    return cond()


def test_say_verbatim_speaks_the_exact_text_without_calling_the_llm(monkeypatch):
    for key, value in {"LOCAL_ASR_SEGMENTED": "true", "LOCAL_SPECULATIVE_LLM": "false",
                       "LOCAL_TTS_STREAMING": "false", "LOCAL_SLOW_REPLY_FILLER_SECONDS": "0",
                       "LOCAL_LLM_PREWARM": "false", "LOCAL_FAST_OPENING": "false"}.items():
        monkeypatch.setenv(key, value)
    pipeline = Pipeline()
    calls: list[list[dict]] = []

    def llm(messages, tools, timeout):
        calls.append(messages)
        return {"role": "assistant", "content": "不该出现"}

    agent = LocalPipelineAgent(pipeline_factory=lambda: pipeline, llm_chat=llm)
    hold = call_agent._INBOUND_TAKEOVER_HOLD_TEXT["zh"]
    agent.preload_phrases([hold])
    transcripts: list[tuple[str, str]] = []
    agent.set_transcript_handler(lambda role, text: transcripts.append((role, text)))
    written: list[bytes] = []
    asyncio.run(agent.start(written.append))
    try:
        # Pre-synthesized while the models loaded: speaking it needs no synthesis.
        assert pipeline.synthesized.count(hold) == 1
        asyncio.run(agent.say_verbatim(hold))
        assert _wait(lambda: len(written) == 1)
        assert calls == []
        assert pipeline.synthesized.count(hold) == 1
        assert transcripts[-1] == ("agent", hold)
        with agent._messages_lock:
            assert agent._messages[-1] == {"role": "assistant", "content": hold}
    finally:
        asyncio.run(agent.stop())


def test_realtime_providers_fall_back_to_an_exact_wording_instruction():
    agent = FakeAgent()
    asyncio.run(agent.say_verbatim("谢谢您的来电，目前不需要这项服务。再见。"))
    assert "一字不差" in agent.said[-1] and "谢谢您的来电，目前不需要这项服务。再见。" in agent.said[-1]


def test_inbound_session_preloads_the_fixed_phrases(monkeypatch):
    monkeypatch.setenv("AGENT_LANGUAGE", "zh")
    session = call_agent.CallAgentService(
        modem_port="unused", audio_keyword="unused", provider="openai", modem=FakeModem(),
    ).session
    registered: list[list[str]] = []

    class Recorder(FakeAgent):
        def preload_phrases(self, phrases):
            registered.append(list(phrases))

    agent = Recorder()
    session._configure_call_agent(agent, lambda: None, None, [], "inbound", None, time.monotonic())
    assert registered and call_agent._INBOUND_TAKEOVER_HOLD_TEXT["zh"] in registered[0]
    assert call_agent._INBOUND_TRIAGE_REJECT_TEXT["zh"] in registered[0]
    registered.clear()
    session._configure_call_agent(agent, lambda: None, None, [], "outbound", "10086", time.monotonic())
    assert registered == []


def test_hold_text_does_not_promise_a_connection():
    for text in call_agent._INBOUND_TAKEOVER_HOLD_TEXT.values():
        assert "一定" not in text and "马上接通" not in text and "will connect" not in text.lower()


def test_verbatim_replaces_an_in_flight_reply_instead_of_interleaving(monkeypatch):
    import threading

    for key, value in {"LOCAL_ASR_SEGMENTED": "true", "LOCAL_SPECULATIVE_LLM": "false",
                       "LOCAL_TTS_STREAMING": "true", "LOCAL_SLOW_REPLY_FILLER_SECONDS": "0",
                       "LOCAL_LLM_PREWARM": "false", "LOCAL_FAST_OPENING": "false"}.items():
        monkeypatch.setenv(key, value)
    synth_gate = threading.Event()

    class SlowPipeline(Pipeline):
        def synthesize(self, text: str) -> bytes:
            if text.startswith("旧"):
                synth_gate.wait(3.0)  # the old reply is still being synthesized
            return super().synthesize(text)

    pipeline = SlowPipeline()
    agent = LocalPipelineAgent(pipeline_factory=lambda: pipeline,
                               llm_chat=lambda m, t, to: {"role": "assistant", "content": "旧回复。"})
    hold = call_agent._INBOUND_TAKEOVER_HOLD_TEXT["zh"]
    agent.preload_phrases([hold])
    written: list[bytes] = []
    asyncio.run(agent.start(written.append))
    try:
        agent._handle_user_text("是送外卖的", agent._new_turn(time.monotonic()))  # brain-free direct call
        time.sleep(0.2)  # the old clause is now blocked inside synthesis
        asyncio.run(agent.say_verbatim(hold))
        assert _wait(lambda: len(written) == 1)
        synth_gate.set()
        time.sleep(0.4)
        assert len(written) == 1  # the old reply never reaches the line after the fixed phrase
    finally:
        synth_gate.set()
        asyncio.run(agent.stop())


def test_verbatim_clears_a_withheld_forced_reply(monkeypatch):
    for key, value in {"LOCAL_ASR_SEGMENTED": "true", "LOCAL_SPECULATIVE_LLM": "false",
                       "LOCAL_TTS_STREAMING": "false", "LOCAL_SLOW_REPLY_FILLER_SECONDS": "0",
                       "LOCAL_LLM_PREWARM": "false", "LOCAL_FAST_OPENING": "false"}.items():
        monkeypatch.setenv(key, value)
    agent = LocalPipelineAgent(pipeline_factory=Pipeline,
                               llm_chat=lambda m, t, to: {"role": "assistant", "content": "x"})
    agent._running = True
    agent._withheld_reply = True
    agent._handle_say_verbatim("好的。")
    assert agent._withheld_reply is False


def test_hold_line_waits_for_confirmed_playout_not_generation(monkeypatch):
    """整句缓存音频一次到达：桥确认写完之前，等待语不算说完。"""
    from fakes import FakeAudioBridge

    monkeypatch.setenv("INBOUND_TAKEOVER_ENABLED", "true")
    session = call_agent.CallAgentService(
        modem_port="unused", audio_keyword="unused", provider="openai", modem=FakeModem(),
    ).session
    session._active = True
    session._session_generation = 7
    session._initialize_takeover_context("inbound")
    session._takeover_endpoint_rejection = lambda: None
    assert session._request_owner_takeover(7)["success"] is True

    class PacedBridge(FakeAudioBridge):
        """Queues chunks and confirms them only when the test plays them out."""

        def __init__(self) -> None:
            super().__init__()
            self.queued: list[bytes] = []

        def write_modem_chunks(self, chunks):
            for chunk in chunks:
                if chunk:
                    self.queued.append(chunk)

        def play_all(self):
            for chunk in self.queued:
                self.downlink.append(chunk)
                if self._output_observer is not None and hasattr(chunk, "written_callback"):
                    chunk.written_callback(len(chunk))()
            self.queued.clear()

    bridge = PacedBridge()
    bridge.set_output_observer(lambda pcm: None)

    class Speaker(FakeAgent):
        async def say_verbatim(self, text: str) -> None:
            self.said.append(text)
            self._on_audio_out(b"\x00\x10" * 4000)  # a whole sentence at once

    agent = Speaker()
    agent._on_audio_out = session._make_agent_audio_handler(agent, bridge, None)

    async def scenario():
        hold = asyncio.create_task(session._speak_takeover_hold_if_needed(agent, bridge, 7))
        await asyncio.sleep(0.6)  # well past the 0.35 s quiet gap
        assert not hold.done(), "generated but not yet played: the hold line is still going"
        bridge.play_all()
        await asyncio.wait_for(hold, timeout=2.0)

    asyncio.run(scenario())
    assert session._takeover_hold_done is True
    assert session._takeover_hold_played >= session._takeover_hold_queued > 0


def _quiet_env(monkeypatch, streaming="false"):
    for key, value in {"LOCAL_ASR_SEGMENTED": "true", "LOCAL_SPECULATIVE_LLM": "false",
                       "LOCAL_TTS_STREAMING": streaming, "LOCAL_SLOW_REPLY_FILLER_SECONDS": "0",
                       "LOCAL_LLM_PREWARM": "false", "LOCAL_FAST_OPENING": "false"}.items():
        monkeypatch.setenv(key, value)


def test_queued_verbatim_is_dropped_when_superseded(monkeypatch):
    _quiet_env(monkeypatch)
    # Deterministic version without the worker: enqueue, supersede, then consume.
    agent2 = LocalPipelineAgent(pipeline_factory=Pipeline,
                                llm_chat=lambda m, t, to: {"role": "assistant", "content": "x"})
    agent2._pipeline = Pipeline()
    agent2._running = True
    out: list[bytes] = []
    agent2._on_audio_out = out.append
    asyncio.run(agent2.say_verbatim("第一句。"))
    first = agent2._brain_queue.get_nowait()
    asyncio.run(agent2.say_verbatim("第二句。"))  # a newer fixed phrase supersedes the first
    second = agent2._brain_queue.get_nowait()
    agent2._handle_say_verbatim(first.instructions, first.turn)
    agent2._handle_say_verbatim(second.instructions, second.turn)
    assert len(out) == 1
    with agent2._messages_lock:
        assert [m["content"] for m in agent2._messages if m.get("role") == "assistant"] == ["第二句。"]
    asyncio.run(agent2.say_verbatim("第三句。"))
    third = agent2._brain_queue.get_nowait()
    agent2._cancel_turn()  # the caller starts talking before it is spoken
    agent2._handle_say_verbatim(third.instructions, third.turn)
    assert len(out) == 1
    agent2._running = False


def test_cancelled_tool_batch_keeps_every_call_paired():
    agent = LocalPipelineAgent(pipeline_factory=Pipeline,
                               llm_chat=lambda m, t, to: {"role": "assistant", "content": "x"})
    agent._running = True
    turn = agent._new_turn(time.monotonic())
    calls = [{"id": "a", "type": "function", "function": {"name": "first", "arguments": "{}"}},
             {"id": "b", "type": "function", "function": {"name": "second", "arguments": "{}"}}]

    class Tools:
        def dispatch(self, name, args):
            turn.cancelled.set()  # cancelled while the first tool runs
            return {"success": True}

    agent._tools = Tools()
    with agent._messages_lock:
        agent._messages = [{"role": "assistant", "content": None, "tool_calls": calls}]
    assert agent._run_tool_calls(calls, turn) is True
    with agent._messages_lock:
        results = {m["tool_call_id"]: m["content"] for m in agent._messages if m.get("role") == "tool"}
    assert set(results) == {"a", "b"}
    assert "CANCELLED" in results["b"] and "CANCELLED" not in results["a"]


def test_stale_verbatim_does_not_clear_a_newer_withheld_reply():
    agent = LocalPipelineAgent(pipeline_factory=Pipeline,
                               llm_chat=lambda m, t, to: {"role": "assistant", "content": "x"})
    agent._pipeline = Pipeline()
    agent._running = True
    agent._on_audio_out = lambda pcm: None
    asyncio.run(agent.say_verbatim("请稍等。"))
    stale = agent._brain_queue.get_nowait()
    agent._cancel_turn()  # new speech supersedes the queued fixed phrase
    agent._withheld_reply = True  # a newer forced turn withheld its reply meanwhile
    agent._handle_say_verbatim(stale.instructions, stale.turn)
    assert agent._withheld_reply is True
    agent._running = False
