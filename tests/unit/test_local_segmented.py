"""Segmented offline ASR uses fake VAD frames and event-controlled recognizers."""

from __future__ import annotations

import asyncio
import sys
import threading
from types import SimpleNamespace

import pytest
from test_local_agent import FakePipeline, _wait_until

from agentcall.agents import local_agent


class SegmentedPipeline(FakePipeline):
    speaking = False

    def __init__(self):
        super().__init__()
        self.asr_threads = []

    def vad_push(self, pcm):
        code = pcm[0]
        self.speaking = code == 1
        return [b"first"] if code == 2 else [b"second"] if code == 3 else []

    def is_speech_detected(self):
        return self.speaking

    def transcribe(self, segment):
        self.asr_threads.append(threading.current_thread().name)
        self.transcripts.append(segment)
        return {b"first": "余额八十四", b"second": "点三一元"}[segment]


def feed(agent, code, seconds=0.1):
    agent._process_audio(bytes([code, 0]) * round(16000 * seconds))


def end_turn(agent):
    # Silero already observed 0.25s before delivering the final segment.
    feed(agent, 0, 0.35)


@pytest.fixture(autouse=True)
def segmented(monkeypatch):
    monkeypatch.setenv("LOCAL_ASR_SEGMENTED", "true")
    monkeypatch.setenv("LOCAL_ASR_SEGMENT_SILENCE", "0.25")
    monkeypatch.setenv("LOCAL_TURN_SILENCE", "0.6")


def test_segments_are_recognized_early_and_submitted_once_in_order():
    pipeline, requests = SegmentedPipeline(), []
    agent = local_agent.LocalPipelineAgent(
        pipeline_factory=lambda: pipeline,
        llm_chat=lambda messages, *a: requests.append(messages) or {"role": "assistant", "content": ""},
    )
    asyncio.run(agent.start(lambda pcm: None))
    try:
        feed(agent, 1)
        feed(agent, 2)
        assert _wait_until(lambda: pipeline.transcripts == [b"first"])
        assert requests == []
        feed(agent, 1)
        feed(agent, 3)
        end_turn(agent)
        assert _wait_until(lambda: len(requests) == 1)
        assert requests[0][-1] == {"role": "user", "content": "余额八十四点三一元"}
        assert pipeline.transcripts == [b"first", b"second"]
        assert pipeline.asr_threads == ["local-asr", "local-asr"]
        assert agent._asr_thread.is_alive()
    finally:
        asyncio.run(agent.stop())
    assert agent._asr_thread is None


def test_turn_silence_counts_pcm_and_resets_when_speech_resumes():
    agent = local_agent.LocalPipelineAgent(pipeline_factory=SegmentedPipeline, llm_chat=lambda *a: {})
    agent._pipeline = SegmentedPipeline()
    agent._running = True
    try:
        feed(agent, 0, 2)  # Silence before speech must not create empty turns.
        assert agent._asr_queue.empty()
        feed(agent, 2)
        assert agent._silence_samples == 4000
        feed(agent, 0, 0.34)
        assert agent._turn is None
        feed(agent, 1, 3)  # Neither speech nor elapsed wall time counts as silence.
        assert agent._silence_samples == 0
        feed(agent, 0, 0.34)
        assert agent._turn is None
        feed(agent, 3)
        feed(agent, 0, 0.34)
        assert agent._turn is None
        feed(agent, 0, 0.01)
        assert agent._turn is not None
        assert agent._asr_queue.qsize() == 3  # Two segments, one end-of-turn barrier.
        feed(agent, 0, 2)
        assert agent._asr_queue.qsize() == 3
    finally:
        agent._running = False


def test_default_turn_ends_after_035_seconds_following_segment():
    agent = local_agent.LocalPipelineAgent(pipeline_factory=SegmentedPipeline, llm_chat=lambda *a: {})
    agent._pipeline = SegmentedPipeline()
    agent._running = True
    try:
        feed(agent, 2)
        feed(agent, 0, 0.34)
        assert agent._turn is None
        assert agent._silence_samples == 9440  # 0.25 + 0.34 seconds at 16kHz.
        feed(agent, 0, 0.01)
        assert agent._turn is not None
        assert agent._asr_queue.qsize() == 2  # Segment, then the endpoint barrier.
    finally:
        agent._running = False


