"""Streaming overlap and cancellation use events, not long timing sleeps."""

import asyncio
import threading

import pytest
from test_local_agent import FakePipeline, _wait_until
from test_local_llm import FakeConnection, sse

from agentcall.agents.local_agent import LocalPipelineAgent
from agentcall.agents.tools import HANGUP_SPEC, SEND_DTMF_SPEC, SEND_SMS_SPEC, ToolRegistry


def test_first_audio_precedes_llm_end_and_transcript_is_whole(monkeypatch):
    monkeypatch.setenv("LOCAL_TTS_STREAMING", "true")
    first_audio, finish_llm = threading.Event(), threading.Event()
    pipeline = FakePipeline()
    transcripts, latencies, audio = [], [], []

    def stream(messages, tools, timeout, on_content):
        on_content("你好，")
        assert first_audio.wait(1), "TTS must run before SSE completes"
        assert transcripts == [("user", "hello")]
        on_content("请稍等。")
        finish_llm.set()
        return {"role": "assistant", "content": "你好，请稍等。"}

    agent = LocalPipelineAgent(pipeline_factory=lambda: pipeline, llm_stream=stream)
    agent.set_transcript_handler(lambda r, t: transcripts.append((r, t)))
    agent.set_latency_handler(lambda stage, ms: latencies.append((stage, ms)))

    def output(pcm):
        audio.append(pcm)
        first_audio.set()

    asyncio.run(agent.start(output))
    try:
        asyncio.run(agent.send_audio(b"hello"))
        assert _wait_until(lambda: len(audio) == 2)
        assert finish_llm.is_set()
        assert pipeline.synthesized == ["你好，", "请稍等。"]
        assert transcripts == [("user", "hello"), ("agent", "你好，请稍等。")]
        assert [s for s, _ in latencies] == ["llm_first_token", "first_clause_ready", "tts_first_audio"]
        assert all(ms >= 0 for _, ms in latencies)
    finally:
        asyncio.run(agent.stop())


def test_new_speech_discards_queued_and_inflight_old_audio(monkeypatch):
    monkeypatch.setenv("LOCAL_TTS_STREAMING", "true")
    synthesizing, release = threading.Event(), threading.Event()

    class SlowPipeline(FakePipeline):
        def synthesize(self, text):
            self.synthesized.append(text)
            if text == "旧的，":
                synthesizing.set()
                assert release.wait(1)
            return text.encode()

    pipeline, audio, interrupts = SlowPipeline(), [], []

    def stream(messages, tools, timeout, on_content):
        text = "旧的，丢掉。" if messages[-1]["content"] == "old" else "新的。"
        on_content(text)
        return {"role": "assistant", "content": text}

    agent = LocalPipelineAgent(pipeline_factory=lambda: pipeline, llm_stream=stream)
    agent.set_user_interrupt_handler(lambda: interrupts.append(True) or True)
    asyncio.run(agent.start(audio.append))
    try:
        asyncio.run(agent.send_audio(b"old"))
        assert synthesizing.wait(1)
        asyncio.run(agent.send_audio(b"new"))
        assert _wait_until(lambda: pipeline.transcripts == ["old", "new"])
        release.set()
        assert _wait_until(lambda: audio == ["新的。".encode()])
        assert pipeline.synthesized == ["旧的，", "新的。"]
        assert interrupts
    finally:
        release.set()
        asyncio.run(agent.stop())


def test_stop_cancels_inflight_synthesis_without_output(monkeypatch):
    monkeypatch.setenv("LOCAL_TTS_STREAMING", "true")
    entered, release = threading.Event(), threading.Event()

    class SlowPipeline(FakePipeline):
        def synthesize(self, text):
            entered.set()
            release.wait(1)
            return b"old audio"

    agent = LocalPipelineAgent(pipeline_factory=SlowPipeline,
                               llm_chat=lambda *a: {"role": "assistant", "content": "你好，再见。"})
    audio = []
    asyncio.run(agent.start(audio.append))
    asyncio.run(agent.send_audio(b"hi"))
    assert entered.wait(1)

    async def stop():
        task = asyncio.create_task(agent.stop())
        while agent._running:
            await asyncio.sleep(0)
        release.set()
        await task

    asyncio.run(stop())
    assert audio == []
    assert agent._tts_queue.empty()


