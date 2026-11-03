"""Headless frozen Windows smoke check; never starts a service or enumerates ports."""

from __future__ import annotations

import contextlib
import importlib
import io
import json
import os
import sys
from pathlib import Path


def run() -> int:
    from . import config, platforms

    support = config.app_support_dir()
    report_path = Path(os.environ.get("CALLPILOT_SELFTEST_REPORT", str(support / "selftest.json"))).resolve()
    if platforms.IS_WINDOWS and config._is_frozen():
        support.mkdir(parents=True, exist_ok=True)
        os.chdir(support)
    result: dict = {"ok": False, "frozen": bool(getattr(sys, "frozen", False)), "platform": sys.platform}
    transcript = io.StringIO()
    checked = []
    try:
        # GUI bootloaders have no stdout/stderr. Preserve the existing import check
        # without relying on console output or loading any user's .env file.
        with contextlib.redirect_stdout(transcript), contextlib.redirect_stderr(transcript):
            app = importlib.import_module("app")
            if app._selftest() != 0:
                raise RuntimeError("Application module import selftest failed")
            for name in ("agentcall.modem", "agentcall.audio_bridge", "agentcall.port_detect",
                         "agentcall.web.server", "aiortc", "av", "pylibsrtp", "sounddevice", "webview"):
                importlib.import_module(name)
                checked.append(name)
            if platforms.IS_WINDOWS:
                # Load the native/pythonnet assemblies but never construct a Form.
                for name in ("clr", "webview.platforms.winforms", "webview.platforms.edgechromium",
                             "pystray._win32", "PIL.Image", "agentcall.windows_runtime"):
                    importlib.import_module(name)
                    checked.append(name)
        result["defaults"] = {
            "modem_port": platforms.default_modem_port(),
            "simcom_audio": platforms.default_audio_mode("simcom"),
            "quectel_audio": platforms.default_audio_mode(),
            "pcm_baud": config.get_int("MODEM_PCM_BAUD"),
            "env_path": str(config.env_file_path()),
            "data_path": str(config.data_dir()),
        }
        if platforms.IS_WINDOWS:
            assert result["defaults"]["modem_port"] == "auto"
            assert result["defaults"]["simcom_audio"] == "nmea"
        result["ok"] = True
    except Exception as exc:
        # Third-party import output/exception messages can contain credentials.
        # Only publish structured fields, never arbitrary transcripts/tracebacks.
        result["error_type"] = type(exc).__name__
    result["imports"] = checked
    report_path.write_text(json.dumps(result, ensure_ascii=True, indent=2), encoding="utf-8")
    return 0 if result["ok"] else 1
