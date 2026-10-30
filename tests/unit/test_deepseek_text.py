"""Auxiliary local text calls must not depend on the voice provider's SDK."""

from __future__ import annotations

import io
import json
import threading
import urllib.error

import dashscope
import pytest
from fakes import FakeModem

from agentcall import (
    call_playbooks,
    config,
    dtmf_judge,
    prompt_gen,
    result_verification,
    summarizer,
    task_intake,
    triage_compliance,
    triage_judge,
)
from agentcall.call_agent import CallAgentService


@pytest.fixture(params=["local", "moss"])
def deepseek(monkeypatch, request):
    monkeypatch.setenv("AGENT_PROVIDER", request.param)
    monkeypatch.setenv("MOSS_API_KEY", "fake-moss-key")
    monkeypatch.setenv("LOCAL_LLM_PROVIDER", "deepseek")
    monkeypatch.setenv("LOCAL_LLM_MODEL", "deepseek-flash")
    monkeypatch.setenv("LOCAL_LLM_BASE_URL", "https://deepseek.invalid/v1/")
    monkeypatch.setenv("LOCAL_LLM_THINKING", "disabled")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fake-deepseek-key")
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    for key in ("PROMPT_GEN_MODEL", "SUMMARY_MODEL", "DTMF_JUDGE_MODEL", "INTAKE_MODEL"):
        monkeypatch.delenv(key, raising=False)

    def forbidden(**kwargs):
        pytest.fail("local/moss+deepseek must never call DashScope")

    monkeypatch.setattr(dashscope.Generation, "call", staticmethod(forbidden))
    prompt_gen.clear_cache()
    yield request.param
    prompt_gen.clear_cache()


@pytest.mark.parametrize("agent,expected", [
    ("qwen", "qwen"), ("openai", "openai"), ("doubao", "qwen"),
    ("local", None), ("moss", None),
])
@pytest.mark.parametrize("local", ["dashscope", "deepseek"])
def test_text_backend_routing_matrix(monkeypatch, agent, expected, local):
    monkeypatch.setenv("AGENT_PROVIDER", agent)
    monkeypatch.setenv("LOCAL_LLM_PROVIDER", local)
    backend = expected or ("deepseek" if local == "deepseek" else "qwen")
    assert prompt_gen.text_backend_for_agent() == backend
    assert prompt_gen.text_backend_for_agent(agent) == backend


@pytest.mark.parametrize("thinking", ["disabled", "low", "high"])
@pytest.mark.parametrize("override", ["", "explicit-model"])
def test_deepseek_request_contract(deepseek, monkeypatch, thinking, override):
    monkeypatch.setenv("LOCAL_LLM_THINKING", thinking)
    messages = [{"role": "user", "content": "hello"}]
    seen = []

    def http(url, **kwargs):
        seen.append((url, kwargs))
        return 200, b'{"choices":[{"message":{"content":"ok"}}]}'

    monkeypatch.setattr(prompt_gen, "_http_request_json", http)
    assert prompt_gen.select_text_model("deepseek", override) == (override or "deepseek-flash")
    assert prompt_gen.call_text_model(
        messages, provider="deepseek", model=override, timeout=0.5, max_tokens=923,
    ) == ("ok", None)
    url, request = seen[0]
    body = json.loads(request["body"])
    assert url == "https://deepseek.invalid/v1/chat/completions"
    assert request["method"] == "POST"
    assert request["headers"]["Authorization"] == "Bearer fake-deepseek-key"
    assert request["timeout"] == 0.5
    assert body["model"] == (override or "deepseek-flash")
    assert body["messages"] == messages
    assert body["stream"] is False
    assert body["max_tokens"] == 923
    assert body["thinking"] == {"type": "disabled" if thinking == "disabled" else "enabled"}
    assert body.get("reasoning_effort") == (None if thinking == "disabled" else thinking)


@pytest.mark.parametrize("status,body,error", [
    (401, b"unauthorized", "401"),
    (429, b"rate limited", "429"),
    (200, b"not json", "解析失败"),
    (200, b"[]", "解析失败"),
    (200, b'{"choices":[]}', "解析失败"),
    (200, b'{"choices":[{"message":{"content":""}}]}', "没有文本"),
])
def test_deepseek_errors_return_tuple(deepseek, monkeypatch, status, body, error):
    monkeypatch.setattr(prompt_gen, "_http_request_json", lambda *a, **kw: (status, body))
    text, message = prompt_gen.call_text_model([], provider="deepseek", model="", timeout=0.5)
    assert text is None and error in message


@pytest.mark.parametrize("exception,error", [
    (urllib.error.HTTPError("https://deepseek.invalid", 503, "busy", {}, io.BytesIO(b"busy")), "503"),
    (urllib.error.URLError("offline"), "offline"),
    (TimeoutError("socket timeout"), "socket timeout"),
])
def test_deepseek_transport_errors(deepseek, monkeypatch, exception, error):
    def http(*args, **kwargs):
        raise exception

    monkeypatch.setattr(prompt_gen, "_http_request_json", http)
    text, message = prompt_gen.call_text_model([], provider="deepseek", model="", timeout=0.5)
    assert text is None and error in message


