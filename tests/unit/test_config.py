"""config 模块单测：类型化读取、凭证校验、.env 写回与面板值。"""

from __future__ import annotations

import codecs
import os
import re
import threading
import time
from pathlib import Path

import pytest

from agentcall import config, platforms
from agentcall.config import (
    CONFIG_SPECS,
    app_support_dir,
    call_log_dir,
    data_dir,
    env_file_path,
    get_bool,
    get_float,
    get_int,
    get_spec,
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


def test_agent_uplink_gain_default_and_env_example(monkeypatch):
    """#80-E:模型输入增益默认不改变音频，并有可发现的示例配置。"""
    _unset(monkeypatch, "AGENT_UPLINK_GAIN")
    example = (Path(__file__).resolve().parents[2] / ".env.example").read_text(
        encoding="utf-8"
    )

    assert get_float("AGENT_UPLINK_GAIN") == pytest.approx(1.0)
    assert get_spec("AGENT_UPLINK_GAIN").requires_restart is False
    assert re.search(r"^AGENT_UPLINK_GAIN=1\.0$", example, re.MULTILINE)


def test_get_bool_truthy_values(monkeypatch):
    for raw in ("true", "TRUE", "1", "yes", "Yes"):
        monkeypatch.setenv("RECORDING_ENABLED", raw)
        assert get_bool("RECORDING_ENABLED") is True, raw
    for raw in ("false", "0", "no", "banana", ""):
        monkeypatch.setenv("RECORDING_ENABLED", raw)
        assert get_bool("RECORDING_ENABLED") is False, raw


def test_get_bool_default(monkeypatch):
    _unset(monkeypatch, "SUMMARY_ENABLED", "MONITOR_AI_PLAYBACK", "RECORDING_ENABLED")
    assert get_bool("SUMMARY_ENABLED") is True
    assert get_bool("MONITOR_AI_PLAYBACK") is False
    assert get_bool("RECORDING_ENABLED") is False


def test_recording_registry_and_env_example_default_to_off():
    example = (Path(__file__).resolve().parents[2] / ".env.example").read_text(
        encoding="utf-8"
    )

    assert get_spec("RECORDING_ENABLED").default == "false"
    assert re.search(r"^RECORDING_ENABLED=false$", example, re.MULTILINE)


def test_dtmf_judge_registry_defaults_off_and_rejects_enforce(tmp_path, monkeypatch):
    _unset(monkeypatch, "DTMF_JUDGE_MODE", "DTMF_JUDGE_MODEL")
    example = (Path(__file__).resolve().parents[2] / ".env.example").read_text(
        encoding="utf-8"
    )

    mode_spec = get_spec("DTMF_JUDGE_MODE")
    assert mode_spec.kind == "select"
    assert mode_spec.default == "off"
    assert mode_spec.choices == ("off", "shadow")
    assert mode_spec.requires_restart is False
    assert get_str("DTMF_JUDGE_MODEL") == ""
    assert re.search(r"^DTMF_JUDGE_MODE=off$", example, re.MULTILINE)
    assert re.search(r"^DTMF_JUDGE_MODEL=$", example, re.MULTILINE)

    with pytest.raises(ValueError, match="off, shadow"):
        update_env_file(
            {"DTMF_JUDGE_MODE": "enforce"}, env_path=tmp_path / ".env"
        )


def test_shadow_judge_uses_selected_text_backend_credentials(monkeypatch):
    monkeypatch.setenv("DTMF_JUDGE_MODE", "shadow")
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)

    assert validate_provider_credentials("openai") == []

    monkeypatch.setenv("DASHSCOPE_API_KEY", "qwen-key")
    assert validate_provider_credentials("qwen") == []


def test_default_provider_and_text_models_match_env_example(monkeypatch):
    _unset(monkeypatch, "AGENT_PROVIDER", "SUMMARY_MODEL", "DTMF_JUDGE_MODEL")
    example = (Path(__file__).resolve().parents[2] / ".env.example").read_text(
        encoding="utf-8"
    )

    assert get_str("AGENT_PROVIDER") == "openai"
    assert get_str("SUMMARY_MODEL") == ""
    assert get_str("DTMF_JUDGE_MODEL") == ""
    assert re.search(r"^AGENT_PROVIDER=openai$", example, re.MULTILINE)
    assert re.search(r"^SUMMARY_MODEL=$", example, re.MULTILINE)
    assert re.search(r"^DTMF_JUDGE_MODEL=$", example, re.MULTILINE)


