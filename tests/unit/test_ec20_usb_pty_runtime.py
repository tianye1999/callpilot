"""Runtime helper tests for the bundled EC20 USB bridge."""

from __future__ import annotations

import errno

import pytest

pytest.importorskip("fcntl", reason="EC20 PTY bridge is POSIX-only")

import usb.core
import usb.util

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
    """只实现 bulk 写与 halt 恢复用到的 USB 调用面。"""

    def __init__(self, write_outcomes, clear_outcomes=()):
        self.write_outcomes = list(write_outcomes)
        self.clear_outcomes = list(clear_outcomes)
        self.writes = 0
        self.clear_halts = 0
        self.events = []

    def write(self, endpoint, data, timeout=None):
        self.writes += 1
        self.events.append(("write", endpoint, timeout))
        outcome = (
            self.write_outcomes.pop(0) if self.write_outcomes else None
        )
        if isinstance(outcome, int):
            return min(outcome, len(data))
        if outcome is not None:
            raise outcome
        return len(data)

    def clear_halt(self, endpoint):
        self.clear_halts += 1
        self.events.append(("clear_halt", endpoint))
        outcome = (
            self.clear_outcomes.pop(0) if self.clear_outcomes else None
        )
        if outcome is not None:
            raise outcome


def _drive_writes(outcomes):
    """用真实恢复 helper 跑 pty_to_usb 的超时计数策略。

    每个音频帧最多消耗两项 write outcome（首次写 + clear_halt 后重试）。
    """
    tolerance = ec20_usb_pty.WRITE_TIMEOUT_TOLERANCE
    dev = _FakeDev(outcomes)
    consecutive = 0
    died = False
    while dev.write_outcomes:
        try:
            ec20_usb_pty.write_bulk_with_recovery(dev, 0x05, b"\x00" * 320)
            consecutive = 0
        except usb.core.USBTimeoutError:
            consecutive += 1
            if consecutive >= tolerance:
                died = True
                break
    return dev, died


def test_write_stall_clears_halt_then_retries_same_frame():
    stalled = usb.core.USBError("pipe error", -9, errno.EPIPE)
    dev = _FakeDev([stalled, None])

    recovered = ec20_usb_pty.write_bulk_with_recovery(dev, 0x05, b"pcm")

    assert recovered is True
    assert dev.events == [
        ("write", 0x05, ec20_usb_pty.WRITE_TIMEOUT_MS),
        ("clear_halt", 0x05),
        ("write", 0x05, ec20_usb_pty.WRITE_TIMEOUT_MS),
    ]


def test_bulk_short_write_retries_remaining_pcm_without_losing_bytes():
    dev = _FakeDev([1, 2])

    recovered = ec20_usb_pty.write_bulk_with_recovery(dev, 0x05, b"pcm")

    assert recovered is False
    assert dev.writes == 2


def test_clear_halt_failure_is_distinct_from_retry_timeout():
    stalled = usb.core.USBError("pipe error", -9, errno.EPIPE)
    clear_failed = usb.core.USBError("clear failed")
    dev = _FakeDev([stalled], [clear_failed])

    with pytest.raises(ec20_usb_pty.EndpointRecoveryError, match="clear_halt"):
        ec20_usb_pty.write_bulk_with_recovery(dev, 0x05, b"pcm")


def test_timeout_is_not_misclassified_as_endpoint_stall():
    timeout = usb.core.USBTimeoutError("timed out", None, None)
    dev = _FakeDev([timeout])

    with pytest.raises(usb.core.USBTimeoutError):
        ec20_usb_pty.write_bulk_with_recovery(dev, 0x05, b"pcm")
    assert dev.clear_halts == 0