def test_cached_clause_is_not_synthesized_again(monkeypatch):
    monkeypatch.setenv("LOCAL_TTS_STREAMING", "true")
    monkeypatch.setenv("LOCAL_TTS_CACHE_PHRASES", "你好，|请稍等。|你好，")
    pipeline, audio = FakePipeline(), []
    agent = LocalPipelineAgent(pipeline_factory=lambda: pipeline,
                               llm_chat=lambda *a: {"role": "assistant", "content": "你好，请稍等。"})
    asyncio.run(agent.start(audio.append))
    try:
        assert pipeline.synthesized == ["你好，", "请稍等。"]
        assert audio == []  # Startup caches are silent.
        asyncio.run(agent.send_audio(b"hi"))
        assert _wait_until(lambda: len(audio) == 2)
        assert pipeline.synthesized == ["你好，", "请稍等。"]
    finally:
        asyncio.run(agent.stop())


def test_streaming_failures_mark_fatal_and_drop_pending_speech(monkeypatch):
    monkeypatch.setenv("LOCAL_TTS_STREAMING", "true")

    def broken(*args):
        raise RuntimeError("fake broken stream")

    agent = LocalPipelineAgent(pipeline_factory=FakePipeline, llm_stream=broken)
    asyncio.run(agent.start(lambda _: None))
    try:
        for expected in range(1, 4):
            asyncio.run(agent.send_audio(b"hi"))
            assert _wait_until(lambda: agent._llm_failures == expected)
        assert agent.fatal
    finally:
        asyncio.run(agent.stop())


@pytest.mark.parametrize("spec", [SEND_DTMF_SPEC, HANGUP_SPEC])
def test_streamed_terminal_and_dtmf_tools_do_not_generate_followup_speech(monkeypatch, spec):
    monkeypatch.setenv("LOCAL_TTS_STREAMING", "true")
    calls, requests, audio = [], [], []
    registry = ToolRegistry()
    registry.register(spec, lambda args: calls.append(args) or {"success": True})

    def stream(messages, tools, timeout, on_content):
        requests.append(messages)
        return {"role": "assistant", "content": "", "tool_calls": [{
            "id": "call_1", "type": "function",
            "function": {"name": spec["function"]["name"], "arguments": '{"digits":"1"}'},
        }]}

    agent = LocalPipelineAgent(pipeline_factory=FakePipeline, llm_stream=stream)
    agent.set_tools(registry)
    asyncio.run(agent.start(audio.append))
    try:
        asyncio.run(agent.send_audio(b"press one"))
        assert _wait_until(lambda: calls)
        assert len(requests) == 1
        assert audio == []
    finally:
        asyncio.run(agent.stop())


def test_repeat_check_late_cuts_pending_audio_but_does_not_delay_first_clause(monkeypatch):
    monkeypatch.setenv("LOCAL_TTS_STREAMING", "true")
    first_audio, second_synthesis, release = threading.Event(), threading.Event(), threading.Event()
    reply = "您好，我想查询我的流量使用情况谢谢。"
    audio, late_cuts = [], []

    class Pipeline(FakePipeline):
        def synthesize(self, text):
            if text != "您好，":
                second_synthesis.set()
                assert release.wait(1)
            return text.encode()

    def stream(messages, tools, timeout, on_content):
        on_content("您好，")
        assert first_audio.wait(1)
        on_content(reply[3:])
        assert second_synthesis.wait(1)
        return {"role": "assistant", "content": reply}

    agent = LocalPipelineAgent(pipeline_factory=Pipeline, llm_stream=stream)
    assert agent._suppressor.should_suppress(reply) is False
    assert agent._suppressor.should_suppress(reply) is False
    agent.set_late_cut_handler(lambda: late_cuts.append(True) or True)

    def output(pcm):
        audio.append(pcm)
        first_audio.set()

    asyncio.run(agent.start(output))
    try:
        asyncio.run(agent.send_audio(b"hi"))
        assert _wait_until(lambda: late_cuts)
        release.set()
        agent._tts_queue.join()
        assert audio == ["您好，".encode()]
        assert any("系统指令" in m.get("content", "") for m in agent._messages)
    finally:
        release.set()
        asyncio.run(agent.stop())


def test_cancelled_llm_cannot_execute_late_tools_or_count_as_failure(monkeypatch):
    monkeypatch.setenv("LOCAL_TTS_STREAMING", "true")
    entered, release = threading.Event(), threading.Event()
    calls, audio = [], []
    registry = ToolRegistry()
    registry.register(SEND_DTMF_SPEC, lambda args: calls.append(args) or {"success": True})

    def stream(messages, tools, timeout, on_content):
        if messages[-1]["content"] == "old":
            entered.set()
            assert release.wait(1)
            return {"role": "assistant", "content": "", "tool_calls": [{
                "id": "stale", "function": {"name": "send_dtmf", "arguments": '{"digits":"1"}'},
            }]}
        on_content("新的。")
        return {"role": "assistant", "content": "新的。"}

    agent = LocalPipelineAgent(pipeline_factory=FakePipeline, llm_stream=stream)
    agent.set_tools(registry)
    asyncio.run(agent.start(audio.append))
    try:
        asyncio.run(agent.send_audio(b"old"))
        assert entered.wait(1)
        old_turn = agent._turn
        asyncio.run(agent.send_audio(b"new"))
        assert _wait_until(lambda: old_turn.cancelled.is_set())
        release.set()
        assert _wait_until(lambda: audio)
        assert calls == []
        assert agent._llm_failures == 0 and not agent.fatal
    finally:
        release.set()
        asyncio.run(agent.stop())


