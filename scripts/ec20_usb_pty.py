"""Expose Quectel EC20 USB vendor serial interfaces as macOS PTYs.

macOS can see EC20/EG25 USB interfaces but does not create /dev/cu.* ports for
Quectel vendor-specific serial functions. This bridge talks to the bulk USB
endpoints with libusb/PyUSB and presents a pseudo terminal for pyserial.
"""

from __future__ import annotations

import argparse
import errno
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

import usb.backend.libusb1
import usb.core
import usb.util

logger = logging.getLogger("ec20_usb_pty")

VID = 0x2C7C
PID = 0x0125

# 其他 libusb 可达的厂商串口模组（如 SIMCom SIM7600 = 1e0e:9001）用 --vid/--pid 指向；
# 桥只搬运 bulk 端点字节，与 AT 方言无关，但上层通话链路仍按 EC20 调校。
DEFAULT_VID = VID
DEFAULT_PID = PID

# 已知的厂商串口模组 VID：--vid 省略时按这张表扫描，插上即可认出。
# 加新厂商只需在这里补一行，桥的搬运逻辑与 AT 方言无关。
KNOWN_VENDORS = {
    0x2C7C: "Quectel",
    0x1E0E: "SIMCom",
}

LOCK_PATH = Path("/tmp/ec20-usb-pty.lock")

# 单次 USB 写超时不致命（PCM 实时流常见）；连续这么多次才判定链路已死。
WRITE_TIMEOUT_TOLERANCE = 10

# 单次 bulk 写的等待上限。实时 PCM 下宁可快速丢帧也不能阻塞：8kHz/16bit 单声道
# 是 16000 B/s，按 512 字节一写约 31 次/秒，原来的 1000ms 意味着一次卡顿就吞掉
# ~31 帧、连续 10 次就是 10 秒哑音（真机 2026-08-01 实测）。200ms 仍远大于正常
# 写入耗时，只是不再把实时流拖死。
WRITE_TIMEOUT_MS = 200

# Windows 的厂商串口驱动在打开虚拟 COM 口时会发 CDC ACM 的
# SET_CONTROL_LINE_STATE。SIMCom 的 audio 口虽然描述符标成 VENDOR class，
# 仍可能用 DTR/RTS 作为 host 已打开端口的门控；libusb 直连不会替我们做这一步。
CDC_HOST_TO_INTERFACE = 0x21
CDC_SET_CONTROL_LINE_STATE = 0x22
CDC_DTR_RTS = 0x03
CONTROL_TRANSFER_TIMEOUT_MS = 1000


class EndpointRecoveryError(RuntimeError):
    """Bulk OUT 明确 STALL 后无法清除 endpoint halt。"""


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
    interrupt_in: int | None = None
    interrupt_max_packet: int = 0


@dataclass
class BridgeHandle:
    dev: usb.core.Device
    port: UsbPort
    link: str
    master_fd: int
    slave_fd: int
    stop: threading.Event
    # 该桥是否致命：AT/控制口挂了整个进程没意义，数据口（PCM 等）挂了不该连坐。
    # 真机 2026-08-04：PCM 口写超时判死后整座桥退出、清掉 /tmp/ec20-at，
    # 结果模组"掉线"、ATH 发不出去，通话中挂不掉电话。
    critical: bool = False
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


def find_all_devices(**criteria: int) -> list[usb.core.Device]:
    """枚举匹配 ``criteria`` 的 USB 设备；libusb 缺失时给可操作的安装提示。"""
    try:
        return list(usb.core.find(find_all=True, backend=libusb_backend(), **criteria))
    except usb.core.NoBackendError:
        # pyusb 是纯 Python 包，真正的 USB 访问依赖系统 libusb；
        # 干净的 Mac 上没有它，裸 traceback 会劝退第一次跑桥的用户。
        raise SystemExit(
            "libusb not found — pyusb needs the system libusb library.\n"
            "  Install it:  brew install libusb   (macOS)\n"
            "               sudo apt install libusb-1.0-0   (Debian/Ubuntu)"
        ) from None