@pytest.mark.parametrize("turn_silence", ["0.25", "0.2", "0", "-0.1"])
def test_turn_at_or_below_segment_silence_warns_once_and_ends_on_delivery(monkeypatch, caplog, turn_silence):
    monkeypatch.setenv("LOCAL_TURN_SILENCE", turn_silence)
    requests = []
    agent = local_agent.LocalPipelineAgent(
        pipeline_factory=SegmentedPipeline,
        llm_chat=lambda m, *a: requests.append(m) or {"role": "assistant", "content": ""},
    )

    def warnings():
        return [r for r in caplog.records if r.name == local_agent.__name__ and "LOCAL_TURN_SILENCE" in r.message]

    assert not warnings()
    asyncio.run(agent.start(lambda pcm: None))
    try:
        assert len(warnings()) == 1 and warnings()[0].levelname == "WARNING"
        assert "LOCAL_ASR_SEGMENT_SILENCE" in warnings()[0].message
        for count, code in enumerate((2, 3), start=1):
            feed(agent, code)
            assert agent._turn is not None
            assert _wait_until(lambda: len(requests) == count)
        assert not agent.fatal
        assert len(warnings()) == 1  # No per-segment warning spam.
    finally:
        asyncio.run(agent.stop())


def test_turn_waits_for_last_asr_and_measures_only_tail(monkeypatch):
    entered, release, replied = threading.Event(), threading.Event(), threading.Event()
    clock = [10.0]
    monkeypatch.setattr(local_agent, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setenv("LOCAL_TTS_STREAMING", "true")

    class Pipeline(SegmentedPipeline):
        def transcribe(self, segment):
            if segment == b"second":
                entered.set()
                assert release.wait(1)
            return super().transcribe(segment)

        def synthesize(self, text):
            clock[0] = 20.2
            return b"audio"

    def stream(messages, tools, timeout, on_content):
        clock[0] = 20.15
        on_content("好的。")
        return {"role": "assistant", "content": "好的。"}

    pipeline, stages = Pipeline(), []
    agent = local_agent.LocalPipelineAgent(pipeline_factory=lambda: pipeline, llm_stream=stream)
    agent.set_latency_handler(lambda stage, ms: stages.append((stage, ms)))
    asyncio.run(agent.start(lambda pcm: replied.set()))
    try:
        feed(agent, 2)
        assert _wait_until(lambda: pipeline.transcripts == [b"first"])
        clock[0] = 19.0
        feed(agent, 3)
        assert entered.wait(1)
        clock[0] = 20.0
        end_turn(agent)
        assert agent._turn.ended_at == 20.0
        assert not replied.is_set() and len(agent._messages) == 1
        clock[0] = 20.12
        release.set()
        assert replied.wait(1)
        assert dict(stages) == {
            "asr_tail": 120.0, "llm_first_token": 150.0,
            "first_clause_ready": 150.0, "tts_first_audio": 200.0,
        }
    finally:
        release.set()
        asyncio.run(agent.stop())


def test_asr_completed_before_endpoint_has_zero_tail(monkeypatch):
    pipeline, stages = SegmentedPipeline(), []
    agent = local_agent.LocalPipelineAgent(pipeline_factory=lambda: pipeline, llm_chat=lambda *a: {})
    agent.set_latency_handler(lambda s, ms: stages.append((s, ms)))
    asyncio.run(agent.start(lambda pcm: None))
    try:
        feed(agent, 2)
        assert _wait_until(lambda: agent._asr_queue.unfinished_tasks == 0)
        end_turn(agent)
        assert _wait_until(lambda: stages)
        assert stages[0] == ("asr_tail", 0.0)
    finally:
        asyncio.run(agent.stop())


def test_barge_in_cancels_response_but_keeps_unsubmitted_segments(monkeypatch):
    monkeypatch.setenv("LOCAL_TTS_STREAMING", "true")
    pipeline, requests, interrupts = SegmentedPipeline(), [], []
    agent = local_agent.LocalPipelineAgent(
        pipeline_factory=lambda: pipeline,
        llm_chat=lambda m, *a: requests.append(m) or {"role": "assistant", "content": ""},
    )
    agent.set_user_interrupt_handler(lambda: interrupts.append(True) or True)
    asyncio.run(agent.start(lambda pcm: None))
    try:
        old = agent._new_turn(None)
        feed(agent, 1)
        assert old.cancelled.is_set()
        feed(agent, 2)
        assert _wait_until(lambda: pipeline.transcripts)
        feed(agent, 0, 0.3)
        feed(agent, 1)
        feed(agent, 3)
        assert requests == []
        end_turn(agent)
        assert _wait_until(lambda: requests)
        assert requests[0][-1]["content"] == "余额八十四点三一元"
        assert len(interrupts) == 2
    finally:
        asyncio.run(agent.stop())


def test_busy_brain_merges_completed_text_turns():
    entered, release = threading.Event(), threading.Event()
    requests = []

    def chat(messages, *args):
        requests.append(messages)
        if len(requests) == 1:
            entered.set()
            assert release.wait(1)
        return {"role": "assistant", "content": ""}

    agent = local_agent.LocalPipelineAgent(pipeline_factory=SegmentedPipeline, llm_chat=chat)
    asyncio.run(agent.start(lambda pcm: None))
    try:
        asyncio.run(agent.say("hold brain"))
        assert entered.wait(1)
        feed(agent, 2)
        end_turn(agent)
        feed(agent, 3)
        end_turn(agent)
        assert _wait_until(lambda: agent._asr_queue.unfinished_tasks == 0)
        assert agent._brain_queue.qsize() == 2
        release.set()
        assert _wait_until(lambda: len(requests) == 2)
        assert requests[1][-1]["content"] == "余额八十四点三一元"
    finally:
        release.set()
        asyncio.run(agent.stop())


def test_disabled_keeps_whole_segment_on_brain_thread(monkeypatch):
    monkeypatch.setenv("LOCAL_ASR_SEGMENTED", "false")
    monkeypatch.setenv("LOCAL_TURN_SILENCE", "invalid-unused-setting")
    pipeline, replied, stages = SegmentedPipeline(), threading.Event(), []
    agent = local_agent.LocalPipelineAgent(
        pipeline_factory=lambda: pipeline,
        llm_chat=lambda *a: replied.set() or {"role": "assistant", "content": ""},
    )
    agent.set_latency_handler(lambda s, ms: stages.append(s))
    asyncio.run(agent.start(lambda pcm: None))
    try:
        feed(agent, 2)
        assert replied.wait(1)  # No extra silence/end marker is needed.
        assert pipeline.asr_threads == ["local-brain"]
        assert agent._asr_thread is None
        assert "asr_tail" not in stages
    finally:
        asyncio.run(agent.stop())


def test_real_vad_worker_delivers_segments_to_asr_worker():
    pipeline, replied = SegmentedPipeline(), threading.Event()
    agent = local_agent.LocalPipelineAgent(
        pipeline_factory=lambda: pipeline,
        llm_chat=lambda *a: replied.set() or {"role": "assistant", "content": ""},
    )
    asyncio.run(agent.start(lambda pcm: None))
    try:
        async def send():
            for code in [1, 2, 0, 1, 3, 0, 0, 0, 0, 0, 0]:
                await agent.send_audio(bytes([code, 0]) * 1600)

        asyncio.run(send())
        assert replied.wait(1)
        assert [m["content"] for m in agent._messages if m["role"] == "user"] == ["余额八十四点三一元"]
        assert pipeline.asr_threads == ["local-asr", "local-asr"]
        assert _wait_until(lambda: agent._audio_queue.unfinished_tasks == 0)
    finally:
        asyncio.run(agent.stop())


def test_late_asr_input_survives_response_cancellation(monkeypatch):
    monkeypatch.setenv("LOCAL_TTS_STREAMING", "true")
    entered, release = threading.Event(), threading.Event()
    requests = []

    class Pipeline(SegmentedPipeline):
        def transcribe(self, segment):
            if segment == b"first":
                entered.set()
                assert release.wait(1)
            return super().transcribe(segment)

    agent = local_agent.LocalPipelineAgent(
        pipeline_factory=Pipeline,
        llm_chat=lambda m, *a: requests.append(m) or {"role": "assistant", "content": ""},
    )
    asyncio.run(agent.start(lambda pcm: None))
    try:
        feed(agent, 2)
        assert entered.wait(1)
        end_turn(agent)
        old = agent._turn
        feed(agent, 1)
        assert old.cancelled.is_set()
        release.set()
        assert _wait_until(lambda: len(agent._messages) == 2)
        assert agent._messages[-1]["content"] == "余额八十四"
        assert requests == []
        feed(agent, 3)
        end_turn(agent)
        assert _wait_until(lambda: requests)
        assert [m["content"] for m in requests[0] if m["role"] == "user"] == ["余额八十四", "点三一元"]
    finally:
        release.set()
        asyncio.run(agent.stop())


def test_segmented_barge_in_drops_inflight_tts_but_speaks_combined_input(monkeypatch):
    monkeypatch.setenv("LOCAL_TTS_STREAMING", "true")
    entered, release = threading.Event(), threading.Event()
    audio = []

    class Pipeline(SegmentedPipeline):
        def synthesize(self, text):
            if text == "旧的。":
                entered.set()
                assert release.wait(1)
            return text.encode()

    def chat(messages, *args):
        reply = "新的。" if messages[-1]["content"] == "余额八十四点三一元" else "旧的。"
        return {"role": "assistant", "content": reply}

    agent = local_agent.LocalPipelineAgent(pipeline_factory=Pipeline, llm_chat=chat)
    asyncio.run(agent.start(audio.append))
    try:
        asyncio.run(agent.say("opening"))
        assert entered.wait(1)
        feed(agent, 1)
        feed(agent, 2)
        feed(agent, 1)
        feed(agent, 3)
        end_turn(agent)
        release.set()
        assert _wait_until(lambda: audio)
        assert audio == ["新的。".encode()]
    finally:
        release.set()
        asyncio.run(agent.stop())


def test_asr_failure_discards_partial_turn_and_recovers(caplog):
    requests = []

    class Pipeline(SegmentedPipeline):
        def transcribe(self, segment):
            if segment == b"first":
                raise RuntimeError("fake recognition failure")
            return super().transcribe(segment)

    agent = local_agent.LocalPipelineAgent(
        pipeline_factory=Pipeline,
        llm_chat=lambda m, *a: requests.append(m) or {"role": "assistant", "content": ""},
    )
    asyncio.run(agent.start(lambda pcm: None))
    try:
        feed(agent, 2)
        feed(agent, 3)
        end_turn(agent)
        assert _wait_until(lambda: agent._asr_queue.unfinished_tasks == 0)
        assert not requests and "fake recognition failure" in caplog.text
        feed(agent, 3)
        end_turn(agent)
        assert _wait_until(lambda: requests)
        assert requests[0][-1]["content"] == "点三一元"
    finally:
        asyncio.run(agent.stop())


def test_stop_during_asr_does_not_submit_late_input():
    entered, release = threading.Event(), threading.Event()
    requests = []

    class Pipeline(SegmentedPipeline):
        def transcribe(self, segment):
            entered.set()
            assert release.wait(1)
            return super().transcribe(segment)

    agent = local_agent.LocalPipelineAgent(pipeline_factory=Pipeline, llm_chat=lambda *a: requests.append(a))
    asyncio.run(agent.start(lambda pcm: None))
    feed(agent, 2)
    end_turn(agent)
    assert entered.wait(1)

    async def stop():
        stopping = asyncio.create_task(agent.stop())
        while agent._running:
            await asyncio.sleep(0)
        release.set()
        await stopping

    try:
        asyncio.run(stop())
    finally:
        release.set()
        asyncio.run(agent.stop())
    assert len(agent._messages) == 1 and not requests
    assert agent._asr_thread is None


def test_segmented_fake_requires_speech_state():
    agent = local_agent.LocalPipelineAgent(pipeline_factory=FakePipeline, llm_chat=lambda *a: {})
    with pytest.raises(ValueError, match="is_speech_detected"):
        asyncio.run(agent.start(lambda pcm: None))
    assert agent.fatal


@pytest.mark.parametrize("seconds", ["nan", "inf"])
def test_invalid_turn_silence_is_rejected(monkeypatch, seconds):
    monkeypatch.setenv("LOCAL_TURN_SILENCE", seconds)
    with pytest.raises(ValueError, match="LOCAL_TURN_SILENCE"):
        local_agent.LocalPipelineAgent(pipeline_factory=SegmentedPipeline)


@pytest.mark.parametrize("enabled,silence", [("false", 0.6), ("true", 0.25)])
def test_real_vad_uses_short_silence_only_when_enabled(monkeypatch, tmp_path, enabled, silence):
    monkeypatch.setenv("LOCAL_ASR_SEGMENTED", enabled)
    captured = []
    monkeypatch.setattr(local_agent, "_get_shared_models", lambda *a: SimpleNamespace(
        root=tmp_path, sample_rate=8000, cached_phrases={},
    ))
    monkeypatch.setitem(sys.modules, "sherpa_onnx", SimpleNamespace(
        VadModelConfig=lambda: SimpleNamespace(silero_vad=SimpleNamespace()),
        VoiceActivityDetector=lambda cfg, **kw: captured.append(cfg),
    ))
    local_agent._SherpaPipeline()
    assert captured[0].silero_vad.min_silence_duration == silence