@pytest.mark.parametrize(("raw", "expected"), [("true", True), ("false", False)])
def test_recording_explicit_config_is_respected(monkeypatch, raw, expected):
    monkeypatch.setenv("RECORDING_ENABLED", raw)

    assert get_bool("RECORDING_ENABLED") is expected
    row = next(item for item in read_panel_values() if item["key"] == "RECORDING_ENABLED")
    assert row["value"] == raw
    assert row["configured"] is True


def test_recording_panel_marks_default_as_not_explicit(monkeypatch):
    _unset(monkeypatch, "RECORDING_ENABLED")

    row = next(item for item in read_panel_values() if item["key"] == "RECORDING_ENABLED")
    assert row["value"] == "false"
    assert row["configured"] is False


def test_unknown_key_raises_keyerror():
    with pytest.raises(KeyError):
        get_str("NO_SUCH_KEY")


def test_modem_defaults_follow_platforms(monkeypatch):
    """MODEM_PORT/MODEM_AUDIO_MODE 默认值必须与 platforms 单一出处一致。"""
    _unset(monkeypatch, "MODEM_PORT", "MODEM_AUDIO_MODE")
    assert get_str("MODEM_PORT") == platforms.default_modem_port()
    assert get_str("MODEM_AUDIO_MODE") == platforms.default_audio_mode()
    # 音频模式的三个可选值不因平台默认变化而缩水
    assert get_spec("MODEM_AUDIO_MODE").choices == ("uac_ffmpeg", "uac", "nmea")


def test_runtime_paths_default_to_project_cwd_or_env_override(tmp_path, monkeypatch):
    _unset(
        monkeypatch,
        "AGENTCALL_APP_SUPPORT_DIR",
        "AGENTCALL_ENV_FILE",
        "AGENTCALL_DATA_DIR",
        "AGENTCALL_LOG_DIR",
        "CALL_LOG_DIR",
    )
    monkeypatch.chdir(tmp_path)

    assert app_support_dir() == tmp_path
    assert env_file_path() == tmp_path / ".env"
    assert data_dir() == tmp_path / "data"
    assert call_log_dir() == tmp_path / "data" / "recordings"

    monkeypatch.setenv("AGENTCALL_APP_SUPPORT_DIR", str(tmp_path / "Support"))
    monkeypatch.setenv("AGENTCALL_ENV_FILE", str(tmp_path / "Support" / ".env"))
    monkeypatch.setenv("AGENTCALL_DATA_DIR", str(tmp_path / "Support" / "Data"))
    monkeypatch.setenv("CALL_LOG_DIR", str(tmp_path / "Support" / "Calls"))
    assert app_support_dir() == tmp_path / "Support"
    assert env_file_path() == tmp_path / "Support" / ".env"
    assert data_dir() == tmp_path / "Support" / "Data"
    assert call_log_dir() == tmp_path / "Support" / "Calls"


def test_call_log_and_dial_queue_follow_config_semantics(tmp_path, monkeypatch):
    """call_log/dial_queue 的 from_env 解析必须与 config 判定逐值一致。

    历史上 call_log 自带的真值集合多了 ``on``，导致 RECORDING_ENABLED=on
    在录音层为真、在设置面板为假；本测试锁定三个模块共用同一套语义。
    """
    from agentcall.call_log import CallLogger
    from agentcall.dial_queue import DialQueue

    monkeypatch.setenv("CALL_LOG_DIR", str(tmp_path / "calls"))
    for raw in ("true", "TRUE", "1", "yes", "on", "ON", "false", "0", "no", ""):
        monkeypatch.setenv("RECORDING_ENABLED", raw)
        assert CallLogger.from_env().recording_enabled is get_bool("RECORDING_ENABLED"), raw

    monkeypatch.setenv("DIAL_WHITELIST", "138*, 10086 ,")
    monkeypatch.setenv("DIAL_INTERVAL_SECONDS", "not-a-number")
    queue = DialQueue.from_env(lambda number: (True, None))
    assert queue._interval == get_float("DIAL_INTERVAL_SECONDS")
    assert queue._whitelist == tuple(
        part.strip() for part in get_str("DIAL_WHITELIST").split(",") if part.strip()
    )


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


def test_validate_openai_missing_and_present(monkeypatch):
    _unset(monkeypatch, "OPENAI_API_KEY")
    errors = validate_provider_credentials("openai")
    assert len(errors) == 1
    assert "OPENAI_API_KEY" in errors[0]

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    assert validate_provider_credentials("openai") == []


