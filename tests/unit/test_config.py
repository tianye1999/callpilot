"""config 模块单测：类型化读取、凭证校验、.env 写回与面板值。"""

from __future__ import annotations

import os

import pytest

from agentcall.config import (
    CONFIG_SPECS,
    get_bool,
    get_float,
    get_int,
    get_str,
    read_panel_values,
    update_env_file,
    validate_provider_credentials,
)


def _unset(monkeypatch, *keys):
    """确保这些环境变量在测试期间不存在，且测试结束后恢复原状。

    先 setenv 让 monkeypatch 记录原值（含「原本不存在」），再 delenv 删除；
    这样即使被测函数在测试中写入 os.environ，teardown 也会还原。
    """
    for key in keys:
        monkeypatch.setenv(key, "__tmp__")
        monkeypatch.delenv(key)


# ---- 类型化读取与默认值 ----


def test_get_str_default_and_env_override(monkeypatch):
    _unset(monkeypatch, "QWEN_VOICE")
    assert get_str("QWEN_VOICE") == "Raymond"
    monkeypatch.setenv("QWEN_VOICE", "Cherry")
    assert get_str("QWEN_VOICE") == "Cherry"


def test_get_int_default_and_env_override(monkeypatch):
    _unset(monkeypatch, "MODEM_BAUD")
    assert get_int("MODEM_BAUD") == 115200
    monkeypatch.setenv("MODEM_BAUD", "9600")
    assert get_int("MODEM_BAUD") == 9600


def test_get_int_invalid_env_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("RECORDING_RETENTION_DAYS", "abc")
    assert get_int("RECORDING_RETENTION_DAYS") == 30


def test_get_float_default_and_env_override(monkeypatch):
    _unset(monkeypatch, "MODEM_TX_GAIN")
    assert get_float("MODEM_TX_GAIN") == pytest.approx(1.0)
    monkeypatch.setenv("MODEM_TX_GAIN", "0.8")
    assert get_float("MODEM_TX_GAIN") == pytest.approx(0.8)


def test_get_bool_truthy_values(monkeypatch):
    for raw in ("true", "TRUE", "1", "yes", "Yes"):
        monkeypatch.setenv("RECORDING_ENABLED", raw)
        assert get_bool("RECORDING_ENABLED") is True, raw
    for raw in ("false", "0", "no", "banana", ""):
        monkeypatch.setenv("RECORDING_ENABLED", raw)
        assert get_bool("RECORDING_ENABLED") is False, raw


def test_get_bool_default(monkeypatch):
    _unset(monkeypatch, "SUMMARY_ENABLED", "MONITOR_AI_PLAYBACK")
    assert get_bool("SUMMARY_ENABLED") is True
    assert get_bool("MONITOR_AI_PLAYBACK") is False


def test_unknown_key_raises_keyerror():
    with pytest.raises(KeyError):
        get_str("NO_SUCH_KEY")


# ---- provider 凭证校验 ----


def test_validate_qwen_missing_and_present(monkeypatch):
    _unset(monkeypatch, "DASHSCOPE_API_KEY")
    errors = validate_provider_credentials("qwen")
    assert len(errors) == 1
    assert "DASHSCOPE_API_KEY" in errors[0]

    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-test")
    assert validate_provider_credentials("qwen") == []


def test_validate_doubao_requires_both_keys(monkeypatch):
    _unset(monkeypatch, "DOUBAO_APP_ID", "DOUBAO_ACCESS_KEY")
    errors = validate_provider_credentials("doubao")
    assert len(errors) == 2
    assert any("DOUBAO_APP_ID" in e for e in errors)
    assert any("DOUBAO_ACCESS_KEY" in e for e in errors)

    monkeypatch.setenv("DOUBAO_APP_ID", "app-1")
    errors = validate_provider_credentials("doubao")
    assert len(errors) == 1
    assert "DOUBAO_ACCESS_KEY" in errors[0]

    monkeypatch.setenv("DOUBAO_ACCESS_KEY", "ak-1")
    assert validate_provider_credentials("doubao") == []


def test_validate_unknown_provider(monkeypatch):
    errors = validate_provider_credentials("gpt")
    assert errors and "gpt" in errors[0]


# ---- update_env_file ----


