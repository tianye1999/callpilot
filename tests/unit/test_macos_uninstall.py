"""Uninstall ordering is tested with a fake OS, never the installed app."""

import pytest

from agentcall import macos_launchd as ld
from agentcall import macos_uninstall as uninstall


def test_uninstall_stops_service_then_bridge_unregisters_before_trash_and_preserves_data(tmp_path):
    layout = ld.make_layout(tmp_path / "CallPilot.app/Contents/MacOS/CallPilot", uid=1,
                            support_dir=tmp_path / "support")
    events = []
    uninstall.execute(layout, delete_data=False,
                      stop=lambda label: events.append(label),
                      close_shell=lambda: events.append("close-shell"),
                      cleanup=lambda: events.append("legacy"),
                      trash=lambda path: events.append("trash"),
                      delete=lambda: events.append("delete"))
    assert events == [ld.APP_LABEL, ld.BRIDGE_LABEL, "close-shell", ld.TRAY_LABEL, "legacy", "trash"]


@pytest.mark.parametrize("delete_data", [False, True])
def test_uninstall_failure_never_trashes_or_deletes(tmp_path, delete_data):
    layout = ld.make_layout(tmp_path / "CallPilot.app/Contents/MacOS/CallPilot", uid=1)
    events = []

    def stop(label):
        raise RuntimeError("still loaded")

    with pytest.raises(RuntimeError, match="still loaded"):
        uninstall.execute(layout, delete_data=delete_data, stop=stop, close_shell=lambda: None,
                          cleanup=lambda: None, trash=events.append, delete=lambda: events.append("delete"))
    assert not events


def test_optional_delete_is_last_and_only_explicit_true(tmp_path):
    layout = ld.make_layout(tmp_path / "CallPilot.app/Contents/MacOS/CallPilot", uid=1)
    events = []
    uninstall.execute(layout, delete_data=True, stop=lambda _: None, close_shell=lambda: None,
                      cleanup=lambda: None, trash=lambda _: events.append("trash"),
                      delete=lambda: events.append("delete"))
    assert events == ["trash", "delete"]


def test_launch_uninstaller_uses_launch_services_not_child_in_tray_job(tmp_path):
    layout = ld.make_layout(tmp_path / "CallPilot.app/Contents/MacOS/CallPilot", uid=1)
    commands = []
    uninstall.launch(layout, runner=lambda cmd, **kwargs: commands.append(cmd))
    assert commands == [["/usr/bin/open", "-n", str(tmp_path / "CallPilot.app"), "--args", "--uninstall-worker"]]


def test_data_delete_rejects_override_or_symlink_before_any_deletion(tmp_path):
    layout = ld.make_layout(tmp_path / "CallPilot", uid=1, support_dir=tmp_path)
    with pytest.raises(RuntimeError, match="data directory"):
        uninstall.validate_data_directory(layout)


