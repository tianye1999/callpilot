"""Reconcile bundled services on launch without touching shared call code."""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
from pathlib import Path

from . import macos_launchd as launchd
from .app_version import VERSION

SERVICE_LABELS = {launchd.APP_LABEL, launchd.BRIDGE_LABEL}
BRIDGE_LOCK_PATH = Path("/tmp/ec20-usb-pty.lock")


class UpgradeBusy(Exception):
    pass


class LegacyUpgradeRequired(RuntimeError):
    pass


def _bridge_running(pid: object) -> bool:
    if sys.platform != "darwin":
        return False
    import fcntl

    try:
        with BRIDGE_LOCK_PATH.open("r") as lock:
            if str(pid) != lock.read().strip():
                return False
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass
    return False


def record_bridge_version(executable: str, support: Path) -> None:
    support.mkdir(parents=True, exist_ok=True)
    path = support / "bridge-version.json"
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps({"version": VERSION, "executable": str(Path(executable).resolve()),
                                     "pid": os.getpid()}),
                         encoding="utf-8")
    temporary.replace(path)


def bridge_current(layout: launchd.LaunchdLayout) -> bool:
    try:
        bridge = json.loads((layout.support_dir / "bridge-version.json").read_text(encoding="utf-8"))
        return (bridge.get("version") == VERSION and bridge.get("executable") == str(layout.executable)
                and _bridge_running(bridge.get("pid")))
    except (OSError, ValueError, AttributeError):
        return False


def _needs_restart(layout: launchd.LaunchdLayout, meta: dict) -> bool:
    if meta and (meta.get("version") != VERSION or meta.get("executable") != str(layout.executable)):
        return True
    if not bridge_current(layout):
        return True
    directory = (layout.launch_agents_dir if launchd.autostart_enabled(layout)
                 else layout.support_dir / "LaunchAgents")
    return any(launchd.plist_needs_update(directory / f"{label}.plist", data)
               for label, data in launchd.build_plists(layout).items() if label in SERVICE_LABELS)


def _lease_request(url: str, operation: str, data: dict) -> dict:
    request = urllib.request.Request(f"{url.rstrip('/')}/api/updates/{operation}",
                                     data=json.dumps(data).encode(), headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=2) as response:
            result = json.loads(response.read(4096))
            return result if isinstance(result, dict) else {}
    except (OSError, ValueError):
        return {}


def acquire_lease(url: str):
    token = _lease_request(url, "prepare", {}).get("token")
    if not isinstance(token, str) or not token:
        return None
    return lambda: _lease_request(url, "resume", {"token": token})


def reconcile(layout, fetch_meta, *, user_initiated=False, install=None, loaded=None,
              sleep=time.sleep, acquire=None):
    import fcntl

    install = install or launchd.install_launch_agents
    loaded = loaded or launchd.agent_loaded
    layout.support_dir.mkdir(parents=True, exist_ok=True)
    # Double-clicks and an existing tray can both reconcile; only one may replace services.
    with (layout.support_dir / "upgrade.lock").open("a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        while True:
            meta = fetch_meta()
            if (meta and (meta.get("call_active") is not False or meta.get("dial_queue_busy"))
                    or not meta and loaded(launchd.APP_LABEL, layout)):
                sleep(1)
                continue
            restart = SERVICE_LABELS if _needs_restart(layout, meta) else set()
            if restart and meta and not meta.get("upgrade_guard"):
                raise LegacyUpgradeRequired(
                    "旧版服务不支持安全切换。请在通话结束后退出 CallPilot，再重新打开一次。\n"
                    "After the call ends, quit CallPilot and reopen it once to finish this legacy upgrade.")
            release = None

            def before_restart():
                nonlocal release
                if meta.get("upgrade_guard"):
                    release = acquire() if acquire else None
                    if release is None:
                        raise UpgradeBusy()

            def verify_bridge():
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    if bridge_current(layout):
                        return True
                    sleep(0.2)
                return False
            try:
                return install(layout, no_restart_labels={launchd.TRAY_LABEL},
                               force_restart_labels=restart, user_initiated=user_initiated,
                               before_restart=before_restart, verify_bridge=verify_bridge)
            except UpgradeBusy:
                sleep(1)
            finally:
                if release is not None:
                    release()
