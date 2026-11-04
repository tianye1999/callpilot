"""HKCU Run 开机自启（仅冻结 Windows 托盘使用）。

托盘开关读写 ``HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run`` 下的
``CallPilot`` 字符串值。值格式为带引号的 exe 绝对路径 + ``--login``。注册表只在
Windows 上惰性导入，源码与 macOS/Linux 不触碰注册表，便于跨平台单测打桩。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "CallPilot"
LOGIN_ARG = "--login"

_RUN_QUOTED_RE = re.compile(r'^"([^"]+)"')


class AutostartUnavailable(RuntimeError):
    """非 Windows 环境调用注册表自启功能。"""


def current_exe() -> Path:
    return Path(sys.executable).resolve()


def run_command(exe: Path | None = None) -> str:
    """生成写入 Run 的启动命令：带引号绝对路径 + 现有登录参数。"""
    return f'"{exe or current_exe()}" {LOGIN_ARG}'


def _winreg():
    if sys.platform != "win32":
        raise AutostartUnavailable("开机自启仅支持 Windows")
    import winreg

    return winreg


def read_run_value() -> str | None:
    """实时读取 Run 值；不存在或非字符串返回 None。"""
    winreg = _winreg()
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ) as key:
            value, _ = winreg.QueryValueEx(key, VALUE_NAME)
    except OSError:
        return None
    return value if isinstance(value, str) else None


def is_enabled(exe: Path | None = None) -> bool:
    return read_run_value() == run_command(exe)


def set_enabled(enabled: bool, exe: Path | None = None) -> None:
    winreg = _winreg()
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, run_command(exe))
        else:
            try:
                winreg.DeleteValue(key, VALUE_NAME)
            except OSError:
                pass


def stale_run_target(exe: Path | None = None) -> Path | None:
    """Run 值指向别的 exe 时返回旧路径；否则返回 None。"""
    value = read_run_value()
    if not value:
        return None
    target = _parse_run_target(value)
    current = exe or current_exe()
    if target is not None and target != current:
        return target
    return None


def _parse_run_target(value: str) -> Path | None:
    """解析 Run 值首个带引号或空白分隔的 exe 路径。"""
    stripped = value.strip()
    if not stripped:
        return None
    match = _RUN_QUOTED_RE.match(stripped)
    first = match.group(1) if match else stripped.split(maxsplit=1)[0]
    return Path(first).resolve()
