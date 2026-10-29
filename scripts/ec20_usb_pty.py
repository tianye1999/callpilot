"""Expose Quectel EC20 USB vendor serial interfaces as macOS PTYs.

macOS can see EC20/EG25 USB interfaces but does not create /dev/cu.* ports for
Quectel vendor-specific serial functions. This bridge talks to the bulk USB
endpoints with libusb/PyUSB and presents a pseudo terminal for pyserial.
"""

from __future__ import annotations

import argparse
import errno
import fcntl
import json
import logging
import logging.handlers
import os
import pty
import select
import signal
import sys
import termios
import threading
import time
import tty
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import usb.backend.libusb1
import usb.core
import usb.util

# 独立脚本不要求 editable 安装；打包版继续使用 bundle 内的模块。
if not getattr(sys, "frozen", False):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from agentcall import modem_hardware  # noqa: E402

logger = logging.getLogger("ec20_usb_pty")

VID = modem_hardware.PRESETS[0].vid
PID = modem_hardware.PRESETS[0].pid

# 连续写超时容忍上限：≤此值视作瞬时忙丢帧继续，超过判定设备掉线拆桥重连。
PTY_WRITE_TIMEOUT_TOLERANCE = 3
# USB→PTY 每块数据的总背压预算，容忍服务短暂调度迟滞但不无限等待。
USB_TO_PTY_WRITE_TIMEOUT = 0.5
PTY_TO_USB_WRITE_TIMEOUT = 1.0

LOCK_PATH = Path("/tmp/ec20-usb-pty.lock")


class BridgeShutdownError(Exception):
    """无法安全关闭时必须退出进程，不允许外层带着活动线程原地重连。"""


class InterfaceStats:
    """只保留计数，不保存音频；两个方向共用有界逐秒环形缓冲。"""

    def __init__(self, interface: int) -> None:
        self.interface = interface
        self._lock = threading.Lock()
        self._total = self._empty()
        self._period = self._empty()
        self._seconds: deque[dict] = deque(maxlen=11)
        self._logged_at = time.monotonic()
        self._first_timeout = False

    @staticmethod
    def _empty() -> dict:
        return dict(in_bytes=0, in_calls=0, out_bytes=0, out_calls=0,
                    out_short=0, out_timeouts=0, pty_dropped=0, out_max_ms=0.0)

    def _prune(self, now: float) -> None:
        while self._seconds and self._seconds[0]["mono_second"] < int(now) - 10:
            self._seconds.popleft()

    def snapshot(self) -> dict:
        with self._lock:
            self._prune(time.monotonic())
            return {"total": self._total.copy(), "seconds": [row.copy() for row in self._seconds]}

    def record(self, **values: float) -> None:
        summary = snapshot = None
        with self._lock:
            now = time.monotonic()
            self._prune(now)
            if not self._seconds or self._seconds[-1]["mono_second"] != int(now):
                self._seconds.append({"mono_second": int(now), **self._empty()})
            for target in (self._total, self._period, self._seconds[-1]):
                for key, value in values.items():
                    target[key] = max(target[key], value) if key == "out_max_ms" else target[key] + value
            if values.get("out_timeouts") and not self._first_timeout:
                self._first_timeout = True
                snapshot = {"total": self._total.copy(), "seconds": [row.copy() for row in self._seconds]}
            if now - self._logged_at >= 5:
                # 空闲 IN 超时轮询不算流量；OUT 尝试即使零写也要可见。
                if any(self._period[k] for k in ("in_bytes", "out_calls", "pty_dropped")):
                    summary = self._period
                self._period = self._empty()
                self._logged_at = now
        if summary is not None:
            logger.info("USB_IO interface=%d mono=%.6f %s", self.interface, now,
                        " ".join(f"{key}={value}" for key, value in summary.items()))
        if snapshot is not None:
            logger.warning("USB_OUT_FIRST_TIMEOUT interface=%d mono=%.6f snapshot=%s",
                           self.interface, now, json.dumps(snapshot, ensure_ascii=False))


def write_usb_bounded(dev, endpoint: int, data: bytes, stop: threading.Event, stats: InterfaceStats) -> bool:
    """短写只续写剩余部分；整块共享时限，零写不冒充 USBTimeoutError。"""
    offset = 0
    deadline = time.monotonic() + PTY_TO_USB_WRITE_TIMEOUT
    while offset < len(data) and not stop.is_set():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        started = time.monotonic()
        try:
            written = dev.write(endpoint, data[offset:], timeout=max(1, round(remaining * 1000)))
        except usb.core.USBError as exc:
            timeout = isinstance(exc, usb.core.USBTimeoutError) or exc.errno == errno.ETIMEDOUT
            stats.record(out_calls=1, out_timeouts=int(timeout), out_max_ms=(time.monotonic() - started) * 1000)
            raise
        stats.record(out_calls=1, out_bytes=written, out_short=int(written < len(data) - offset),
                     out_max_ms=(time.monotonic() - started) * 1000)
        offset += written
        if written == 0:
            stop.wait(min(0.005, max(0, deadline - time.monotonic())))
    return offset == len(data)