def test_deepseek_missing_key_and_invalid_thinking(deepseek, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY")
    assert "DEEPSEEK_API_KEY" in prompt_gen.call_text_model(
        [], provider="deepseek", model="", timeout=0.5,
    )[1]
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fake-key")
    monkeypatch.setenv("LOCAL_LLM_THINKING", "invalid")
    assert "LOCAL_LLM_THINKING" in prompt_gen.call_text_model(
        [], provider="deepseek", model="", timeout=0.5,
    )[1]


def test_deepseek_hard_timeout_does_not_wait_for_transport(deepseek, monkeypatch):
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()

    def http(*args, **kwargs):
        entered.set()
        try:
            assert release.wait(1)
            return 200, b"{}"
        finally:
            finished.set()

    monkeypatch.setattr(prompt_gen, "_http_request_json", http)
    try:
        text, error = prompt_gen.call_text_model([], provider="deepseek", model="", timeout=0.02)
        assert entered.is_set() and not finished.is_set()
        assert text is None and "超时" in error
    finally:
        release.set()
        assert finished.wait(1)


@pytest.mark.parametrize("caller,tokens", [
    ("prompt", 160), ("summary", 600), ("wrap_up", 160), ("playbooks", 900),
    ("intake", 900), ("dtmf", 180), ("triage", 180),
    ("compliance", 60), ("relevance", 60), ("call_agent", 160),
])
@pytest.mark.parametrize("overrides", [False, True])
def test_all_auxiliary_call_sites_use_deepseek(deepseek, monkeypatch, caller, tokens, overrides):
    monkeypatch.setenv("PROMPT_GEN_ENABLED", "true")
    monkeypatch.setenv("CALL_PLAYBOOKS_ENABLED", "true")
    monkeypatch.setenv("NUMBER_PROFILES_ENABLED", "false")
    monkeypatch.setenv("DTMF_JUDGE_MODE", "shadow")
    if overrides:
        for key in ("PROMPT_GEN_MODEL", "SUMMARY_MODEL", "DTMF_JUDGE_MODEL", "INTAKE_MODEL"):
            monkeypatch.setenv(key, "override-model")
    seen = []
    content = json.dumps({
        "scenario": "直接说查询需求", "opening": "查询流量", "summary": "通话摘要",
        "decision": "wrap_up", "reason": "完成", "new_required_info": [], "ivr_notes_update": None,
        "reply": "请提供号码", "ready": False, "draft": None,
    }, ensure_ascii=False)

    def http(url, **kwargs):
        assert url == "https://deepseek.invalid/v1/chat/completions"
        assert kwargs["headers"]["Authorization"] == "Bearer fake-deepseek-key"
        seen.append(json.loads(kwargs["body"]))
        return 200, json.dumps({"choices": [{"message": {"content": content}}]}).encode()

    monkeypatch.setattr(prompt_gen, "_http_request_json", http)
    transcripts = [("user", "查询流量"), ("agent", "稍等"), ("user", "好")]
    if caller == "prompt":
        assert prompt_gen.generate_prompt_scenario("10000", "查询流量", "zh")["ok"]
    elif caller == "summary":
        assert summarizer.summarize_call(transcripts, direction="outbound", number="10000")["ok"]
    elif caller == "wrap_up":
        assert summarizer.judge_wrap_up(transcripts, "查询流量")["decision"] == "wrap_up"
    elif caller == "playbooks":
        call_playbooks.learn_from_call("10000", transcripts, None, call_id="test", updated="now")
    elif caller == "intake":
        assert task_intake.intake_step([{"role": "user", "content": "查询流量"}], owner="owner")["ok"]
    elif caller == "dtmf":
        assert dtmf_judge._default_model_call([], "qwen-plus", 0.5) == (content, None)
    elif caller == "triage":
        assert triage_judge._default_model_call([], 0.5) == (content, None)
    elif caller == "compliance":
        assert triage_compliance._default_compliance_call([]) == (content, None)
    elif caller == "relevance":
        assert result_verification._default_relevance_call([]) == (content, None)
    else:
        service = CallAgentService("unused", "unused", provider=deepseek, modem=FakeModem())
        service.session._outbound_number = "10000"
        service.session._outbound_task_value = "查询流量"
        service.session._start_prompt_generation()
        thread = service.session._prompt_gen_thread
        assert thread is not None
        thread.join(1)
        assert not thread.is_alive()
        assert service.session._take_prompt_scenario() == "直接说查询需求"
    assert len(seen) == 1
    overridable = caller not in {"triage", "compliance", "relevance"}
    assert seen[0]["model"] == ("override-model" if overrides and overridable else "deepseek-flash")
    assert seen[0]["max_tokens"] == tokens


def test_deepseek_shadow_credentials_and_setup(deepseek, monkeypatch):
    monkeypatch.setenv("DTMF_JUDGE_MODE", "shadow")
    assert config.validate_provider_credentials(deepseek) == []
    assert config.credential_status()["ok"] is True
    assert config.any_provider_credentials_ready() is True
    monkeypatch.setenv("SETUP_DONE", "true")
    assert config.setup_required() is False


@pytest.mark.parametrize("llm_provider,text_key,wrong_key", [
    ("deepseek", "DEEPSEEK_API_KEY", "DASHSCOPE_API_KEY"),
    ("dashscope", "DASHSCOPE_API_KEY", "DEEPSEEK_API_KEY"),
])
def test_moss_shadow_requires_selected_text_key(monkeypatch, llm_provider, text_key, wrong_key):
    monkeypatch.setenv("MOSS_API_KEY", "fake-moss-key")
    monkeypatch.setenv("LOCAL_LLM_PROVIDER", llm_provider)
    monkeypatch.setenv("DTMF_JUDGE_MODE", "shadow")
    monkeypatch.delenv(text_key, raising=False)
    monkeypatch.setenv(wrong_key, "wrong-backend-key")
    assert config.validate_provider_credentials("moss") == [f"缺少环境变量 {text_key}（DTMF 文本判官 shadow 必需）"]
    monkeypatch.setenv(text_key, "fake-text-key")
    monkeypatch.delenv(wrong_key)
    assert config.validate_provider_credentials("moss") == []
    monkeypatch.delenv("MOSS_API_KEY")
    assert config.validate_provider_credentials("moss") == ["缺少环境变量 MOSS_API_KEY（moss 必需）"]
