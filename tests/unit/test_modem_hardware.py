"""自动模组识别不依赖真机。"""

import json

import pytest

from agentcall import config, platforms
from agentcall import modem_hardware as hardware


@pytest.mark.parametrize("devices,vendor", [
    ([], None), ([(0x1E0E, 0x9001)], "simcom"),
    ([(0x2C7C, 0x0125)], "quectel"),
    ([(0x1E0E, 0x9001), (0x2C7C, 0x0125)], "quectel"),
])
def test_select_preset_is_deterministic(devices, vendor):
    preset = hardware.select_preset(devices)
    assert (preset.vendor if preset else None) == vendor


def test_bridge_state_atomic_and_fresh(tmp_path, monkeypatch):
    path = tmp_path / "modem.json"
    monkeypatch.setattr(hardware, "STATE_PATH", path)
    preset = hardware.PRESETS[1]
    hardware.write_state(preset, online=True)
    data = hardware.read_state()
    assert data["vendor"] == "simcom"
    assert data["at_port"] == "/tmp/sim7600-at"
    assert data["pcm_port"] == "/tmp/sim7600-pcm"
    assert data["audio_mode"] == "nmea"
    assert data["vid"] == preset.vid and data["pid"] == preset.pid
    assert list(tmp_path.iterdir()) == [path]
    data["updated_at"] -= hardware.STATE_MAX_AGE + 1
    path.write_text(json.dumps(data))
    assert hardware.read_state() is None
    hardware.write_state(preset, online=False)
    assert hardware.read_state() is None


def test_state_replace_failure_preserves_previous_file(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    monkeypatch.setattr(hardware, "STATE_PATH", path)
    hardware.write_state(hardware.PRESETS[0], online=True)
    old = path.read_bytes()
    def fail_replace(source, target):
        assert path.read_bytes() == old
        assert json.loads(hardware.Path(source).read_text())["vendor"] == "simcom"
        raise OSError("replace failed")
    monkeypatch.setattr(hardware.os, "replace", fail_replace)
    with pytest.raises(OSError):
        hardware.write_state(hardware.PRESETS[1], online=True)
    assert path.read_bytes() == old
    assert list(tmp_path.iterdir()) == [path]


def test_auto_resolution_state_scan_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr(hardware, "STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(hardware, "scan_preset", lambda: hardware.PRESETS[0])
    hardware.write_state(hardware.PRESETS[1], online=True)
    assert hardware.detect_modem()["vendor"] == "simcom"
    hardware.STATE_PATH.unlink()
    assert hardware.detect_modem()["vendor"] == "quectel"
    monkeypatch.setattr(hardware, "scan_preset", lambda: hardware.PRESETS[1])
    assert hardware.detect_modem()["vendor"] == "simcom"
    monkeypatch.setattr(hardware, "scan_preset", lambda: None)
    assert hardware.detect_modem()["vendor"] == "quectel"
    def fail():
        raise RuntimeError("scan unavailable")
    monkeypatch.setattr(hardware, "scan_preset", fail)
    assert hardware.detect_modem()["vendor"] == "quectel"


def test_auto_settings_preserve_explicit_values(monkeypatch):
    monkeypatch.setattr(platforms, "IS_MACOS", True)
    monkeypatch.setattr(hardware, "detect_modem", lambda: hardware.PRESETS[1].state())
    for key in ("MODEM_PORT", "MODEM_AUDIO_MODE", "MODEM_PCM_PORT"):
        monkeypatch.delenv(key, raising=False)
    assert hardware.resolve_settings("auto", "old", "uac", None) == (
        "simcom", "/tmp/sim7600-at", "nmea", "/tmp/sim7600-pcm",
    )
    monkeypatch.setenv("MODEM_PORT", "custom")
    monkeypatch.setenv("MODEM_AUDIO_MODE", "uac")
    monkeypatch.setenv("MODEM_PCM_PORT", "")
    assert hardware.resolve_settings("auto", "custom", "uac", None) == (
        "simcom", "custom", "uac", None,
    )
    assert config.is_explicit("MODEM_PCM_PORT")


def test_dotenv_values_are_explicit(tmp_path, monkeypatch):
    from dotenv import load_dotenv

    env = tmp_path / ".env"
    env.write_text("MODEM_PORT=custom-at\nMODEM_AUDIO_MODE=uac\nMODEM_PCM_PORT=\n")
    for key in ("MODEM_PORT", "MODEM_AUDIO_MODE", "MODEM_PCM_PORT"):
        monkeypatch.delenv(key, raising=False)
    load_dotenv(env)
    monkeypatch.setattr(hardware, "detect_modem", lambda: hardware.PRESETS[1].state())
    assert hardware.resolve_settings("auto", config.get_str("MODEM_PORT"),
                                     config.get_str("MODEM_AUDIO_MODE"),
                                     config.get_str("MODEM_PCM_PORT") or None) == (
        "simcom", "custom-at", "uac", None,
    )


def test_non_macos_scan_keeps_native_ports(monkeypatch):
    monkeypatch.setattr(platforms, "IS_MACOS", False)
    monkeypatch.setattr(hardware, "detect_modem", lambda: hardware.PRESETS[1].state())
    for key in ("MODEM_PORT", "MODEM_AUDIO_MODE", "MODEM_PCM_PORT"):
        monkeypatch.delenv(key, raising=False)
    assert hardware.resolve_settings("auto", "auto", "uac", None) == ("simcom", "auto", "nmea", None)


@pytest.mark.parametrize("mac", [False, True])
def test_vendor_scan_uses_platform_transport(monkeypatch, mac):
    from types import SimpleNamespace

    import usb.core
    from serial.tools import list_ports

    monkeypatch.setattr(platforms, "IS_MACOS", mac)
    monkeypatch.setattr(usb.core, "find", lambda **kw: [SimpleNamespace(idVendor=0x1E0E)])
    monkeypatch.setattr(list_ports, "comports", lambda: [SimpleNamespace(vid=0x1E0E)])
    assert hardware.scan_preset().vendor == "simcom"


@pytest.mark.parametrize("mac,frozen,vendor,interval", [
    (True, True, "auto", 60), (True, False, "quectel", 0),
    (False, True, "quectel", 0), (False, False, "quectel", 0),
])
def test_packaged_macos_defaults(monkeypatch, mac, frozen, vendor, interval):
    monkeypatch.setattr(platforms, "IS_MACOS", mac)
    monkeypatch.setattr(platforms.sys, "frozen", frozen, raising=False)
    monkeypatch.delattr(platforms.sys, "_MEIPASS", raising=False)
    assert platforms.default_modem_vendor() == vendor
    assert platforms.default_signal_poll_seconds() == interval
