"""平台差异集中点：Windows / macOS / Linux 的默认值与路径单一出处。

原则：业务代码不直接判 ``sys.platform``——需要分平台的行为一律经此模块，
让「哪里因平台而异」全项目一眼可查。
"""

from __future__ import annotations

import sys
from pathlib import Path

IS_WINDOWS = sys.platform == "win32"
IS_MACOS = sys.platform == "darwin"

# MODEM_PORT 的 "auto" 哨兵：连接时经 port_detect 按 Quectel VID 自动扫描。
AUTO_PORT = "auto"


def default_modem_port() -> str:
    """按平台给 MODEM_PORT 默认值。

    - macOS 无 Quectel 原生驱动，走 USB→PTY 桥暴露的固定符号链接；
    - Windows 有官方驱动，COM 号因机器而异，用 auto 哨兵启动时扫描；
    - Linux 官方驱动下 AT 口惯例为 ttyUSB2（DM/NMEA/AT/PPP 四口顺序）。
    """
    if IS_MACOS:
        return "/tmp/ec20-at"
    if IS_WINDOWS:
        return AUTO_PORT
    return "/dev/ttyUSB2"


def default_audio_mode() -> str:
    """按平台给 MODEM_AUDIO_MODE 默认值。

    macOS 上 PortAudio 打不开 EC20 的 UAC 声卡（AUHAL -66740），须走
    ffmpeg(avfoundation/audiotoolbox)；其他平台 PortAudio 本身可用，
    uac 即标准路径（Windows 经 WASAPI，Linux 经 ALSA）。
    """
    return "uac_ffmpeg" if IS_MACOS else "uac"


def venv_python(project_root: Path) -> Path:
    """项目 venv 内 Python 解释器路径（POSIX bin/ 与 Windows Scripts\\ 之分）。"""
    if IS_WINDOWS:
        return project_root / ".venv" / "Scripts" / "python.exe"
    return project_root / ".venv" / "bin" / "python"