@pytest.fixture
def native_worker(monkeypatch, tmp_path):
    import plistlib
    import sys
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from agentcall import macos_service_management as sm
    from agentcall import macos_shell, macos_upgrade

    layout = ld.make_layout(tmp_path / "CallPilot.app/Contents/MacOS/CallPilot", uid=123,
                            support_dir=tmp_path / "support")
    layout.executable.parent.mkdir(parents=True)
    layout.executable.touch()
    layout.support_dir.mkdir()
    (layout.executable.parent.parent / "Info.plist").write_bytes(plistlib.dumps(
        {"CFBundleIdentifier": "ai.bondings.callpilot"}))
    monkeypatch.setattr(ld, "make_layout", lambda _: layout)
    monkeypatch.setattr(sys, "platform", "darwin")
    native = MagicMock()
    alert = native.NSAlert.alloc.return_value.init.return_value
    alert.runModal.return_value = 1000
    checkbox = native.NSButton.alloc.return_value.initWithFrame_.return_value
    checkbox.state.return_value = 0
    monkeypatch.setitem(sys.modules, "AppKit", native)
    locks = SimpleNamespace(flock=lambda *args: None, LOCK_EX=1, LOCK_NB=2)
    monkeypatch.setitem(sys.modules, "fcntl", locks)
    monkeypatch.setattr(macos_shell, "configure_shell_logging", lambda *args: None)
    monkeypatch.setattr(macos_shell, "ui_language", lambda: "en")
    events = []
    monkeypatch.setattr(macos_shell, "send_command", lambda cmd, *args: events.append(cmd))
    meta = {"call_active": False, "upgrade_guard": True}
    monkeypatch.setattr(macos_shell, "fetch_meta", lambda _: meta)
    monkeypatch.setattr(macos_upgrade, "acquire_lease", lambda _: lambda: events.append("release"))
    monkeypatch.setattr(sm, "agent_running", lambda *args: False)
    monkeypatch.setattr(sm, "NativeServices", lambda: object())
    monkeypatch.setattr(sm, "stop", lambda label, *args, **kw: events.append(label))
    monkeypatch.setattr(sm, "cleanup_legacy", lambda *args: events.append("cleanup"))
    monkeypatch.setattr(uninstall, "_pause_shell", lambda *args: events.append("pause"))
    monkeypatch.setattr(uninstall, "_close_shell", lambda *args: events.append("close"))
    monkeypatch.setattr(uninstall, "_trash", lambda *args: events.append("trash"))
    return SimpleNamespace(events=events, meta=meta, alert=alert, checkbox=checkbox, locks=locks)


def test_native_confirmation_cancel_defaults_to_keep_data(native_worker):
    env = native_worker
    env.alert.runModal.return_value = 1001
    uninstall.worker_main("http://not-accessed.invalid", None, None)
    env.checkbox.setState_.assert_called_once_with(0)
    assert env.events == []


@pytest.mark.parametrize("state", [{"call_active": True}, {"call_active": False, "dial_queue_busy": True}])
def test_worker_busy_call_or_queue_does_not_stop_or_delete(native_worker, state):
    env = native_worker
    env.meta.update(state)
    uninstall.worker_main("http://not-accessed.invalid", None, None)
    assert env.events == ["pause", "resume"]


def test_worker_allows_dormant_registered_job_without_meta(native_worker):
    env = native_worker
    env.meta.clear()
    uninstall.worker_main("http://not-accessed.invalid", None, None)
    assert env.events == ["pause", ld.APP_LABEL, ld.BRIDGE_LABEL, "close", ld.TRAY_LABEL,
                          "cleanup", "trash", "resume"]


def test_worker_unknown_running_call_state_fails_closed(native_worker, monkeypatch):
    from agentcall import macos_service_management as sm

    env = native_worker
    env.meta.clear()
    monkeypatch.setattr(sm, "agent_running", lambda *args: True)
    uninstall.worker_main("http://not-accessed.invalid", None, None)
    assert env.events == ["pause", "resume"]


def test_duplicate_worker_cannot_resume_first_worker(native_worker, monkeypatch):
    def locked(*args):
        raise BlockingIOError()

    monkeypatch.setattr(native_worker.locks, "flock", locked)
    uninstall.worker_main("http://not-accessed.invalid", None, None)
    assert native_worker.events == []
    native_worker.alert.runModal.assert_not_called()


def test_uninstall_holds_real_shell_lock_until_after_trash(tmp_path):
    from types import SimpleNamespace

    layout = ld.make_layout(tmp_path / "CallPilot.app/Contents/MacOS/CallPilot", uid=1)
    events = []
    lock = SimpleNamespace(close=lambda: events.append("unlock"))
    uninstall.execute(layout, delete_data=False, stop=lambda _: None, close_shell=lambda: lock,
                      cleanup=lambda: None, trash=lambda _: events.append("trash"), delete=lambda: None)
    assert events == ["trash", "unlock"]
