"""分诊判官的文本后端必须跟随 AGENT_PROVIDER。

回归：`_default_model_call` 曾硬编码 `provider="openai"`，与兄弟模块
（`dtmf_judge` / `summarizer` 均用 `text_backend_for_agent()`）不一致。
后果是 qwen/doubao/local 部署下，每次判定都因缺 OPENAI_API_KEY 失败，
`INBOUND_TRIAGE_MODE=enforce` 的来电永远拿不到裁决。
"""

from __future__ import annotations

import pytest

from agentcall import triage_judge


def _capture(monkeypatch) -> dict[str, str]:
    seen: dict[str, str] = {}

    def fake_call_text_model(messages, *, provider, model, **kwargs):
        seen["provider"] = provider
        seen["model"] = model
        return None, "stub"

    monkeypatch.setattr(triage_judge, "call_text_model", fake_call_text_model)
    return seen


@pytest.mark.parametrize(
    ("agent_provider", "expected_backend"),
    [
        ("qwen", "qwen"),
        ("doubao", "qwen"),  # 非 openai 的实时 provider 统一回落 qwen 文本后端
        ("local", "deepseek"),
        ("openai", "openai"),  # 既有 OpenAI 部署行为不得回归
    ],
)
def test_backend_follows_agent_provider(monkeypatch, agent_provider, expected_backend):
    seen = _capture(monkeypatch)
    monkeypatch.setenv("AGENT_PROVIDER", agent_provider)
    triage_judge._default_model_call([{"role": "user", "content": "x"}], 1.0)
    assert seen["provider"] == expected_backend


def test_model_is_resolved_for_the_selected_backend(monkeypatch):
    """model 必须随后端一起解析，否则会把 qwen 后端配上 openai 模型名。"""
    seen = _capture(monkeypatch)
    monkeypatch.setenv("AGENT_PROVIDER", "qwen")
    triage_judge._default_model_call([{"role": "user", "content": "x"}], 1.0)
    assert seen["model"], "必须解析出具体模型名"
    assert "gpt" not in seen["model"].lower(), (
        f"qwen 后端不应使用 OpenAI 模型: {seen['model']}"
    )
