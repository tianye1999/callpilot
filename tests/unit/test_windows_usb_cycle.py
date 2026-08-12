"""Windows USB soft-cycle helper unit tests (no real PnP)."""

from __future__ import annotations

from pathlib import Path

from agentcall import windows_usb_cycle


def test_normalize_vid_accepts_hex_forms():
    assert windows_usb_cycle._normalize_vid("1e0e") == "1E0E"
    assert windows_usb_cycle._normalize_vid("0x1E0E") == "1E0E"
    assert windows_usb_cycle._normalize_vid("") == "1E0E"


def test_soft_cycle_skipped_on_non_windows(monkeypatch):
    monkeypatch.setattr(windows_usb_cycle.platforms, "IS_WINDOWS", False)
    result = windows_usb_cycle.soft_cycle_simtech_usb(vid="1e0e")
    assert result.ok is False
    assert result.method == "skip"


def test_soft_cycle_uses_scheduled_task_when_present(monkeypatch, tmp_path):
    monkeypatch.setattr(windows_usb_cycle.platforms, "IS_WINDOWS", True)
    status = tmp_path / "callpilot-usb-cycle.status"
    monkeypatch.setattr(windows_usb_cycle, "_status_path", lambda: status)

    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        if cmd[:2] == ["schtasks", "/Run"]:
            status.write_text("OK:USB\\VID_1E0E&PID_9001\\X", encoding="utf-8")
            return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
        raise AssertionError(f"unexpected cmd {cmd}")

    monkeypatch.setattr(windows_usb_cycle.subprocess, "run", fake_run)
    result = windows_usb_cycle.soft_cycle_simtech_usb(vid="1e0e", timeout_seconds=2.0)
    assert result.ok is True
    assert result.method == "scheduled_task"
    assert any(c[:2] == ["schtasks", "/Run"] for c in calls)


def test_read_status_fail_line(tmp_path):
    status = tmp_path / "s.status"
    status.write_text("FAIL:access denied", encoding="utf-8")
    result = windows_usb_cycle._read_status(status, "direct", 0.0)
    assert result is not None
    assert result.ok is False
    assert "access denied" in result.detail


def test_read_status_accepts_utf8_bom_ok(tmp_path):
    status = tmp_path / "s.status"
    status.write_bytes("\ufeffOK:USB\\VID_1E0E&PID_9001\\X\n".encode("utf-8"))
    result = windows_usb_cycle._read_status(status, "direct", 0.0)
    assert result is not None
    assert result.ok is True


def test_read_status_ignores_ok_written_before_this_run(tmp_path):
    """真机 2026-08-12：SYSTEM 任务留下的 OK 删不掉，被当成每一通的成功。"""
    status = tmp_path / "s.status"
    status.write_text("OK:USB\\VID_1E0E&PID_9001\\X", encoding="utf-8")
    stale_mtime = status.stat().st_mtime
    assert windows_usb_cycle._read_status(status, "direct", stale_mtime) is None


def test_soft_cycle_fails_when_status_is_stale(monkeypatch, tmp_path):
    """状态文件删不掉 + PowerShell 无权限时，必须报失败而不是复用旧 OK。"""
    monkeypatch.setattr(windows_usb_cycle.platforms, "IS_WINDOWS", True)
    status = tmp_path / "usb-cycle.status"
    status.write_text("OK:USB\\VID_1E0E&PID_9001\\X", encoding="utf-8")
    monkeypatch.setattr(windows_usb_cycle, "_status_path", lambda: status)
    monkeypatch.setattr(
        Path, "unlink", lambda self, missing_ok=False: (_ for _ in ()).throw(OSError())
    )

    def fake_run(cmd, **kwargs):
        if cmd[:2] == ["schtasks", "/Run"]:
            return type("R", (), {"returncode": 1, "stdout": "", "stderr": "denied"})()
        return type("R", (), {"returncode": 1, "stdout": "", "stderr": "denied"})()

    monkeypatch.setattr(windows_usb_cycle.subprocess, "run", fake_run)
    result = windows_usb_cycle.soft_cycle_simtech_usb(vid="1e0e", timeout_seconds=2.0)
    assert result.ok is False
    assert result.method == "direct"


def test_helper_scripts_exist():
    root = Path(__file__).resolve().parents[2]
    assert (root / "scripts/windows/soft_cycle_simtech_usb.ps1").is_file()
    assert (root / "scripts/windows/install_usb_cycle_helper.ps1").is_file()


def test_config_spec_and_env_example_have_usb_soft_cycle(monkeypatch):
    from agentcall.config import get_bool, get_spec

    monkeypatch.delenv("MODEM_USB_SOFT_CYCLE_AFTER_HANGUP", raising=False)
    spec = get_spec("MODEM_USB_SOFT_CYCLE_AFTER_HANGUP")
    assert spec.kind == "bool"
    assert spec.requires_restart is True
    example = (Path(__file__).resolve().parents[2] / ".env.example").read_text(
        encoding="utf-8"
    )
    assert "MODEM_USB_SOFT_CYCLE_AFTER_HANGUP=false" in example
    assert get_bool("MODEM_USB_SOFT_CYCLE_AFTER_HANGUP") is False