def test_rejected_barge_in_preserves_existing_audio(monkeypatch):
    monkeypatch.setenv("LOCAL_TTS_STREAMING", "true")
    entered, release = threading.Event(), threading.Event()

    class Pipeline(FakePipeline):
        def synthesize(self, text):
            if text == "旧的。":
                entered.set()
                assert release.wait(1)
            return text.encode()

    def chat(messages, *args):
        return {"role": "assistant", "content": "旧的。" if messages[-1]["content"] == "old" else "新的。"}

    pipeline, audio = Pipeline(), []
    agent = LocalPipelineAgent(pipeline_factory=lambda: pipeline, llm_chat=chat)
    agent.set_user_interrupt_handler(lambda: False)
    asyncio.run(agent.start(audio.append))
    try:
        asyncio.run(agent.send_audio(b"old"))
        assert entered.wait(1)
        asyncio.run(agent.send_audio(b"new"))
        assert _wait_until(lambda: pipeline.transcripts == ["old", "new"])
        release.set()
        assert _wait_until(lambda: len(audio) == 2)
        assert audio == ["旧的。".encode(), "新的。".encode()]
    finally:
        release.set()
        asyncio.run(agent.stop())


@pytest.mark.parametrize("streaming", ["false", "true"])
def test_deepseek_sse_tools_feedback_and_optional_clause_tts(monkeypatch, streaming):
    import io

    from agentcall.agents import local_llm

    monkeypatch.setenv("LOCAL_LLM_PROVIDER", "deepseek")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fake-key")
    monkeypatch.setenv("LOCAL_TTS_STREAMING", streaming)
    replies = [sse(
        {"tool_calls": [{"index": 0, "id": "sms", "function": {"name": "send_", "arguments": "{"}}]},
        {"tool_calls": [{"index": 0, "function": {"name": "sms", "arguments": '"to":"10000"}'}}]},
    ), sse({"content": "短信，"}, {"content": "已发。"})]

    class Connection(FakeConnection):
        def getresponse(self):
            response = io.BytesIO(replies.pop(0))
            response.status = 200
            return response

    connection, calls, audio = Connection(), [], []
    monkeypatch.setattr(local_llm.http.client, "HTTPSConnection", lambda *a, **kw: connection)
    pipeline = FakePipeline()
    registry = ToolRegistry()
    registry.register(SEND_SMS_SPEC, lambda args: calls.append(args) or {"success": True})
    agent = LocalPipelineAgent(pipeline_factory=lambda: pipeline)
    agent.set_tools(registry)
    asyncio.run(agent.start(audio.append))
    try:
        asyncio.run(agent.send_audio(b"send sms"))
        assert _wait_until(lambda: len(audio) == (2 if streaming == "true" else 1))
        assert calls == [{"to": "10000"}]
        assert len(connection.requests) == 2
        assert any(m.get("role") == "tool" for m in connection.requests[1][2]["messages"])
        assert pipeline.synthesized == (["短信，", "已发。"] if streaming == "true" else ["短信，已发。"])
    finally:
        asyncio.run(agent.stop())


