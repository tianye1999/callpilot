"""桥数据面实验使用假 USB 与时钟，不访问设备。"""

import json
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("fcntl", reason="USB PTY 桥仅支持 POSIX")
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts import ec20_usb_pty as bridge


@pytest.mark.parametrize("returns,expected", [([2, 2], [b"abcd", b"cd"]), ([0, 4], [b"abcd", b"abcd"])])
def test_usb_short_and_zero_write_continue(monkeypatch, returns, expected):
    seen = []
    results = iter(returns)
    stats = bridge.InterfaceStats(4)

    def write(endpoint, data, timeout):
        seen.append(bytes(data))
        return next(results)

    assert bridge.write_usb_bounded(SimpleNamespace(write=write), 1, b"abcd", threading.Event(), stats)
    assert seen == expected
    assert stats.snapshot()["total"]["out_bytes"] == 4
    assert stats.snapshot()["total"]["out_calls"] == 2
    assert stats.snapshot()["total"]["out_short"] == 1
    assert stats.snapshot()["total"]["out_timeouts"] == 0


@pytest.mark.parametrize("partial", [False, True])
def test_usb_timeout_preserves_actual_progress_and_first_snapshot(partial, caplog):
    calls = []
    stats = bridge.InterfaceStats(4)

    def write(endpoint, data, timeout):
        calls.append(bytes(data))
        if partial and len(calls) == 1:
            return 2
        raise bridge.usb.core.USBTimeoutError("fake timeout")

    for _ in range(2):
        with pytest.raises(bridge.usb.core.USBTimeoutError):
            bridge.write_usb_bounded(SimpleNamespace(write=write), 1, b"abcd", threading.Event(), stats)
    total = stats.snapshot()["total"]
    assert total["out_bytes"] == (2 if partial else 0)
    assert total["out_timeouts"] == 2
    assert sum("USB_OUT_FIRST_TIMEOUT" in r.message for r in caplog.records) == 1
    if partial:
        assert calls[:2] == [b"abcd", b"cd"]


def test_usb_zero_writes_share_deadline_without_timeout(monkeypatch):
    clock = [0.0]
    timeouts = []
    monkeypatch.setattr(bridge.time, "monotonic", lambda: clock[0])

    def write(endpoint, data, timeout):
        timeouts.append(timeout)
        clock[0] += 0.4
        return 0

    stats = bridge.InterfaceStats(4)
    assert not bridge.write_usb_bounded(SimpleNamespace(write=write), 1, b"abcd", threading.Event(), stats)
    assert timeouts == [1000, 600, 200]
    assert stats.snapshot()["total"]["out_timeouts"] == 0


def test_stats_five_second_active_summary_and_ten_second_ring(monkeypatch, caplog):
    clock = [0.0]
    monkeypatch.setattr(bridge.time, "monotonic", lambda: clock[0])
    stats = bridge.InterfaceStats(4)
    with caplog.at_level("INFO"):
        stats.record(in_calls=1)
        clock[0] = 5
        stats.record(in_calls=1)
        assert not caplog.records
        stats.record(in_bytes=320, out_bytes=160, out_calls=1, pty_dropped=7, out_max_ms=2.5)
        clock[0] = 10
        stats.record(in_calls=1)
        assert sum("USB_IO " in r.message for r in caplog.records) == 1
        clock[0] = 15
        stats.record(in_calls=1)
        assert sum("USB_IO " in r.message for r in caplog.records) == 1
        for second in range(16, 30):
            clock[0] = second
            stats.record(out_bytes=2, out_calls=1)
        stats.record(out_timeouts=1)
    snapshot = json.loads(next(r.message.split("snapshot=", 1)[1] for r in caplog.records
                               if "USB_OUT_FIRST_TIMEOUT" in r.message))
    assert len(snapshot["seconds"]) <= 11
    assert min(row["mono_second"] for row in snapshot["seconds"]) >= 19
    assert snapshot["total"]["pty_dropped"] == 7
    assert snapshot["total"]["out_max_ms"] == 2.5


@pytest.mark.parametrize("setting,reset", [(None, False), ("0", False), ("1", True)])
def test_auto_sim7600_startup_reset_mode(monkeypatch, tmp_path, caplog, setting, reset):
    from dataclasses import replace

    preset = replace(bridge.modem_hardware.PRESETS[1], recovery_request_path=str(tmp_path / "request"))
    monkeypatch.setattr(bridge.modem_hardware, "STATE_PATH", tmp_path / "state.json")
    monkeypatch.delenv("CALLPILOT_BRIDGE_RESET_ON_START", raising=False)
    if setting is not None:
        monkeypatch.setenv("CALLPILOT_BRIDGE_RESET_ON_START", setting)
    monkeypatch.setattr(bridge, "find_auto_device", lambda: (object(), preset))
    calls = []
    monkeypatch.setattr(bridge, "run_bridges_once", lambda *args, **kwargs: calls.append(kwargs) or False)
    with caplog.at_level("INFO"):
        assert bridge.run_auto(threading.Event(), once=True) == 0
    assert calls[0]["reset_first"] is reset
    assert ("首次启动复位模式：开启" if reset else "首次启动复位模式：关闭") in caplog.text
    assert "实验" not in caplog.text