def test_write_all_fd_retries_short_writes_without_losing_pcm_bytes(monkeypatch):
    """PTY 短写不能丢字节；丢一个字节会让后续 int16 PCM 全部错位成噪声。"""
    accepted: list[bytes] = []
    limits = iter([1, 2, 3, 99])

    def fake_write(fd: int, data: memoryview) -> int:
        assert fd == 42
        chunk = bytes(data[: next(limits)])
        accepted.append(chunk)
        return len(chunk)

    monkeypatch.setattr(ec20_usb_pty.os, "write", fake_write)
    pcm = bytes(range(12))

    assert ec20_usb_pty.write_all_fd(42, pcm) is True
    assert b"".join(accepted) == pcm


def test_write_all_fd_reports_zero_progress(monkeypatch):
    monkeypatch.setattr(ec20_usb_pty.os, "write", lambda _fd, _data: 0)

    with pytest.raises(OSError, match="no progress"):
        ec20_usb_pty.write_all_fd(42, b"pcm")


def test_single_write_timeout_is_tolerated_and_next_frame_can_succeed():
    timeout = usb.core.USBTimeoutError("timed out", None, None)
    dev, died = _drive_writes([timeout, None])
    assert dev.writes == 2 and dev.clear_halts == 0 and died is False


def test_consecutive_timeouts_reset_on_success():
    """中间成功一次就该清零，否则长通话里零星超时会累积到误杀。"""
    t = usb.core.USBTimeoutError("timed out", None, None)
    outcomes = []
    for _ in range(6):
        outcomes += [t, t, None]        # 一帧恢复后仍超时，下一帧成功
    dev, died = _drive_writes(outcomes)
    assert died is False and dev.writes == len(outcomes)


def test_sustained_timeouts_eventually_declare_link_dead():
    """真的一直写不进去，还是要停——否则死链路上会无限空转。"""
    t = usb.core.USBTimeoutError("timed out", None, None)
    outcomes = [t] * (ec20_usb_pty.WRITE_TIMEOUT_TOLERANCE + 5)
    dev, died = _drive_writes(outcomes)
    assert died is True
    assert dev.writes == ec20_usb_pty.WRITE_TIMEOUT_TOLERANCE
    assert dev.clear_halts == 0


# ---- CDC 打开握手：claim 后置 DTR/RTS（SIMCom audio 口的可能门控）----


