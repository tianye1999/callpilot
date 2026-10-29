"""产品默认组合与显式关闭的运行验证，所有管线/网络均为 fake。"""

import asyncio

import pytest
from test_local_agent import FakePipeline, _wait_until
from test_local_segmented import SegmentedPipeline, end_turn, feed
from test_local_speculative import Stream

from agentcall import config
from agentcall.agents import local_agent


@pytest.mark.parametrize("provider", ["dashscope", "deepseek"])
def test_product_defaults_pipeline_and_provider_dependency(
    monkeypatch, product_latency_defaults, provider,
):
    monkeypatch.setenv("LOCAL_LLM_PROVIDER", provider)
    stream, requests, warmed = Stream(), [], []
    stream.release.set()
    stream.prewarm = lambda *a, **kw: warmed.append(True)
    monkeypatch.setattr(local_agent, "DeepSeekChat", lambda: stream)
    monkeypatch.setattr(local_agent, "_default_llm_chat",
                        lambda messages, *a: requests.append(messages) or {"role": "assistant", "content": "好的。"})
    agent = local_agent.LocalPipelineAgent(pipeline_factory=SegmentedPipeline)
    audio, events = [], []
    agent.set_turn_event_handler(events.append)
    asyncio.run(agent.start(audio.append))
    try:
        assert agent._streaming and agent._segmented_asr and agent._metrics
        assert config.get_bool("LOCAL_FAST_OPENING") and config.get_bool("PARALLEL_AGENT_CONNECT")
        assert (agent._spec_thread is not None) == (provider == "deepseek")
        assert (agent._warm_thread is not None) == (provider == "deepseek")
        feed(agent, 2)
        assert _wait_until(lambda: agent._asr_queue.unfinished_tasks == 0)
        if provider == "deepseek":
            assert _wait_until(lambda: stream.calls and warmed)
        else:
            assert not stream.calls and not warmed and not requests
        assert not audio
        end_turn(agent)
        assert _wait_until(lambda: audio)
        assert len(stream.calls if provider == "deepseek" else requests) == 1
        assert not agent.fatal and events
    finally:
        asyncio.run(agent.stop())


def test_speculation_without_segmented_asr_falls_back_to_normal_request(monkeypatch, product_latency_defaults):
    monkeypatch.setenv("LOCAL_LLM_PROVIDER", "deepseek")
    monkeypatch.setenv("LOCAL_ASR_SEGMENTED", "false")
    monkeypatch.setenv("LOCAL_LLM_PREWARM", "false")
    stream, audio = Stream(), []
    stream.release.set()
    monkeypatch.setattr(local_agent, "DeepSeekChat", lambda: stream)
    agent = local_agent.LocalPipelineAgent(pipeline_factory=FakePipeline)
    asyncio.run(agent.start(audio.append))
    try:
        assert not agent._speculative and agent._spec_thread is None
        asyncio.run(agent.send_audio(b"hello"))
        assert _wait_until(lambda: audio)
        assert len(stream.calls) == 1 and not agent.fatal
    finally:
        asyncio.run(agent.stop())


def test_explicit_false_preserves_whole_asr_whole_tts_and_no_background_work(monkeypatch):
    for key in ("LOCAL_TTS_STREAMING", "LOCAL_ASR_SEGMENTED", "LOCAL_FAST_OPENING", "LOCAL_LLM_PREWARM",
                "LOCAL_SPECULATIVE_LLM", "LOCAL_LATENCY_METRICS", "PARALLEL_AGENT_CONNECT"):
        monkeypatch.setenv(key, "false")
    pipeline, requests, events, audio = FakePipeline(), [], [], []
    reply = "好的，我会帮您处理。"
    agent = local_agent.LocalPipelineAgent(
        pipeline_factory=lambda: pipeline,
        llm_chat=lambda *a: requests.append(True) or {"role": "assistant", "content": reply},
    )
    agent.set_turn_event_handler(events.append)
    agent.prepare_opening("不应调用 LLM")
    assert not requests
    asyncio.run(agent.start(audio.append))
    try:
        assert agent._asr_thread is None and agent._tts_thread is None
        assert agent._warm_thread is None and agent._spec_thread is None
        asyncio.run(agent.send_audio(b"hello"))
        assert _wait_until(lambda: audio)
        assert pipeline.transcripts == ["hello"] and pipeline.synthesized == [reply]
        assert len(requests) == 1 and not events
        assert not hasattr(audio[0], "on_written")
    finally:
        asyncio.run(agent.stop())
