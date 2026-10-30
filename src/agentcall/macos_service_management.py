"""Bundle-owned agents on macOS 13+. Native calls are isolated for offline tests."""

from __future__ import annotations

import json
import logging
import os
import platform
import plistlib
import re
import subprocess
import time

from . import macos_launchd as ld

logger = logging.getLogger(__name__)

SM_STATUS_NOT_REGISTERED = 0
SM_STATUS_ENABLED = 1
SM_STATUS_REQUIRES_APPROVAL = 2
SM_STATUS_NOT_FOUND = 3
LEGACY_MIGRATION_SECONDS = 180

APPROVAL_REQUIRED_ZH = (
    "请在 系统设置 → 通用 → 登录项与扩展 中打开 CallPilot 的后台开关"
    "（旧版可能显示为 caffeinate），然后点重试。"
)
APPROVAL_REQUIRED_EN = (
    "Turn on CallPilot's background switch in System Settings > General > Login Items & Extensions "
    "(older versions may appear as caffeinate), then click retry."
)


class ApprovalRequired(RuntimeError):
    def __init__(self):
        super().__init__(f"{APPROVAL_REQUIRED_ZH}\n{APPROVAL_REQUIRED_EN}")


def bundled_plists() -> dict[str, dict]:
    arguments = {ld.BRIDGE_LABEL: ["--bridge", "--auto"], ld.APP_LABEL: ["--service"],
                 ld.TRAY_LABEL: ["--login"]}
    return {label: {
        "Label": label, "BundleProgram": "Contents/MacOS/CallPilot",
        "ProgramArguments": ["CallPilot", *args],
        "AssociatedBundleIdentifiers": ["ai.bondings.callpilot"],
        # Only tray starts at login. It verifies the bridge before starting app.
        # Even crash recovery must reconcile the pair after an app overwrite.
        "RunAtLoad": label == ld.TRAY_LABEL,
        "KeepAlive": {"SuccessfulExit": False} if label == ld.TRAY_LABEL else False,
        "ThrottleInterval": 10, "LimitLoadToSessionType": "Aqua",
    } for label, args in arguments.items()}


def embedded_directory(layout):
    bundle = ld.bundle_root_for_executable(layout.executable)
    return bundle / "Contents/Library/LaunchAgents" if bundle else None


def uses_service_management(layout) -> bool:
    directory = embedded_directory(layout)
    return directory is not None and directory.is_dir()


class NativeServices:
    def __init__(self):
        if int(platform.mac_ver()[0].split(".")[0] or 0) < 13:
            raise RuntimeError("CallPilot requires macOS 13 or later")
        from ServiceManagement import SMAppService

        self.api = SMAppService

    def _service(self, label):
        if label not in ld.LAUNCH_LABELS:
            raise ValueError("Unknown CallPilot agent")
        return self.api.agentServiceWithPlistName_(f"{label}.plist")

    def register(self, label):
        service = self._service(label)
        status = service.status()
        if status == SM_STATUS_REQUIRES_APPROVAL:
            raise ApprovalRequired()
        if status == SM_STATUS_ENABLED:
            return
        # Both notRegistered and notFound need registration on first install.
        ok, error = service.registerAndReturnError_(None)
        status = service.status()
        if status == SM_STATUS_REQUIRES_APPROVAL:
            raise ApprovalRequired()
        if not ok or status != SM_STATUS_ENABLED:
            raise RuntimeError(f"SMAppService register {label}: {error}")

    def unregister(self, label):
        service = self._service(label)
        if service.status() in (SM_STATUS_NOT_REGISTERED, SM_STATUS_NOT_FOUND):
            # No bundle-owned registration; a legacy launchd job may still exist.
            return
        ok, error = service.unregisterAndReturnError_(None)
        if not ok:
            raise RuntimeError(f"SMAppService unregister {label}: {error}")

    def open_settings(self):
        self.api.openSystemSettingsLoginItems()

    def check_approval(self):
        # BTM migration is retried by the tray worker, never by a synchronous caller.
        if any(self._service(label).status() == SM_STATUS_REQUIRES_APPROVAL for label in ld.LAUNCH_LABELS):
            raise ApprovalRequired()


