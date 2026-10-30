"""用可控时钟和真实 worker 验证兜底，不访问模型或网络。"""

import asyncio
import threading
from types import SimpleNamespace

import pytest
from test_local_agent import FakePipeline, _wait_until

from agentcall.agents import local_agent
from agentcall.agents.local_agent import LocalPipelineAgent

FILLER = "嗯，您稍等。"


@pytest.fixture
def rig(monkeypatch):
    agents = []
    now = [100.0]
    monkeypatch.setattr(local_agent, "time", SimpleNamespace(monotonic=lambda: now[0]))

    def make(*, streaming=True, speculative=False, seconds="2.5", reply="好的。", prefix="", tool=False):
        monkeypatch.setenv("LOCAL_TTS_STREAMING", str(streaming).lower())
        monkeypatch.setenv("LOCAL_SLOW_REPLY_FILLER_SECONDS", seconds)
        monkeypatch.setenv("LOCAL_LATENCY_METRICS", "true")
        entered, release = threading.Event(), threading.Event()
        pipeline, audio, transcripts, events = FakePipeline(), [], [], []

        def chat(messages, tools, timeout, on_content, cancelled=None, on_tools=None):
            if tool:
                on_tools()
            if prefix:
                on_content(prefix)
            entered.set()
            while not release.wait(0.005):
                if cancelled is not None and cancelled.is_set():
                    raise InterruptedError()
            on_content(reply)
            return {"role": "assistant", "content": reply}

        agent = LocalPipelineAgent(pipeline_factory=lambda: pipeline, llm_chat=lambda *a: {})
        agent._deepseek = SimpleNamespace(chat=chat, close=release.set)
        agent._speculative = speculative
        agent.set_transcript_handler(lambda r, t: transcripts.append((r, t)))
        agent.set_turn_event_handler(events.append)

        def output(pcm):
            audio.append(pcm)
            pcm.on_written()

        asyncio.run(agent.start(output))
        agents.append((agent, release))
        if speculative:
            agent._start_speculation("测试", 0)
            assert entered.wait(1)
        asyncio.run(agent.send_audio("测试".encode()))
        assert entered.wait(1)
        assert _wait_until(lambda: len(agent._messages) >= 2)
        return SimpleNamespace(agent=agent, release=release, pipeline=pipeline, audio=audio,
                               transcripts=transcripts, events=events, now=now)

    yield make
    for agent, release in agents:
        release.set()
        worker = agent._tts_thread
        filler_worker = agent._filler_thread
        asyncio.run(agent.stop())
        assert worker is None or not worker.is_alive()
        assert filler_worker is None or not filler_worker.is_alive()


@pytest.mark.parametrize("streaming", [True, False])
@pytest.mark.parametrize("speculative", [True, False])
def test_slow_filler_at_endpoint_deadline_once_cached_and_not_in_history(rig, streaming, speculative, caplog):
    with caplog.at_level("INFO"):
        r = rig(streaming=streaming, speculative=speculative)
        assert r.pipeline.synthesized == [FILLER]
        r.now[0] = 102.49
        r.agent._maybe_slow_filler()
        assert not r.audio
        r.now[0] = 102.5
        assert _wait_until(lambda: len(r.audio) == 1)
        r.now[0] = 120.0
        r.agent._maybe_slow_filler()
        assert len(r.audio) == 1
        assert r.transcripts[-1] == ("agent", FILLER)
        assert all(m.get("content") != FILLER for m in r.agent._messages)
        r.release.set()
        assert _wait_until(lambda: len(r.audio) == 2)
        assert r.pipeline.synthesized == [FILLER, "好的。"]
        assert r.agent._messages[-1]["content"] == "好的。"
        stages = {e["stage"]: e for e in r.events}
        assert stages["slow_filler"]["ms"] == 2500.0
        assert stages["first_pcm_written"]["ms"] == 2500.0
        assert "[local_slow_filler] played turn=1 waited_ms=2500" in caplog.text