def bundled_libusb_path() -> Path | None:
    """Return bundled libusb dylib path when running from the macOS app."""
    base = getattr(sys, "_MEIPASS", None)
    if not base:
        return None
    candidate = Path(base) / "lib" / "libusb-1.0.0.dylib"
    return candidate if candidate.is_file() else None


def libusb_backend():
    """PyUSB backend, preferring the dylib bundled in CallPilot.app."""
    bundled = bundled_libusb_path()
    if bundled is None:
        return None

    def find_library(_name: str) -> str:
        return str(bundled)

    return usb.backend.libusb1.get_backend(find_library=find_library)


def acquire_instance_lock() -> object:
    """进程唯一锁：防止两个桥实例争抢 USB claim 导致双双不可用。

    返回持有的文件对象（进程退出自动释放）；已有实例时报错。
    """
    lock_file = LOCK_PATH.open("a+", encoding="utf-8")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock_file.seek(0)
        holder = lock_file.read().strip() or "未知"
        raise RuntimeError(
            f"另一个 ec20_usb_pty 实例正在运行 (pid={holder})；"
            "同一时刻只能有一个桥占用 EC20 USB 接口。"
        ) from None
    lock_file.truncate(0)
    lock_file.write(str(os.getpid()))
    lock_file.flush()
    return lock_file


@dataclass(frozen=True)
class UsbPort:
    interface: int
    bulk_in: int
    bulk_out: int
    max_packet: int


@dataclass
class BridgeHandle:
    dev: usb.core.Device
    port: UsbPort
    link: str
    master_fd: int
    slave_fd: int
    stop: threading.Event
    closed: bool = False
    # 因「模组持续不排空端点」而拆桥（区别于真拔线）：置位后主循环会先跑
    # USB 组合切换恢复再重连，见 usb_composition_recovery。
    degraded: bool = False
    threads: list[threading.Thread] = field(default_factory=list)
    stats: InterfaceStats | None = None

    def close(self) -> None:
        if self.closed:
            return
        self.stop.set()
        # 先停生产者，避免 flush 后又写入；USB 调用自身带 100/1000ms 超时。
        deadline = time.monotonic() + 1.5
        for thread in self.threads:
            thread.join(max(0.0, deadline - time.monotonic()))
        if any(thread.is_alive() for thread in self.threads):
            # 不能与仍使用 fd/libusb 的线程竞态 close；交给进程退出回收。
            raise BridgeShutdownError(f"interface {self.port.interface} 桥线程未按时退出，拒绝关闭活动 PTY")
        # macOS close 会等待 tty 待发数据排空；无读者时必须先丢弃两个方向的队列。
        for fd in (self.master_fd, self.slave_fd):
            try:
                termios.tcflush(fd, termios.TCIOFLUSH)
            except (OSError, termios.error) as exc:
                if exc.args[0] != errno.EBADF:
                    raise BridgeShutdownError("PTY 清空失败，拒绝执行可能阻塞的 close") from exc
        try:
            usb.util.release_interface(self.dev, self.port.interface)
        except Exception:
            pass
        for fd in (self.master_fd, self.slave_fd):
            try:
                os.close(fd)
            except OSError:
                pass
        path = Path(self.link)
        if path.is_symlink():
            path.unlink()
        self.closed = True


def find_device(vid: int = VID, pid: int = PID) -> usb.core.Device:
    try:
        dev = usb.core.find(idVendor=vid, idProduct=pid, backend=libusb_backend())
    except usb.core.NoBackendError:
        # pyusb 是纯 Python 包，真正的 USB 访问依赖系统 libusb；
        # 干净的 Mac 上没有它，裸 traceback 会劝退第一次跑桥的用户。
        raise SystemExit(
            "libusb not found — pyusb needs the system libusb library.\n"
            "  Install it:  brew install libusb   (macOS)\n"
            "               sudo apt install libusb-1.0-0   (Debian/Ubuntu)"
        ) from None
    if dev is None:
        raise RuntimeError(f"未找到 USB 设备 ({vid:04x}:{pid:04x})")
    return dev


