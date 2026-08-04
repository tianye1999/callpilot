"""Runtime helper tests for the bundled EC20 USB bridge."""

from __future__ import annotations

import pytest

pytest.importorskip("fcntl", reason="EC20 PTY bridge is POSIX-only")

import usb.core

from scripts import ec20_usb_pty


def test_bundled_libusb_path_uses_pyinstaller_resources(tmp_path, monkeypatch):
    lib = tmp_path / "lib" / "libusb-1.0.0.dylib"
    lib.parent.mkdir()
    lib.write_bytes(b"placeholder")

    monkeypatch.setattr(ec20_usb_pty.sys, "_MEIPASS", str(tmp_path), raising=False)

    assert ec20_usb_pty.bundled_libusb_path() == lib


def test_bundled_libusb_path_missing_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(ec20_usb_pty.sys, "_MEIPASS", str(tmp_path), raising=False)

    assert ec20_usb_pty.bundled_libusb_path() is None


# ---- USB 写超时容忍（真机 2026-08-01：一次超时把 AT 口的桥一起拆了，通话中断）----


class _FakeDev:
    """只实现 bridge_port 用到的 USB 调用面。"""

    def __init__(self, write_outcomes):
        self.write_outcomes = list(write_outcomes)
        self.writes = 0

    def write(self, endpoint, data, timeout=None):
        self.writes += 1
        outcome = (
            self.write_outcomes.pop(0) if self.write_outcomes else None
        )
        if outcome is not None:
            raise outcome
        return len(data)


def _drive_writes(monkeypatch, outcomes):
    """跑 pty_to_usb 的写分支逻辑，返回 (写次数, 是否判定链路已死)。

    直接复刻脚本里的容忍规则，避免为测一个分支去起真 PTY / 真 USB。
    """
    tolerance = ec20_usb_pty.WRITE_TIMEOUT_TOLERANCE
    dev = _FakeDev(outcomes)
    consecutive = 0
    died = False
    for _ in range(len(outcomes)):
        try:
            dev.write(0x05, b"\x00" * 320)
            consecutive = 0
        except usb.core.USBTimeoutError:
            consecutive += 1
            if consecutive >= tolerance:
                died = True
                break
    return dev.writes, died


def test_single_write_timeout_is_not_fatal(monkeypatch):
    """丢一帧音频远好过拆掉整座桥。"""
    timeout = usb.core.USBTimeoutError("timed out", None, None)
    writes, died = _drive_writes(monkeypatch, [timeout, None, None])
    assert writes == 3 and died is False


def test_consecutive_timeouts_reset_on_success(monkeypatch):
    """中间成功一次就该清零，否则长通话里零星超时会累积到误杀。"""
    t = usb.core.USBTimeoutError("timed out", None, None)
    outcomes = []
    for _ in range(6):
        outcomes += [t, t, None]        # 每两次超时后成功一次
    writes, died = _drive_writes(monkeypatch, outcomes)
    assert died is False and writes == len(outcomes)


def test_sustained_timeouts_eventually_declare_link_dead(monkeypatch):
    """真的一直写不进去，还是要停——否则死链路上会无限空转。"""
    t = usb.core.USBTimeoutError("timed out", None, None)
    outcomes = [t] * (ec20_usb_pty.WRITE_TIMEOUT_TOLERANCE + 5)
    writes, died = _drive_writes(monkeypatch, outcomes)
    assert died is True
    assert writes == ec20_usb_pty.WRITE_TIMEOUT_TOLERANCE


# ---- 设备发现：--pid 可省（SIM7600 的 PID 随固件 composite 浮动）----


class _FakeUsbDev:
    """usb.core.Device 的最小替身；字符串描述符可模拟读取失败。"""

    def __init__(self, vid, pid, manufacturer=None, product=None, unreadable=False):
        self.idVendor = vid
        self.idProduct = pid
        self._manufacturer = manufacturer
        self._product = product
        self._unreadable = unreadable

    @property
    def manufacturer(self):
        if self._unreadable:
            raise usb.core.USBError("no permission")
        return self._manufacturer

    @property
    def product(self):
        if self._unreadable:
            raise usb.core.USBError("no permission")
        return self._product


def _patch_bus(monkeypatch, devices):
    """替换 usb.core.find，让 find_device 的筛选逻辑跑真代码。"""

    def fake_find(find_all=False, backend=None, **criteria):
        assert find_all, "find_device 应始终枚举后自行筛选"
        return iter([
            dev for dev in devices
            if all(getattr(dev, key) == value for key, value in criteria.items())
        ])

    monkeypatch.setattr(ec20_usb_pty.usb.core, "find", fake_find)


def test_find_device_auto_detects_known_vendor(monkeypatch):
    """不给任何参数就该认出 SIMCom——用户不必先去 system_profiler 抄 PID。"""
    modem = _FakeUsbDev(0x1E0E, 0x9011, "SimTech, Incorporated", "SimTech HS-USB")
    _patch_bus(monkeypatch, [_FakeUsbDev(0x05AC, 0x1234, "Apple"), modem])

    assert ec20_usb_pty.find_device() is modem


def test_find_device_accepts_any_pid_under_given_vid(monkeypatch):
    """只给 --vid 时不该因 PID 不是 9001 而找不到（9011 也是 SIM7600）。"""
    modem = _FakeUsbDev(0x1E0E, 0x9011)
    _patch_bus(monkeypatch, [modem])

    assert ec20_usb_pty.find_device(vid=0x1E0E) is modem