@pytest.mark.parametrize("entry", ["auto_sim7600", "sim7600", "auto_ec20", "ec20"])
@pytest.mark.parametrize("setting", [None, "0", "1"])
def test_bridge_entry_reset_policy_on_start_fast_failure_and_normal_reconnect(
    monkeypatch, tmp_path, caplog, entry, setting,
):
    """只运行入口策略；假 USB、时钟和临时请求文件隔离正在运行的桥。"""
    from dataclasses import replace

    from scripts import sim7600_usb_pty

    sim7600 = entry.endswith("sim7600")
    preset = replace(bridge.modem_hardware.PRESETS[int(sim7600)],
                     recovery_request_path=str(tmp_path / "request") if sim7600 else None)
    assert preset.reset_on_start is False
    monkeypatch.setattr(sim7600_usb_pty, "RECOVERY_REQUEST_PATH", str(tmp_path / "request"))
    if setting is not None:
        monkeypatch.setenv("CALLPILOT_BRIDGE_RESET_ON_START", setting)
    monkeypatch.setattr(bridge, "find_auto_device", lambda: (object(), preset))
    monkeypatch.setattr(bridge, "wait_for_device", lambda *args, **kwargs: object())
    monkeypatch.setattr(bridge, "acquire_instance_lock", lambda: object())
    monkeypatch.setattr(bridge.signal, "signal", lambda *args: None)
    monkeypatch.setattr(threading.Event, "wait", lambda *args: False)
    clock = [100.0]
    monkeypatch.setattr(bridge.time, "monotonic", lambda: clock[0])
    resets = []

    def run(dev, maps, stop, **kwargs):
        resets.append(kwargs["reset_first"])
        if len(resets) == 1:
            raise bridge.usb.core.USBError("fake stalled endpoint", errno=bridge.errno.EIO)
        clock[0] += 10
        if len(resets) == 3:
            stop.set()
        return False

    monkeypatch.setattr(bridge, "run_bridges_once", run)
    with caplog.at_level("INFO"):
        if entry.startswith("auto"):
            monkeypatch.setattr(sys, "argv", ["bridge", "--auto"])
            assert bridge.main() == 0
        elif sim7600:
            monkeypatch.setattr(sys, "argv", ["sim7600_usb_pty"])
            assert sim7600_usb_pty.main() == 0
        else:
            monkeypatch.setattr(sys, "argv", ["bridge", "--map", "2:/fake-at"])
            assert bridge.main() == 0
    # EC20 的自动入口原本不复位；旧入口仍保留快速失败复位。
    assert resets == [sim7600 and setting == "1", entry != "auto_ec20", False]
    assert "实验" not in caplog.text


@pytest.mark.parametrize("hold", [False, True])
def test_pcm_stall_hold_preserves_at_and_ignores_recovery_request(monkeypatch, tmp_path, caplog, hold):
    monkeypatch.delenv("CALLPILOT_BRIDGE_HOLD_ON_PCM_STALL", raising=False)
    if hold:
        monkeypatch.setenv("CALLPILOT_BRIDGE_HOLD_ON_PCM_STALL", "1")
    stop = threading.Event()
    closed = []
    handles = [SimpleNamespace(port=SimpleNamespace(interface=i), stop=threading.Event(), degraded=i == 4,
                               closed=False, stats=bridge.InterfaceStats(i), threads=[],
                               close=lambda i=i: closed.append(i)) for i in (2, 4)]
    handles[1].stop.set()
    ports = {2: handles[0].port, 4: handles[1].port}
    monkeypatch.setattr(bridge, "discover_ports", lambda dev: ports)
    monkeypatch.setattr(bridge, "bridge_port", lambda dev, port, link: handles[0 if port.interface == 2 else 1])
    monkeypatch.setattr(bridge.usb.util, "dispose_resources", lambda dev: closed.append("dispose"))
    request = tmp_path / "request"

    def heartbeat():
        assert hold
        assert closed == [4]
        assert not handles[0].stop.is_set()
        request.write_text("degraded", encoding="utf-8")

    def sleep(seconds):
        assert not handles[0].stop.is_set()
        stop.set()

    monkeypatch.setattr(bridge.time, "sleep", sleep)
    device = SimpleNamespace(idVendor=0x1E0E, idProduct=0x9001)
    result = bridge.run_bridges_once(device, [(2, "/fake-at"), (4, "/fake-pcm")], stop,
                                     recovery_request=request, heartbeat=heartbeat)
    assert result is (not hold)
    assert closed[-1] == "dispose"
    assert sum("PCM_STALL_HOLD" in r.message for r in caplog.records) == int(hold)
    if hold:
        assert request.exists()


