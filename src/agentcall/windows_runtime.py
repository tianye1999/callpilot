"""Windows roles for the shared tray executable; no imports with device side effects."""

from __future__ import annotations

import importlib
import logging
import logging.handlers
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import config

ROLES = ('tray', 'service', 'window')
logger = logging.getLogger(__name__)


def seed_env(support: Path, template: Path) -> None:
    from dotenv import unset_key

    support.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + 10
    lock = acquire_lock(support, 'config')
    while lock is None:
        if time.monotonic() >= deadline:
            raise TimeoutError('Timed out preparing user configuration')
        time.sleep(0.05)
        lock = acquire_lock(support, 'config')
    try:
        target = support / '.env'
        if target.exists():
            return
        content = template.read_text(encoding='utf-8')
        with target.open('x', encoding='utf-8') as dest:
            dest.write(content)
        # Shared template has Mac device paths. Omission preserves platform/vendor
        # defaults, including automatic PCM pairing if the owner selects SIMCom.
        for key in ('MODEM_PORT', 'MODEM_AUDIO_MODE', 'MODEM_PCM_PORT'):
            unset_key(target, key)
    finally:
        lock.close()


def acquire_lock(support: Path, role: str):
    handle = (support / f'{role}.lock').open('a+b')
    handle.seek(0)
    try:
        if sys.platform == 'win32':
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def shutdown(support: Path, timeout: float = 45) -> int:
    deadline = time.monotonic() + timeout
    for role in ROLES:
        (support / f'{role}.stop').touch()
    # Check all locks together: tray must release only after its children exit.
    while True:
        locks = [acquire_lock(support, role) for role in ROLES]
        stopped = all(lock is not None for lock in locks)
        for lock in locks:
            if lock is not None:
                lock.close()
        if stopped:
            return 0
        if time.monotonic() >= deadline:
            return 1
        time.sleep(0.2)


class RestartBudget:
    """Initial start plus five retries; manual restart replenishes the budget."""

    def __init__(self) -> None:
        self.attempts = 0

    def next_delay(self) -> float | None:
        if self.attempts >= 6:
            return None
        delay = 0 if self.attempts == 0 else 2 ** (self.attempts - 1)
        self.attempts += 1
        return delay


def run_service(support: Path, *, smoke: bool = False) -> None:
    app = importlib.import_module('app')

    app.main(stop_requested=lambda: (support / 'service.stop').exists(), hardware_selftest=smoke)


def run_window(support: Path) -> None:
    desktop = importlib.import_module('desktop_app')
    tray = importlib.import_module('tray_app')

    webview = desktop._import_webview()
    window = webview.create_window('CallPilot', html=desktop.startup_html(), width=1100, height=780)

    def watch():
        loaded = False
        while not closed.wait(0.3):
            if (support / 'window.stop').exists():
                window.destroy()
                return
            if (support / 'window.open').exists():
                (support / 'window.open').unlink(missing_ok=True)
                window.restore()
                window.show()
            if not loaded and tray.probe_online(tray.web_url(), timeout=0.5):
                window.load_url(tray.web_url())
                loaded = True

    closed = threading.Event()
    window.events.closed += lambda: closed.set()
    webview.start(watch)


def run_tray(support: Path) -> None:
    import pystray
    from PIL import Image

    tray = importlib.import_module('tray_app')

    stop = threading.Event()
    restart = threading.Event()
    service: subprocess.Popen | None = None
    window: subprocess.Popen | None = None

    def open_window(*_):
        nonlocal window
        (support / 'window.open').touch()
        if window is None or window.poll() is not None:
            window = subprocess.Popen(tray.dashboard_command(), cwd=support)

    def quit_tray(*_):
        stop.set()

    def supervise():
        nonlocal service
        budget = RestartBudget()
        due = time.monotonic()
        while not stop.wait(0.25):
            if (support / 'tray.stop').exists():
                stop.set()
                break
            if (support / 'tray.open').exists():
                (support / 'tray.open').unlink(missing_ok=True)
                open_window()
            if restart.is_set():
                (support / 'service.stop').touch()
                if service is not None and service.poll() is None:
                    continue
                restart.clear()
                budget = RestartBudget()
                due = time.monotonic()
            if service is None or service.poll() is not None:
                if time.monotonic() >= due:
                    delay = budget.next_delay()
                    if delay is None:
                        icon.title = 'CallPilot: service stopped (retry limit)'
                        continue
                    if stop.wait(delay) or (support / 'tray.stop').exists():
                        break
                    try:
                        service = subprocess.Popen([sys.executable, '--service'], cwd=support)
                    except OSError:
                        logger.exception('Could not start backend')
                    due = time.monotonic() + 1
            online = tray.probe_online(tray.web_url(), timeout=0.5)
            icon.title = 'CallPilot: running' if online else 'CallPilot: starting / offline'
        for role in ('window', 'service'):
            (support / f'{role}.stop').touch()
        # Never release tray ownership while a child still holds installed files.
        for child in (window, service):
            if child is not None:
                child.wait()
        icon.stop()

    icon = pystray.Icon('CallPilot', Image.open(tray.icon_path(True)), 'CallPilot', menu=pystray.Menu(
        pystray.MenuItem('Open dashboard', open_window, default=True),
        pystray.MenuItem('Restart service', lambda *_: restart.set()),
        pystray.MenuItem('Quit CallPilot', quit_tray),
    ))

    def setup(icon):
        icon.visible = True
        if '--login' not in sys.argv:
            open_window()
        supervise()

    icon.run(setup=setup)


def main() -> None:
    if '--selftest' in sys.argv:
        from .windows_selftest import run

        raise SystemExit(run())
    support = config.app_support_dir()
    support.mkdir(parents=True, exist_ok=True)
    if '--shutdown' in sys.argv:
        raise SystemExit(shutdown(support))
    resources = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parents[2]))
    seed_env(support, resources / '.env.example')
    role = 'service' if '--service' in sys.argv else 'window' if '--window' in sys.argv else 'tray'
    logs = config.log_dir()
    logs.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, handlers=[logging.handlers.RotatingFileHandler(
        logs / f'{role}.log', maxBytes=2 * 1024 * 1024, backupCount=2, encoding='utf-8',
    )])
    lock = acquire_lock(support, role)
    if lock is None:
        if role != 'service':
            (support / f'{role}.open').touch()
        return
    try:
        (support / f'{role}.stop').unlink(missing_ok=True)
        os.chdir(support)
        if role == 'service':
            run_service(support, smoke='--hardware-selftest' in sys.argv)
        elif role == 'window':
            run_window(support)
        else:
            run_tray(support)
    except Exception:
        logger.exception('Windows %s failed', role)
        raise
    finally:
        lock.close()