def test_validate_unknown_provider(monkeypatch):
    errors = validate_provider_credentials("gpt")
    assert errors and "gpt" in errors[0]


# ---- OpenAI provider 注册项 ----


def test_openai_registered_defaults(monkeypatch):
    """OpenAI provider 注册项的默认值与下拉 choices。"""
    _unset(monkeypatch, "OPENAI_REALTIME_MODEL", "OPENAI_VOICE",
           "OPENAI_REALTIME_URL", "AGENT_MODEL_NAME_OPENAI",
           "OPENAI_RECONNECT_MAX")
    model_spec = get_spec("OPENAI_REALTIME_MODEL")
    assert model_spec.kind == "select"
    assert model_spec.default == "gpt-realtime-2.1-mini"
    assert model_spec.choices == (
        "gpt-realtime-2.1-mini",
        "gpt-realtime-2.1",
        "gpt-realtime-2",
        "gpt-realtime",
        "gpt-realtime-mini",
    )
    assert get_str("OPENAI_REALTIME_MODEL") == "gpt-realtime-2.1-mini"
    assert get_str("OPENAI_VOICE") == "alloy"
    assert get_str("OPENAI_REALTIME_URL") == ""   # 留空走官方端点
    assert get_str("AGENT_MODEL_NAME_OPENAI") == "OpenAI Realtime"
    assert get_int("OPENAI_RECONNECT_MAX") == 2   # 与 QWEN_RECONNECT_MAX 默认一致
    assert model_spec.requires_restart
    assert get_spec("OPENAI_REALTIME_URL").requires_restart


def test_agent_provider_choices_include_openai_and_local():
    assert get_spec("AGENT_PROVIDER").choices == ("qwen", "doubao", "openai", "local")


def test_tool_security_config_defaults(monkeypatch):
    _unset(monkeypatch, "SMS_RATE_LIMIT_PER_HOUR", "TOOL_QUERY_CODE_ENABLED")
    assert get_int("SMS_RATE_LIMIT_PER_HOUR") == 10
    assert get_bool("TOOL_QUERY_CODE_ENABLED") is True


def test_sms_email_forwarding_defaults_and_secret_mask(monkeypatch):
    keys = (
        "SMS_EMAIL_FORWARD_ENABLED",
        "SMS_EMAIL_RECIPIENT",
        "SMS_EMAIL_SMTP_HOST",
        "SMS_EMAIL_SMTP_PORT",
        "SMS_EMAIL_SMTP_SECURITY",
        "SMS_EMAIL_SMTP_USERNAME",
        "SMS_EMAIL_SMTP_PASSWORD",
        "SMS_EMAIL_FROM",
    )
    _unset(monkeypatch, *keys)

    assert get_bool("SMS_EMAIL_FORWARD_ENABLED") is False
    assert get_str("SMS_EMAIL_RECIPIENT") == ""
    assert get_int("SMS_EMAIL_SMTP_PORT") == 587
    assert get_str("SMS_EMAIL_SMTP_SECURITY") == "starttls"
    password = get_spec("SMS_EMAIL_SMTP_PASSWORD")
    assert password.secret is True and password.editable is True
    rows = {row["key"]: row for row in read_panel_values()}
    assert rows["SMS_EMAIL_SMTP_PASSWORD"]["value"] == "未设置"

    monkeypatch.setenv("SMS_EMAIL_SMTP_PASSWORD", "must-not-be-returned")
    rows = {row["key"]: row for row in read_panel_values()}
    assert rows["SMS_EMAIL_SMTP_PASSWORD"]["value"] == "已设置"
    assert "must-not-be-returned" not in str(rows["SMS_EMAIL_SMTP_PASSWORD"])


def test_enabling_sms_email_forwarding_requires_complete_valid_config(tmp_path, monkeypatch):
    keys = (
        "SMS_EMAIL_FORWARD_ENABLED",
        "SMS_EMAIL_RECIPIENT",
        "SMS_EMAIL_SMTP_HOST",
        "SMS_EMAIL_SMTP_PORT",
        "SMS_EMAIL_SMTP_SECURITY",
        "SMS_EMAIL_SMTP_USERNAME",
        "SMS_EMAIL_SMTP_PASSWORD",
        "SMS_EMAIL_FROM",
    )
    _unset(monkeypatch, *keys)
    env_file = tmp_path / ".env"

    with pytest.raises(ValueError, match="SMS_EMAIL_RECIPIENT"):
        update_env_file(
            {"SMS_EMAIL_FORWARD_ENABLED": "true"},
            env_path=env_file,
        )

    assert not env_file.exists()
    assert "SMS_EMAIL_FORWARD_ENABLED" not in os.environ