def test_bridge_loop_short_zero_and_timeout_count_rules(monkeypatch, caplog):
    """直接运行假 PTY 的写循环，完整块重置计数，零写不增计数，四次超时才拆桥。"""
    clock = [0.0]
    monkeypatch.setattr(bridge.time, "monotonic", lambda: clock[0])
    results = iter(["timeout", "timeout", 2, 2, "timeout", 0, "timeout", "timeout", "timeout"])
    payloads = []

    def write(endpoint, data, timeout):
        payloads.append(bytes(data))
        result = next(results)
        clock[0] += 1.01 if result == 0 else 0.1
        if result == "timeout":
            raise bridge.usb.core.USBTimeoutError("fake timeout")
        return result

    monkeypatch.setattr(bridge.pty, "openpty", lambda: (101, 102))
    monkeypatch.setattr(bridge, "make_raw", lambda fd: None)
    monkeypatch.setattr(bridge.os, "set_blocking", lambda *args: None)
    monkeypatch.setattr(bridge.os, "ttyname", lambda fd: "/fake-pty")
    monkeypatch.setattr(bridge, "link_pty", lambda *args: None)
    monkeypatch.setattr(bridge.usb.util, "claim_interface", lambda *args: None)
    monkeypatch.setattr(bridge.threading, "Thread", lambda target, **kwargs:
                        SimpleNamespace(target=target, start=lambda: None))
    monkeypatch.setattr(bridge.select, "select", lambda *args: ([101], [], []))
    monkeypatch.setattr(bridge.os, "read", lambda *args: b"abcd")
    handle = bridge.bridge_port(SimpleNamespace(write=write), bridge.UsbPort(4, 1, 2, 512), "/fake-link")
    handle.threads[1].target()
    assert payloads[2:4] == [b"abcd", b"cd"]
    assert handle.degraded and handle.stop.is_set()
    total = handle.stats.snapshot()["total"]
    assert total["out_calls"] == 9
    assert total["out_timeouts"] == 6
    assert total["out_short"] == 2
    assert total["out_bytes"] == 4
    assert sum("USB 写超时，丢帧继续" in r.message for r in caplog.records) == 5
    assert "连续写超时 4 次，判定掉线，触发重连" in caplog.text


def test_usb_non_timeout_error_does_not_increment_timeout_count():
    def write(*args, **kwargs):
        raise bridge.usb.core.USBError("disconnected", errno=bridge.errno.ENODEV)

    stats = bridge.InterfaceStats(4)
    with pytest.raises(bridge.usb.core.USBError):
        bridge.write_usb_bounded(SimpleNamespace(write=write), 1, b"abcd", threading.Event(), stats)
    total = stats.snapshot()["total"]
    assert total["out_calls"] == 1
    assert total["out_timeouts"] == 0


@pytest.mark.parametrize("setting", [None, "0", "1"])
@pytest.mark.parametrize("reset_first", [False, True])
def test_run_bridges_once_honors_reset_decision_regardless_of_env(
    monkeypatch, caplog, setting, reset_first,
):
    monkeypatch.delenv("CALLPILOT_BRIDGE_RESET_ON_START", raising=False)
    if setting is not None:
        monkeypatch.setenv("CALLPILOT_BRIDGE_RESET_ON_START", setting)
    calls = []
    dev = SimpleNamespace(reset=lambda: calls.append("reset"))
    monkeypatch.setattr(bridge.time, "sleep", lambda seconds: calls.append(("sleep", seconds)))
    monkeypatch.setattr(bridge, "discover_ports", lambda dev: calls.append("discover") or {})
    monkeypatch.setattr(bridge.usb.util, "dispose_resources", lambda dev: calls.append("dispose"))
    stop = threading.Event()
    stop.set()
    with caplog.at_level("INFO"):
        assert bridge.run_bridges_once(dev, [], stop, reset_first=reset_first) is False
    assert calls == (["reset", ("sleep", 1.0)] if reset_first else []) + ["discover", "dispose"]
    assert [r.message for r in caplog.records] == (
        ["已复位 USB 设备（清除 stall 端点）"] if reset_first else []
    )
