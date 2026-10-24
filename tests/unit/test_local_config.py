"""Local pipeline switches must remain opt-in and stay in the env example."""

from pathlib import Path

import pytest
from dotenv import dotenv_values

from agentcall import config


@pytest.mark.parametrize(("key", "default"), [
    ("LOCAL_LLM_PROVIDER", "dashscope"),
    ("LOCAL_LLM_BASE_URL", "https://api.deepseek.com"),
    ("DEEPSEEK_API_KEY", ""),
    ("LOCAL_LLM_THINKING", "disabled"),
    ("LOCAL_TTS_MODEL", "vits-piper-zh_CN-chaowen-medium-int8"),
    ("LOCAL_TTS_SPEAKER_ID", "0"),
    ("LOCAL_TTS_STREAMING", "false"),
    ("LOCAL_TTS_CACHE_PHRASES", ""),
    ("LOCAL_TTS_PEAK", ""),
])
def test_local_config_defaults_and_env_sync(key, default):
    assert config.get_spec(key).default == default
    example = dotenv_values(Path(__file__).resolve().parents[2] / ".env.example")
    assert example[key] == default


def test_deepseek_credentials_do_not_require_dashscope(monkeypatch):
    monkeypatch.setenv("LOCAL_LLM_PROVIDER", "deepseek")
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    assert "DEEPSEEK_API_KEY" in config.validate_provider_credentials("local")[0]
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fake-key")
    assert config.validate_provider_credentials("local") == []
    assert config.get_spec("DEEPSEEK_API_KEY").secret
    monkeypatch.setenv("LOCAL_LLM_PROVIDER", "dashscope")
    assert "DASHSCOPE_API_KEY" in config.validate_provider_credentials("local")[0]