def legacy_migration_remaining(support) -> float:
    path = support / "legacy-cleanup.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        timestamp = value.get("time") if isinstance(value, dict) else None
        if isinstance(timestamp, (int, float)) and not isinstance(timestamp, bool):
            age = time.time() - timestamp
            if 0 <= age < LEGACY_MIGRATION_SECONDS:
                return LEGACY_MIGRATION_SECONDS - age
    except (OSError, ValueError):
        pass
    path.unlink(missing_ok=True)
    return 0


def _record_legacy_cleanup(support):
    # Do not extend the migration window on retries or after reopening the app.
    if legacy_migration_remaining(support):
        return
    support.mkdir(parents=True, exist_ok=True)
    path = support / "legacy-cleanup.json"
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        os.chmod(temporary, 0o600)
        json.dump({"time": time.time()}, stream)
    temporary.replace(path)


def set_login_enabled(layout, enabled, runner=subprocess.run):
    # launchctl enable/disable changes future scheduling, not a running job.
    # Keep registration bundle-owned and do not unregister active calls.
    result = ld._run_launchctl(["enable" if enabled else "disable", f"{layout.gui_domain}/{ld.TRAY_LABEL}"],
                              runner)
    if result.returncode:
        raise RuntimeError(f"Cannot change CallPilot login scheduling: {result.stderr}")


def job_state(label, layout, runner=subprocess.run) -> str | None:
    result = ld._run_launchctl(["print", f"{layout.gui_domain}/{label}"], runner)
    if result.returncode:
        if f'Could not find service "{label}"' in (result.stderr or ""):
            return None
        raise RuntimeError(f"Cannot inspect {label}: {result.stderr}")
    return result.stdout or ""


def agent_running(label, layout, runner=subprocess.run) -> bool:
    """Registered idle agents are not an unknown running service. Fail closed."""
    state = job_state(label, layout, runner)
    if state is None:
        return False
    if re.search(r"^\s*pid = [1-9][0-9]*\s*$", state, re.MULTILINE):
        return True
    return not bool(re.search(r"^\s*state = (not running|waiting|exited)\s*$", state, re.MULTILINE))


def wait_unloaded(label, layout, runner=subprocess.run, *, sleep=time.sleep, timeout=10):
    deadline = time.monotonic() + timeout
    while True:
        state = ld._run_launchctl(["print", f"{layout.gui_domain}/{label}"], runner)
        if state.returncode:
            if f'Could not find service "{label}"' in (state.stderr or ""):
                return
            raise RuntimeError(f"Cannot confirm {label} stopped: {state.stderr}")
        if time.monotonic() >= deadline:
            raise RuntimeError(f"Previous service generation is still loaded: {label}")
        sleep(0.1)


def stop(label, layout, runner=subprocess.run, *, services=None, sleep=time.sleep):
    (services or NativeServices()).unregister(label)
    ld._run_launchctl(["bootout", f"{layout.gui_domain}/{label}"], runner)
    wait_unloaded(label, layout, runner, sleep=sleep)


def legacy_paths(layout, label):
    for directory in (layout.launch_agents_dir, layout.support_dir / "LaunchAgents"):
        path = directory / f"{label}.plist"
        if path.is_symlink():
            raise RuntimeError(f"Refusing legacy plist symlink: {path}")
        if path.exists():
            with path.open("rb") as stream:
                if plistlib.load(stream).get("Label") != label:
                    raise RuntimeError(f"Unexpected legacy plist label: {path}")
            yield path


def cleanup_legacy(layout, runner=subprocess.run, *, labels=None, sleep=time.sleep) -> bool:
    removed = False
    allowed = set(ld.LAUNCH_LABELS if labels is None else labels)
    if not allowed.issubset(ld.LAUNCH_LABELS):
        raise ValueError("Unknown CallPilot agent")
    for label in (ld.APP_LABEL, ld.BRIDGE_LABEL, ld.TRAY_LABEL):
        if label not in allowed:
            continue
        paths = list(legacy_paths(layout, label))
        if paths:
            ld._run_launchctl(["bootout", f"{layout.gui_domain}/{label}"], runner)
            wait_unloaded(label, layout, runner, sleep=sleep)
            for path in paths:
                path.unlink()
                removed = True
                logger.info("Removed legacy CallPilot agent %s: %s", label, path)
    return removed