def discover_ports(dev: usb.core.Device) -> dict[int, UsbPort]:
    try:
        cfg = dev.get_active_configuration()
    except usb.core.USBError:
        dev.set_configuration()
        cfg = dev.get_active_configuration()
    ports: dict[int, UsbPort] = {}
    for intf in cfg:
        bulk_in = None
        bulk_out = None
        max_packet = 512
        for ep in intf:
            attrs = usb.util.endpoint_type(ep.bmAttributes)
            direction = usb.util.endpoint_direction(ep.bEndpointAddress)
            if attrs != usb.util.ENDPOINT_TYPE_BULK:
                continue
            if direction == usb.util.ENDPOINT_IN:
                bulk_in = ep.bEndpointAddress
                max_packet = ep.wMaxPacketSize
            elif direction == usb.util.ENDPOINT_OUT:
                bulk_out = ep.bEndpointAddress
        if bulk_in is not None and bulk_out is not None:
            ports[intf.bInterfaceNumber] = UsbPort(
                interface=intf.bInterfaceNumber,
                bulk_in=bulk_in,
                bulk_out=bulk_out,
                max_packet=max_packet,
            )
    return ports


def read_response(dev: usb.core.Device, port: UsbPort, timeout: float) -> bytes:
    deadline = time.monotonic() + timeout
    chunks: list[bytes] = []
    while time.monotonic() < deadline:
        try:
            data = dev.read(port.bulk_in, port.max_packet, timeout=200)
        except usb.core.USBTimeoutError:
            continue
        if data:
            chunks.append(bytes(data))
            joined = b"".join(chunks)
            if b"\r\nOK\r\n" in joined or b"\r\nERROR\r\n" in joined:
                break
    return b"".join(chunks)


def probe_at(dev: usb.core.Device, port: UsbPort) -> bytes:
    try:
        usb.util.claim_interface(dev, port.interface)
    except usb.core.USBError as exc:
        raise RuntimeError(
            f"无法占用 USB interface {port.interface}: {exc}. "
            "请确认没有另一个 ec20_usb_pty.py 正在运行；如刚异常退出，重插 EC20 USB 后再试。"
        ) from exc
    try:
        while True:
            try:
                dev.read(port.bulk_in, port.max_packet, timeout=50)
            except Exception:
                break
        dev.write(port.bulk_out, b"AT\r", timeout=1000)
        return read_response(dev, port, 1.5)
    finally:
        usb.util.release_interface(dev, port.interface)


def at_on_interface(
    dev: usb.core.Device, port: UsbPort, cmd: str,
    wait: float = 1.5, rounds: int = 15,
) -> str:
    """在指定 bulk 接口上发一条 AT 并返回响应文本（恢复流程用，独立于桥）。"""
    usb.util.claim_interface(dev, port.interface)
    try:
        while True:  # 清残留 URC
            try:
                dev.read(port.bulk_in, port.max_packet, timeout=50)
            except usb.core.USBError:
                break
        dev.write(port.bulk_out, (cmd + "\r").encode("ascii"), timeout=1000)
        time.sleep(wait)
        data = b""
        for _ in range(rounds):
            try:
                data += bytes(dev.read(port.bulk_in, port.max_packet, timeout=250))
            except usb.core.USBError:
                break
        return data.decode("ascii", "ignore")
    finally:
        try:
            usb.util.release_interface(dev, port.interface)
        except Exception:  # noqa: BLE001
            pass


