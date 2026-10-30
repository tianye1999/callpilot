"""物理重插提示的跨进程标记、来电恢复证据与托盘优先级。"""

import json

import pytest

from agentcall import macos_shell
from agentcall import modem_hardware as hardware
from agentcall.modem import Sim7600Modem


def test_power_cycle_marker_atomic_write_persists_without_expiry(monkeypatch):
    path = hardware.NEEDS_POWER_CYCLE_PATH
    now = [100.0]
    monkeypatch.setattr(hardware.time, "time", lambda: now[0])
    replaced = []
    replace = hardware.os.replace

    def observe_replace(source, target):
        assert target == path
        assert hardware.Path(source).parent == path.parent
        assert json.loads(hardware.Path(source).read_text(encoding="utf-8"))["reason"] == "composition recovery failed"
        replaced.append(target)
        replace(source, target)

    monkeypatch.setattr(hardware.os, "replace", observe_replace)
    hardware.write_needs_power_cycle("composition recovery failed")
    first = hardware.read_needs_power_cycle()
    assert first is not None
    assert first["reason"] == "composition recovery failed"
    assert any(value == 100 for value in first.values()), "标记必须带写入时间戳"
    assert replaced == [path]
    assert list(path.parent.iterdir()) == [path]
    now[0] += 90 * 86400
    assert hardware.read_needs_power_cycle() == first
    hardware.clear_needs_power_cycle()
    hardware.clear_needs_power_cycle()
    assert hardware.read_needs_power_cycle() is None


@pytest.mark.parametrize("content", ["", "{", "[]", "null", "\"bad\""])
def test_power_cycle_marker_unreadable_or_invalid_is_absent(content, monkeypatch):
    path = hardware.NEEDS_POWER_CYCLE_PATH
    assert hardware.read_needs_power_cycle() is None
    path.write_text(content)
    assert hardware.read_needs_power_cycle() is None

    def unreadable(*args, **kwargs):
        raise PermissionError("test unreadable")

    monkeypatch.setattr(hardware.Path, "read_text", unreadable)
    assert hardware.read_needs_power_cycle() is None


@pytest.mark.parametrize("source", ["ring", "clcc"])
def test_power_cycle_cleared_by_actual_incoming_call(source):
    modem = Sim7600Modem("unused-fake-port")
    hardware.write_needs_power_cycle("failed")
    if source == "ring":
        modem._buffer = "\r\nRING\r\n"
        modem._process_buffer()
    else:
        modem._process_clcc_response('+CLCC: 1,1,4,0,0,"+15555550123",145\r\nOK\r\n')
    assert hardware.read_needs_power_cycle() is None


@pytest.mark.parametrize("response", ["OK\r\n", '+CLCC: 1,0,0,0,0,"+15555550123",145\r\nOK\r\n'])
def test_power_cycle_not_cleared_by_at_health_or_outbound_call(response):
    modem = Sim7600Modem("unused-fake-port")
    hardware.write_needs_power_cycle("failed")
    modem._process_clcc_response(response)
    assert hardware.read_needs_power_cycle() is not None


@pytest.mark.parametrize("lang", ["zh", "en"])
def test_tray_power_cycle_overrides_registered_signal_but_not_call_or_service_down(lang):
    meta = {
        "hardware": {"usb_online": True, "modem_connected": True, "needs_power_cycle": True,
                     "signal": {"level": "excellent"}},
        "sim": {"present": True, "network_attached": True, "registered": True},
        "credentials": {"ok": True},
    }
    result = macos_shell.tray_status(meta, uptime=100, now=1000, lang=lang)
    assert result.state == "attention"
    if lang == "zh":
        assert result.title == "模组需要重新插拔：拔下 USB，等 5 秒再插上"
    else:
        assert "USB" in result.title and "5 seconds" in result.title
    meta["call_active"] = True
    assert macos_shell.tray_status(meta, uptime=100, now=1000, lang=lang).state == "in_call"
    unavailable = macos_shell.tray_status(None, uptime=100, now=1000, lang=lang)
    assert unavailable.state == "attention"
    assert ("服务没有运行" if lang == "zh" else "Service is not running") in unavailable.title


def test_tray_power_cycle_false_keeps_ready_state():
    meta = {"hardware": {"modem_connected": True, "needs_power_cycle": False},
            "sim": {"network_attached": True}}
    assert macos_shell.tray_status(meta, uptime=100, now=1000).state == "ready"