class _FakeControlDev:
    def __init__(self, error=None):
        self.error = error
        self.calls = []

    def ctrl_transfer(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if self.error is not None:
            raise self.error
        return 0


def test_set_control_line_state_sends_dtr_and_rts():
    dev = _FakeControlDev()

    assert ec20_usb_pty.set_control_line_state(dev, 4) is True
    assert dev.calls == [((0x21, 0x22, 0x03, 4), {"timeout": 1000})]


def test_unsupported_control_line_state_does_not_break_bridge():
    dev = _FakeControlDev(usb.core.USBError("pipe error"))

    assert ec20_usb_pty.set_control_line_state(dev, 4) is False


# ---- 串口状态 interrupt-IN：Linux option 驱动会持续轮询 ----


class _FakeEndpoint:
    def __init__(self, address, attrs, max_packet):
        self.bEndpointAddress = address
        self.bmAttributes = attrs
        self.wMaxPacketSize = max_packet


class _FakeInterface(list):
    def __init__(self, number, endpoints):
        super().__init__(endpoints)
        self.bInterfaceNumber = number


class _FakeDescriptorDev:
    def __init__(self, config):
        self.config = config

    def get_active_configuration(self):
        return self.config


def test_discover_ports_preserves_interrupt_in_endpoint():
    intf = _FakeInterface(4, [
        _FakeEndpoint(0x87, usb.util.ENDPOINT_TYPE_INTR, 16),
        _FakeEndpoint(0x88, usb.util.ENDPOINT_TYPE_BULK, 512),
        _FakeEndpoint(0x05, usb.util.ENDPOINT_TYPE_BULK, 512),
    ])

    port = ec20_usb_pty.discover_ports(_FakeDescriptorDev([intf]))[4]

    assert (port.bulk_in, port.bulk_out, port.max_packet) == (0x88, 0x05, 512)
    assert (port.interrupt_in, port.interrupt_max_packet) == (0x87, 16)


def test_interrupt_notifications_are_continuously_drained():
    stop = __import__("threading").Event()

    class Dev:
        reads = []

        def read(self, endpoint, size, timeout=None):
            self.reads.append((endpoint, size, timeout))
            stop.set()
            return b"\xa1\x20\x00\x00"

    dev = Dev()
    port = ec20_usb_pty.UsbPort(4, 0x88, 0x05, 512, 0x87, 16)

    ec20_usb_pty.drain_interrupt_notifications(dev, port, stop)

    assert dev.reads == [(0x87, 16, 100)]


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


# ---- 级联隔离：数据口坏掉不得连坐控制口（真机 2026-08-04：挂不掉电话）----


class _FakeHandle:
    """BridgeHandle 的最小替身，只提供监督循环用到的面。"""

    def __init__(self, interface: int, critical: bool):
        self.port = ec20_usb_pty.UsbPort(interface, 0x81, 0x01, 512)
        self.critical = critical
        self.stop = __import__("threading").Event()
        self.closed = False

    def close(self):
        self.closed = True


def _supervise(handles, stop, max_rounds=50):
    """复刻 run_bridges_once 的监督循环判定，返回 (是否退出, 存活句柄)。

    不起真 PTY/USB：这里要验的是"谁该被摘掉、谁该活着"的策略。
    """
    live = list(handles)
    for _ in range(max_rounds):
        if stop.is_set():
            return True, live
        if [h for h in live if h.critical and h.stop.is_set()]:
            return True, live
        for h in [h for h in live if not h.critical and h.stop.is_set()]:
            h.close()
            live.remove(h)
        if not live:
            return True, live
    return False, live


def test_data_interface_death_keeps_control_port_alive():
    """PCM 口判死时 AT 口必须继续服务，否则通话中 ATH 发不出去。"""
    at = _FakeHandle(2, critical=True)
    pcm = _FakeHandle(4, critical=False)
    stop = __import__("threading").Event()

    pcm.stop.set()                      # 模拟 interface 4 写超时判死
    exited, live = _supervise([at, pcm], stop, max_rounds=3)

    assert exited is False              # 整桥不退出
    assert pcm.closed is True           # 数据口被摘掉
    assert at.closed is False and at in live   # 控制口存活


def test_control_interface_death_exits_whole_bridge():
    """AT 口没了，桥继续跑没有意义——交给上层重建。"""
    at = _FakeHandle(2, critical=True)
    pcm = _FakeHandle(4, critical=False)
    stop = __import__("threading").Event()

    at.stop.set()
    exited, _live = _supervise([at, pcm], stop)

    assert exited is True


def test_all_data_ports_dead_without_control_exits():
    """只映射了数据口且全死，没什么可服务的了。"""
    a = _FakeHandle(4, critical=False)
    stop = __import__("threading").Event()
    a.stop.set()

    exited, live = _supervise([a], stop)

    assert exited is True
    assert live == [] and a.closed is True


def test_first_map_is_marked_critical(monkeypatch):
    """约定：首个 --map 是控制口。MODEM_BRIDGE_MAPS 默认也把 AT 放第一位。"""
    created: list[tuple[int, bool]] = []

    def fake_bridge_port(dev, port, link, critical=False):
        created.append((port.interface, critical))
        h = _FakeHandle(port.interface, critical)
        h.stop.set()          # 立刻置位，让监督循环尽快退出
        return h

    monkeypatch.setattr(ec20_usb_pty, "bridge_port", fake_bridge_port)
    monkeypatch.setattr(ec20_usb_pty, "discover_ports", lambda dev: {
        2: ec20_usb_pty.UsbPort(2, 0x84, 0x03, 512),
        4: ec20_usb_pty.UsbPort(4, 0x88, 0x05, 512),
    })
    monkeypatch.setattr(ec20_usb_pty.usb.util, "dispose_resources", lambda dev: None)

    ec20_usb_pty.run_bridges_once(
        object(), [(2, "/tmp/at"), (4, "/tmp/pcm")], __import__("threading").Event()
    )

    assert created == [(2, True), (4, False)]