def find_at_port_dynamic(
    vid: int, timeout: float = 60.0,
) -> tuple[usb.core.Device | None, UsbPort | None]:
    """只按 VID 找设备并逐个 bulk 接口探 AT。

    切换 USB 组合后 **AT 口会换接口号**（真机：9001 组合在 interface 2，
    9011 组合跑到 interface 4/5），写死接口号会直接失联，故必须动态探测。
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            dev = usb.core.find(idVendor=vid, backend=libusb_backend())
        except usb.core.NoBackendError:
            return None, None
        if dev is None:
            time.sleep(1.0)
            continue
        try:
            ports = discover_ports(dev)
        except usb.core.USBError:
            time.sleep(1.0)
            continue
        for port in ports.values():
            try:
                if "OK" in at_on_interface(dev, port, "AT", wait=0.4, rounds=8):
                    return dev, port
            except usb.core.USBError:
                continue
        usb.util.dispose_resources(dev)
        time.sleep(1.0)
    return None, None


def usb_composition_recovery(
    vid: int, target_pid: int, alt_pid: int,
    attempts: int = 3, settle: float = 20.0,
) -> bool:
    """PCM 子系统卡死的软件恢复：切到备用 USB 组合再切回，等效物理断电重插。

    真机实测（2026-08-12）：``AT+CRESET`` 与 ``AT+CFUN=0/1`` 都救不回卡死的
    PCM——射频/SIM 重启了，音频子系统依旧坏；**只有触发 USB 重新枚举有效**。
    组合切换与物理断电都能触发，故用它做无人值守的自动恢复。

    两个真机踩过的坑，这里都必须处理：
    1. 备用组合下 AT 口不在原接口号——用 ``find_at_port_dynamic`` 动态探；
    2. 切回指令若在重枚举/启动完成前下发会返回 ``ERROR`` 并**滞留在备用组合**
       ——故每次切换后等待 ``settle`` 秒，并**回读校验**，失败重试。
    """
    logger.warning(
        "检测到 PCM 子系统卡死，开始 USB 组合切换恢复（%04x → %04x → %04x）",
        target_pid, alt_pid, target_pid,
    )
    for attempt in range(1, attempts + 1):
        dev, port = find_at_port_dynamic(vid)
        if dev is None or port is None:
            logger.error("恢复第 %d 次：找不到 AT 口", attempt)
            time.sleep(settle)
            continue
        current = at_on_interface(dev, port, "AT+CUSBPIDSWITCH?")
        if f"{target_pid:04X}" in current.upper() and attempt > 1:
            logger.info("已回到目标组合 %04x，恢复完成", target_pid)
            usb.util.dispose_resources(dev)
            return True
        # 在目标组合上：先切走；已在备用组合（上次没切回来）：直接切回。
        going_out = f"{target_pid:04X}" in current.upper()
        pid = alt_pid if going_out else target_pid
        logger.info("恢复第 %d 次：切换到 %04x（AT 口 interface %d）",
                    attempt, pid, port.interface)
        at_on_interface(dev, port, f"AT+CUSBPIDSWITCH={pid:04X},1,1", wait=2.0)
        usb.util.dispose_resources(dev)
        time.sleep(settle)  # 等重枚举+固件启动，发太早会 ERROR 并卡在备用组合

        if not going_out:
            continue  # 本轮是切回，下一轮开头会回读校验

        dev, port = find_at_port_dynamic(vid)
        if dev is None or port is None:
            logger.error("恢复第 %d 次：切走后失联，重试", attempt)
            continue
        logger.info("恢复第 %d 次：切回 %04x（AT 口 interface %d）",
                    attempt, target_pid, port.interface)
        at_on_interface(dev, port, f"AT+CUSBPIDSWITCH={target_pid:04X},1,1", wait=2.0)
        usb.util.dispose_resources(dev)
        time.sleep(settle)

        dev, port = find_at_port_dynamic(vid)
        if dev is None or port is None:
            logger.error("恢复第 %d 次：切回后失联，重试", attempt)
            continue
        verify = at_on_interface(dev, port, "AT+CUSBPIDSWITCH?")
        usb.util.dispose_resources(dev)
        if f"{target_pid:04X}" in verify.upper():
            logger.warning("USB 组合切换恢复成功，已回到 %04x", target_pid)
            return True
        logger.error("恢复第 %d 次：回读未确认（%r），重试", attempt, verify.strip())
    logger.error(
        "USB 组合切换恢复连续 %d 次失败——需人工物理断电重插", attempts
    )
    return False


def make_raw(fd: int) -> None:
    tty.setraw(fd)
    attrs = termios.tcgetattr(fd)
    attrs[3] = attrs[3] & ~(termios.ECHO | termios.ICANON)
    termios.tcsetattr(fd, termios.TCSANOW, attrs)


def link_pty(slave_name: str, link: str) -> None:
    path = Path(link)
    if path.exists() or path.is_symlink():
        path.unlink()
    path.symlink_to(slave_name)


def write_pty_bounded(master_fd: int, data: bytes, stop: threading.Event) -> int:
    """在总时限内补齐部分写入，返回超时或停止时尚未写出的字节数。"""
    remaining = memoryview(data)
    deadline = time.monotonic() + USB_TO_PTY_WRITE_TIMEOUT
    while remaining and not stop.is_set():
        if time.monotonic() >= deadline:
            break
        try:
            written = os.write(master_fd, remaining)
        except BlockingIOError:
            written = 0
        if written:
            remaining = remaining[written:]
            continue
        # 分段等待让关闭能及时响应；部分写入和再次 EAGAIN 都不重置截止时间。
        wait = min(0.05, deadline - time.monotonic())
        if wait > 0:
            select.select([], [master_fd], [], wait)
    return len(remaining)


def bridge_port(
    dev: usb.core.Device,
    port: UsbPort,
    link: str,
) -> BridgeHandle:
    master_fd, slave_fd = pty.openpty()
    stop = threading.Event()
    handle = BridgeHandle(dev, port, link, master_fd, slave_fd, stop)
    stats = handle.stats = InterfaceStats(port.interface)
    try:
        # Keep the slave side open so the master does not see EIO before a client opens it.
        make_raw(slave_fd)
        os.set_blocking(master_fd, False)
        slave_name = os.ttyname(slave_fd)
        try:
            usb.util.claim_interface(dev, port.interface)
        except usb.core.USBError as exc:
            raise RuntimeError(
                f"无法占用 USB interface {port.interface}: {exc}. "
                "请确认没有另一个 ec20_usb_pty.py 正在运行；如刚异常退出，重插 EC20 USB 后再试。"
            ) from exc
        link_pty(slave_name, link)
    except Exception:
        handle.close()
        raise
    logger.info(
        "interface %d: %s -> %s (in=0x%02x, out=0x%02x)",
        port.interface, link, slave_name, port.bulk_in, port.bulk_out,
    )

    def usb_to_pty() -> None:
        last_drop_log = float("-inf")
        while not stop.is_set():
            try:
                data = dev.read(port.bulk_in, port.max_packet, timeout=100)
            except usb.core.USBTimeoutError:
                stats.record(in_calls=1)
                continue
            except Exception as exc:  # noqa: BLE001
                stats.record(in_calls=1)
                if not stop.is_set():
                    logger.error("interface %d USB read failed: %s", port.interface, exc)
                stop.set()
                return
            stats.record(in_calls=1, in_bytes=len(data))
            if stop.is_set():
                return
            if data:
                try:
                    dropped = write_pty_bounded(master_fd, bytes(data), stop)
                except OSError as exc:
                    if not stop.is_set():
                        logger.error("interface %d PTY write failed: %s", port.interface, exc)
                    stop.set()
                    return
                stats.record(pty_dropped=dropped)
                # 短暂迟滞保留数据；PCM/AT 持续无读者才丢弃，不能拖死整座桥。
                if dropped and not stop.is_set() and time.monotonic() - last_drop_log >= 5.0:
                    logger.warning("interface %d PTY 缓冲满，丢弃 %d 字节", port.interface, dropped)
                    last_drop_log = time.monotonic()

    def pty_to_usb() -> None:
        consecutive_timeouts = 0
        while not stop.is_set():
            try:
                ready, _, _ = select.select([master_fd], [], [], 0.1)
                if not ready:
                    continue
                data = os.read(master_fd, port.max_packet)
            except BlockingIOError:
                continue
            except OSError as exc:
                if not stop.is_set():
                    logger.error("interface %d PTY read failed: %s", port.interface, exc)
                stop.set()
                return
            if data:
                try:
                    if write_usb_bounded(dev, port.bulk_out, data, stop, stats):
                        consecutive_timeouts = 0
                except usb.core.USBError as exc:
                    if isinstance(exc, usb.core.USBTimeoutError) or exc.errno == errno.ETIMEDOUT:
                        # 瞬时写超时（模组侧忙/端点暂 NAK）：丢帧继续，不因单帧超时拆桥
                        # （否则连累同桥 AT 口，通话中掉线）。但持续超时=设备可能已掉线/
                        # 重启，需拆桥触发外层重连，否则会永远空转丢帧连不回来。
                        consecutive_timeouts += 1
                        if consecutive_timeouts <= PTY_WRITE_TIMEOUT_TOLERANCE:
                            if not stop.is_set():
                                logger.warning("interface %d USB 写超时，丢帧继续", port.interface)
                            continue
                        if not stop.is_set():
                            logger.error(
                                "interface %d 连续写超时 %d 次，判定掉线，触发重连",
                                port.interface, consecutive_timeouts,
                            )
                        # 设备还在总线上却持续不收数据 = PCM 子系统卡死，不是拔线。
                        # 标记后由主循环跑组合切换恢复（真机唯一有效的软件手段）。
                        handle.degraded = True
                        stop.set()
                        return
                    if not stop.is_set():
                        logger.error("interface %d USB write failed: %s", port.interface, exc)
                    stop.set()
                    return

    handle.threads = [
        threading.Thread(target=usb_to_pty, name=f"ec20-usb-to-pty-{port.interface}", daemon=True),
        threading.Thread(target=pty_to_usb, name=f"ec20-pty-to-usb-{port.interface}", daemon=True),
    ]
    for thread in handle.threads:
        thread.start()
    return handle


def parse_map(value: str) -> tuple[int, str]:
    try:
        iface_text, link = value.split(":", 1)
        iface = int(iface_text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("--map 格式应为 IFACE:LINK，例如 2:/tmp/ec20-at") from exc
    if not link:
        raise argparse.ArgumentTypeError("--map 的 LINK 不能为空")
    return iface, link


def wait_for_device(
    stop: threading.Event,
    poll_seconds: float = 2.0,
    vid: int = VID,
    pid: int = PID,
) -> usb.core.Device | None:
    """阻塞等待设备出现（模组重插/通话重枚举场景）；stop 置位时返回 None。"""
    announced = False
    while not stop.is_set():
        try:
            return find_device(vid, pid)
        except RuntimeError:
            if not announced:
                logger.warning("未检测到 USB 设备 (%04x:%04x)，等待设备接入…", vid, pid)
                announced = True
            stop.wait(poll_seconds)
    return None


def run_bridges_once(
    dev: usb.core.Device,
    maps: list[tuple[int, str]],
    stop: threading.Event,
    reset_first: bool = False,
    recovery_request: Path | None = None,
    heartbeat: Callable[[], None] | None = None,
) -> bool:
    """建立全部桥并阻塞运行，直到 stop 置位或任一桥断开（如设备被拔出）。

    返回是否因「模组持续不排空端点」而断开（True 时主循环会跑组合切换恢复）。

    reset_first=True 时先 dev.reset()：macOS 睡眠/重枚举后 bulk 端点常处于 stall，
    不复位则重连后每次 read 立即 [Errno 5] 死循环（见 docs/roadmap.md USB 排查）。
    """
    if reset_first:
        if os.environ.get("CALLPILOT_BRIDGE_RESET_ON_START") == "0":
            logger.info("启动不复位（实验开关） CALLPILOT_BRIDGE_RESET_ON_START=0")
            reset_first = False
    if reset_first:
        try:
            dev.reset()
            logger.info("已复位 USB 设备（清除 stall 端点）")
            time.sleep(1.0)  # 复位后设备重新枚举需片刻
        except Exception as exc:  # noqa: BLE001
            logger.warning("USB 复位失败（继续尝试桥接）: %s", exc)
    ports = discover_ports(dev)
    handles: list[BridgeHandle] = []
    try:
        for iface, link in maps:
            if iface not in ports:
                raise RuntimeError(f"接口 {iface} 不存在，可用接口: {sorted(ports)}")
            handles.append(bridge_port(dev, ports[iface], link))

        requested = False
        held = False
        hold_pcm = (os.environ.get("CALLPILOT_BRIDGE_HOLD_ON_PCM_STALL") == "1"
                    and getattr(dev, "idVendor", None) == modem_hardware.SIM7600_VID
                    and getattr(dev, "idProduct", None) == modem_hardware.SIM7600_PID)
        while not stop.is_set():
            failed = [handle for handle in handles if handle.stop.is_set() and not handle.closed]
            if hold_pcm and not held and any(h.port.interface == 4 and h.degraded for h in failed):
                # 不发 AT、不枚举、不释放整个设备，只停 PCM 并冻结计数现场。
                pcm = next(h for h in failed if h.port.interface == 4)
                pcm.close()
                held = True
                snapshot = [{"interface": h.port.interface, "stopped": h.stop.is_set(),
                             "closed": h.closed, "degraded": h.degraded,
                             "threads_alive": [t.name for t in h.threads if t.is_alive()],
                             "stats": h.stats.snapshot() if h.stats is not None else None} for h in handles]
                logger.warning("PCM_STALL_HOLD PCM 卡死取证：保留 AT 桥，暂停组合切换 snapshot=%s",
                               json.dumps(snapshot, ensure_ascii=False))
            if any(h.stop.is_set() and not (held and h.port.interface == 4) for h in handles):
                break
            if heartbeat is not None:
                heartbeat()
            # app 侧检测到「CPCMREG 启用需重试」= 模组已劣化，通话结束后写此文件请求
            # 自愈。它覆盖「模组收数据但播放卡」这一形态——那种形态不产生写超时，
            # 光靠桥自己发现不了（真机 2026-08-12）。
            if not held and recovery_request is not None and recovery_request.exists():
                logger.warning("收到 app 的自愈请求（%s），拆桥执行组合切换", recovery_request)
                requested = True
                break
            time.sleep(0.2)
        return not held and (requested or any(handle.degraded for handle in handles))
    finally:
        # 先通知全部桥，不能因第一个桥关闭失败而让其余生产者继续运行。
        for handle in handles:
            handle.stop.set()
        shutdown_error = None
        for handle in handles:
            try:
                handle.close()
            except BridgeShutdownError as exc:
                shutdown_error = exc
        if shutdown_error is not None:
            raise shutdown_error
        usb.util.dispose_resources(dev)


def find_auto_device() -> tuple[usb.core.Device, modem_hardware.ModemPreset] | None:
    devices = list(usb.core.find(find_all=True, backend=libusb_backend()))
    preset = modem_hardware.select_preset([(d.idVendor, d.idProduct) for d in devices])
    if preset is None:
        return None
    return next(d for d in devices if (d.idVendor, d.idProduct) == (preset.vid, preset.pid)), preset


def run_auto(stop: threading.Event, *, once: bool = False, poll_seconds: float = 2) -> int:
    """每次拆桥都重新选预设；在线心跳过期后，崩溃残留不会误报在线。"""
    modem_hardware.STATE_PATH.unlink(missing_ok=True)
    failures = 0
    selected_vendor: str | None = None
    while not stop.is_set():
        try:
            found = find_auto_device()
        except usb.core.USBError as exc:
            logger.warning("USB 自动扫描失败: %s", exc)
            found = None
        if found is None:
            failures = 0
            stop.wait(poll_seconds)
            continue
        dev, preset = found
        first_selection = selected_vendor != preset.vendor
        if first_selection:
            failures = 0
        selected_vendor = preset.vendor
        request = Path(preset.recovery_request_path) if preset.recovery_request_path else None
        if request is not None:
            request.unlink(missing_ok=True)
        last_write = 0.0

        def heartbeat() -> None:
            nonlocal last_write
            if time.monotonic() - last_write >= 3:
                modem_hardware.write_state(preset, online=True)
                last_write = time.monotonic()

        logger.info("自动识别模组: %s (%04x:%04x)", preset.model, preset.vid, preset.pid)
        reset_first = preset.reset_on_start and (first_selection or failures > 0)
        if os.environ.get("CALLPILOT_BRIDGE_RESET_ON_START") == "0":
            reset_first = False
            logger.info("启动不复位（实验开关） CALLPILOT_BRIDGE_RESET_ON_START=0")
        started = time.monotonic()
        degraded = False
        try:
            degraded = run_bridges_once(
                dev, [parse_map(m) for m in preset.maps], stop,
                reset_first=reset_first,
                recovery_request=request, heartbeat=heartbeat,
            )
        except (RuntimeError, usb.core.USBError) as exc:
            logger.warning("自动桥接失败: %s", exc)
            if once:
                return 1
        finally:
            modem_hardware.write_state(preset, online=False)
        if once or stop.is_set():
            break
        if degraded and preset.recover_alt_pid is not None:
            usb_composition_recovery(preset.vid, preset.pid, preset.recover_alt_pid)
            if request is not None:
                request.unlink(missing_ok=True)
        failures = failures + 1 if time.monotonic() - started < 5 else 0
        if failures >= int(os.environ.get("EC20_BRIDGE_FAIL_THRESHOLD", "6")):
            return 3
        stop.wait(min(poll_seconds * 2 ** failures, 30))
    return 0


def main(
    default_vid: int = VID,
    default_pid: int = PID,
    default_maps: list[str] | None = None,
    prog: str | None = None,
    description: str = "EC20 USB vendor serial PTY bridge for macOS",
    reset_on_start: bool = False,
    recover_alt_pid: int | None = None,
    recovery_request_path: str | None = None,
) -> int:
    parser = argparse.ArgumentParser(prog=prog, description=description)
    parser.add_argument("--auto", action="store_true", help="自动识别已知模组并选择桥接预设")
    parser.add_argument("--list", action="store_true", help="列出 USB bulk 接口后退出")
    parser.add_argument("--probe", action="store_true", help="对每个 bulk 接口发送 AT 探测后退出")
    parser.add_argument(
        "--vid", type=lambda s: int(s, 0), default=default_vid,
        help=f"USB Vendor ID（默认 0x{default_vid:04x}）",
    )
    parser.add_argument(
        "--pid", type=lambda s: int(s, 0), default=default_pid,
        help=f"USB Product ID（默认 0x{default_pid:04x}）",
    )
    parser.add_argument(
        "--map",
        action="append",
        default=[],
        type=parse_map,
        metavar="IFACE:LINK",
        help="桥接接口到 symlink，例如 2:/tmp/ec20-at；可重复",
    )
    parser.add_argument(
        "--once", action="store_true",
        help="桥断开（设备拔出）后直接退出，不等待重插自动重连",
    )
    parser.add_argument(
        "--reset-on-start", action="store_true",
        help="首次桥接前先 dev.reset() 清除 stall 端点（SIM7600 对 USB 故障敏感，推荐开）",
    )
    parser.add_argument(
        "--recover-alt-pid", type=lambda s: int(s, 16),
        help="PCM 卡死时自动切到该备用 USB 组合再切回以恢复（十六进制，如 9011）；"
             "不给则只重连不恢复",
    )
    parser.add_argument("--log-file", help="同时把日志写入指定文件")
    args = parser.parse_args()
    reset_on_start = reset_on_start or args.reset_on_start
    if args.recover_alt_pid is not None:
        recover_alt_pid = args.recover_alt_pid
    recovery_request = (
        Path(recovery_request_path) if recovery_request_path else None
    )
    if recovery_request is not None:
        recovery_request.unlink(missing_ok=True)  # 启动时清掉陈旧请求

    # 未显式给 --map 时用厂商默认映射（Quectel 默认为空，仍要求显式 --map）。
    if not args.map and default_maps:
        args.map = [parse_map(m) for m in default_maps]

    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if args.log_file:
        handlers.append(
            logging.handlers.RotatingFileHandler(
                args.log_file, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8",
            ) if getattr(sys, "frozen", False) else logging.FileHandler(args.log_file, encoding="utf-8")
        )
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=handlers,
        # 打包入口的已有 handler 不能让桥的文件日志配置失效。
        force=bool(getattr(sys, "frozen", False)),
    )

    _lock = acquire_instance_lock()  # noqa: F841  # 持有到进程退出

    if args.list or args.probe:
        dev = find_device(args.vid, args.pid)
        ports = discover_ports(dev)
        if args.list:
            for port in ports.values():
                print(
                    f"interface {port.interface}: "
                    f"in=0x{port.bulk_in:02x} out=0x{port.bulk_out:02x} max={port.max_packet}"
                )
            return 0
        for port in ports.values():
            try:
                response = probe_at(dev, port).decode("ascii", "ignore").replace("\r\n", " | ")
            except RuntimeError as exc:
                print(f"interface {port.interface}: {exc}")
                continue
            print(f"interface {port.interface}: {response or '(no response)'}")
        return 0

    if not args.map and not args.auto:
        parser.error("需要 --list、--probe 或至少一个 --map IFACE:LINK")

    stop = threading.Event()

    def handle_signal(_signum: int, _frame: object) -> None:
        stop.set()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    if args.auto:
        return run_auto(stop, once=args.once)

    # 连续快速失败计数：超阈值则 sys.exit 交 launchd 冷启（含全新 libusb 上下文），
    # 比原地自旋更可能复位；手动运行（无 launchd）时同样退出，避免抖动风暴。
    consecutive_fast_fail = 0
    fail_threshold = int(os.environ.get("EC20_BRIDGE_FAIL_THRESHOLD", "6"))
    backoff = 1.0
    first_run = True
    while not stop.is_set():
        dev = wait_for_device(stop, vid=args.vid, pid=args.pid)
        if dev is None:
            break
        started_at = time.monotonic()
        # 非首轮（重连）先复位设备清 stall 端点；reset_on_start 时首轮也复位
        # （SIM7600 对 USB 故障敏感，残留 stall 会让首次桥接直接 EIO）。
        reset_first = consecutive_fast_fail > 0 or (first_run and reset_on_start)
        first_run = False
        degraded = False
        try:
            degraded = run_bridges_once(
                dev, args.map, stop, reset_first=reset_first,
                recovery_request=recovery_request,
            )
        except (RuntimeError, usb.core.USBError) as exc:
            # USBError：设备僵死/枚举中时 set_configuration 等处会抛，
            # 不捕获会炸穿进程，launchd 每 10s 重启一次形成崩溃风暴；
            # 捕获后走快速失败退避，下一轮自动带 dev.reset() 清 stall。
            logger.error("桥接失败: %s", exc)
            if args.once:
                return 1
        if stop.is_set() or args.once:
            break

        # 设备仍在总线上却不收 PCM = 模组音频子系统卡死；软重启/CFUN 都救不回，
        # 只有 USB 重新枚举有效（WIL-109）。此时通话已随拆桥结束，恢复不会打断通话。
        if degraded and recover_alt_pid is not None:
            usb_composition_recovery(args.vid, args.pid, recover_alt_pid)
            if recovery_request is not None:
                recovery_request.unlink(missing_ok=True)  # 消费掉，避免反复触发
            consecutive_fast_fail = 0
            backoff = 1.0
            continue

        # 判定本轮是否"秒挂"：桥接维持不足 5s 视为快速失败，触发退避。
        ran_seconds = time.monotonic() - started_at
        if ran_seconds < 5.0:
            consecutive_fast_fail += 1
            if consecutive_fast_fail >= fail_threshold:
                logger.error(
                    "桥连续 %d 次快速失败，退出交由 launchd 冷启（或请重插 EC20 / 检查睡眠）",
                    consecutive_fast_fail,
                )
                return 3
            logger.warning(
                "桥断开（第 %d 次快速失败），%.0fs 后带 USB 复位重连…",
                consecutive_fast_fail, backoff,
            )
            stop.wait(backoff)
            backoff = min(backoff * 2, 30.0)
        else:
            # 曾正常运行过一段时间，属偶发掉线：重置退避。
            consecutive_fast_fail = 0
            backoff = 1.0
            logger.warning("桥已断开（设备可能被拔出），等待重插后自动重连…")
            stop.wait(1.0)

    logger.info("桥已退出，symlink 已清理")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
