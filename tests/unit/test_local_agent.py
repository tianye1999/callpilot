"""LocalPipelineAgent 三段式管线单测（fake 管线注入，零模型零网络）。"""

from __future__ import annotations

import asyncio
import time

from agentcall.agents.local_agent import LocalPipelineAgent
from agentcall.agents.tools import SEND_SMS_SPEC, ToolRegistry


class FakePipeline:
    """确定性 fake：每次 vad_push 原样切一段；转写/合成可编程。"""

    sample_rate = 22050

    def __init__(self) -> None:
        self.transcripts: list[str] = []
        self.synthesized: list[str] = []

    def vad_push(self, pcm16: bytes) -> list[bytes]:
        return [pcm16] if pcm16 else []

    def vad_flush(self) -> list[bytes]:
        return []

    def transcribe(self, segment_pcm16: bytes) -> str:
        text = segment_pcm16.decode("utf-8", errors="ignore")
        self.transcripts.append(text)
        return text

    def synthesize(self, text: str) -> bytes:
        self.synthesized.append(text)
        return b"\x01\x02" * 160


def _wait_until(cond, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.02)
    return False


def _make_agent(replies):
    """构造注入 fake 管线与脚本化 LLM 的 agent。replies: list[dict]（按序弹出）。"""
    pipeline = FakePipeline()
    seen_requests: list[list[dict]] = []

    def scripted_llm(messages, tools, timeout):
        seen_requests.append([dict(m) for m in messages])
        if not replies:
            return {"role": "assistant", "content": ""}
        item = replies.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    agent = LocalPipelineAgent(pipeline_factory=lambda: pipeline, llm_chat=scripted_llm)
    return agent, pipeline, seen_requests


def test_speech_to_reply_full_pipeline():
    replies = [{"role": "assistant", "content": "好的，我帮您问一下。"}]
    agent, pipeline, seen = _make_agent(replies)
    agent.set_session_instructions("你是测试助手")
    transcripts: list[tuple[str, str]] = []
    agent.set_transcript_handler(lambda role, text: transcripts.append((role, text)))
    audio_out: list[bytes] = []

    asyncio.run(agent.start(audio_out.append))
    try:
        asyncio.run(agent.send_audio("你好请问在吗".encode()))
        assert _wait_until(lambda: len(audio_out) >= 1)
    finally:
        asyncio.run(agent.stop())

    assert pipeline.transcripts == ["你好请问在吗"]
    assert ("user", "你好请问在吗") in transcripts
    assert ("agent", "好的，我帮您问一下。") in transcripts
    assert pipeline.synthesized == ["好的，我帮您问一下。"]
    # 对话历史：system + user + assistant
    assert seen[0][0]["role"] == "system"
    assert seen[0][0]["content"] == "你是测试助手"


def test_output_rate_follows_pipeline_sample_rate():
    agent, pipeline, _seen = _make_agent([])
    pipeline.sample_rate = 16000
    asyncio.run(agent.start(lambda pcm: None))
    try:
        assert agent.output_rate == 16000
    finally:
        asyncio.run(agent.stop())


def test_say_generates_speech_and_keeps_history():
    replies = [{"role": "assistant", "content": "你好，我是李明的助理。"}]
    agent, pipeline, seen = _make_agent(replies)
    agent.set_session_instructions("sys")
    asyncio.run(agent.start(lambda pcm: None))
    try:
        asyncio.run(agent.say("请直接说：你好，我是李明的助理。"))
        assert _wait_until(lambda: pipeline.synthesized == ["你好，我是李明的助理。"])
    finally:
        asyncio.run(agent.stop())
    # say 的系统指令进入请求上下文
    assert any("请直接说" in m.get("content", "") for m in seen[0])


def test_tool_call_roundtrip_and_result_feedback():
    calls: list[dict] = []

    def send_sms_handler(args: dict) -> dict:
        calls.append(args)
        return {"success": True, "message": "短信已发送"}

    registry = ToolRegistry()
    registry.register(SEND_SMS_SPEC, send_sms_handler)

    replies = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "send_sms",
                        "arguments": '{"to": "10000", "content": "hi"}',
                    },
                }
            ],
        },
        {"role": "assistant", "content": "短信已经发出去了。"},
    ]
    agent, pipeline, seen = _make_agent(replies)
    agent.set_tools(registry)
    asyncio.run(agent.start(lambda pcm: None))
    try:
        asyncio.run(agent.send_audio("帮我发条短信".encode()))
        assert _wait_until(lambda: pipeline.synthesized == ["短信已经发出去了。"])
    finally:
        asyncio.run(agent.stop())

    assert calls == [{"to": "10000", "content": "hi"}]
    # 第二次 LLM 请求应带 tool 角色的结果回填
    assert any(m.get("role") == "tool" for m in seen[1])


def test_llm_failures_mark_fatal():
    replies = [RuntimeError("boom"), RuntimeError("boom"), RuntimeError("boom")]
    agent, _pipeline, _seen = _make_agent(replies)
    asyncio.run(agent.start(lambda pcm: None))
    try:
        for _ in range(3):
            asyncio.run(agent.send_audio(b"hello"))
        assert _wait_until(lambda: agent.fatal)
    finally:
        asyncio.run(agent.stop())


def test_repeat_suppression_blocks_third_identical_reply():
    same = {"role": "assistant", "content": "您好，我想查一下流量使用情况谢谢。"}
    replies = [dict(same), dict(same), dict(same)]
    agent, pipeline, _seen = _make_agent(replies)
    asyncio.run(agent.start(lambda pcm: None))
    try:
        # 逐段等待处理完再发下一段：否则三段并发入队会被 utterance 合并成一轮，
        # 且计数随 worker 调度浮动（CI 慢时 flaky）。每段独立成轮才能验"第三次抑制"。
        for i, text in enumerate(("第一句", "第二句", "第三句"), start=1):
            asyncio.run(agent.send_audio(text.encode()))
            assert _wait_until(lambda n=i: len(pipeline.transcripts) == n)
    finally:
        asyncio.run(agent.stop())
    # RepeatSuppressor 语义：重复第二次仍放行（对方要求重复属合法），第三次抑制。
    assert pipeline.transcripts == ["第一句", "第二句", "第三句"]
    assert len(pipeline.synthesized) == 2


def test_pipeline_init_failure_sets_fatal():
    def broken_factory():
        raise RuntimeError("模型缺失")

    agent = LocalPipelineAgent(pipeline_factory=broken_factory, llm_chat=lambda *a: {})
    try:
        asyncio.run(agent.start(lambda pcm: None))
    except RuntimeError:
        pass
    assert agent.fatal


def test_first_run_downloads_missing_models_and_reports_progress():
    """首启缺模型：工厂收到 on_progress 回调、状态经 set_status_handler 播出。"""
    progress: list[str] = []

    def factory(on_progress=None):
        if on_progress:
            on_progress("下载中…")
        return FakePipeline()

    agent = LocalPipelineAgent(pipeline_factory=factory, llm_chat=lambda *a: {})
    agent.set_status_handler(progress.append)
    asyncio.run(agent.start(lambda pcm: None))
    try:
        assert "下载中…" in progress
    finally:
        asyncio.run(agent.stop())


def test_factory_without_on_progress_still_works():
    """fake/旧工厂不接受 on_progress 时，start() 退回无参调用不报错。"""
    agent = LocalPipelineAgent(
        pipeline_factory=lambda: FakePipeline(), llm_chat=lambda *a: {}
    )
    asyncio.run(agent.start(lambda pcm: None))
    try:
        assert agent.output_rate == 22050
    finally:
        asyncio.run(agent.stop())
