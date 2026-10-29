"""Generate and manage CallPilot launchd agents for the bundled macOS app."""

from __future__ import annotations

import json
import logging
import os
import plistlib
import subprocess
import time
from dataclasses import dataclass
from functools import wraps
from pathlib import Path
from typing import Callable, Sequence

from .app_version import VERSION

BRIDGE_LABEL = "com.agentcall.bridge"
APP_LABEL = "com.agentcall.app"
TRAY_LABEL = "com.agentcall.tray"
# 安装顺序 bridge→app→tray（图标出现时服务已就绪）；卸载按 reversed 先撤 UI。
LAUNCH_LABELS = (BRIDGE_LABEL, APP_LABEL, TRAY_LABEL)

_DEFAULT_PATH = "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin"

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LaunchdLayout:
    executable: Path
    resources_dir: Path
    support_dir: Path
    launch_agents_dir: Path
    uid: int

    @property
    def gui_domain(self) -> str:
        return f"gui/{self.uid}"

    @property
    def env_file(self) -> Path:
        return self.support_dir / ".env"

    @property
    def data_dir(self) -> Path:
        return self.support_dir / "data"

    @property
    def log_dir(self) -> Path:
        return self.support_dir / "logs"


@dataclass(frozen=True)
class LaunchctlFailure:
    label: str
    action: str
    command: tuple[str, ...]
    returncode: int
    stdout: str = ""
    stderr: str = ""

    @property
    def message(self) -> str:
        details = (self.stderr or self.stdout or "").strip()
        base = f"{self.action} {self.label} failed with exit {self.returncode}"
        return f"{base}: {details}" if details else base


@dataclass
class LaunchdInstallResult:
    changed: list[str]
    failures: list[LaunchctlFailure]
    warnings: list[LaunchctlFailure]

    def __bool__(self) -> bool:
        return bool(self.changed or self.failures or self.warnings)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, list):
            return self.changed == other and not self.failures
        return super().__eq__(other)

    def failure_summary(self) -> str:
        return "; ".join(failure.message for failure in self.failures)


