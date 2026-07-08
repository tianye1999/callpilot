"""Expose Quectel EC20 USB vendor serial interfaces as macOS PTYs.

macOS can see EC20/EG25 USB interfaces but does not create /dev/cu.* ports for
Quectel vendor-specific serial functions. This bridge talks to the bulk USB
endpoints with libusb/PyUSB and presents a pseudo terminal for pyserial.
"""

from __future__ import annotations

import argparse
import fcntl
import logging
import os
import pty
import select
import signal
import sys
import termios
import threading
import time
import tty
from dataclasses import dataclass
from pathlib import Path

import usb.core
import usb.util

logger = logging.getLogger("ec20_usb_pty")

VID = 0x2C7C
PID = 0x0125

LOCK_PATH = Path("/tmp/ec20-usb-pty.lock")


def acquire_instance_lock() -> object:
    """进程唯一锁：防止两个桥实例争抢 USB claim 导致双双不可用。

    返回持有的文件对象（进程退出自动释放）；已有实例时报错。
    """
    lock_file = LOCK_PATH.open("a+")
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

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.stop.set()
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


def find_device() -> usb.core.Device:
    try:
        dev = usb.core.find(idVendor=VID, idProduct=PID)
    except usb.core.NoBackendError:
        # pyusb 是纯 Python 包，真正的 USB 访问依赖系统 libusb；
        # 干净的 Mac 上没有它，裸 traceback 会劝退第一次跑桥的用户。
        raise SystemExit(
            "libusb not found — pyusb needs the system libusb library.\n"
            "  Install it:  brew install libusb   (macOS)\n"
            "               sudo apt install libusb-1.0-0   (Debian/Ubuntu)"
        ) from None
    if dev is None:
        raise RuntimeError("未找到 Quectel EC20/EG25 USB 设备 (2c7c:0125)")
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


def bridge_port(
    dev: usb.core.Device,
    port: UsbPort,
    link: str,
) -> BridgeHandle:
    master_fd, slave_fd = pty.openpty()
    stop = threading.Event()
    handle = BridgeHandle(dev, port, link, master_fd, slave_fd, stop)
    try:
        # Keep the slave side open so the master does not see EIO before a client opens it.
        make_raw(slave_fd)
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
        while not stop.is_set():
            try:
                data = dev.read(port.bulk_in, port.max_packet, timeout=100)
            except usb.core.USBTimeoutError:
                continue
            except Exception as exc:  # noqa: BLE001
                if not stop.is_set():
                    logger.error("interface %d USB read failed: %s", port.interface, exc)
                stop.set()
                return
            if data:
                try:
                    os.write(master_fd, bytes(data))
                except OSError as exc:
                    if not stop.is_set():
                        logger.error("interface %d PTY write failed: %s", port.interface, exc)
                    stop.set()
                    return

    def pty_to_usb() -> None:
        while not stop.is_set():
            try:
                ready, _, _ = select.select([master_fd], [], [], 0.1)
                if not ready:
                    continue
                data = os.read(master_fd, port.max_packet)
            except OSError as exc:
                if not stop.is_set():
                    logger.error("interface %d PTY read failed: %s", port.interface, exc)
                stop.set()
                return
            if data:
                try:
                    dev.write(port.bulk_out, data, timeout=1000)
                except Exception as exc:  # noqa: BLE001
                    if not stop.is_set():
                        logger.error("interface %d USB write failed: %s", port.interface, exc)
                    stop.set()
                    return

    threading.Thread(target=usb_to_pty, name=f"ec20-usb-to-pty-{port.interface}", daemon=True).start()
    threading.Thread(target=pty_to_usb, name=f"ec20-pty-to-usb-{port.interface}", daemon=True).start()
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


def wait_for_device(stop: threading.Event, poll_seconds: float = 2.0) -> usb.core.Device | None:
    """阻塞等待 EC20 出现（模组重插场景）；stop 置位时返回 None。"""
    announced = False
    while not stop.is_set():
        try:
            return find_device()
        except RuntimeError:
            if not announced:
                logger.warning("未检测到 EC20 (2c7c:0125)，等待设备接入…")
                announced = True
            stop.wait(poll_seconds)
    return None


def run_bridges_once(
    dev: usb.core.Device,
    maps: list[tuple[int, str]],
    stop: threading.Event,
    reset_first: bool = False,
) -> None:
    """建立全部桥并阻塞运行，直到 stop 置位或任一桥断开（如设备被拔出）。

    reset_first=True 时先 dev.reset()：macOS 睡眠/重枚举后 bulk 端点常处于 stall，
    不复位则重连后每次 read 立即 [Errno 5] 死循环（见 docs/roadmap.md USB 排查）。
    """
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

        while not stop.is_set() and all(not handle.stop.is_set() for handle in handles):
            time.sleep(0.2)
    finally:
        for handle in handles:
            handle.close()
        usb.util.dispose_resources(dev)


def main() -> int:
    parser = argparse.ArgumentParser(description="EC20 USB vendor serial PTY bridge for macOS")
    parser.add_argument("--list", action="store_true", help="列出 USB bulk 接口后退出")
    parser.add_argument("--probe", action="store_true", help="对每个 bulk 接口发送 AT 探测后退出")
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
    parser.add_argument("--log-file", help="同时把日志写入指定文件")
    args = parser.parse_args()

    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if args.log_file:
        handlers.append(logging.FileHandler(args.log_file, encoding="utf-8"))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=handlers,
    )

    _lock = acquire_instance_lock()  # noqa: F841  # 持有到进程退出

    if args.list or args.probe:
        dev = find_device()
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

    if not args.map:
        parser.error("需要 --list、--probe 或至少一个 --map IFACE:LINK")

    stop = threading.Event()

    def handle_signal(_signum: int, _frame: object) -> None:
        stop.set()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    # 连续快速失败计数：超阈值则 sys.exit 交 launchd 冷启（含全新 libusb 上下文），
    # 比原地自旋更可能复位；手动运行（无 launchd）时同样退出，避免抖动风暴。
    consecutive_fast_fail = 0
    fail_threshold = int(os.environ.get("EC20_BRIDGE_FAIL_THRESHOLD", "6"))
    backoff = 1.0
    while not stop.is_set():
        dev = wait_for_device(stop)
        if dev is None:
            break
        started_at = time.monotonic()
        # 非首轮（重连）先复位设备，清除重枚举后的 stall 端点。
        reset_first = consecutive_fast_fail > 0
        try:
            run_bridges_once(dev, args.map, stop, reset_first=reset_first)
        except (RuntimeError, usb.core.USBError) as exc:
            # USBError：设备僵死/枚举中时 set_configuration 等处会抛，
            # 不捕获会炸穿进程，launchd 每 10s 重启一次形成崩溃风暴；
            # 捕获后走快速失败退避，下一轮自动带 dev.reset() 清 stall。
            logger.error("桥接失败: %s", exc)
            if args.once:
                return 1
        if stop.is_set() or args.once:
            break

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
