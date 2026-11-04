"""USB 模组预设与桥状态：桥、服务共用，导入时不访问硬件。"""

from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
import threading
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
LEGACY_RECOVERY_REQUEST_PATH = "/tmp/sim7600-recover-request"
STATE_PATH = Path("/tmp/callpilot-modem.json")


def recovery_request_path() -> Path:
    """App 与 USB 桥共用的自愈哨兵路径；跨平台（旧值仅 POSIX /tmp 可写）。

    写方（modem._request_recovery_if_degraded）与消费方（ec20_usb_pty 桥）
    都必须经此函数解析，不得各自硬编码：冻结版 macOS 用 ``AGENTCALL_DATA_DIR``
    指向同一个数据目录，开发版靠双方相同的 ``WorkingDirectory`` 落在同一
    ``data/`` 下。旧 ``/tmp/sim7600-recover-request`` 保留为迁移兼容，
    桥启动时清掉陈旧哨兵。
    """
    return config.data_dir() / "sim7600-recover-request"


def write_recovery_request(reason: str) -> Path:
    """原子写入自愈请求哨兵；父目录不存在时创建，保证 Windows 数据目录可用。"""
    path = recovery_request_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".sim7600-recover-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(f"{reason} at {time.time():.0f}\n")
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)
    return path


def clear_recovery_request() -> None:
    """消费/清理新旧两代自愈哨兵；桥启动时调用，避免陈旧请求反复触发。"""
    for candidate in (recovery_request_path(), Path(LEGACY_RECOVERY_REQUEST_PATH)):
        try:
            candidate.unlink(missing_ok=True)
        except OSError:
            logger.warning("清自愈哨兵失败: %s", candidate)
NEEDS_POWER_CYCLE_PATH = Path("/tmp/callpilot-modem-needs-power-cycle")
STATE_MAX_AGE = 15.0
# 服务初始化识别与 meta 观测扫描互斥，不能同时枚举同一设备。
USB_SCAN_LOCK = threading.Lock()
USB_SCAN_ONLINE: bool | None = None


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
    reset_on_start: bool = False  # 仅控制首次启动；快速失败恢复由桥入口单独决定
    recover_alt_pid: int | None = None
    recovery_request: bool = False
    serial_at_pattern: str = r"\bAT\b"
    serial_pcm_pattern: str | None = None

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
                # 启动复位会导致首通 PCM 数据面起不来，默认仅快速失败后复位。
                "nmea", False, SIM7600_ALT_PID, True,
                serial_pcm_pattern=r"\b(?:Audio|PCM)\b"),
)


def select_preset(devices: list[tuple[int, int]]) -> ModemPreset | None:
    return next((p for p in PRESETS if (p.vid, p.pid) in devices), None)


def write_state(preset: ModemPreset, *, online: bool) -> None:
    data = {**preset.state(), "online": online, "updated_at": time.time()}
    # 同目录临时文件 + replace，读方只会见到完整的一版；不跟随目标符号链接。
    fd, name = tempfile.mkstemp(prefix=".callpilot-modem-", dir=STATE_PATH.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream)
        os.replace(name, STATE_PATH)
    finally:
        Path(name).unlink(missing_ok=True)


def read_state() -> dict | None:
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
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


def write_needs_power_cycle(reason: str) -> None:
    """持久告警不设有效期；重启桥不能证明模组已恢复。"""
    name = None
    try:
        fd, name = tempfile.mkstemp(prefix=".callpilot-power-cycle-", dir=NEEDS_POWER_CYCLE_PATH.parent)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump({"updated_at": time.time(), "reason": reason}, stream)
        os.replace(name, NEEDS_POWER_CYCLE_PATH)
    except OSError as exc:
        logger.warning("写模组重插标记失败: %s", exc)
    finally:
        if name is not None:
            Path(name).unlink(missing_ok=True)


def read_needs_power_cycle() -> dict | None:
    try:
        data = json.loads(NEEDS_POWER_CYCLE_PATH.read_text(encoding="utf-8"))
        if (isinstance(data, dict) and isinstance(data.get("updated_at"), (int, float))
                and isinstance(data.get("reason"), str)):
            return data
    except (OSError, ValueError):
        pass
    return None


def clear_needs_power_cycle() -> None:
    try:
        NEEDS_POWER_CYCLE_PATH.unlink(missing_ok=True)
    except OSError as exc:
        logger.warning("清模组重插标记失败: %s", exc)


def scan_preset() -> ModemPreset | None:
    global USB_SCAN_ONLINE
    with USB_SCAN_LOCK:
        started = time.monotonic()
        result = None
        online = None
        try:
            result = _scan_preset()
            online = result is not None
            return result
        finally:
            USB_SCAN_ONLINE = online
            ended = time.monotonic()
            logger.info("USB_SCAN source=setup start_mono=%.6f end_mono=%.6f duration_ms=%.3f result=%s",
                        started, ended, (ended - started) * 1000, result.vendor if result else None)


def _scan_preset() -> ModemPreset | None:
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
    if platforms.IS_WINDOWS and vendor.strip().lower() in {"auto", "simcom"}:
        from .port_detect import detect_serial_ports

        selected = detect_modem()["vendor"] if vendor.strip().lower() == "auto" else "simcom"
        if selected == "simcom":
            if not config.is_explicit("MODEM_AUDIO_MODE"):
                audio_mode = platforms.default_audio_mode("simcom")
            # Only resolve a pair for auto AT; fixed installations retain explicit COM values.
            if port == platforms.AUTO_PORT:
                at, pcm = detect_serial_ports("simcom")
                if at:
                    port = at
                if pcm_port == platforms.AUTO_PORT or (not pcm_port and not config.is_explicit("MODEM_PCM_PORT")):
                    pcm_port = pcm
            return selected, port, audio_mode, pcm_port
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
