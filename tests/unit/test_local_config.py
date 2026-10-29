"""锁定 physical-sim 提速默认值、显式关闭与 env 示例同步。"""

from pathlib import Path

import pytest
from dotenv import dotenv_values

from agentcall import config


@pytest.mark.parametrize(("key", "default"), [
    ("LOCAL_LLM_PROVIDER", "dashscope"),
    ("LOCAL_LLM_BASE_URL", "https://api.deepseek.com"),
    ("DEEPSEEK_API_KEY", ""),
    ("LOCAL_LLM_THINKING", "disabled"),
    ("LOCAL_ASR_SEGMENTED", "true"),
    ("LOCAL_ASR_SEGMENT_SILENCE", "0.25"),
    ("LOCAL_TURN_SILENCE", "0.6"),
    ("LOCAL_TTS_MODEL", "vits-icefall-zh-aishell3"),
    ("LOCAL_TTS_SPEAKER_ID", "25"),
    ("LOCAL_TTS_STREAMING", "true"),
    ("LOCAL_TTS_CACHE_PHRASES", ""),
    ("LOCAL_SLOW_REPLY_FILLER_SECONDS", "2.5"),
    ("LOCAL_SLOW_REPLY_FILLER", "嗯，您稍等。"),
    ("LOCAL_TTS_PEAK", ""),
    ("LOCAL_FAST_OPENING", "true"),
    ("LOCAL_INBOUND_GREETING", ""),
    ("LOCAL_LLM_PREWARM", "true"),
    ("LOCAL_LLM_PREWARM_IDLE", "15.0"),
    ("LOCAL_SPECULATIVE_LLM", "true"),
    ("LOCAL_LATENCY_METRICS", "true"),
    ("PARALLEL_AGENT_CONNECT", "true"),
])
def test_local_config_defaults_and_env_sync(monkeypatch, key, default):
    monkeypatch.delenv(key, raising=False)
    assert config.get_spec(key).default == default
    assert config.get_str(key) == default
    example = dotenv_values(Path(__file__).resolve().parents[2] / ".env.example")
    assert example[key] == default
    if key.startswith("LOCAL_SLOW_REPLY_FILLER"):
        linux = dotenv_values(Path(__file__).resolve().parents[2] / "scripts/linux/.env.example")
        assert linux[key] == default


@pytest.mark.parametrize("key", [
    "LOCAL_TTS_STREAMING", "LOCAL_ASR_SEGMENTED", "LOCAL_FAST_OPENING", "LOCAL_LLM_PREWARM",
    "LOCAL_SPECULATIVE_LLM", "LOCAL_LATENCY_METRICS", "PARALLEL_AGENT_CONNECT",
])
def test_speed_switches_allow_explicit_false(monkeypatch, key):
    monkeypatch.setenv(key, "false")
    assert config.get_bool(key) is False


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


def test_turn_silence_label_describes_physical_silence():
    label = config.get_spec("LOCAL_TURN_SILENCE").label
    assert "物理静音" in label and "含切段等待" in label
