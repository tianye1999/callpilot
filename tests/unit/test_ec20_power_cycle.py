"""桥恢复提示：只用 USB 替身，不打开真实 PTY 或串口。"""

import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("fcntl", reason="EC20 PTY bridge is POSIX-only")
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts import ec20_usb_pty as bridge


class Stop:
    def __init__(self, waits=100):
        self.stopped = False
        self.waits = waits

    def is_set(self):
        return self.stopped

    def set(self):
        self.stopped = True

    def wait(self, timeout):
        self.waits -= 1
        if self.waits == 0:
            self.set()
        return self.stopped


@pytest.fixture(autouse=True)
def isolate_bridge(monkeypatch, tmp_path):
    hardware = bridge.modem_hardware
    request = str(tmp_path / "request")
    monkeypatch.setattr(hardware, "PRESETS", tuple(
        replace(p, recovery_request_path=request) if p.recovery_request_path else p for p in hardware.PRESETS
    ))
    monkeypatch.setattr(bridge, "acquire_instance_lock", lambda: object())
    monkeypatch.setattr(bridge.signal, "signal", lambda *args: None)


@pytest.mark.parametrize("recovered", [False, True])
def test_ec20_auto_power_cycle_recovery_result(monkeypatch, recovered):
    hardware = bridge.modem_hardware
    hardware.write_needs_power_cycle("old failure")
    stop = Stop()
    scans = [0]

    def find():
        scans[0] += 1
        if scans[0] == 2:
            assert (hardware.read_needs_power_cycle() is None) is recovered
            stop.set()
        return object(), hardware.PRESETS[1]

    monkeypatch.setattr(bridge, "find_auto_device", find)
    monkeypatch.setattr(bridge, "run_bridges_once", lambda *args, **kwargs: True)
    monkeypatch.setattr(bridge, "usb_composition_recovery", lambda *args: recovered)
    assert bridge.run_auto(stop, poll_seconds=0) == 0
    marker = hardware.read_needs_power_cycle()
    if not recovered:
        assert marker is not None and marker["reason"] != "old failure"


@pytest.mark.parametrize("recovered", [False, True])
def test_ec20_main_power_cycle_recovery_result(monkeypatch, recovered):
    hardware = bridge.modem_hardware
    hardware.write_needs_power_cycle("old failure")
    waits = [0]

    def wait(stop, **kwargs):
        waits[0] += 1
        if waits[0] == 2:
            assert (hardware.read_needs_power_cycle() is None) is recovered
            stop.set()
            return None
        return object()

    monkeypatch.setattr(bridge.sys, "argv", ["bridge", "--map", "2:/tmp/unused-test-at"])
    monkeypatch.setattr(bridge, "wait_for_device", wait)
    monkeypatch.setattr(bridge, "run_bridges_once", lambda *args, **kwargs: True)
    monkeypatch.setattr(bridge, "usb_composition_recovery", lambda *args: recovered)
    assert bridge.main(recover_alt_pid=0x9011) == 0
    marker = hardware.read_needs_power_cycle()
    if not recovered:
        assert marker is not None and marker["reason"] != "old failure"


@pytest.mark.parametrize("entry", ["auto", "main"])
def test_ec20_bridge_restart_does_not_clear_power_cycle(monkeypatch, entry):
    hardware = bridge.modem_hardware
    hardware.write_needs_power_cycle("still requires physical reset")
    old = hardware.read_needs_power_cycle()
    monkeypatch.setattr(bridge, "run_bridges_once", lambda *args, **kwargs: False)
    if entry == "auto":
        monkeypatch.setattr(bridge, "find_auto_device", lambda: (object(), hardware.PRESETS[1]))
        assert bridge.run_auto(Stop(), once=True) == 0
    else:
        monkeypatch.setattr(bridge.sys, "argv", ["bridge", "--map", "2:/tmp/unused-test-at", "--once"])
        monkeypatch.setattr(bridge, "wait_for_device", lambda *args, **kwargs: object())
        assert bridge.main() == 0
    assert hardware.read_needs_power_cycle() == old