@pytest.mark.parametrize("thinking", ["low", "high"])
@pytest.mark.parametrize("streaming", ["false", "true"])
def test_thinking_tool_rounds_and_new_user_preserve_reasoning_privately(monkeypatch, caplog, thinking, streaming):
    import io
    import logging

    from agentcall.agents import local_llm

    monkeypatch.setenv("LOCAL_LLM_PROVIDER", "deepseek")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fake-key")
    monkeypatch.setenv("LOCAL_LLM_BASE_URL", "https://example.invalid/v1")
    monkeypatch.setenv("LOCAL_LLM_THINKING", thinking)
    monkeypatch.setenv("LOCAL_TTS_STREAMING", streaming)
    caplog.set_level(logging.DEBUG, logger="agentcall")
    reasoning = [f"internal-plan-{i}" for i in range(4)]
    replies = [sse(
        {"reasoning_content": reasoning[i][:8]},
        {"reasoning_content": reasoning[i][8:], "tool_calls": [{
            "index": 0, "id": f"lookup-{i}",
            "function": {"name": "lookup", "arguments": "{}"},
        }]},
    ) for i in range(2)] + [
        sse({"reasoning_content": reasoning[2]}, {"content": "第一句。"}),
        sse({"reasoning_content": reasoning[3]}, {"content": "第二句。"}),
    ]

    class Connection(FakeConnection):
        def getresponse(self):
            response = io.BytesIO(replies.pop(0))
            response.status = 200
            return response

    connection, transcripts, audio, calls = Connection(), [], [], []
    monkeypatch.setattr(local_llm.http.client, "HTTPSConnection", lambda *a, **kw: connection)
    pipeline = FakePipeline()
    registry = ToolRegistry()
    registry.register({"type": "function", "function": {
        "name": "lookup", "parameters": {"type": "object", "properties": {}},
    }}, lambda args: calls.append(args) or {"success": True})
    agent = LocalPipelineAgent(pipeline_factory=lambda: pipeline)
    agent.set_tools(registry)
    agent.set_transcript_handler(lambda role, text: transcripts.append((role, text)))
    asyncio.run(agent.start(audio.append))
    try:
        asyncio.run(agent.send_audio(b"first"))
        assert _wait_until(lambda: len(audio) == 1)
        asyncio.run(agent.send_audio(b"next"))
        assert _wait_until(lambda: len(audio) == 2)
        assert calls == [{}, {}]
        assert len(connection.requests) == 4
        for i, (_, _, body, _) in enumerate(connection.requests):
            assert body["thinking"] == {"type": "enabled"}
            assert body["reasoning_effort"] == thinking
            assistants = [m for m in body["messages"] if m["role"] == "assistant"]
            assert [m["reasoning_content"] for m in assistants] == reasoning[:i]
            assert len([m for m in body["messages"] if m["role"] == "tool"]) == min(i, 2)
        assert connection.requests[3][2]["messages"][-1] == {"role": "user", "content": "next"}
        assert pipeline.synthesized == ["第一句。", "第二句。"]
        assert transcripts == [("user", "first"), ("agent", "第一句。"),
                               ("user", "next"), ("agent", "第二句。")]
        assert all(secret not in caplog.text for secret in reasoning)
        assert [m["reasoning_content"] for m in agent._messages if m["role"] == "assistant"] == reasoning
    finally:
        asyncio.run(agent.stop())


def test_streaming_tool_loop_retains_round_limit(monkeypatch):
    monkeypatch.setenv("LOCAL_TTS_STREAMING", "true")
    calls = []
    registry = ToolRegistry()
    registry.register(SEND_SMS_SPEC, lambda args: calls.append(args) or {"success": True})

    def stream(*args):
        return {"role": "assistant", "content": "", "tool_calls": [{
            "id": f"call_{len(calls)}", "function": {"name": "send_sms", "arguments": "{}"},
        }]}

    agent = LocalPipelineAgent(pipeline_factory=FakePipeline, llm_stream=stream)
    agent.set_tools(registry)
    audio = []
    asyncio.run(agent.start(audio.append))
    try:
        asyncio.run(agent.send_audio(b"hi"))
        assert _wait_until(lambda: len(calls) == 4)
        assert not audio and not agent.fatal
    finally:
        asyncio.run(agent.stop())


def test_vad_speech_start_cancels_audio_before_next_segment_is_complete(monkeypatch):
    monkeypatch.setenv("LOCAL_TTS_STREAMING", "true")
    entered, release = threading.Event(), threading.Event()

    class Pipeline(FakePipeline):
        speaking = False

        def is_speech_detected(self):
            return self.speaking

        def vad_push(self, pcm):
            self.speaking = pcm == b"new speech starts"
            return [] if self.speaking else [pcm]

        def synthesize(self, text):
            self.synthesized.append(text)
            entered.set()
            assert release.wait(1)
            return b"stale"

    pipeline, audio = Pipeline(), []
    agent = LocalPipelineAgent(pipeline_factory=lambda: pipeline,
                               llm_chat=lambda *a: {"role": "assistant", "content": "旧句，丢掉。"})
    asyncio.run(agent.start(audio.append))
    try:
        asyncio.run(agent.send_audio(b"old"))
        assert entered.wait(1)
        old_turn = agent._turn
        asyncio.run(agent.send_audio(b"new speech starts"))
        assert _wait_until(lambda: old_turn.cancelled.is_set())
        assert pipeline.transcripts == ["old"]
        release.set()
        assert _wait_until(lambda: agent._tts_queue.unfinished_tasks == 0)
        assert audio == []
        assert pipeline.synthesized == ["旧句，"]
    finally:
        release.set()
        asyncio.run(agent.stop())
