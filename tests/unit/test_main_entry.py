"""精简 CLI 入口的配置注册表接线测试。"""

from __future__ import annotations

import runpy
from pathlib import Path

from agentcall import config


def _load_parser():
    main_path = Path(__file__).resolve().parents[2] / "main.py"
    namespace = runpy.run_path(str(main_path), run_name="callpilot_main_test")
    return namespace["build_parser"]()


def test_cli_fallback_defaults_match_config_registry(monkeypatch):
    keys = (
        "MODEM_PORT",
        "MODEM_BAUD",
        "MODEM_AUDIO_KEYWORD",
        "MODEM_AUDIO_MODE",
        "MODEM_PCM_PORT",
        "MODEM_PCM_BAUD",
        "MODEM_TX_GAIN",
        "AGENT_PROVIDER",
    )
    for key in keys:
        monkeypatch.delenv(key, raising=False)

    args = _load_parser().parse_args([])

    assert args.port == config.get_str("MODEM_PORT")
    assert args.baud == config.get_int("MODEM_BAUD")
    assert args.audio_keyword == config.get_str("MODEM_AUDIO_KEYWORD")
    assert args.audio_mode == config.get_str("MODEM_AUDIO_MODE")
    assert args.pcm_port == config.get_str("MODEM_PCM_PORT")
    assert args.pcm_baud == config.get_int("MODEM_PCM_BAUD")
    assert args.tx_gain == config.get_float("MODEM_TX_GAIN")
    assert args.provider == config.get_str("AGENT_PROVIDER")


def test_cli_defaults_come_from_config_registry(monkeypatch):
    values = {
        "MODEM_PORT": "registry-port",
        "MODEM_BAUD": "57600",
        "MODEM_AUDIO_KEYWORD": "registry-audio",
        "MODEM_AUDIO_MODE": "nmea",
        "MODEM_PCM_PORT": "registry-pcm",
        "MODEM_PCM_BAUD": "460800",
        "MODEM_TX_GAIN": "0.7",
        "AGENT_PROVIDER": "openai",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)

    parser = _load_parser()
    args = parser.parse_args([])

    assert args.port == "registry-port"
    assert args.baud == 57600
    assert args.audio_keyword == "registry-audio"
    assert args.audio_mode == "nmea"
    assert args.pcm_port == "registry-pcm"
    assert args.pcm_baud == 460800
    assert args.tx_gain == 0.7
    assert args.provider == "openai"
    assert parser.parse_args(["--audio-mode", "uac_ffmpeg"]).audio_mode == "uac_ffmpeg"
