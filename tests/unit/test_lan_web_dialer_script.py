"""Static and small-unit checks for the LAN dialer script entrypoint."""

from __future__ import annotations

import importlib.util
import stat
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "lan_web_dialer.py"


def load_script_module():
    spec = importlib.util.spec_from_file_location("lan_web_dialer_script", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_generated_tls_key_is_owner_read_write_only(tmp_path, monkeypatch):
    module = load_script_module()

    def fake_run(args, **kwargs):
        key = Path(args[args.index("-keyout") + 1])
        cert = Path(args[args.index("-out") + 1])
        key.write_text("key", encoding="utf-8")
        cert.write_text("cert", encoding="utf-8")
        key.chmod(0o644)

    monkeypatch.setattr(module.subprocess, "run", fake_run)

    _cert, key = module._ensure_self_signed_cert(tmp_path, "192.168.1.23")

    assert stat.S_IMODE(key.stat().st_mode) == 0o600


def test_lan_dialer_disables_access_log_to_avoid_query_token_leaks():
    text = SCRIPT.read_text(encoding="utf-8")

    assert "access_log=None" in text
