"""Quectel AT 串口自动探测（``MODEM_PORT=auto`` 时使用）。

Windows 官方驱动把 EC20/EG25 暴露为多个 COM 口，AT 口的 description
通常含 "AT"（如 "Quectel USB AT Port"）；描述不可用时按 Quectel 四口
惯例顺序（DM/NMEA/AT/PPP）取第 3 个接口回退。纯扫描无副作用，
``list_ports.comports`` 可在测试中替换。

Windows 真机行为待硬件验证（本机无 Windows 环境）。
"""

from __future__ import annotations

import logging
import re

from serial.tools import list_ports

logger = logging.getLogger(__name__)

# Quectel 的 USB Vendor ID（EC20/EG25 全系共用）。
QUECTEL_VID = 0x2C7C

# 官方驱动四口惯例顺序 DM/NMEA/AT/PPP，AT 口是第 3 个（下标 2）。
_AT_INTERFACE_INDEX = 2

# 匹配描述中的独立单词 "AT"，避免 "DATA" 之类的子串误中。
_AT_WORD_RE = re.compile(r"\bAT\b", re.IGNORECASE)


def _device_order_key(device: str) -> tuple[str, int]:
    """按尾部数字排序设备名（COM9 < COM10、ttyUSB2 < ttyUSB10）。"""
    match = re.search(r"(\d+)$", device)
    if match is None:
        return device, -1
    return device[: match.start()], int(match.group(1))


def detect_at_port() -> str | None:
    """扫描 Quectel 设备并返回 AT 口设备名；找不到返回 ``None``。

    优先取 description 含独立单词 "AT" 的口；没有则按接口顺序惯例
    取第 3 个 Quectel 口回退；无 Quectel 设备或口数不足时返回 ``None``。
    """
    quectel = [p for p in list_ports.comports() if p.vid == QUECTEL_VID]
    if not quectel:
        logger.info("未扫描到 Quectel 设备 (VID=0x%04X)", QUECTEL_VID)
        return None

    for port in quectel:
        if _AT_WORD_RE.search(port.description or ""):
            logger.info(
                "探测到 Quectel AT 口: %s (%s)", port.device, port.description
            )
            return port.device

    if len(quectel) > _AT_INTERFACE_INDEX:
        ordered = sorted(quectel, key=lambda p: _device_order_key(p.device))
        fallback = ordered[_AT_INTERFACE_INDEX]
        logger.info(
            "Quectel 口描述均不含 AT，按第 %d 口惯例回退: %s",
            _AT_INTERFACE_INDEX + 1,
            fallback.device,
        )
        return fallback.device

    logger.warning(
        "Quectel 设备仅 %d 个串口且描述不含 AT，无法确定 AT 口", len(quectel)
    )
    return None


__all__ = ["QUECTEL_VID", "detect_at_port"]