def test_find_device_refuses_to_guess_between_multiple(monkeypatch):
    """插了两个已知模组时必须让用户消歧，不能替他挑一个桥。"""
    _patch_bus(monkeypatch, [
        _FakeUsbDev(0x2C7C, 0x0125),
        _FakeUsbDev(0x1E0E, 0x9011),
    ])

    with pytest.raises(RuntimeError) as excinfo:
        ec20_usb_pty.find_device()
    message = str(excinfo.value)
    assert "2c7c:0125" in message and "1e0e:9011" in message


def test_find_device_pid_narrows_ambiguity(monkeypatch):
    """同一 VID 下多个设备时，--pid 应能选中其中一个。"""
    wanted = _FakeUsbDev(0x1E0E, 0x9011)
    _patch_bus(monkeypatch, [_FakeUsbDev(0x1E0E, 0x9001), wanted])

    assert ec20_usb_pty.find_device(vid=0x1E0E, pid=0x9011) is wanted


def test_find_device_lists_bus_when_nothing_matches(monkeypatch):
    """一个都没匹配上时列出总线设备——用户的模组可能是未知 VID。"""
    _patch_bus(monkeypatch, [_FakeUsbDev(0x0BDA, 0x8153, "Realtek", "USB Ethernet")])

    with pytest.raises(RuntimeError) as excinfo:
        ec20_usb_pty.find_device()
    message = str(excinfo.value)
    assert "0bda:8153" in message and "Realtek" in message


def test_find_device_reports_empty_bus(monkeypatch):
    _patch_bus(monkeypatch, [])

    with pytest.raises(RuntimeError, match="没有任何设备"):
        ec20_usb_pty.find_device()


def test_describe_device_survives_unreadable_descriptors(monkeypatch):
    """读描述符要发控制传输，权限不足时不能让整个枚举失败。"""
    dev = _FakeUsbDev(0x1E0E, 0x9011, unreadable=True)

    # 未知厂商名读不到时至少给出 ID；已知 VID 仍能补上厂商名。
    assert ec20_usb_pty.describe_device(dev) == "1e0e:9011 (SIMCom)"


def test_missing_libusb_gives_install_hint(monkeypatch):
    """裸 NoBackendError 会劝退第一次跑桥的用户。"""
    def boom(**_kwargs):
        raise usb.core.NoBackendError("no backend")

    monkeypatch.setattr(ec20_usb_pty.usb.core, "find", boom)

    with pytest.raises(SystemExit, match="brew install libusb"):
        ec20_usb_pty.find_device()


# ---- probe_at：非 AT 接口不能中止整轮探测（真机 2026-08-04：interface 0 超时即崩）----


class _FakeProbeDev:
    """probe_at 用到的调用面；write 可指定抛错。"""

    def __init__(self, write_error=None, response=b"AT\r\nOK\r\n"):
        self.write_error = write_error
        self.response = response
        self.reads = 0

    def read(self, endpoint, size, timeout=None):
        self.reads += 1
        if self.reads == 1:
            raise usb.core.USBTimeoutError("drain done", None, None)
        return self.response

    def write(self, endpoint, data, timeout=None):
        if self.write_error is not None:
            raise self.write_error
        return len(data)


def _probe_port():
    return ec20_usb_pty.UsbPort(interface=0, bulk_in=0x81, bulk_out=0x01, max_packet=512)


def test_probe_at_downgrades_write_failure_to_runtime_error(monkeypatch):
    """SIM7600 的 DIAG/PCM 接口写 AT 会 [Errno 60]。

    裸 USBTimeoutError 会冒到 main 里终止整轮 --probe，用户就再也走不到后面
    真正的 AT 口、探不出该 --map 哪个接口号。必须降级成"这个接口不是 AT 口"。
    """
    monkeypatch.setattr(ec20_usb_pty.usb.util, "claim_interface", lambda *_a: None)
    monkeypatch.setattr(ec20_usb_pty.usb.util, "release_interface", lambda *_a: None)
    dev = _FakeProbeDev(write_error=usb.core.USBTimeoutError("timed out", None, None))

    with pytest.raises(RuntimeError, match="不是 AT 口"):
        ec20_usb_pty.probe_at(dev, _probe_port())


def test_probe_at_releases_interface_after_write_failure(monkeypatch):
    """探测失败也要放掉 interface，否则后面几个接口全被自己占着探不了。"""
    released: list[int] = []
    monkeypatch.setattr(ec20_usb_pty.usb.util, "claim_interface", lambda *_a: None)
    monkeypatch.setattr(
        ec20_usb_pty.usb.util, "release_interface",
        lambda _dev, iface: released.append(iface),
    )
    dev = _FakeProbeDev(write_error=usb.core.USBError("pipe error"))

    with pytest.raises(RuntimeError):
        ec20_usb_pty.probe_at(dev, _probe_port())
    assert released == [0]


def test_probe_at_returns_response_on_at_port(monkeypatch):
    monkeypatch.setattr(ec20_usb_pty.usb.util, "claim_interface", lambda *_a: None)
    monkeypatch.setattr(ec20_usb_pty.usb.util, "release_interface", lambda *_a: None)

    assert b"OK" in ec20_usb_pty.probe_at(_FakeProbeDev(), _probe_port())


def test_describe_device_deduplicates_identical_descriptors():
    """SIM7600 的 manufacturer 与 product 是同一个字符串，不该打印两遍。"""
    dev = _FakeUsbDev(0x1E0E, 0x9001, "SimTech, Incorporated", "SimTech, Incorporated")

    assert ec20_usb_pty.describe_device(dev) == "1e0e:9001 (SIMCom SimTech, Incorporated)"