def test_update_replaces_in_place_and_keeps_comments(tmp_path, monkeypatch):
    _unset(monkeypatch, "QWEN_VOICE")
    env = tmp_path / ".env"
    env.write_text(
        "# 模组配置\nMODEM_PORT=/dev/old\n\n# 音色\nQWEN_VOICE=Cherry\n",
        encoding="utf-8",
    )

    updated = update_env_file({"QWEN_VOICE": "Raymond"}, env_path=env)

    assert updated == ["QWEN_VOICE"]
    assert env.read_text(encoding="utf-8").splitlines() == [
        "# 模组配置",
        "MODEM_PORT=/dev/old",
        "",
        "# 音色",
        "QWEN_VOICE=Raymond",
    ]
    assert os.environ["QWEN_VOICE"] == "Raymond"


def test_update_appends_new_key_at_end(tmp_path, monkeypatch):
    _unset(monkeypatch, "MODEM_TX_GAIN")
    env = tmp_path / ".env"
    env.write_text("# 只有注释\nQWEN_VOICE=Cherry\n", encoding="utf-8")

    updated = update_env_file({"MODEM_TX_GAIN": "0.5"}, env_path=env)

    assert updated == ["MODEM_TX_GAIN"]
    lines = env.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "# 只有注释"
    assert lines[-1] == "MODEM_TX_GAIN=0.5"
    assert os.environ["MODEM_TX_GAIN"] == "0.5"


def test_update_creates_missing_file(tmp_path, monkeypatch):
    _unset(monkeypatch, "SUMMARY_MODEL")
    env = tmp_path / ".env"

    updated = update_env_file({"SUMMARY_MODEL": "qwen-max"}, env_path=env)

    assert updated == ["SUMMARY_MODEL"]
    assert env.read_text(encoding="utf-8") == "SUMMARY_MODEL=qwen-max\n"


def test_update_rejects_non_editable_key(tmp_path, monkeypatch):
    _unset(monkeypatch, "WEB_HOST", "QWEN_VOICE")
    env = tmp_path / ".env"
    env.write_text("# 原文\n", encoding="utf-8")

    with pytest.raises(ValueError):
        update_env_file({"QWEN_VOICE": "Cherry", "WEB_HOST": "0.0.0.0"}, env_path=env)

    # 整批拒绝：文件与环境都不应被改动
    assert env.read_text(encoding="utf-8") == "# 原文\n"
    assert "QWEN_VOICE" not in os.environ
    assert "WEB_HOST" not in os.environ


def test_update_rejects_unknown_key(tmp_path):
    env = tmp_path / ".env"
    with pytest.raises(ValueError):
        update_env_file({"NO_SUCH_KEY": "x"}, env_path=env)
    assert not env.exists()


def test_update_rejects_invalid_select_and_int(tmp_path):
    env = tmp_path / ".env"
    with pytest.raises(ValueError):
        update_env_file({"AGENT_PROVIDER": "gpt"}, env_path=env)
    with pytest.raises(ValueError):
        update_env_file({"RECORDING_RETENTION_DAYS": "many"}, env_path=env)
    assert not env.exists()


def test_update_quotes_value_with_spaces(tmp_path, monkeypatch):
    _unset(monkeypatch, "MONITOR_OUTPUT_DEVICE")
    env = tmp_path / ".env"

    update_env_file({"MONITOR_OUTPUT_DEVICE": "MacBook Air 扬声器"}, env_path=env)

    assert env.read_text(encoding="utf-8") == 'MONITOR_OUTPUT_DEVICE="MacBook Air 扬声器"\n'
    assert os.environ["MONITOR_OUTPUT_DEVICE"] == "MacBook Air 扬声器"


# ---- read_panel_values ----


def test_panel_covers_all_specs_and_fields():
    rows = read_panel_values()
    assert len(rows) == len(CONFIG_SPECS)
    for row in rows:
        assert {"key", "label", "kind", "default", "choices", "editable",
                "secret", "requires_restart", "value"} <= set(row)


def test_panel_masks_secret_value(monkeypatch):
    _unset(monkeypatch, "DASHSCOPE_API_KEY")
    rows = {row["key"]: row for row in read_panel_values()}
    assert rows["DASHSCOPE_API_KEY"]["value"] == "未设置"
    assert rows["DASHSCOPE_API_KEY"]["editable"] is False

    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-secret")
    rows = {row["key"]: row for row in read_panel_values()}
    assert rows["DASHSCOPE_API_KEY"]["value"] == "已设置"
    assert "sk-secret" not in str(rows["DASHSCOPE_API_KEY"])


def test_panel_reflects_env_value(monkeypatch):
    monkeypatch.setenv("QWEN_VOICE", "Cherry")
    rows = {row["key"]: row for row in read_panel_values()}
    assert rows["QWEN_VOICE"]["value"] == "Cherry"
    assert rows["QWEN_VOICE"]["default"] == "Raymond"