def test_sms_email_forward_config_is_written_atomically_when_valid(tmp_path, monkeypatch):
    keys = (
        "SMS_EMAIL_FORWARD_ENABLED",
        "SMS_EMAIL_RECIPIENT",
        "SMS_EMAIL_SMTP_HOST",
        "SMS_EMAIL_SMTP_PORT",
        "SMS_EMAIL_SMTP_SECURITY",
        "SMS_EMAIL_SMTP_USERNAME",
        "SMS_EMAIL_SMTP_PASSWORD",
        "SMS_EMAIL_FROM",
    )
    _unset(monkeypatch, *keys)
    env_file = tmp_path / ".env"
    updates = {
        "SMS_EMAIL_FORWARD_ENABLED": "true",
        "SMS_EMAIL_RECIPIENT": "owner@example.com",
        "SMS_EMAIL_SMTP_HOST": "smtp.example.com",
        "SMS_EMAIL_SMTP_PORT": "587",
        "SMS_EMAIL_SMTP_SECURITY": "starttls",
        "SMS_EMAIL_SMTP_USERNAME": "sender@example.com",
        "SMS_EMAIL_SMTP_PASSWORD": "test-app-password",
        "SMS_EMAIL_FROM": "sender@example.com",
    }

    assert update_env_file(updates, env_path=env_file) == list(updates)

    rendered = env_file.read_text(encoding="utf-8")
    for key, value in updates.items():
        assert f"{key}={value}" in rendered


@pytest.mark.parametrize(
    ("updates", "error_key"),
    [
        ({"SMS_EMAIL_RECIPIENT": "a@example.com,b@example.com"}, "SMS_EMAIL_RECIPIENT"),
        ({"SMS_EMAIL_FROM": "Name <sender@example.com>"}, "SMS_EMAIL_FROM"),
        ({"SMS_EMAIL_SMTP_PORT": "0"}, "SMS_EMAIL_SMTP_PORT"),
        ({"SMS_EMAIL_SMTP_HOST": "smtp example.com"}, "SMS_EMAIL_SMTP_HOST"),
    ],
)
def test_sms_email_config_rejects_unsafe_values_even_while_disabled(
    tmp_path, monkeypatch, updates, error_key
):
    _unset(monkeypatch, "SMS_EMAIL_FORWARD_ENABLED")

    with pytest.raises(ValueError, match=error_key):
        update_env_file(updates, env_path=tmp_path / ".env")


def test_disabling_sms_email_forwarding_does_not_require_smtp_config(tmp_path, monkeypatch):
    _unset(monkeypatch, "SMS_EMAIL_FORWARD_ENABLED")

    updated = update_env_file(
        {"SMS_EMAIL_FORWARD_ENABLED": "false"},
        env_path=tmp_path / ".env",
    )

    assert updated == ["SMS_EMAIL_FORWARD_ENABLED"]


def test_disabling_sms_email_forwarding_ignores_broken_existing_port(tmp_path, monkeypatch):
    monkeypatch.setenv("SMS_EMAIL_FORWARD_ENABLED", "true")
    monkeypatch.setenv("SMS_EMAIL_SMTP_PORT", "not-a-port")

    updated = update_env_file(
        {"SMS_EMAIL_FORWARD_ENABLED": "false"},
        env_path=tmp_path / ".env",
    )

    assert updated == ["SMS_EMAIL_FORWARD_ENABLED"]
    assert os.environ["SMS_EMAIL_FORWARD_ENABLED"] == "false"


def test_enabling_sms_email_forwarding_rejects_invalid_existing_security(
    tmp_path, monkeypatch
):
    values = {
        "SMS_EMAIL_RECIPIENT": "owner@example.com",
        "SMS_EMAIL_SMTP_HOST": "smtp.example.com",
        "SMS_EMAIL_SMTP_PORT": "587",
        "SMS_EMAIL_SMTP_SECURITY": "plaintext",
        "SMS_EMAIL_SMTP_USERNAME": "sender@example.com",
        "SMS_EMAIL_SMTP_PASSWORD": "test-app-password",
        "SMS_EMAIL_FROM": "sender@example.com",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)

    with pytest.raises(ValueError, match="SMS_EMAIL_SMTP_SECURITY"):
        update_env_file(
            {"SMS_EMAIL_FORWARD_ENABLED": "true"},
            env_path=tmp_path / ".env",
        )