def app_support_dir(home: Path | None = None) -> Path:
    """Return the per-user Application Support directory for CallPilot."""
    override = os.getenv("AGENTCALL_APP_SUPPORT_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    base = (home or Path.home()) / "Library" / "Application Support"
    return base / "CallPilot"


def launch_agents_dir(home: Path | None = None) -> Path:
    return (home or Path.home()) / "Library" / "LaunchAgents"


def bundle_root_for_executable(executable: str | Path) -> Path | None:
    """Return ``*.app`` root for a bundled executable, or None outside a bundle."""
    path = Path(executable).resolve()
    parts = path.parts
    for index, part in enumerate(parts):
        if part.endswith(".app"):
            return Path(*parts[: index + 1])
    return None


def resources_dir_for_executable(executable: str | Path) -> Path:
    bundle_root = bundle_root_for_executable(executable)
    if bundle_root is not None:
        return bundle_root / "Contents" / "Resources"
    return Path(executable).resolve().parent


def _current_uid() -> int:
    getuid = getattr(os, "getuid", None)
    if not callable(getuid):
        raise RuntimeError("launchd layout is only available on macOS")
    return int(getuid())


def make_layout(
    executable: str | Path,
    *,
    home: Path | None = None,
    uid: int | None = None,
    resources_dir: str | Path | None = None,
    support_dir: str | Path | None = None,
    launch_dir: str | Path | None = None,
) -> LaunchdLayout:
    exe = Path(executable).resolve()
    return LaunchdLayout(
        executable=exe,
        resources_dir=Path(resources_dir).resolve() if resources_dir else resources_dir_for_executable(exe),
        support_dir=Path(support_dir).expanduser() if support_dir else app_support_dir(home),
        launch_agents_dir=Path(launch_dir).expanduser() if launch_dir else launch_agents_dir(home),
        uid=uid if uid is not None else _current_uid(),
    )


def _environment(layout: LaunchdLayout) -> dict[str, str]:
    bin_dir = layout.resources_dir / "bin"
    lib_dir = layout.resources_dir / "lib"
    path_parts = [str(bin_dir), _DEFAULT_PATH]
    env = {
        "PATH": ":".join(path_parts),
        "AGENTCALL_APP_SUPPORT_DIR": str(layout.support_dir),
        "AGENTCALL_ENV_FILE": str(layout.env_file),
        "AGENTCALL_DATA_DIR": str(layout.data_dir),
        "CALL_LOG_DIR": str(layout.data_dir / "recordings"),
        "CALLPILOT_BUNDLE_VERSION": VERSION,
    }
    env["DYLD_LIBRARY_PATH"] = str(lib_dir)
    return env


def build_plists(layout: LaunchdLayout) -> dict[str, dict]:
    common = {
        "AssociatedBundleIdentifiers": ["ai.bondings.callpilot"],
        "KeepAlive": True,
        "RunAtLoad": True,
        "ThrottleInterval": 10,
        "LimitLoadToSessionType": "Aqua",
        "WorkingDirectory": str(layout.support_dir),
        "EnvironmentVariables": _environment(layout),
    }
    bridge = {
        **common,
        "Label": BRIDGE_LABEL,
        "ProgramArguments": [
            "/usr/bin/caffeinate",
            "-s",
            str(layout.executable),
            "--bridge",
            "--auto",
            "--log-file",
            str(layout.log_dir / "modem_usb_pty.log"),
        ],
        "StandardOutPath": str(layout.log_dir / "launchd-bridge.out.log"),
        "StandardErrorPath": str(layout.log_dir / "launchd-bridge.err.log"),
    }
    app = {
        **common,
        "Label": APP_LABEL,
        "ProgramArguments": [
            "/usr/bin/caffeinate",
            "-s",
            str(layout.executable),
            "--service",
        ],
        "StandardOutPath": str(layout.log_dir / "launchd-app.out.log"),
        "StandardErrorPath": str(layout.log_dir / "launchd-app.err.log"),
    }
    tray = {
        **common,
        "Label": TRAY_LABEL,
        # 菜单栏 UI 进程（无参数入口）。不裹 caffeinate——防睡眠由 --service
        # 单元承担，UI 进程不该额外持系统断言。
        "ProgramArguments": [str(layout.executable), "--login"],
        # KeepAlive 用 SuccessfulExit=False：崩溃（非零退出）自动拉起；
        # 单例让位/用户主动退出（退出码 0）不再拉起——否则双击实例与
        # launchd 实例互踢会形成 ThrottleInterval 周期的重启风暴。
        "KeepAlive": {"SuccessfulExit": False},
        "StandardOutPath": str(layout.log_dir / "launchd-tray.out.log"),
        "StandardErrorPath": str(layout.log_dir / "launchd-tray.err.log"),
    }
    return {BRIDGE_LABEL: bridge, APP_LABEL: app, TRAY_LABEL: tray}


def plist_bytes(data: dict) -> bytes:
    return plistlib.dumps(data, sort_keys=True)


def plist_needs_update(path: Path, expected: dict) -> bool:
    if not path.exists():
        return True
    try:
        with path.open("rb") as f:
            current = plistlib.load(f)
    except (OSError, plistlib.InvalidFileException, ValueError):
        return True
    return current != expected


def _run_launchctl(
    args: Sequence[str],
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    *,
    check: bool = False,
) -> subprocess.CompletedProcess:
    return runner(["launchctl", *args], capture_output=True, text=True, check=check, timeout=5)


def autostart_enabled(layout: LaunchdLayout) -> bool:
    try:
        value = json.loads((layout.support_dir / "startup.json").read_text())
        return value.get("enabled", True) is not False
    except (OSError, ValueError, AttributeError):
        return True


def _serialized_settings(function):
    """托盘安装与服务写设置共用锁，避免关闭自启后安装线程又写回登录项。"""
    @wraps(function)
    def wrapped(layout, *args, **kwargs):
        layout.support_dir.mkdir(parents=True, exist_ok=True)
        with (layout.support_dir / "startup.lock").open("a") as lock:
            try:
                import fcntl
            except ImportError:
                return function(layout, *args, **kwargs)
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            return function(layout, *args, **kwargs)
    return wrapped


@_serialized_settings
def set_autostart(layout: LaunchdLayout, enabled: bool) -> None:
    """只改变下次登录：移除登录扫描目录的 plist 不会卸载已加载的任务。"""
    from . import macos_service_management as sm

    layout.support_dir.mkdir(parents=True, exist_ok=True)
    if sm.uses_service_management(layout):
        sm.set_login_enabled(layout, enabled)
    else:
        layout.launch_agents_dir.mkdir(parents=True, exist_ok=True)
        for label, data in build_plists(layout).items():
            path = layout.launch_agents_dir / f"{label}.plist"
            if enabled:
                path.write_bytes(plist_bytes(data))
            else:
                path.unlink(missing_ok=True)
    path = layout.support_dir / "startup.json"
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps({"enabled": enabled}), encoding="utf-8")
    temporary.replace(path)