@pytest.mark.parametrize("seconds, cancel", [("0", False), ("2.5", True)])
def test_disabled_or_cancelled_turn_never_plays_filler(rig, seconds, cancel):
    r = rig(seconds=seconds)
    if cancel:
        r.agent._cancel_turn()
    r.now[0] = 110.0
    r.agent._maybe_slow_filler()
    assert not r.audio
    r.release.set()
    assert _wait_until(lambda: r.agent._brain_queue.unfinished_tasks == 0)
    r.agent._tts_queue.join()
    assert not any(e["stage"] == "slow_filler" for e in r.events)
    if seconds == "0":
        assert r.pipeline.synthesized == ["好的。"]
        assert r.agent._filler_thread is None


def test_fast_llm_never_plays_filler(rig):
    r = rig()
    r.release.set()
    assert _wait_until(lambda: r.audio)
    r.now[0] = 110.0
    r.agent._maybe_slow_filler()
    assert len(r.audio) == 1
    assert not any(e["stage"] == "slow_filler" for e in r.events)


@pytest.mark.parametrize("speculative", [True, False])
def test_tool_signal_disables_filler_while_llm_still_waiting(rig, speculative):
    r = rig(speculative=speculative, tool=True)
    assert _wait_until(lambda: r.agent._waiting_batch is None)
    r.now[0] = 110.0
    r.agent._maybe_slow_filler()
    assert not r.audio


@pytest.mark.parametrize("speculative", [True, False])
def test_first_normal_clause_disables_filler_before_llm_finishes(rig, speculative):
    r = rig(speculative=speculative, prefix="您好。")
    assert _wait_until(lambda: r.audio)
    r.now[0] = 110.0
    r.agent._maybe_slow_filler()
    assert len(r.audio) == 1
    assert not any(e["stage"] == "slow_filler" for e in r.events)


def test_bracket_tokens_alone_do_not_disable_slow_filler(rig):
    r = rig(prefix="（保", reply="持等待）")
    r.now[0] = 102.5
    assert _wait_until(lambda: r.audio)
    r.release.set()
    assert _wait_until(lambda: r.agent._brain_queue.unfinished_tasks == 0)
    r.agent._tts_queue.join()
    assert r.pipeline.synthesized == [FILLER]


@pytest.mark.parametrize("reply", ["", "（保持等待）"])
def test_fast_silent_reply_never_plays_filler(rig, reply):
    r = rig(reply=reply)
    r.release.set()
    assert _wait_until(lambda: r.agent._brain_queue.unfinished_tasks == 0)
    r.now[0] = 110.0
    r.agent._maybe_slow_filler()
    assert not r.audio


@pytest.mark.parametrize("reply", ["", "（保持等待）"])
def test_silent_final_reply_after_filler_stays_silent(rig, reply):
    r = rig(reply=reply)
    r.now[0] = 102.5
    assert _wait_until(lambda: r.audio)
    r.release.set()
    assert _wait_until(lambda: r.agent._brain_queue.unfinished_tasks == 0)
    r.agent._tts_queue.join()
    assert len(r.audio) == 1 and r.pipeline.synthesized == [FILLER]


def test_filler_throttled_for_eight_seconds_across_turns(rig):
    r = rig(reply="")
    r.now[0] = 102.5
    assert _wait_until(lambda: r.audio)
    r.release.set()
    assert _wait_until(lambda: r.agent._brain_queue.unfinished_tasks == 0)
    r.release.clear()
    r.now[0] = 103.0
    asyncio.run(r.agent.send_audio(b"second"))
    assert _wait_until(lambda: len(r.agent._messages) == 4)
    r.now[0] = 110.49
    r.agent._maybe_slow_filler()
    assert len(r.audio) == 1
    r.now[0] = 110.5
    assert _wait_until(lambda: len(r.audio) == 2)