def describe_device(dev: usb.core.Device) -> str:
    """``vid:pid (厂商 产品)``；字符串描述符读不到时退化成只有 ID。

    读 manufacturer/product 要发控制传输，未授权或设备忙时会抛——描述只是
    给人看的，绝不能因此让整个枚举失败。
    """
    parts: list[str] = []
    for attr in ("manufacturer", "product"):
        try:
            value = getattr(dev, attr, None)
        except Exception:  # noqa: BLE001  # 控制传输失败：权限/设备忙/无描述符
            value = None
        if not value:
            continue
        text = str(value).strip()
        # SIM7600 的 manufacturer 与 product 是同一个字符串（都是
        # "SimTech, Incorporated"），照原样拼会打印两遍。
        if text and text.lower() not in (p.lower() for p in parts):
            parts.append(text)
    vendor = KNOWN_VENDORS.get(dev.idVendor)
    if vendor and vendor.lower() not in " ".join(parts).lower():
        parts.insert(0, vendor)
    label = f"{dev.idVendor:04x}:{dev.idProduct:04x}"
    return f"{label} ({' '.join(parts)})" if parts else label


def _no_match_message(scope: str) -> str:
    """没匹配上时把总线上的设备全列出来——用户的模组可能是未知 VID。"""
    lines = [f"未找到 USB 设备（{scope}）"]
    others = find_all_devices()
    if others:
        lines.append("当前 USB 总线上的设备：")
        lines.extend(f"  {describe_device(dev)}" for dev in others)
        lines.append("若你的模组在上面，用 --vid/--pid 指定它（十六进制）。")
    else:
        lines.append("USB 总线上没有任何设备——请确认模组已插好并已上电。")
    return "\n".join(lines)


