import io
import json

import pytest

from agentcall import config
from agentcall.agents import local_llm
from agentcall.agents.local_agent import LocalPipelineAgent


def test_dashscope_wire(monkeypatch):
    monkeypatch.setenv("LOCAL_LLM_PROVIDER", "dashscope")
    monkeypatch.setenv("LOCAL_LLM_THINKING", "disabled")
    monkeypatch.delenv("LOCAL_LLM_MODEL", raising=False)
    monkeypatch.delenv("LOCAL_LLM_DASHSCOPE_BASE_URL", raising=False)
    monkeypatch.setenv("LOCAL_LLM_BASE_URL", "https://old-deepseek.example")
    history = [{"role": "assistant", "content": "ok", "reasoning_content": "private"}]
    tools = [{"type": "function", "function": {"name": "send_dtmf"}}]
    body = local_llm.chat_body(history, tools)
    assert body == {"model": "qwen3.6-flash", "messages": [{"role": "assistant", "content": "ok"}],
                    "stream": True, "enable_thinking": False, "tools": tools}
    assert "reasoning_content" in history[0]
    assert local_llm.chat_endpoint() == "https://dashscope.aliyuncs.com/compatible-mode/v1"
    assert config.get_spec("LOCAL_LLM_DASHSCOPE_BASE_URL").default == local_llm.chat_endpoint()
    agent = LocalPipelineAgent()
    assert agent._deepseek is not None
    agent._deepseek.close()


@pytest.mark.parametrize("thinking", ["low", "high"])
def test_dashscope_rejects_thinking(monkeypatch, thinking):
    monkeypatch.setenv("LOCAL_LLM_PROVIDER", "dashscope")
    monkeypatch.setenv("LOCAL_LLM_THINKING", thinking)
    with pytest.raises(ValueError, match="disabled"):
        local_llm.chat_body([], None)


@pytest.mark.parametrize("url", ["http://example.com", "https://user:pass@example.com", "https://x/?key=secret"])
def test_dashscope_requires_https_root(monkeypatch, url):
    monkeypatch.setenv("LOCAL_LLM_PROVIDER", "dashscope")
    monkeypatch.setenv("LOCAL_LLM_DASHSCOPE_BASE_URL", url)
    with pytest.raises(ValueError, match="HTTPS"):
        local_llm.chat_endpoint()


def test_qwen_stream_usage_null_content_and_tool_fragments():
    events = [
        {"choices": [{"delta": {"role": "assistant", "content": ""}}]},
        {"choices": [{"delta": {"content": None, "tool_calls": [
            {"index": 0, "id": "call_1", "type": "function",
             "function": {"name": "send_dtmf", "arguments": ""}}]}}]},
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "function": {"arguments": '{"digits":'}}]}}]},
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "function": {"arguments": '"2#"}'}}]}, "finish_reason": "tool_calls"}]},
        {"choices": [], "usage": {"total_tokens": 20}},
    ]
    wire = "".join(f"data: {json.dumps(e)}\n\n" for e in events) + "data: [DONE]\n\n"
    seen = []
    tools_seen = []
    result = local_llm.read_sse(io.BytesIO(wire.encode()), seen.append, lambda: tools_seen.append(True))
    assert seen == []
    assert tools_seen == [True]
    assert result["tool_calls"][0]["function"] == {"name": "send_dtmf", "arguments": '{"digits":"2#"}'}

def test_dashscope_uses_dashscope_key_not_deepseek(monkeypatch):
    monkeypatch.setenv("LOCAL_LLM_PROVIDER", "dashscope")
    monkeypatch.setenv("LOCAL_LLM_THINKING", "disabled")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-deepseek-only")
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    client = local_llm.DeepSeekChat()
    with pytest.raises(ValueError, match="缺少 DASHSCOPE_API_KEY"):
        client.chat([], None, 1.0, lambda text: None)
    client.close()


def test_deepseek_uses_deepseek_key(monkeypatch):
    monkeypatch.setenv("LOCAL_LLM_PROVIDER", "deepseek")
    monkeypatch.setenv("LOCAL_LLM_THINKING", "disabled")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    client = local_llm.DeepSeekChat()
    with pytest.raises(ValueError, match="缺少 DEEPSEEK_API_KEY"):
        client.chat([], None, 1.0, lambda text: None)
    client.close()


def test_deepseek_body_regression(monkeypatch):
    monkeypatch.setenv("LOCAL_LLM_PROVIDER", "deepseek")
    monkeypatch.setenv("LOCAL_LLM_THINKING", "disabled")
    monkeypatch.setenv("LOCAL_LLM_MODEL", "deepseek-flash")
    history = [{"role": "user", "content": "你好"}]
    body = local_llm.chat_body(history, None)
    assert body == {"model": "deepseek-flash", "messages": history, "stream": True,
                    "thinking": {"type": "disabled"}}
    assert "enable_thinking" not in body


def test_deepseek_thinking_high_preserves_wire_contract(monkeypatch):
    monkeypatch.setenv("LOCAL_LLM_PROVIDER", "deepseek")
    monkeypatch.setenv("LOCAL_LLM_THINKING", "high")
    monkeypatch.setenv("LOCAL_LLM_MODEL", "deepseek-flash")
    tools = [{"type": "function", "function": {"name": "send_dtmf"}}]
    history = [{"role": "assistant", "content": "ok", "reasoning_content": "thought"}]
    body = local_llm.chat_body(history, tools)
    assert body["thinking"] == {"type": "enabled"}
    assert body["reasoning_effort"] == "high"
    # With tools, DeepSeek must keep prior reasoning on the wire.
    assert body["messages"][0] == history[0]


def test_verify_model_skips_for_dashscope(monkeypatch):
    monkeypatch.setenv("LOCAL_LLM_PROVIDER", "dashscope")
    ok, message = local_llm.verify_model()
    assert ok is True
    assert "跳过" in message

