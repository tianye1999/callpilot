"""平台差异集中点：Windows / macOS / Linux 的默认值与路径单一出处。

原则：业务代码不直接判 ``sys.platform``——需要分平台的行为一律经此模块，
让「哪里因平台而异」全项目一眼可查。
"""

from __future__ import annotations

import sys
from pathlib import Path

IS_WINDOWS = sys.platform == "win32"
IS_MACOS = sys.platform == "darwin"
IS_LINUX = sys.platform == "linux"

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


def default_pcm_pin_8k() -> bool:
    """仅 Linux 默认钉住 SIM7600 PCM 8k；macOS / Windows 保持原有命令序列。"""
    return IS_LINUX


def default_reset_after_call() -> bool:
    """Linux SIM7600 每通后复位；其他平台默认保持原有行为。"""
    return IS_LINUX


def default_parallel_agent_connect() -> bool:
    """Linux overlaps AI startup with modem preparation; other platforms opt in."""
    return IS_LINUX


def default_modem_answer_greeting() -> str:
    """Only Linux enables the SIM7600 native answer greeting by default."""
    return "喂" if IS_LINUX else ""


def default_modem_answer_greeting_delay_ms() -> int:
    return 500


def default_modem_tts_params() -> str:
    return "2,2,0,1,1,0"


def venv_python(project_root: Path) -> Path:
    """项目 venv 内 Python 解释器路径（POSIX bin/ 与 Windows Scripts\\ 之分）。"""
    if IS_WINDOWS:
        return project_root / ".venv" / "Scripts" / "python.exe"
    return project_root / ".venv" / "bin" / "python"