def _bootout(label: str, layout: LaunchdLayout, runner: Callable[..., subprocess.CompletedProcess]) -> None:
    _run_launchctl(["bootout", f"{layout.gui_domain}/{label}"], runner)


def agent_loaded(
    label: str,
    layout: LaunchdLayout,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> bool:
    result = _run_launchctl(["print", f"{layout.gui_domain}/{label}"], runner)
    return result.returncode == 0


def _failure(label: str, action: str, result: subprocess.CompletedProcess) -> LaunchctlFailure:
    return LaunchctlFailure(
        label=label,
        action=action,
        command=tuple(str(part) for part in result.args),
        returncode=int(result.returncode),
        stdout=result.stdout or "",
        stderr=result.stderr or "",
    )


def _wait_until_unloaded(
    label: str,
    layout: LaunchdLayout,
    runner: Callable[..., subprocess.CompletedProcess],
    *,
    timeout: float,
    interval: float,
    sleep: Callable[[float], None],
) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        if not agent_loaded(label, layout, runner):
            return True
        if time.monotonic() >= deadline:
            return False
        sleep(interval)


def _bootstrap_with_retry(
    label: str,
    path: Path,
    layout: LaunchdLayout,
    runner: Callable[..., subprocess.CompletedProcess],
    *,
    attempts: int,
    retry_seconds: float,
    sleep: Callable[[float], None],
) -> LaunchctlFailure | None:
    last_failure: LaunchctlFailure | None = None
    for attempt in range(1, attempts + 1):
        result = _run_launchctl(["bootstrap", layout.gui_domain, str(path)], runner)
        if result.returncode == 0:
            if attempt > 1:
                logger.info("launchctl bootstrap succeeded for %s after %d attempts", label, attempt)
            return None
        last_failure = _failure(label, "bootstrap", result)
        logger.warning("%s (attempt %d/%d)", last_failure.message, attempt, attempts)
        if attempt < attempts:
            sleep(retry_seconds)
    return last_failure


@_serialized_settings
def install_launch_agents(
    layout: LaunchdLayout,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    *,
    sleep: Callable[[float], None] = time.sleep,
    unload_timeout: float = 10.0,
    unload_interval: float = 0.2,
    bootstrap_attempts: int = 3,
    bootstrap_retry_seconds: float = 2.0,
    no_restart_labels: frozenset[str] | set[str] = frozenset(),
    force_restart_labels: frozenset[str] | set[str] = frozenset(),
    before_restart: Callable[[], None] | None = None,
    verify_bridge: Callable[[], bool] | None = None,
    user_initiated: bool = False,
) -> LaunchdInstallResult:
    """Write missing/stale plists and bootstrap changed agents.

    Returns labels that were written and bootstrapped.

    ``no_restart_labels``：plist 变更时原地写盘、不 bootout 在跑实例（改动下次
    respawn/登录生效），未加载时照常 bootstrap。给「安装方即被装单元本身」的
    场景用——tray 进程安装 tray 单元时若 bootout 自己，会被 launchd 杀死在
    写盘之前，导致新 plist 永远落不了地。
    """
    layout.support_dir.mkdir(parents=True, exist_ok=True)
    layout.data_dir.mkdir(parents=True, exist_ok=True)
    layout.log_dir.mkdir(parents=True, exist_ok=True)
    if not layout.env_file.exists():
        layout.env_file.touch(mode=0o600)

    from . import macos_service_management as sm

    if sm.uses_service_management(layout):
        return sm.install(layout, runner, sleep=sleep, no_restart_labels=no_restart_labels,
                          force_restart_labels=force_restart_labels, before_restart=before_restart,
                          verify_bridge=verify_bridge, user_initiated=user_initiated)
    layout.launch_agents_dir.mkdir(parents=True, exist_ok=True)

    changed: list[str] = []
    failures: list[LaunchctlFailure] = []
    warnings: list[LaunchctlFailure] = []
    if user_initiated:
        # 只在用户主动启动时清除本 App 的历史 disabled 状态，登录启动不改变用户域设置。
        for label in LAUNCH_LABELS:
            result = _run_launchctl(["enable", f"{layout.gui_domain}/{label}"], runner)
            if result.returncode:
                failures.append(_failure(label, "enable", result))
    # 关闭自启仍可手动 bootstrap；运行期 plist 不放在登录扫描目录。
    enabled = autostart_enabled(layout)
    plist_dir = layout.launch_agents_dir if enabled else layout.support_dir / "LaunchAgents"
    plist_dir.mkdir(parents=True, exist_ok=True)
    # A replacement is one generation: stop the service before its bridge,
    # then bootstrap bridge -> service. Never leave a new bridge under old code.
    stopped = set()
    if force_restart_labels and before_restart is not None:
        before_restart()
    for label in reversed(LAUNCH_LABELS):
        if label not in force_restart_labels or label in no_restart_labels:
            continue
        result = _run_launchctl(["bootout", f"{layout.gui_domain}/{label}"], runner)
        if result.returncode:
            warnings.append(_failure(label, "bootout", result))
        if not _wait_until_unloaded(label, layout, runner, timeout=unload_timeout,
                                    interval=unload_interval, sleep=sleep):
            failures.append(LaunchctlFailure(label, "wait-unloaded", tuple(result.args), 1,
                                             stderr="Previous service generation is still loaded"))
            return LaunchdInstallResult(changed, failures, warnings)
        stopped.add(label)
    for label, data in build_plists(layout).items():
        if failures and force_restart_labels:
            break
        if not enabled:
            (layout.launch_agents_dir / f"{label}.plist").unlink(missing_ok=True)
        path = plist_dir / f"{label}.plist"
        if not plist_needs_update(path, data) and label not in stopped:
            if not agent_loaded(label, layout, runner):
                failure = _bootstrap_with_retry(
                    label,
                    path,
                    layout,
                    runner,
                    attempts=bootstrap_attempts,
                    retry_seconds=bootstrap_retry_seconds,
                    sleep=sleep,
                )
                if failure is None:
                    changed.append(label)
                else:
                    failures.append(failure)
            continue
        if label in no_restart_labels:
            # 原地更新：只写盘不踢在跑实例；未加载（如首次安装）才 bootstrap。
            path.write_bytes(plist_bytes(data))
            if not agent_loaded(label, layout, runner):
                failure = _bootstrap_with_retry(
                    label,
                    path,
                    layout,
                    runner,
                    attempts=bootstrap_attempts,
                    retry_seconds=bootstrap_retry_seconds,
                    sleep=sleep,
                )
                if failure is not None:
                    failures.append(failure)
                    continue
            changed.append(label)
            continue
        if label in stopped:
            path.write_bytes(plist_bytes(data))
            failure = _bootstrap_with_retry(
                label, path, layout, runner, attempts=bootstrap_attempts,
                retry_seconds=bootstrap_retry_seconds, sleep=sleep,
            )
            if failure is not None:
                failures.append(failure)
            else:
                changed.append(label)
                if label == BRIDGE_LABEL and verify_bridge is not None and not verify_bridge():
                    failures.append(LaunchctlFailure(label, "verify-version", (), 1,
                                                     stderr="New bridge did not acquire its instance lock"))
            continue
        bootout = _run_launchctl(["bootout", f"{layout.gui_domain}/{label}"], runner)
        if bootout.returncode != 0:
            warning = _failure(label, "bootout", bootout)
            warnings.append(warning)
            logger.info("%s", warning.message)
        if not _wait_until_unloaded(
            label,
            layout,
            runner,
            timeout=unload_timeout,
            interval=unload_interval,
            sleep=sleep,
        ):
            failure = LaunchctlFailure(
                label=label,
                action="wait-unloaded",
                command=("launchctl", "print", f"{layout.gui_domain}/{label}"),
                returncode=0,
                stderr=f"still loaded after {unload_timeout:.1f}s",
            )
            failures.append(failure)
            logger.error("%s", failure.message)
            continue
        path.write_bytes(plist_bytes(data))
        failure = _bootstrap_with_retry(
            label,
            path,
            layout,
            runner,
            attempts=bootstrap_attempts,
            retry_seconds=bootstrap_retry_seconds,
            sleep=sleep,
        )
        if failure is None:
            changed.append(label)
        else:
            failures.append(failure)
    return LaunchdInstallResult(changed=changed, failures=failures, warnings=warnings)


def uninstall_launch_agents(
    layout: LaunchdLayout,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> list[str]:
    removed: list[str] = []
    for label in reversed(LAUNCH_LABELS):
        path = layout.launch_agents_dir / f"{label}.plist"
        _bootout(label, layout, runner)
        if path.exists():
            path.unlink()
            removed.append(label)
    return removed


__all__ = [
    "APP_LABEL",
    "BRIDGE_LABEL",
    "TRAY_LABEL",
    "LAUNCH_LABELS",
    "LaunchdLayout",
    "LaunchctlFailure",
    "LaunchdInstallResult",
    "agent_loaded",
    "app_support_dir",
    "build_plists",
    "bundle_root_for_executable",
    "install_launch_agents",
    "launch_agents_dir",
    "make_layout",
    "plist_needs_update",
    "resources_dir_for_executable",
    "uninstall_launch_agents",
]