def test_empty_asr_tail_does_not_disqualify_uncancelled_pending_turn(rig):
    r = rig()
    pending = r.agent._turn
    r.agent._handle_user_text("", r.agent._new_turn(101.0))
    assert not pending.cancelled.is_set()
    r.now[0] = 102.5
    assert _wait_until(lambda: r.audio)
    assert next(e for e in r.events if e["stage"] == "slow_filler")["turn_id"] == pending.sequence


def test_stop_prevents_pending_filler_and_joins_worker(rig):
    r = rig()
    worker = r.agent._tts_thread
    asyncio.run(r.agent.stop())
    r.now[0] = 110.0
    r.agent._maybe_slow_filler()
    assert not r.audio and not worker.is_alive()


def test_filler_output_failure_does_not_kill_workers_or_lose_normal_reply(rig):
    r = rig()
    failed = threading.Event()
    output = r.agent._on_audio_out

    def flaky_output(pcm):
        if not failed.is_set():
            failed.set()
            raise RuntimeError("模拟输出失败")
        output(pcm)

    r.agent._on_audio_out = flaky_output
    r.now[0] = 102.5
    assert failed.wait(1)
    r.release.set()
    assert _wait_until(lambda: r.audio)
    assert r.agent._tts_thread.is_alive() and r.agent._filler_thread.is_alive()


def test_cancelled_old_synthesis_cannot_block_new_turn_filler(rig):
    r = rig(reply="")
    synthesizing, finish_synthesis = threading.Event(), threading.Event()
    synthesize = r.pipeline.synthesize

    def slow_synthesis(text):
        if text == "旧回复。":
            synthesizing.set()
            assert finish_synthesis.wait(3)
        return synthesize(text)

    r.pipeline.synthesize = slow_synthesis
    r.agent._queue_clause("旧回复。", local_agent._SpeechBatch(r.agent._turn))
    assert synthesizing.wait(1)
    r.agent._cancel_turn()
    assert _wait_until(lambda: r.agent._brain_queue.unfinished_tasks == 0)
    asyncio.run(r.agent.send_audio(b"new"))
    assert _wait_until(lambda: len(r.agent._messages) == 3)
    try:
        r.now[0] = 102.5
        assert _wait_until(lambda: r.audio, timeout=1)
        assert r.transcripts[-1] == ("agent", FILLER)
        assert not finish_synthesis.is_set()
    finally:
        finish_synthesis.set()


@pytest.mark.parametrize("streaming", [True, False])
def test_new_speech_interrupts_pending_filler(rig, streaming):
    r = rig(streaming=streaming)
    old = r.agent._turn
    asyncio.run(r.agent.send_audio(b"new"))
    # Keep the clock fixed and both LLM replies gated until the new request is pending.
    assert _wait_until(lambda: (batch := r.agent._waiting_batch) is not None and batch.turn is not old)
    new = r.agent._waiting_batch.turn
    assert old.cancelled.is_set() and not new.cancelled.is_set()
    r.now[0] = 110.0
    r.agent._maybe_slow_filler()
    # Advancing the shared clock also makes the new turn's filler legitimately due.
    assert [e["turn_id"] for e in r.events if e["stage"] == "slow_filler"] == [new.sequence]
    assert len(r.audio) == 1
    assert r.pipeline.synthesized == [FILLER]

    r.release.set()
    assert _wait_until(lambda: r.agent._brain_queue.unfinished_tasks == 0)
    r.agent._tts_queue.join()
    assert len(r.audio) == 2
    assert r.pipeline.synthesized == [FILLER, "好的。"]
    assert [e["turn_id"] for e in r.events if e["stage"] == "first_pcm_written"] == [new.sequence]
    assert not old.reply_started


@pytest.mark.parametrize("seconds", ["-1", "nan", "inf"])
def test_filler_seconds_must_be_finite_and_nonnegative(monkeypatch, seconds):
    monkeypatch.setenv("LOCAL_SLOW_REPLY_FILLER_SECONDS", seconds)
    with pytest.raises(ValueError, match="LOCAL_SLOW_REPLY_FILLER_SECONDS"):
        LocalPipelineAgent(llm_chat=lambda *a: {})
