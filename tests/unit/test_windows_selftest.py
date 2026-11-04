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
    def noisy_selftest():
        print('setup_sms_token=SECRET_SENTINEL setup_call_token=SECRET_SENTINEL')
        if fail:
            raise RuntimeError('setup_call_token=SECRET_SENTINEL')
        return 0

    monkeypatch.setattr(app, "_selftest", noisy_selftest)
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
    assert 'SECRET_SENTINEL' not in report.read_text(encoding='utf-8')
    if not fail:
        assert "webview.platforms.edgechromium" in imports


def test_frozen_default_report_and_import_cwd_are_outside_install(monkeypatch, tmp_path):
    install = tmp_path / 'Program Files'
    support = tmp_path / 'User Data'
    install.mkdir()
    monkeypatch.chdir(install)
    monkeypatch.setenv('AGENTCALL_APP_SUPPORT_DIR', str(support))
    monkeypatch.delenv('CALLPILOT_SELFTEST_REPORT', raising=False)
    monkeypatch.setattr('sys.frozen', True, raising=False)
    monkeypatch.setattr(platforms, 'IS_WINDOWS', True)
    monkeypatch.setattr(platforms, 'IS_MACOS', False)
    import_cwds = []

    def load(name):
        import_cwds.append(Path.cwd())
        return SimpleNamespace(_selftest=lambda: 0)

    monkeypatch.setattr(windows_selftest, 'importlib', SimpleNamespace(import_module=load))
    assert windows_selftest.run() == 0
    assert (support / 'selftest.json').is_file()
    assert not list(install.iterdir())
    assert import_cwds and set(import_cwds) == {support}


def test_portable_smoke_logs_only_allowlisted_meta_and_asserts_data_root():
    root = Path(__file__).resolve().parents[2]
    script = (root / 'scripts/windows/test_portable.ps1').read_text(encoding='utf-8')
    assert '$meta |' not in script
    assert '$($meta' not in script
    assert 'VersionMatches=$versionMatches' in script
    assert 'LocalAppDataCreated=' in script
    assert 'EnvPreserved=' in script
    assert 'data-root' in script
    assert "Join-Path $env:LOCALAPPDATA 'CallPilot'" in script
    assert '--hardware-selftest' in script
    assert '--shutdown' in script
