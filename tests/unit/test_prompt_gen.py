"""动态场景提示词生成：轻量模型调用、缓存与兜底。"""

from __future__ import annotations

import json
from types import SimpleNamespace

import dashscope

from agentcall import prompt_gen


def make_dashscope_response(content: str, status_code: int = 200) -> SimpleNamespace:
    return SimpleNamespace(
        status_code=status_code,
        message="",
        output=SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
        ),
    )


def test_build_messages_contain_number_task_and_no_code_mapping():
    messages = prompt_gen.build_prompt_messages(
        "10000", "查询流量", "zh", owner="李明", persona="数字分身"
    )

    text = "\n".join(msg["content"] for msg in messages)
    assert "10000" in text
    assert "查询流量" in text
    assert "李明" in text
    assert "数字分身" in text
    assert "自己判断" in text
    assert "严禁虚构任何身份、公司或人名" in messages[0]["content"]
    assert "映射" not in text


def test_text_model_auto_selection_tracks_provider_and_honors_override():
    assert prompt_gen.select_text_model("qwen", "") == "qwen-plus"
    assert prompt_gen.select_text_model("openai", "") == "gpt-4o-mini"
    assert prompt_gen.select_text_model("openai", "gpt-4.1-mini") == "gpt-4.1-mini"


def test_qwen_prompt_generation_truncates_and_caches(monkeypatch):
    prompt_gen.clear_cache()
    calls: list[dict] = []
    long_text = "开门见山说查流量。" * 30
    payload = json.dumps(
        {"scenario": long_text, "opening": "查一下本机流量"},
        ensure_ascii=False,
    )

    def fake_call(**kwargs):
        calls.append(kwargs)
        return make_dashscope_response(payload)

    monkeypatch.setenv("PROMPT_GEN_ENABLED", "true")
    monkeypatch.setenv("AGENT_PROVIDER", "qwen")
    monkeypatch.setenv("PROMPT_GEN_MODEL", "qwen-test")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-test")
    monkeypatch.setattr(dashscope.Generation, "call", staticmethod(fake_call))

    first = prompt_gen.generate_prompt_scenario("10000", "查询流量", "zh", timeout=1)
    second = prompt_gen.generate_prompt_scenario("10000", "查询流量", "zh", timeout=1)

    assert first["ok"] is True
    assert first["scenario"] == long_text[:200]
    assert first["opening"] == "查一下本机流量"
    assert first["model"] == "qwen-test"
    assert first["cached"] is False
    assert second["ok"] is True
    assert second["cached"] is True
    assert second["opening"] == "查一下本机流量"
    assert len(calls) == 1


def test_prompt_generation_plain_text_falls_back_to_scenario_only(monkeypatch):
    prompt_gen.clear_cache()

    monkeypatch.setenv("PROMPT_GEN_ENABLED", "true")
    monkeypatch.setenv("AGENT_PROVIDER", "qwen")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-test")
    monkeypatch.setattr(
        dashscope.Generation,
        "call",
        staticmethod(lambda **kw: make_dashscope_response("直接说明来意，别自我介绍。")),
    )

    result = prompt_gen.generate_prompt_scenario("13800000000", "确认时间", "zh", timeout=1)

    assert result["ok"] is True
    assert result["scenario"] == "直接说明来意，别自我介绍。"
    assert result["opening"] == ""


def test_openai_prompt_generation_uses_chat_completions(monkeypatch):
    prompt_gen.clear_cache()
    captured: dict = {}

    def fake_http(url, *, method="GET", headers=None, body=None, timeout=5.0):
        captured.update({
            "url": url,
            "method": method,
            "headers": headers,
            "body": json.loads((body or b"").decode("utf-8")),
            "timeout": timeout,
        })
        payload = {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {"scenario": "直接说要查询账单。", "opening": "查询账单"},
                            ensure_ascii=False,
                        )
                    }
                }
            ]
        }
        return 200, json.dumps(payload).encode("utf-8")

    monkeypatch.setenv("AGENT_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.delenv("PROMPT_GEN_MODEL", raising=False)
    monkeypatch.setattr(prompt_gen, "_http_request_json", fake_http)

    result = prompt_gen.generate_prompt_scenario("95555", "查询账单", "zh", timeout=1)

    assert result["ok"] is True
    assert result["scenario"] == "直接说要查询账单。"
    assert result["opening"] == "查询账单"
    assert result["model"] == "gpt-4o-mini"
    assert captured["url"] == "https://api.openai.com/v1/chat/completions"
    assert captured["method"] == "POST"
    assert captured["headers"]["Authorization"] == "Bearer sk-test"
    assert captured["body"]["model"] == "gpt-4o-mini"


def test_prompt_generation_disabled_and_exceptions_are_fallbacks(monkeypatch):
    prompt_gen.clear_cache()
    monkeypatch.setenv("PROMPT_GEN_ENABLED", "false")
    disabled = prompt_gen.generate_prompt_scenario("10000", "查询流量", "zh", timeout=1)
    assert disabled["ok"] is False
    assert "关闭" in disabled["error"]

    monkeypatch.setenv("PROMPT_GEN_ENABLED", "true")
    monkeypatch.setenv("AGENT_PROVIDER", "qwen")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-test")
    monkeypatch.setattr(
        dashscope.Generation,
        "call",
        staticmethod(lambda **kw: (_ for _ in ()).throw(RuntimeError("api down"))),
    )
    failed = prompt_gen.generate_prompt_scenario("10000", "查询流量", "zh", timeout=1)
    assert failed["ok"] is False
    assert "api down" in failed["error"]
