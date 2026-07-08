"""config 模块单测：类型化读取、凭证校验、.env 写回与面板值。"""

from __future__ import annotations

import os
import re
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
    """OpenAI provider 注册项的默认值（模型选型已定 gpt-realtime-mini）。"""
    _unset(monkeypatch, "OPENAI_REALTIME_MODEL", "OPENAI_VOICE",
           "OPENAI_REALTIME_URL", "AGENT_MODEL_NAME_OPENAI",
           "OPENAI_RECONNECT_MAX")
    assert get_str("OPENAI_REALTIME_MODEL") == "gpt-realtime-mini"
    assert get_str("OPENAI_VOICE") == "alloy"
    assert get_str("OPENAI_REALTIME_URL") == ""   # 留空走官方端点
    assert get_str("AGENT_MODEL_NAME_OPENAI") == "OpenAI Realtime Mini"
    assert get_int("OPENAI_RECONNECT_MAX") == 2   # 与 QWEN_RECONNECT_MAX 默认一致
    assert get_spec("OPENAI_REALTIME_MODEL").requires_restart
    assert get_spec("OPENAI_REALTIME_URL").requires_restart


def test_agent_provider_choices_include_openai():
    assert get_spec("AGENT_PROVIDER").choices == ("qwen", "doubao", "openai")


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


def test_panel_covers_visible_specs_and_fields():
    """面板返回全部非 hidden spec，且字段齐全；hidden 项绝不出现。"""
    rows = read_panel_values()
    visible_keys = {spec.key for spec in CONFIG_SPECS if not spec.hidden}
    assert {row["key"] for row in rows} == visible_keys
    assert len(rows) == len(visible_keys)
    for row in rows:
        assert {"key", "label", "kind", "default", "choices", "editable",
                "secret", "requires_restart", "value"} <= set(row)


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
    assert config.setup_required() is False

    monkeypatch.delenv("DASHSCOPE_API_KEY")
    config.mark_setup_done(env_path=tmp_path / ".env")
    assert os.environ["SETUP_DONE"] == "true"
    assert config.setup_required() is False
    assert "SETUP_DONE=true" in (tmp_path / ".env").read_text(encoding="utf-8")


def test_panel_reflects_env_value(monkeypatch):
    monkeypatch.setenv("QWEN_VOICE", "Cherry")
    rows = {row["key"]: row for row in read_panel_values()}
    assert rows["QWEN_VOICE"]["value"] == "Cherry"
    assert rows["QWEN_VOICE"]["default"] == "Raymond"


# ---- 收口回归护栏 ----


def test_registered_defaults_match_original_call_sites(monkeypatch):
    """本轮收口的配置项，注册表默认值必须与原调用点硬编码一致。"""
    _unset(monkeypatch, "MODEM_PCM_PORT", "MODEM_PCM_BAUD", "SUMMARY_TIMEOUT",
           "QWEN_PREWARM_TIMEOUT", "QWEN_PREWARM_INTERVAL", "DASHSCOPE_REALTIME_URL",
           "REPEAT_SUPPRESS_SIMILARITY")
    assert get_str("MODEM_PCM_PORT") == ""            # app.py 原 os.getenv 无默认
    assert get_int("MODEM_PCM_BAUD") == 921600        # app.py 原硬编码 "921600"
    assert get_float("SUMMARY_TIMEOUT") == pytest.approx(30.0)   # summarizer 原 "30"
    assert get_float("QWEN_PREWARM_TIMEOUT") == pytest.approx(5.0)    # 原模块常量
    assert get_float("QWEN_PREWARM_INTERVAL") == pytest.approx(240.0)  # 原函数入参默认
    assert get_str("DASHSCOPE_REALTIME_URL") == ""    # 留空走 SDK 内置端点
    assert get_float("REPEAT_SUPPRESS_SIMILARITY") == pytest.approx(0.9)


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
