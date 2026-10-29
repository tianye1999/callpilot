"""抢跑流必须在判停后消费，过期输入不能执行工具或写对话。"""

import asyncio
import threading
from types import SimpleNamespace

import pytest
from test_local_agent import _wait_until
from test_local_segmented import SegmentedPipeline, end_turn, feed

from agentcall.agents import local_agent
from agentcall.agents.local_agent import LocalPipelineAgent
from agentcall.agents.tools import SEND_DTMF_SPEC, ToolRegistry


class Stream:
    def __init__(self, message=None):
        self.calls = []
        self.cancelled = []
        self.release = threading.Event()
        self.message = message or {"role": "assistant", "content": "好的。"}

    def chat(self, messages, tools, timeout, on_content, cancelled=None, on_tools=None):
        self.calls.append(messages)
        if self.message.get("tool_calls") and on_tools is not None:
            on_tools()
        on_content("好的。")
        while not self.release.wait(0.005):
            if cancelled is not None and cancelled.is_set():
                self.cancelled.append(True)
                raise InterruptedError()
        return self.message

    def close(self):
        self.release.set()


@pytest.fixture
def rig(monkeypatch):
    monkeypatch.setenv("LOCAL_ASR_SEGMENTED", "true")
    monkeypatch.setenv("LOCAL_TTS_STREAMING", "true")
    monkeypatch.setenv("LOCAL_SPECULATIVE_LLM", "true")
    monkeypatch.setenv("LOCAL_LLM_PREWARM", "false")
    stream, audio = Stream(), []
    agent = LocalPipelineAgent(pipeline_factory=SegmentedPipeline, llm_chat=lambda *a: {})
    agent._deepseek = stream
    asyncio.run(agent.start(audio.append))
    yield agent, stream, audio
    asyncio.run(agent.stop())


def test_speculative_hit_reuses_inflight_stream_only_after_endpoint(rig):
    agent, stream, audio = rig
    feed(agent, 2)
    assert _wait_until(lambda: stream.calls)
    assert not audio and len(agent._messages) == 1
    end_turn(agent)
    assert _wait_until(lambda: audio)
    assert len(stream.calls) == 1
    stream.release.set()
    assert _wait_until(lambda: len(agent._messages) == 3)


def test_resume_cancels_and_text_change_restarts_without_stale_history(rig):
    agent, stream, audio = rig
    feed(agent, 2)
    assert _wait_until(lambda: stream.calls)
    feed(agent, 1)
    assert _wait_until(lambda: stream.cancelled)
    feed(agent, 3)
    assert _wait_until(lambda: len(stream.calls) == 2)
    assert stream.calls[1][-1]["content"] == "余额八十四点三一元"
    assert not audio and len(agent._messages) == 1
    end_turn(agent)
    stream.release.set()
    assert _wait_until(lambda: audio)
    assert len(stream.calls) == 2


def test_speculative_tools_wait_for_confirmation(rig):
    agent, stream, audio = rig
    calls = []
    registry = ToolRegistry()
    registry.register(SEND_DTMF_SPEC, lambda args: calls.append(args) or {"success": True})
    agent.set_tools(registry)
    stream.message = {"role": "assistant", "content": "", "tool_calls": [
        {"id": "x", "function": {"name": "send_dtmf", "arguments": '{"digits":"1"}'}},
    ]}
    stream.release.set()
    feed(agent, 2)
    assert _wait_until(lambda: stream.calls)
    assert not audio and not calls
    end_turn(agent)
    assert _wait_until(lambda: calls)
    assert calls == [{"digits": "1"}] and not audio


def test_stop_cancels_speculation_and_joins_worker(rig):
    agent, stream, audio = rig
    feed(agent, 2)
    assert _wait_until(lambda: stream.calls)
    worker = agent._speculation.thread
    asyncio.run(agent.stop())
    assert not worker.is_alive() and not audio


def test_speculative_cancel_does_not_block_next_asr(rig):
    agent, stream, audio = rig
    entered, release = threading.Event(), threading.Event()
    original = stream.chat

    def connecting(*args, **kwargs):
        entered.set()
        assert release.wait(2)
        return original(*args, **kwargs)

    stream.chat = connecting
    feed(agent, 2)
    assert entered.wait(1)
    feed(agent, 1)
    feed(agent, 3)
    try:
        assert _wait_until(lambda: agent._asr_queue.unfinished_tasks == 0, timeout=0.5)
        assert agent._pipeline.transcripts == [b"first", b"second"]
        assert agent._spec_jobs.qsize() <= 1 and not audio
    finally:
        release.set()
        stream.release.set()


def test_speculative_metrics_preserve_token_and_clause_ready_before_endpoint(rig, monkeypatch):
    agent, stream, audio = rig
    now, events = [10.0], []
    monkeypatch.setattr(local_agent, "time", SimpleNamespace(monotonic=lambda: now[0]))
    agent._metrics = True
    agent.set_turn_event_handler(events.append)
    feed(agent, 2)
    assert _wait_until(lambda: agent._speculation and agent._speculation.events.qsize() >= 2)
    now[0] = 20.0
    end_turn(agent)
    assert _wait_until(lambda: audio)
    stages = {event["stage"]: event for event in events if "stage" in event}
    assert stages["llm_first_token"]["ms"] == -10000
    assert stages["first_clause_ready"]["ms"] == -10000
    stream.release.set()


def test_confirmed_pending_job_cannot_be_evicted_by_new_speculation(rig):
    agent, stream, audio = rig
    entered, release = threading.Event(), threading.Event()
    original = stream.chat

    def connecting(*args, **kwargs):
        entered.set()
        assert release.wait(2)
        return original(*args, **kwargs)

    stream.chat = connecting
    agent.set_user_interrupt_handler(lambda: False)
    feed(agent, 2)
    assert entered.wait(1)
    feed(agent, 1)
    feed(agent, 3)
    assert _wait_until(lambda: agent._spec_jobs.qsize() == 1)
    pending = agent._speculation
    end_turn(agent)
    assert _wait_until(lambda: pending.claimed)
    try:
        agent._start_speculation("新的话", agent._speech_revision)
        assert not pending.cancelled.is_set() and agent._spec_jobs.qsize() == 1
        release.set()
        stream.release.set()
        assert _wait_until(lambda: audio)
    finally:
        release.set()
        stream.release.set()