def find_device(
    vid: int | None = None, pid: int | None = None
) -> usb.core.Device:
    """定位模组：``vid`` 省略时扫 :data:`KNOWN_VENDORS`，``pid`` 省略时按 vid 枚举。

    ``pid`` 可省是关键：SIMCom SIM7600 的 PID 随固件 composite 配置浮动
    （9000-9007 / 9011 / 9016 / 9018-901b / 9020-902b …），写死一个值等于
    要求用户先手工查一遍 ``system_profiler``。唯一匹配才返回，多个候选时
    列出来让用户用 ``--pid`` 消歧——绝不替用户猜该桥哪个模组。
    """
    if vid is None:
        candidates = [d for d in find_all_devices() if d.idVendor in KNOWN_VENDORS]
        scope = "已知厂商 " + "/".join(
            f"{v:04x} {name}" for v, name in KNOWN_VENDORS.items()
        )
    else:
        candidates = find_all_devices(idVendor=vid)
        scope = f"vid {vid:04x}"
    if pid is not None:
        candidates = [d for d in candidates if d.idProduct == pid]
        scope += f" pid {pid:04x}"

    if not candidates:
        raise RuntimeError(_no_match_message(scope))
    if len(candidates) > 1:
        listing = "\n  ".join(describe_device(dev) for dev in candidates)
        raise RuntimeError(
            f"{scope} 匹配到 {len(candidates)} 个设备，无法确定桥哪一个；"
            f"请用 --vid/--pid 指定：\n  {listing}"
        )
    dev = candidates[0]
    logger.info("匹配到模组 %s", describe_device(dev))
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
        interrupt_in = None
        interrupt_max_packet = 0
        for ep in intf:
            attrs = usb.util.endpoint_type(ep.bmAttributes)
            direction = usb.util.endpoint_direction(ep.bEndpointAddress)
            if attrs == usb.util.ENDPOINT_TYPE_INTR and direction == usb.util.ENDPOINT_IN:
                interrupt_in = ep.bEndpointAddress
                interrupt_max_packet = ep.wMaxPacketSize
            elif attrs == usb.util.ENDPOINT_TYPE_BULK and direction == usb.util.ENDPOINT_IN:
                bulk_in = ep.bEndpointAddress
                max_packet = ep.wMaxPacketSize
            elif attrs == usb.util.ENDPOINT_TYPE_BULK and direction == usb.util.ENDPOINT_OUT:
                bulk_out = ep.bEndpointAddress
        if bulk_in is not None and bulk_out is not None:
            ports[intf.bInterfaceNumber] = UsbPort(
                interface=intf.bInterfaceNumber,
                bulk_in=bulk_in,
                bulk_out=bulk_out,
                max_packet=max_packet,
                interrupt_in=interrupt_in,
                interrupt_max_packet=interrupt_max_packet,
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
        try:
            dev.write(port.bulk_out, b"AT\r", timeout=1000)
        except usb.core.USBError as exc:
            # 非 AT 接口（DIAG/QMI/PCM…）常常直接拒收或写超时。这必须降级成
            # 「这个接口不是 AT 口」，绝不能中止整轮探测——否则 interface 0 一超时
            # 就再也走不到后面真正的 AT 口，用户永远探不出该 --map 哪个号。
            raise RuntimeError(f"写入失败（大概不是 AT 口）: {exc}") from exc
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


def set_control_line_state(dev: usb.core.Device, interface: int) -> bool:
    """置 DTR/RTS，模拟 Windows 厂商串口驱动打开 COM 口时的握手。

    这些接口的描述符通常是 VENDOR class，部分 Quectel 固件可能不接受 CDC
    class request。握手失败不能让原本可用的 AT 桥退化，因此只告警并继续。
    """
    try:
        dev.ctrl_transfer(
            CDC_HOST_TO_INTERFACE,
            CDC_SET_CONTROL_LINE_STATE,
            CDC_DTR_RTS,
            interface,
            timeout=CONTROL_TRANSFER_TIMEOUT_MS,
        )
    except usb.core.USBError as exc:
        logger.warning(
            "interface %d 设置 DTR/RTS 失败（继续桥接）: %s",
            interface,
            exc,
        )
        return False
    logger.info("interface %d 已设置 DTR/RTS", interface)
    return True


def write_bulk_with_recovery(
    dev: usb.core.Device,
    endpoint: int,
    data: bytes,
) -> bool:
    """写一个 bulk chunk；仅在明确 STALL/PIPE 时清 halt 并重试一次。

    返回值表示本次是否走过恢复路径。重试仍超时则把 ``USBTimeoutError``
    交给调用方累计；普通 timeout 是持续 NAK，不等于 endpoint halt，不能
    每帧都 clear_halt。clear_halt 本身失败表示端点无法恢复，转换成独立
    异常，避免被普通写超时容忍逻辑吞掉。
    """
    try:
        dev.write(endpoint, data, timeout=WRITE_TIMEOUT_MS)
        return False
    except usb.core.USBTimeoutError:
        raise
    except usb.core.USBError as exc:
        if exc.errno != errno.EPIPE:
            raise
        try:
            dev.clear_halt(endpoint)
        except usb.core.USBError as clear_exc:
            raise EndpointRecoveryError(
                f"endpoint 0x{endpoint:02x} clear_halt 失败: {clear_exc}"
            ) from clear_exc
        dev.write(endpoint, data, timeout=WRITE_TIMEOUT_MS)
        return True


def drain_interrupt_notifications(
    dev: usb.core.Device,
    port: UsbPort,
    stop: threading.Event,
) -> None:
    """持续接收厂商串口的 interrupt-IN 状态通知。

    Linux ``option`` 驱动会为 SIMCom 9001 的每个串口提交并反复重提 interrupt
    URB（主要承载 DCD/DSR/RI）。它不是 PCM 数据流，但若 host 完全不轮询这个
    endpoint，就没有完整模拟官方串口驱动的打开状态。通知读取失败只关闭这条
    辅助通道；bulk 数据桥仍可继续。
    """
    if port.interrupt_in is None:
        return
    size = port.interrupt_max_packet or 64
    while not stop.is_set():
        try:
            data = dev.read(port.interrupt_in, size, timeout=100)
        except usb.core.USBTimeoutError:
            continue
        except Exception as exc:  # noqa: BLE001
            if not stop.is_set():
                logger.warning(
                    "interface %d USB interrupt read failed（bulk 继续）: %s",
                    port.interface,
                    exc,
                )
            return
        if data:
            logger.debug(
                "interface %d USB interrupt notification: %s",
                port.interface,
                bytes(data).hex(),
            )


def bridge_port(
    dev: usb.core.Device,
    port: UsbPort,
    link: str,
    critical: bool = False,
) -> BridgeHandle:
    master_fd, slave_fd = pty.openpty()
    stop = threading.Event()
    handle = BridgeHandle(dev, port, link, master_fd, slave_fd, stop, critical=critical)
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
        set_control_line_state(dev, port.interface)
        link_pty(slave_name, link)
    except Exception:
        handle.close()
        raise
    logger.info(
        "interface %d: %s -> %s (in=0x%02x, out=0x%02x, intr=%s)",
        port.interface,
        link,
        slave_name,
        port.bulk_in,
        port.bulk_out,
        f"0x{port.interrupt_in:02x}" if port.interrupt_in is not None else "none",
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
        # 写超时容忍：PCM 实时流下模组 OUT 端点可能瞬时写满。丢一帧音频远好过
        # 拆掉整座桥——真机 2026-08-01：一次 [Errno 60] 把 AT 口的桥一起带走，
        # 通话当场中断、模组随后掉线。读路径本就 continue 掉超时，写路径此前
        # 却是致命的，这里补齐对称性；连续超时到阈值才判定链路真的死了。
        consecutive_timeouts = 0
        reported_recovery = False
        successful_bytes = 0
        successful_writes = 0
        started_at = time.monotonic()
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
                    recovered = write_bulk_with_recovery(dev, port.bulk_out, data)
                    if recovered and not reported_recovery:
                        logger.warning(
                            "interface %d USB endpoint STALL，clear_halt 后重试成功",
                            port.interface,
                        )
                        reported_recovery = True
                    successful_bytes += len(data)
                    successful_writes += 1
                    consecutive_timeouts = 0
                except usb.core.USBTimeoutError:
                    consecutive_timeouts += 1
                    if consecutive_timeouts >= WRITE_TIMEOUT_TOLERANCE:
                        if not stop.is_set():
                            logger.error(
                                "interface %d 连续 %d 次 USB 写超时，判定链路已死",
                                port.interface, consecutive_timeouts,
                            )
                        stop.set()
                        return
                    # 首次用 warning 提示，后续降到 debug，避免长通话刷屏。
                    log = logger.warning if consecutive_timeouts == 1 else logger.debug
                    log(
                        "interface %d USB 写超时，丢弃 %d 字节（第 %d/%d 次）；"
                        "此前成功 %d bytes/%d writes/%.2fs",
                        port.interface, len(data), consecutive_timeouts,
                        WRITE_TIMEOUT_TOLERANCE,
                        successful_bytes, successful_writes,
                        time.monotonic() - started_at,
                    )
                except EndpointRecoveryError as exc:
                    if not stop.is_set():
                        logger.error("interface %d USB endpoint 恢复失败: %s", port.interface, exc)
                    stop.set()
                    return
                except Exception as exc:  # noqa: BLE001
                    if not stop.is_set():
                        logger.error("interface %d USB write failed: %s", port.interface, exc)
                    stop.set()
                    return

    if port.interrupt_in is not None:
        threading.Thread(
            target=drain_interrupt_notifications,
            args=(dev, port, stop),
            name=f"ec20-usb-interrupt-{port.interface}",
            daemon=True,
        ).start()
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


def parse_usb_id(value: str) -> int:
    """解析 --vid/--pid：一律按十六进制读（`1e0e` 与 `0x1e0e` 等价）。"""
    try:
        number = int(value, 16)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"USB ID 应为十六进制，例如 1e0e；收到 {value!r}") from exc
    if not 0 <= number <= 0xFFFF:
        raise argparse.ArgumentTypeError(f"USB ID 超出 16 位范围: {value!r}")
    return number


def wait_for_device(
    stop: threading.Event,
    poll_seconds: float = 2.0,
    vid: int | None = None,
    pid: int | None = None,
) -> usb.core.Device | None:
    """阻塞等待模组出现（重插场景）；stop 置位时返回 None。"""
    announced = False
    while not stop.is_set():
        try:
            return find_device(vid, pid)
        except RuntimeError as exc:
            if not announced:
                # 首次带上 find_device 的完整诊断（含总线设备清单），之后不再刷屏。
                logger.warning("未检测到模组，等待设备接入…\n%s", exc)
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
        for index, (iface, link) in enumerate(maps):
            if iface not in ports:
                raise RuntimeError(f"接口 {iface} 不存在，可用接口: {sorted(ports)}")
            # 首个 --map 视为控制口（约定即 AT 口，MODEM_BRIDGE_MAPS 默认也把它放
            # 第一位）：它挂了整个桥没意义。其余是数据口，坏掉只摘自己。
            handles.append(bridge_port(dev, ports[iface], link, critical=index == 0))

        while not stop.is_set():
            dead_critical = [h for h in handles if h.critical and h.stop.is_set()]
            if dead_critical:
                logger.error(
                    "控制口 interface %d 链路已死，整桥退出",
                    dead_critical[0].port.interface,
                )
                break
            # 数据口（PCM 等）死掉只摘掉它自己：绝不能连坐 AT 口——那会让模组
            # 看起来"掉线"、通话中 ATH 发不出去，用户挂不掉电话
            # （真机 2026-08-04 实测的故障链）。
            for handle in [h for h in handles if not h.critical and h.stop.is_set()]:
                logger.error(
                    "数据口 interface %d 链路已死，仅关闭该口；控制口继续服务",
                    handle.port.interface,
                )
                handle.close()
                handles.remove(handle)
            if not handles:
                logger.error("全部桥均已关闭，退出")
                break
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
    parser.add_argument(
        "--vid", type=parse_usb_id, default=None, metavar="HEX",
        help="USB Vendor ID，十六进制；省略时扫描已知厂商（"
             + "/".join(f"{v:04x} {n}" for v, n in KNOWN_VENDORS.items()) + "）",
    )
    parser.add_argument(
        "--pid", type=parse_usb_id, default=None, metavar="HEX",
        help="USB Product ID，十六进制；省略时按 VID 枚举，"
             "仅在同一 VID 匹配到多个设备时才需要指定",
    )
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
        dev = find_device(args.vid, args.pid)
        ports = discover_ports(dev)
        if args.list:
            # 先打设备身份：--pid 可省之后，用户需要知道到底认到了哪一个，
            # 且 --map 用的 interface 号只在这台设备上成立。
            print(f"device {describe_device(dev)}")
            for port in ports.values():
                print(
                    f"interface {port.interface}: "
                    f"in=0x{port.bulk_in:02x} out=0x{port.bulk_out:02x} max={port.max_packet}"
                )
            if not ports:
                print("(未发现任何 bulk 接口——该设备可能不是厂商串口模组)")
            return 0
        print(f"device {describe_device(dev)}")
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
        dev = wait_for_device(stop, vid=args.vid, pid=args.pid)
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
