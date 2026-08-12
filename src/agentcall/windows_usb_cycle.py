"""Windows：挂断后对 SimTech/SIMCom USB 复合设备做软拔插。

SIM7600 USB Audio 跨通劣化时，软 ``AT+CRESET`` 不够，物理断电才归零。
对本机 USB 复合设备 Disable→Enable 是最接近冷插拔的宿主侧缓解：下一通更
容易走「首启一次 mode=1」路径。需要管理员权限；优先走已安装的计划任务
（``scripts/windows/install_usb_cycle_helper.ps1``），避免每通弹 UAC。
"""

from __future__ import annotations

import logging
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from . import platforms

logger = logging.getLogger(__name__)

_TASK_NAME = "CallPilotSimTechUsbCycle"
# Must match scripts/windows/soft_cycle_simtech_usb.ps1 default: SYSTEM task
# and the Edge process share this path (user %TEMP% does not).
_STATUS_PATH = Path(os.environ.get("ProgramData", r"C:\ProgramData")) / "CallPilot" / "usb-cycle.status"
_SCRIPT_REL = Path("scripts") / "windows" / "soft_cycle_simtech_usb.ps1"


@dataclass(frozen=True)
class UsbCycleResult:
    ok: bool
    detail: str
    method: str = ""


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _status_path() -> Path:
    return _STATUS_PATH


def _script_path() -> Path:
    return _project_root() / _SCRIPT_REL


def _normalize_vid(vid: str) -> str:
    text = (vid or "").strip().lower().removeprefix("0x")
    if len(text) > 4:
        text = text[-4:]
    return text.upper() or "1E0E"


def soft_cycle_simtech_usb(
    *,
    vid: str = "1e0e",
    timeout_seconds: float = 45.0,
) -> UsbCycleResult:
    """Disable/Enable the SIMCom USB composite device.

    Prefer the elevated scheduled task; fall back to an in-process PowerShell
    attempt (works when Edge itself is elevated).
    """
    if not platforms.IS_WINDOWS:
        return UsbCycleResult(False, "not_windows", "skip")

    vid_norm = _normalize_vid(vid)
    status = _status_path()
    # 状态文件多半由 SYSTEM 计划任务写在 ProgramData 下，非管理员的 Edge 删不掉它。
    # 删除失败必须继续用「本次调用之后才落盘」来判定，否则会把上一次（甚至几小时前）
    # 的 OK 当成本次成功——真机 2026-08-12：软拔插连续多通报成功，实际一次都没执行。
    stale_mtime = _mtime_or_none(status)
    try:
        status.unlink(missing_ok=True)
    except OSError as exc:
        logger.info("USB 软拔插状态文件无法删除（%s），改按写入时间判定新鲜度", exc)
    else:
        stale_mtime = None

    task_result = _run_via_scheduled_task(vid_norm, status, timeout_seconds, stale_mtime)
    if task_result is not None:
        return task_result

    return _run_via_direct_powershell(vid_norm, status, timeout_seconds, stale_mtime)


def _mtime_or_none(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def wait_for_simtech_ports(
    *,
    vid: str = "1e0e",
    timeout_seconds: float = 20.0,
) -> bool:
    """Wait until at least one SimTech Ports-class device for ``vid`` is OK."""
    if not platforms.IS_WINDOWS:
        return False
    vid_norm = _normalize_vid(vid)
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if _ports_present(vid_norm):
            return True
        time.sleep(0.5)
    return False


def _run_via_scheduled_task(
    vid: str,
    status: Path,
    timeout_seconds: float,
    stale_mtime: float | None,
) -> UsbCycleResult | None:
    # 普通用户进程对 /Query 可能「拒绝访问」，但仍可能 /Run 成功——直接尝试 Run。
    started = subprocess.run(
        ["schtasks", "/Run", "/TN", _TASK_NAME],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if started.returncode != 0:
        logger.info(
            "计划任务 %s 未能触发，改走直接 PowerShell: %s",
            _TASK_NAME,
            (started.stderr or started.stdout or "").strip()[:200],
        )
        return None

    return _wait_status(status, timeout_seconds, stale_mtime, method="scheduled_task")


def _run_via_direct_powershell(
    vid: str,
    status: Path,
    timeout_seconds: float,
    stale_mtime: float | None,
) -> UsbCycleResult:
    script = _script_path()
    if not script.is_file():
        return UsbCycleResult(False, f"missing_script:{script}", "direct")

    # Non-elevated Edge usually fails Disable-PnpDevice; still try so elevated
    # deployments work without the helper task.
    proc = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
            "-Vid",
            vid,
            "-StatusFile",
            str(status),
        ],
        capture_output=True,
        text=True,
        timeout=max(15.0, timeout_seconds),
        check=False,
    )
    parsed = _read_status(status, "direct", stale_mtime)
    if parsed is not None and parsed.ok:
        return parsed

    detail = parsed.detail if parsed is not None else ""
    if not detail:
        err = (proc.stderr or proc.stdout or f"exit={proc.returncode}").strip()
        detail = err[:240] or "direct_powershell_failed"
    logger.warning(
        "USB 软拔插直接执行失败（多半缺管理员权限）。"
        "请以管理员运行一次 scripts/windows/install_usb_cycle_helper.ps1。"
        " detail=%s",
        detail,
    )
    return UsbCycleResult(False, detail, "direct")


def _wait_status(
    status: Path,
    timeout_seconds: float,
    stale_mtime: float | None,
    *,
    method: str,
) -> UsbCycleResult:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        result = _read_status(status, method, stale_mtime)
        if result is not None:
            return result
        time.sleep(0.25)
    return UsbCycleResult(False, "status_timeout", method)


def _read_status(
    status: Path,
    method: str,
    stale_mtime: float | None = None,
) -> UsbCycleResult | None:
    """读状态文件。

    ``stale_mtime`` 是本次运行前就存在的那份状态的时间戳（删不掉时才有值）；
    未被重写就返回 None，避免把上一次的 OK 当成本次成功。
    """
    try:
        written_at = status.stat().st_mtime
    except OSError:
        return None
    if stale_mtime is not None and written_at <= stale_mtime:
        return None
    try:
        # PowerShell Set-Content -Encoding UTF8 会带 BOM；不能只用 startswith("OK")。
        text = status.read_text(encoding="utf-8-sig").strip()
    except OSError as exc:
        return UsbCycleResult(False, f"status_read:{exc}", method)
    if text.upper().startswith("OK"):
        return UsbCycleResult(True, text, method)
    return UsbCycleResult(False, text or "empty_status", method)


def _ports_present(vid: str) -> bool:
    proc = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-Command",
            (
                f"$v='{vid}'; "
                "@(Get-PnpDevice -Class Ports -PresentOnly -EA SilentlyContinue | "
                "Where-Object { $_.InstanceId -like \"USB\\VID_$v*\" -and $_.Status -eq 'OK' }).Count"
            ),
        ],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    try:
        return int((proc.stdout or "0").strip().splitlines()[-1]) > 0
    except (ValueError, IndexError):
        return False
