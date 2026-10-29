"""USB 模组预设与桥状态：桥、服务共用，导入时不访问硬件。"""

from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from . import config, platforms

logger = logging.getLogger(__name__)

# 从 SIM7600 开发桥迁入；原脚本继续导出这些常量，避免两套预设漂移。
SIM7600_VID = 0x1E0E
SIM7600_PID = 0x9001
SIM7600_ALT_PID = 0x9011
SIM7600_MAPS = ["2:/tmp/sim7600-at", "4:/tmp/sim7600-pcm"]
RECOVERY_REQUEST_PATH = "/tmp/sim7600-recover-request"
STATE_PATH = Path("/tmp/callpilot-modem.json")
STATE_MAX_AGE = 15.0


@dataclass(frozen=True)
class ModemPreset:
    vendor: str
    model: str
    vid: int
    pid: int
    maps: tuple[str, ...]
    at_port: str
    pcm_port: str | None
    audio_mode: str
    reset_on_start: bool = False
    recover_alt_pid: int | None = None
    recovery_request_path: str | None = None

    def state(self) -> dict:
        return {
            "vendor": self.vendor, "model": self.model, "vid": self.vid, "pid": self.pid,
            "at_port": self.at_port, "pcm_port": self.pcm_port,
            "audio_mode": self.audio_mode,
        }


# 同时插入时按固定顺序优先 Quectel；已选定的设备断开前不抢占。
PRESETS = (
    ModemPreset("quectel", "EC20/EG25", 0x2C7C, 0x0125,
                ("2:/tmp/ec20-at", "1:/tmp/ec20-nmea", "3:/tmp/ec20-modem"),
                "/tmp/ec20-at", None, "uac_ffmpeg"),
    ModemPreset("simcom", "SIM7600", SIM7600_VID, SIM7600_PID, tuple(SIM7600_MAPS),
                SIM7600_MAPS[0].split(":", 1)[1], SIM7600_MAPS[1].split(":", 1)[1],
                "nmea", True, SIM7600_ALT_PID, RECOVERY_REQUEST_PATH),
)


def select_preset(devices: list[tuple[int, int]]) -> ModemPreset | None:
    return next((p for p in PRESETS if (p.vid, p.pid) in devices), None)


def write_state(preset: ModemPreset, *, online: bool) -> None:
    data = {**preset.state(), "online": online, "updated_at": time.time()}
    # 同目录临时文件 + replace，读方只会见到完整的一版；不跟随目标符号链接。
    fd, name = tempfile.mkstemp(prefix=".callpilot-modem-", dir=STATE_PATH.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(data, stream)
        os.replace(name, STATE_PATH)
    finally:
        Path(name).unlink(missing_ok=True)


def read_state() -> dict | None:
    try:
        data = json.loads(STATE_PATH.read_text())
        if not isinstance(data, dict) or data.get("online") is not True:
            return None
        age = time.time() - float(data["updated_at"])
        preset = next((p for p in PRESETS if p.vendor == data.get("vendor")), None)
        if preset is None or not 0 <= age <= STATE_MAX_AGE:
            return None
        if (data.get("vid"), data.get("pid")) != (preset.vid, preset.pid):
            return None
        if not isinstance(data.get("at_port"), str) or not data["at_port"]:
            return None
        if data.get("audio_mode") not in {"uac", "uac_ffmpeg", "nmea"}:
            return None
        if data.get("pcm_port") is not None and not isinstance(data["pcm_port"], str):
            return None
        return data
    except (OSError, ValueError, KeyError, TypeError):
        return None


def scan_preset() -> ModemPreset | None:
    if platforms.IS_MACOS:
        import usb.backend.libusb1
        import usb.core

        base = getattr(sys, "_MEIPASS", None)
        bundled = Path(base) / "lib/libusb-1.0.0.dylib" if base else None
        backend = (
            usb.backend.libusb1.get_backend(find_library=lambda _: str(bundled))
            if bundled and bundled.is_file() else None
        )
        vids = {d.idVendor for d in usb.core.find(find_all=True, backend=backend)}
    else:
        from serial.tools import list_ports

        vids = {p.vid for p in list_ports.comports()}
    return next((p for p in PRESETS if p.vid in vids), None)


def detect_modem() -> dict:
    try:
        state = read_state()
        if state is not None:
            return state
        preset = scan_preset()
        if preset is not None:
            return preset.state()
    except Exception as exc:  # noqa: BLE001
        logger.warning("模组自动检测失败，沿用 Quectel: %s", type(exc).__name__)
    return PRESETS[0].state()


def resolve_settings(
    vendor: str, port: str, audio_mode: str, pcm_port: str | None,
) -> tuple[str, str, str, str | None]:
    if vendor.strip().lower() != "auto":
        return vendor, port, audio_mode, pcm_port
    state = detect_modem()
    if state["vendor"] == "simcom":
        # /tmp 路径属于 Mac 桥；其他平台只扫描厂商，保留原生端口配置。
        bridge_ports = platforms.IS_MACOS or state.get("online") is True
        if bridge_ports and not config.is_explicit("MODEM_PORT"):
            port = state["at_port"]
        if not config.is_explicit("MODEM_AUDIO_MODE"):
            audio_mode = state["audio_mode"]
        if bridge_ports and not config.is_explicit("MODEM_PCM_PORT"):
            pcm_port = state["pcm_port"]
    return state["vendor"], port, audio_mode, pcm_port
