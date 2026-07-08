"""Generate and manage CallPilot launchd agents for the bundled macOS app."""

from __future__ import annotations

import os
import plistlib
import subprocess
import time
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

BRIDGE_LABEL = "com.agentcall.bridge"
APP_LABEL = "com.agentcall.app"
LAUNCH_LABELS = (BRIDGE_LABEL, APP_LABEL)

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
        uid=uid if uid is not None else os.getuid(),
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
    }
    env["DYLD_LIBRARY_PATH"] = str(lib_dir)
    return env


def build_plists(layout: LaunchdLayout) -> dict[str, dict]:
    common = {
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
            "--map",
            "2:/tmp/ec20-at",
            "--map",
            "1:/tmp/ec20-nmea",
            "--map",
            "3:/tmp/ec20-modem",
            "--log-file",
            str(layout.log_dir / "ec20_usb_pty.log"),
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
    return {BRIDGE_LABEL: bridge, APP_LABEL: app}


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
    return runner(["launchctl", *args], capture_output=True, text=True, check=check)


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


def install_launch_agents(
    layout: LaunchdLayout,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    *,
    sleep: Callable[[float], None] = time.sleep,
    unload_timeout: float = 10.0,
    unload_interval: float = 0.2,
    bootstrap_attempts: int = 3,
    bootstrap_retry_seconds: float = 2.0,
) -> LaunchdInstallResult:
    """Write missing/stale plists and bootstrap changed agents.

    Returns labels that were written and bootstrapped.
    """
    layout.support_dir.mkdir(parents=True, exist_ok=True)
    layout.data_dir.mkdir(parents=True, exist_ok=True)
    layout.log_dir.mkdir(parents=True, exist_ok=True)
    layout.launch_agents_dir.mkdir(parents=True, exist_ok=True)
    if not layout.env_file.exists():
        layout.env_file.touch(mode=0o600)

    changed: list[str] = []
    failures: list[LaunchctlFailure] = []
    warnings: list[LaunchctlFailure] = []
    for label, data in build_plists(layout).items():
        path = layout.launch_agents_dir / f"{label}.plist"
        if not plist_needs_update(path, data):
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
