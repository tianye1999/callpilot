"""HKCU Run 自启开关的跨平台单测：用假 winreg 打桩，不在 macOS 上碰注册表。"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from agentcall import windows_autostart as autostart


def make_fake_winreg(values: dict[str, str] | None = None) -> types.ModuleType:
    store = dict(values or {})
    module = types.ModuleType("winreg")
    module.HKEY_CURRENT_USER = object()
    module.KEY_READ = 0x20019
    module.KEY_SET_VALUE = 0x0002
    module.REG_SZ = 1
    module.values = store  # test inspection hook

    class FakeKey:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def open_key(root, subkey, reserved=0, access=0):
        return FakeKey()

    def create_key(root, subkey, reserved=0, access=0):
        return FakeKey()

    def query_value(key, name):
        if name not in store:
            raise OSError("value not found")
        return store[name], module.REG_SZ

    def set_value(key, name, reserved, value_type, value):
        store[name] = value

    def delete_value(key, name):
        if name not in store:
            raise OSError("value not found")
        del store[name]

    module.OpenKey = open_key
    module.CreateKeyEx = create_key
    module.QueryValueEx = query_value
    module.SetValueEx = set_value
    module.DeleteValue = delete_value
    return module


@pytest.fixture
def fake_winreg(monkeypatch):
    module = make_fake_winreg()
    monkeypatch.setitem(sys.modules, "winreg", module)
    monkeypatch.setattr(sys, "platform", "win32")
    return module


def test_run_command_quotes_exe_and_keeps_login_arg():
    # Path renders with the host separator (backslashes on Windows); assert on that rendering.
    exe = Path("C:/Program Files/CallPilot/CallPilot.exe")
    assert autostart.run_command(exe) == f'"{exe}" --login'


def test_read_run_value_missing_returns_none(fake_winreg):
    assert autostart.read_run_value() is None


def test_set_enabled_writes_then_deletes_run_value(fake_winreg, monkeypatch):
    monkeypatch.setattr(sys, "executable", "C:/CallPilot/CallPilot.exe")
    autostart.set_enabled(True)
    assert autostart.read_run_value() == autostart.run_command()
    assert autostart.is_enabled()
    autostart.set_enabled(False)
    assert autostart.read_run_value() is None
    assert not autostart.is_enabled()


def test_stale_run_target_detects_old_exe_path(fake_winreg, monkeypatch):
    old = "C:/Old/CallPilot.exe"
    fake_winreg.values[autostart.VALUE_NAME] = f'"{old}" --login'
    monkeypatch.setattr(sys, "executable", "C:/New/CallPilot.exe")
    assert autostart.stale_run_target() == Path(old).resolve()


def test_stale_run_target_none_when_path_matches(fake_winreg, monkeypatch):
    monkeypatch.setattr(sys, "executable", "C:/CallPilot/CallPilot.exe")
    fake_winreg.values[autostart.VALUE_NAME] = autostart.run_command()
    assert autostart.stale_run_target() is None


def test_non_windows_raises_autostart_unavailable(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    with pytest.raises(autostart.AutostartUnavailable):
        autostart.read_run_value()
