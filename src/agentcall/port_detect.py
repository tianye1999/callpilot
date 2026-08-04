"""模组 AT 串口自动探测（``MODEM_PORT=auto`` 时使用）。

Windows 官方驱动把 EC20/EG25 暴露为多个 COM 口，AT 口的 description
通常含 "AT"（如 "Quectel USB AT Port"）；描述不可用时按 Quectel 四口
惯例顺序（DM/NMEA/AT/PPP）取第 3 个接口回退。纯扫描无副作用，
``list_ports.comports`` 可在测试中替换。

扫描哪个厂商由 ``MODEM_USB_VID`` 决定（见 :func:`modem_usb_vid`）——桥的
``--vid``、安装向导的硬件检测与本模块共用这一个取值，避免同一个 VID
在三处各配一遍、改一处漏两处。

Windows 真机行为待硬件验证（本机无 Windows 环境）。
"""

from __future__ import annotations

import logging
import re

from serial.tools import list_ports

from . import config

logger = logging.getLogger(__name__)

# Quectel 的 USB Vendor ID（EC20/EG25 全系共用），也是 MODEM_USB_VID 的默认值。
QUECTEL_VID = 0x2C7C
DEFAULT_MODEM_VID = QUECTEL_VID

# 官方驱动四口惯例顺序 DM/NMEA/AT/PPP，AT 口是第 3 个（下标 2）。
_AT_INTERFACE_INDEX = 2

# 匹配描述中的独立单词 "AT"，避免 "DATA" 之类的子串误中。
_AT_WORD_RE = re.compile(r"\bAT\b", re.IGNORECASE)


def modem_usb_vid() -> int:
    """模组 USB 厂商号（``MODEM_USB_VID``，十六进制）；非法时退回 Quectel 默认值。

    非 Quectel 模组（如 SIMCom SIM7600 = ``1e0e``）只有配对了 VID，安装向导才
    不会一直报「硬件尚未就绪」、``MODEM_PORT=auto`` 才扫得到口。配置写错时宁可
    退回默认值并告警，也不要让模组检测直接崩掉。
    """
    raw = (config.get_str("MODEM_USB_VID") or "").strip()
    try:
        vid = int(raw, 16)
    except ValueError:
        logger.warning("MODEM_USB_VID 不是合法十六进制(%r),回退 Quectel 默认值", raw)
        return DEFAULT_MODEM_VID
    if not 0 <= vid <= 0xFFFF:
        logger.warning("MODEM_USB_VID 超出 16 位范围(%r),回退 Quectel 默认值", raw)
        return DEFAULT_MODEM_VID
    return vid


def _device_order_key(device: str) -> tuple[str, int]:
    """按尾部数字排序设备名（COM9 < COM10、ttyUSB2 < ttyUSB10）。"""
    match = re.search(r"(\d+)$", device)
    if match is None:
        return device, -1
    return device[: match.start()], int(match.group(1))


def detect_at_port() -> str | None:
    """扫描模组串口并返回 AT 口设备名；找不到返回 ``None``。

    优先取 description 含独立单词 "AT" 的口。都不含时**只对 Quectel** 按四口
    惯例取第 3 个回退：别的厂商口序不同（SIM7600 是六口布局），猜错会把 DM 或
    NMEA 口当 AT 口用，表现是所有 AT 指令静默超时——比直接报「探测不到」难查
    得多，所以宁可不猜。
    """
    vid = modem_usb_vid()
    matched = [p for p in list_ports.comports() if p.vid == vid]
    if not matched:
        logger.info("未扫描到模组串口 (VID=0x%04X)", vid)
        return None

    for port in matched:
        if _AT_WORD_RE.search(port.description or ""):
            logger.info("探测到模组 AT 口: %s (%s)", port.device, port.description)
            return port.device

    if vid != QUECTEL_VID:
        logger.warning(
            "VID=0x%04X 的 %d 个串口描述均不含 AT，且该厂商口序未知，不做猜测；"
            "请把 MODEM_PORT 指定为具体串口",
            vid, len(matched),
        )
        return None

    if len(matched) > _AT_INTERFACE_INDEX:
        ordered = sorted(matched, key=lambda p: _device_order_key(p.device))
        fallback = ordered[_AT_INTERFACE_INDEX]
        logger.info(
            "Quectel 口描述均不含 AT，按第 %d 口惯例回退: %s",
            _AT_INTERFACE_INDEX + 1,
            fallback.device,
        )
        return fallback.device

    logger.warning(
        "Quectel 设备仅 %d 个串口且描述不含 AT，无法确定 AT 口", len(matched)
    )
    return None


__all__ = ["DEFAULT_MODEM_VID", "QUECTEL_VID", "detect_at_port", "modem_usb_vid"]