def test_enabled_sms_email_config_reports_invalid_existing_port_cleanly(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("SMS_EMAIL_FORWARD_ENABLED", "true")
    monkeypatch.setenv("SMS_EMAIL_SMTP_PORT", "not-a-port")

    with pytest.raises(ValueError, match="SMS_EMAIL_SMTP_PORT 需要整数") as exc_info:
        update_env_file(
            {"SMS_EMAIL_RECIPIENT": "owner@example.com"},
            env_path=tmp_path / ".env",
        )

    assert "not-a-port" not in str(exc_info.value)


def test_manual_response_control_defaults(monkeypatch):
    _unset(
        monkeypatch,
        "MANUAL_RESPONSE_CONTROL",
        "MANUAL_RESPONSE_SILENCE_MS",
        "MANUAL_RESPONSE_MAX_WAIT_MS",
    )
    assert get_bool("MANUAL_RESPONSE_CONTROL") is False
    assert get_int("MANUAL_RESPONSE_SILENCE_MS") == 1000
    assert get_int("MANUAL_RESPONSE_MAX_WAIT_MS") == 8000


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


def test_update_env_file_concurrent_writers_do_not_lose_keys(
    tmp_path, monkeypatch
):
    keys = {
        "QWEN_VOICE": "Raymond",
        "MODEM_TX_GAIN": "0.75",
        "SUMMARY_MODEL": "qwen-plus",
        "MONITOR_OUTPUT_DEVICE": "Built-in Output",
    }
    _unset(monkeypatch, *keys)
    env = tmp_path / ".env"
    env.write_text("# concurrent updates\n", encoding="utf-8")
    original_write_text = Path.write_text

    def slow_write_text(path, data, *args, **kwargs):
        if path == env:
            time.sleep(0.03)
        return original_write_text(path, data, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", slow_write_text)
    start = threading.Barrier(len(keys) + 1)
    errors: list[Exception] = []

    def writer(key: str, value: str) -> None:
        try:
            start.wait(timeout=2)
            update_env_file({key: value}, env_path=env)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [
        threading.Thread(target=writer, args=item, daemon=True)
        for item in keys.items()
    ]
    for thread in threads:
        thread.start()
    start.wait(timeout=2)
    for thread in threads:
        thread.join(timeout=2)

    assert not errors
    assert all(not thread.is_alive() for thread in threads)
    text = env.read_text(encoding="utf-8")
    for key, value in keys.items():
        assert f"{key}={config._format_assignment(key, value).split('=', 1)[1]}" in text


def test_update_env_file_handles_utf8_bom_and_preserves_crlf(tmp_path, monkeypatch):
    _unset(monkeypatch, "QWEN_VOICE")
    env = tmp_path / ".env"
    env.write_bytes(codecs.BOM_UTF8 + b"QWEN_VOICE=Cherry\r\n# keep\r\n")

    update_env_file({"QWEN_VOICE": "Raymond"}, env_path=env)

    raw = env.read_bytes()
    assert raw.startswith(codecs.BOM_UTF8)
    assert b"QWEN_VOICE=Raymond\r\n" in raw
    assert b"QWEN_VOICE=Cherry" not in raw
    assert raw.count(b"QWEN_VOICE=") == 1


def test_remote_web_dialer_defaults_off_and_masks_livekit_secrets(monkeypatch):
    _unset(
        monkeypatch,
        "REMOTE_WEB_DIALER_ENABLED",
        "LIVEKIT_API_KEY",
        "LIVEKIT_API_SECRET",
    )
    assert get_bool("REMOTE_WEB_DIALER_ENABLED") is False
    assert get_str("REMOTE_DTMF_MODE") == "qvts"
    assert get_int("REMOTE_GATEWAY_PORT") == 47445
    assert get_int("REMOTE_MAX_PAIRED_DEVICES") == 5
    assert get_int("REMOTE_PAIRING_TTL_SECONDS") == 300
    rows = {row["key"]: row for row in read_panel_values()}
    assert rows["LIVEKIT_API_KEY"]["secret"] is True
    assert rows["LIVEKIT_API_SECRET"]["secret"] is True
    assert rows["LIVEKIT_API_SECRET"]["value"] == "未设置"


def test_enabling_remote_web_dialer_requires_complete_secure_config(
    tmp_path, monkeypatch
):
    for key in (
        "REMOTE_WEB_DIALER_ENABLED",
        "REMOTE_CONTROL_URL",
        "LIVEKIT_URL",
        "LIVEKIT_API_KEY",
        "LIVEKIT_API_SECRET",
    ):
        _unset(monkeypatch, key)
    env = tmp_path / ".env"

    with pytest.raises(ValueError, match="完整配置"):
        update_env_file({"REMOTE_WEB_DIALER_ENABLED": "true"}, env_path=env)
    assert not env.exists()

    updates = {
        "REMOTE_WEB_DIALER_ENABLED": "true",
        "REMOTE_CONTROL_URL": "https://dial.example/remote_dialer.html",
        "LIVEKIT_URL": "wss://project.livekit.cloud",
        "LIVEKIT_API_KEY": "api-key",
        "LIVEKIT_API_SECRET": "api-secret",
    }
    assert update_env_file(updates, env_path=env) == list(updates)


def test_hosted_remote_mode_needs_no_local_tunnel_or_livekit_secret(
    tmp_path, monkeypatch
):
    for key in (
        "REMOTE_WEB_DIALER_ENABLED",
        "REMOTE_CLOUD_ENABLED",
        "REMOTE_CLOUD_URL",
        "REMOTE_CONTROL_URL",
        "LIVEKIT_URL",
        "LIVEKIT_API_KEY",
        "LIVEKIT_API_SECRET",
    ):
        _unset(monkeypatch, key)
    updates = {
        "REMOTE_WEB_DIALER_ENABLED": "true",
        "REMOTE_CLOUD_ENABLED": "true",
        "REMOTE_CLOUD_URL": "https://api.bondings.ai",
    }

    assert update_env_file(updates, env_path=tmp_path / ".env") == list(updates)

    with pytest.raises(ValueError, match="HTTPS"):
        update_env_file(
            {"REMOTE_CLOUD_URL": "http://api.bondings.ai?token=secret"},
            env_path=tmp_path / ".env",
        )


@pytest.mark.parametrize(
    ("key", "value", "message"),
    [
        ("REMOTE_CONTROL_URL", "http://dial.example", "HTTPS"),
        ("REMOTE_CONTROL_URL", "https://dial.example/#token", "HTTPS"),
        ("LIVEKIT_URL", "ws://project.livekit.cloud", "WSS"),
        ("REMOTE_DISCONNECT_GRACE_SECONDS", "-1", "不能小于"),
        ("REMOTE_OUTBOUND_MAX_SECONDS", "0", "必须大于"),
        ("REMOTE_DIAL_LIMIT_PER_HOUR", "-1", "不能小于"),
    ],
)
def test_remote_web_dialer_rejects_insecure_or_invalid_values(
    key, value, message, tmp_path, monkeypatch
):
    monkeypatch.setenv("REMOTE_WEB_DIALER_ENABLED", "true")
    monkeypatch.setenv(
        "REMOTE_CONTROL_URL", "https://dial.example/remote_dialer.html"
    )
    monkeypatch.setenv("LIVEKIT_URL", "wss://project.livekit.cloud")
    monkeypatch.setenv("LIVEKIT_API_KEY", "api-key")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "api-secret")

    with pytest.raises(ValueError, match=message):
        update_env_file({key: value}, env_path=tmp_path / ".env")


# ---- read_panel_values ----


def test_panel_covers_visible_specs_and_fields():
    """面板返回全部非 hidden spec，且字段齐全；hidden 项绝不出现。"""
    rows = read_panel_values()
    visible_keys = {spec.key for spec in CONFIG_SPECS if not spec.hidden}
    assert {row["key"] for row in rows} == visible_keys
    assert len(rows) == len(visible_keys)
    for row in rows:
        assert {"key", "label", "kind", "default", "choices", "editable",
                "secret", "requires_restart", "value", "help"} <= set(row)


def test_panel_includes_voice_preview_help_links():
    """help 字段带出面板；音色项含官网试听链接，无 help 的项为空串。"""
    rows = {row["key"]: row for row in read_panel_values()}
    assert rows["QWEN_VOICE"]["help"].startswith("https://")
    assert rows["OPENAI_VOICE"]["help"].startswith("https://")
    assert rows["OWNER_NAME"]["help"] == ""


def test_hidden_specs_are_internal_only():
    """hidden 项必须同时不可编辑（防止面板渲染不出却能经 API 写入）。"""
    hidden = [spec for spec in CONFIG_SPECS if spec.hidden]
    assert hidden, "注册表应存在 hidden 内部项"
    for spec in hidden:
        assert spec.editable is False, spec.key


def test_panel_masks_secret_value(monkeypatch):
    _unset(monkeypatch, "DASHSCOPE_API_KEY")
    rows = {row["key"]: row for row in read_panel_values()}
    assert rows["DASHSCOPE_API_KEY"]["value"] == "未设置"
    assert rows["DASHSCOPE_API_KEY"]["editable"] is True

    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-secret")
    rows = {row["key"]: row for row in read_panel_values()}
    assert rows["DASHSCOPE_API_KEY"]["value"] == "已设置"
    assert "sk-secret" not in str(rows["DASHSCOPE_API_KEY"])


def test_provider_keys_are_editable_so_fresh_install_can_be_configured():
    rows = {row["key"]: row for row in read_panel_values()}
    for key in ("DASHSCOPE_API_KEY", "DOUBAO_APP_ID", "DOUBAO_ACCESS_KEY", "OPENAI_API_KEY"):
        assert rows[key]["editable"] is True
        assert rows[key]["secret"] is True


def test_setup_done_hidden_and_setup_required_logic(monkeypatch, tmp_path):
    _unset(
        monkeypatch,
        "SETUP_DONE",
        "DASHSCOPE_API_KEY",
        "DOUBAO_APP_ID",
        "DOUBAO_ACCESS_KEY",
        "OPENAI_API_KEY",
    )
    assert config.get_spec("SETUP_DONE").hidden is True
    assert config.setup_required() is True

    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-valid")
    assert config.setup_required() is True

    config.complete_setup(False, env_path=tmp_path / ".env")
    assert os.environ["SETUP_DONE"] == "true"
    assert os.environ["RECORDING_ENABLED"] == "false"
    assert config.setup_required() is False
    assert "SETUP_DONE=true" in (tmp_path / ".env").read_text(encoding="utf-8")


@pytest.mark.parametrize("enabled", [True, False])
def test_complete_setup_atomically_persists_recording_choice(tmp_path, monkeypatch, enabled):
    _unset(monkeypatch, "SETUP_DONE", "RECORDING_ENABLED")
    env = tmp_path / ".env"

    updated = config.complete_setup(enabled, env_path=env)

    expected = "true" if enabled else "false"
    assert updated == ["RECORDING_ENABLED", "SETUP_DONE"]
    assert env.read_text(encoding="utf-8") == (
        f"RECORDING_ENABLED={expected}\nSETUP_DONE=true\n"
    )
    assert config.setup_required() is False


def test_existing_setup_done_remains_complete_without_recording_choice(monkeypatch):
    _unset(monkeypatch, "RECORDING_ENABLED")
    monkeypatch.setenv("SETUP_DONE", "true")

    assert config.setup_required() is False


def test_panel_reflects_env_value(monkeypatch):
    monkeypatch.setenv("QWEN_VOICE", "Cherry")
    rows = {row["key"]: row for row in read_panel_values()}
    assert rows["QWEN_VOICE"]["value"] == "Cherry"
    assert rows["QWEN_VOICE"]["default"] == "Raymond"


def test_panel_marks_doubao_choice_experimental():
    rows = {row["key"]: row for row in read_panel_values()}
    provider = rows["AGENT_PROVIDER"]
    assert provider["choice_labels"]["doubao"] == "doubao (experimental)"
    assert provider["choices"] == ["qwen", "doubao", "openai", "local"]


# ---- 收口回归护栏 ----


def test_registered_defaults_match_original_call_sites(monkeypatch):
    """本轮收口的配置项，注册表默认值必须与原调用点硬编码一致。"""
    _unset(monkeypatch, "MODEM_PCM_PORT", "MODEM_PCM_BAUD", "SUMMARY_TIMEOUT",
           "QWEN_PREWARM_TIMEOUT", "QWEN_PREWARM_INTERVAL", "DASHSCOPE_REALTIME_URL",
           "REPEAT_SUPPRESS_SIMILARITY", "WRAP_UP_JUDGE_GRACE_SECONDS",
           "WRAP_UP_JUDGE_INTERVAL_SECONDS")
    assert get_str("MODEM_PCM_PORT") == ""            # app.py 原 os.getenv 无默认
    assert get_int("MODEM_PCM_BAUD") == 921600        # app.py 原硬编码 "921600"
    assert get_float("SUMMARY_TIMEOUT") == pytest.approx(30.0)   # summarizer 原 "30"
    assert get_float("QWEN_PREWARM_TIMEOUT") == pytest.approx(5.0)    # 原模块常量
    assert get_float("QWEN_PREWARM_INTERVAL") == pytest.approx(240.0)  # 原函数入参默认
    assert get_str("DASHSCOPE_REALTIME_URL") == ""    # 留空走 SDK 内置端点
    assert get_float("REPEAT_SUPPRESS_SIMILARITY") == pytest.approx(0.9)
    assert get_float("WRAP_UP_JUDGE_GRACE_SECONDS") == pytest.approx(20.0)
    assert get_float("WRAP_UP_JUDGE_INTERVAL_SECONDS") == pytest.approx(15.0)
    assert get_spec("WRAP_UP_JUDGE_GRACE_SECONDS").hidden
    assert get_spec("WRAP_UP_JUDGE_INTERVAL_SECONDS").hidden
    example = (Path(__file__).resolve().parents[2] / ".env.example").read_text(
        encoding="utf-8"
    )
    assert "WRAP_UP_JUDGE_GRACE_SECONDS=20.0" in example
    assert "WRAP_UP_JUDGE_INTERVAL_SECONDS=15.0" in example


def test_editable_specs_covered_by_env_example():
    """防脱节：每个面板可编辑 spec 的 key 必须出现在 .env.example。"""
    example = Path(__file__).resolve().parents[2] / ".env.example"
    assign_re = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")
    keys = set()
    for line in example.read_text(encoding="utf-8").splitlines():
        match = assign_re.match(line)
        if match:
            keys.add(match.group(1))
    missing = [spec.key for spec in CONFIG_SPECS
               if spec.editable and spec.key not in keys]
    assert not missing, f".env.example 缺少可编辑配置项: {missing}"


def test_voice_specs_are_selects_with_default_in_choices():
    """音色改为下拉(select)：默认值必须在 choices 内，否则面板/写回校验不一致。"""
    for key, expected_default in (("QWEN_VOICE", "Raymond"), ("OPENAI_VOICE", "alloy")):
        spec = get_spec(key)
        assert spec.kind == "select", key
        assert spec.default == expected_default, key
        assert expected_default in spec.choices, key
    # OpenAI Realtime 全部 10 个音色齐全
    assert set(get_spec("OPENAI_VOICE").choices) == {
        "alloy", "ash", "ballad", "coral", "echo",
        "sage", "shimmer", "verse", "marin", "cedar",
    }


def test_local_monitor_settings_require_service_restart():
    """监听播放器只在服务启动时构造，面板保存后必须触发重启。"""
    for key in (
        "MONITOR_AI_PLAYBACK",
        "MONITOR_OUTPUT_DEVICE",
        "MONITOR_AI_GAIN",
        "MONITOR_UPLINK_GAIN",
    ):
        assert get_spec(key).requires_restart, key


def test_is_loopback_host():
    from agentcall.config import is_loopback_host

    assert is_loopback_host("127.0.0.1")
    assert is_loopback_host("localhost")
    assert is_loopback_host(" ::1 ")
    assert not is_loopback_host("0.0.0.0")
    assert not is_loopback_host("192.168.1.10")


def test_dtmf_tone_numeric_ranges_rejected_on_write(tmp_path):
    """#80-D:DTMF 标定参数写回前做范围校验,防面板误填炸掉远程按键路径。"""
    env_file = tmp_path / ".env"

    for key, bad in (
        ("DTMF_TONE_MS", "0"),          # 必须 >0
        ("DTMF_TONE_MS", "5000"),       # 上限 2000
        ("DTMF_TONE_AMPLITUDE", "0"),   # (0,1] 下界排他
        ("DTMF_TONE_AMPLITUDE", "1.5"), # 上界 1
        ("DTMF_TONE_AMPLITUDE", "nan"), # NaN 拒绝
    ):
        with pytest.raises(ValueError, match=key):
            update_env_file({key: bad}, env_path=env_file)
    assert not env_file.exists()

    # 合法边界值可写
    updated = update_env_file(
        {"DTMF_TONE_MS": "200", "DTMF_TONE_AMPLITUDE": "1"},
        env_path=env_file,
    )
    assert set(updated) == {"DTMF_TONE_MS", "DTMF_TONE_AMPLITUDE"}
    os.environ.pop("DTMF_TONE_MS", None)
    os.environ.pop("DTMF_TONE_AMPLITUDE", None)
