from types import SimpleNamespace

import pytest

from agentcall import config, modem_hardware, platforms, port_detect


def port(device, description=None, interface=None, vid=0x1E0E, pid=0x9001):
    return SimpleNamespace(device=device, description=description, interface=interface, vid=vid, pid=pid)


@pytest.mark.parametrize("ports,expected", [
    ([port("COM8", "SimTech HS-USB AT Port 9001"), port("COM10", "SimTech HS-USB Audio 9001")],
     ("COM8", "COM10")),
    ([port("COM8", interface="AT Port"), port("COM10", interface="Audio")], ("COM8", "COM10")),
    ([port("COM8"), port("COM10")], (None, None)),
    ([port("COM8", "NMEA"), port("COM10", "Diagnostics")], (None, None)),
    ([port("COM8", "AT Port", pid=0x9011)], (None, None)),
    ([], (None, None)),
    ([port("COM2", "Quectel USB AT Port", vid=0x2C7C)], (None, None)),
    ([port("COM2", "Quectel USB AT Port", vid=0x2C7C), port("COM8", "AT Port"),
      port("COM10", "Audio")], ("COM8", "COM10")),
])
def test_simcom_roles(monkeypatch, ports, expected):
    monkeypatch.setattr(port_detect.list_ports, "comports", lambda: ports)
    assert port_detect.detect_serial_ports("simcom") == expected


def test_windows_simcom_settings(monkeypatch):
    monkeypatch.setattr(platforms, "IS_WINDOWS", True)
    monkeypatch.setattr(platforms, "IS_MACOS", False)
    for key in ("MODEM_AUDIO_MODE", "MODEM_PCM_PORT"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("MODEM_PORT", "auto")
    monkeypatch.setattr(port_detect.list_ports, "comports", lambda: [
        port("COM8", "AT Port"), port("COM10", "Audio"),
    ])
    assert platforms.default_audio_mode("simcom") == "nmea"
    assert modem_hardware.resolve_settings("simcom", "auto", "uac", None) == (
        "simcom", "COM8", "nmea", "COM10",
    )
    assert modem_hardware.resolve_settings("simcom", "auto", "uac", "COM30") == (
        "simcom", "COM8", "nmea", "COM30",
    )
    monkeypatch.setenv("MODEM_AUDIO_MODE", "uac")
    monkeypatch.setenv("MODEM_PCM_PORT", "COM30")
    assert modem_hardware.resolve_settings("simcom", "COM20", config.get_str("MODEM_AUDIO_MODE"), "COM30") == (
        "simcom", "COM20", "uac", "COM30",
    )


@pytest.mark.parametrize("error", [False, True])
def test_windows_pcm_native_com_without_pty_fallback(monkeypatch, error):
    import errno

    from agentcall.audio_bridge import SerialPcmAudioBridge

    monkeypatch.setattr(platforms, "IS_WINDOWS", True)
    calls = []
    sentinel = object()

    def fake_serial(**kwargs):
        calls.append(kwargs)
        if error:
            raise OSError(errno.ENOTTY, "Not a PTY")
        return sentinel

    monkeypatch.setattr("serial.Serial", fake_serial)
    bridge = SerialPcmAudioBridge("COM10", baudrate=921600)
    if error:
        with pytest.raises(OSError):
            bridge._open_serial()
    else:
        assert bridge._open_serial() is sentinel
    assert len(calls) == 1
    assert calls[0]["port"] == "COM10"
    assert calls[0]["baudrate"] == 921600


def test_frozen_windows_support_path(monkeypatch, tmp_path):
    monkeypatch.setattr(platforms, "IS_WINDOWS", True)
    monkeypatch.setattr(config, "_is_frozen", lambda: True)
    exe_dir = tmp_path / "CallPilot"
    exe_dir.mkdir()
    monkeypatch.setattr("sys.executable", str(exe_dir / "CallPilot.exe"), raising=False)
    monkeypatch.delenv("AGENTCALL_APP_SUPPORT_DIR", raising=False)
    assert config.app_support_dir() == exe_dir / config.PORTABLE_DATA_DIR