def install(layout, runner=subprocess.run, *, services=None, sleep=time.sleep,
            force_restart_labels=frozenset(), no_restart_labels=frozenset(),
            before_restart=None, verify_bridge=None, user_initiated=False) -> ld.LaunchdInstallResult:
    services = services or NativeServices()
    changed: list[str] = []
    action, label = "prepare", ld.APP_LABEL
    # Must remain outside the error conversion: reconcile retries UpgradeBusy.
    if force_restart_labels and before_restart is not None:
        before_restart()
    try:
        # Disabled legacy caffeinate items can block the same bundle-owned label.
        # Only preclean idle jobs; running legacy services remain reconcile-owned.
        for label in (ld.APP_LABEL, ld.BRIDGE_LABEL):
            if list(legacy_paths(layout, label)) and not agent_running(label, layout, runner):
                action = "cleanup-legacy"
                if cleanup_legacy(layout, runner, labels={label}, sleep=sleep):
                    _record_legacy_cleanup(layout.support_dir)
        action = "check-approval"
        services.check_approval()
        for label in (ld.APP_LABEL, ld.BRIDGE_LABEL, ld.TRAY_LABEL):
            if label in force_restart_labels and label not in no_restart_labels:
                action = "unregister"
                stop(label, layout, runner, services=services, sleep=sleep)
        # Tray migration is done from an independent user-launched successor,
        # before registration; a launchd tray must never bootout itself.
        cleanup_legacy(layout, runner, labels={ld.APP_LABEL, ld.BRIDGE_LABEL}, sleep=sleep)
        if user_initiated:
            set_login_enabled(layout, True, runner)
        for label in ld.LAUNCH_LABELS:
            if job_state(label, layout, runner) is None:
                # SM may still remember a job removed by the normal quit path.
                action = "unregister"
                services.unregister(label)
                action = "register"
                services.register(label)
                changed.append(label)
            if label != ld.TRAY_LABEL:
                action = "kickstart"
                result = ld._run_launchctl(["kickstart", f"{layout.gui_domain}/{label}"], runner)
                if result.returncode:
                    raise RuntimeError(result.stderr or "Unable to start agent")
            if label == ld.BRIDGE_LABEL and verify_bridge is not None:
                action = "verify-version"
                if not verify_bridge():
                    raise RuntimeError("New bridge did not acquire its instance lock/version")
        (layout.support_dir / "legacy-cleanup.json").unlink(missing_ok=True)
        return ld.LaunchdInstallResult(changed, [], [])
    except (RuntimeError, OSError, ValueError) as exc:
        logger.exception("SMAppService installation failed")
        if isinstance(exc, ApprovalRequired):
            action = ("legacy-migration-pending" if legacy_migration_remaining(layout.support_dir)
                      else "approval-required")
        return ld.LaunchdInstallResult(changed, [ld.LaunchctlFailure(label, action, (), 1, stderr=str(exc))], [])
    finally:
        if not ld.autostart_enabled(layout):
            set_login_enabled(layout, False, runner)


def prepare_legacy_tray(layout, *, login, runner=subprocess.run) -> bool:
    """A legacy launchd-owned tray exits before its detached successor cleans it."""
    paths = list(legacy_paths(layout, ld.TRAY_LABEL))
    if not paths:
        return False
    import os

    if login or os.environ.get("XPC_SERVICE_NAME") == ld.TRAY_LABEL:
        bundle = ld.bundle_root_for_executable(layout.executable)
        runner(["/usr/bin/open", "-n", str(bundle), "--args", "--migrated-login"], check=True,
               capture_output=True, text=True, timeout=10)
        return True
    return False