@pytest.mark.parametrize("observation", ["absent", "scan_error"])
def test_ec20_auto_power_cycle_clear_requires_confirmed_disappearance(monkeypatch, observation):
    hardware = bridge.modem_hardware
    hardware.write_needs_power_cycle("failed")
    stop = Stop(waits=2)
    scans = [0]

    def find():
        scans[0] += 1
        if scans[0] == 1:
            return object(), hardware.PRESETS[1]
        if observation == "scan_error":
            raise bridge.usb.core.USBError("test scan failure")
        return None

    monkeypatch.setattr(bridge, "find_auto_device", find)
    monkeypatch.setattr(bridge, "run_bridges_once", lambda *args, **kwargs: False)
    assert bridge.run_auto(stop, poll_seconds=0) == 0
    assert (hardware.read_needs_power_cycle() is None) is (observation == "absent")


def test_ec20_auto_start_without_device_is_not_disappearance(monkeypatch):
    hardware = bridge.modem_hardware
    hardware.write_needs_power_cycle("failed")
    monkeypatch.setattr(bridge, "find_auto_device", lambda: None)
    assert bridge.run_auto(Stop(waits=1), poll_seconds=0) == 0
    assert hardware.read_needs_power_cycle() is not None


def test_ec20_main_wait_clears_power_cycle_only_when_device_absent(monkeypatch):
    hardware = bridge.modem_hardware
    hardware.write_needs_power_cycle("failed")

    def absent(*args, **kwargs):
        raise RuntimeError("test device absent")

    monkeypatch.setattr(bridge, "find_device", absent)
    assert bridge.wait_for_device(Stop(waits=1), poll_seconds=0) is None
    assert hardware.read_needs_power_cycle() is None


def test_ec20_main_usb_scan_error_is_not_power_cycle_evidence(monkeypatch):
    hardware = bridge.modem_hardware
    hardware.write_needs_power_cycle("failed")

    def failed_scan(*args, **kwargs):
        raise bridge.usb.core.USBError("test scan failed")

    monkeypatch.setattr(bridge, "find_device", failed_scan)
    with pytest.raises(bridge.usb.core.USBError):
        bridge.wait_for_device(Stop(waits=1), poll_seconds=0)
    assert hardware.read_needs_power_cycle() is not None


@pytest.mark.parametrize("entry", ["auto", "main"])
@pytest.mark.parametrize("unplug", [False, True])
def test_ec20_power_cycle_kept_while_stuck_in_alt_composition(monkeypatch, entry, unplug):
    hardware = bridge.modem_hardware
    preset = hardware.PRESETS[1]
    device = SimpleNamespace(idVendor=preset.vid, idProduct=preset.pid)
    stop = Stop(waits=3)
    present = [True]
    wait = stop.wait

    def wait_then_unplug(timeout):
        if unplug and stop.waits == 2:
            present[0] = False
        return wait(timeout)

    stop.wait = wait_then_unplug

    def find(**kwargs):
        if kwargs.get("find_all"):
            return [device] if present[0] else []
        return device if present[0] and kwargs.get("idProduct", device.idProduct) == device.idProduct else None

    def failed_recovery(*args):
        device.idProduct = preset.recover_alt_pid
        return False

    monkeypatch.setattr(bridge, "libusb_backend", lambda: None)
    monkeypatch.setattr(bridge.usb.core, "find", find)
    if entry == "auto":
        monkeypatch.setattr(bridge, "run_bridges_once", lambda *args, **kwargs: True)
        monkeypatch.setattr(bridge, "usb_composition_recovery", failed_recovery)
        assert bridge.run_auto(stop, poll_seconds=0) == 0
    else:
        failed_recovery()
        hardware.write_needs_power_cycle("failed")
        assert bridge.wait_for_device(stop, poll_seconds=0, vid=preset.vid, pid=preset.pid) is None
    assert (hardware.read_needs_power_cycle() is None) is unplug
