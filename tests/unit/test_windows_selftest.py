import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentcall import platforms, windows_selftest


@pytest.mark.parametrize("fail", [False, True])
def test_headless_report_without_hardware(monkeypatch, tmp_path, fail):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2]))
    import app
    import desktop_app

    def forbidden(*args, **kwargs):
        raise AssertionError("Selftest must not touch hardware, windows or services")

    monkeypatch.setattr("serial.Serial", forbidden)
    monkeypatch.setattr("serial.tools.list_ports.comports", forbidden)
    monkeypatch.setattr(desktop_app, "ensure_service_running", forbidden)
    monkeypatch.setattr(desktop_app, "_import_webview", forbidden)
    monkeypatch.setattr(platforms, "IS_WINDOWS", True)
    monkeypatch.setattr(platforms, "IS_MACOS", False)
    monkeypatch.setattr(app, "_selftest", lambda: 1 if fail else 0)
    imports = []
    monkeypatch.setattr(windows_selftest, "importlib", SimpleNamespace(
        import_module=lambda name: app if name == "app" else imports.append(name),
    ))
    report = tmp_path / "result.json"
    monkeypatch.setenv("CALLPILOT_SELFTEST_REPORT", str(report))
    monkeypatch.setattr("sys.argv", ["CallPilot.exe", "--selftest"])
    with pytest.raises(SystemExit) as exc:
        desktop_app.main()
    assert exc.value.code == int(fail)
    assert json.loads(report.read_text(encoding="utf-8"))["ok"] is not fail
    if not fail:
        assert "webview.platforms.edgechromium" in imports
