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
STABLE_RUNTIME_SECONDS = 10 * 60
SLOW_RETRY_SECONDS = 60
FAST_RETRY_DELAYS = (0, 1, 2, 4, 8, 16)
logger = logging.getLogger(__name__)


def _require_frozen() -> None:
    if not config._is_frozen():
        raise RuntimeError('Windows tray runtime requires a frozen executable; use app.py for source development')


def _report_portable_error(support: Path, exc: BaseException, role: str) -> None:
    """只读便携目录无法落数据时，给出托盘弹窗与日志，不静默改写到别处。"""
    message = (
        'CallPilot 便携数据目录不可写：\n'
        f'{support}\n\n'
        '请把 CallPilot 解压到可写目录（如桌面）后重试。'
    )
    logger.error('Portable data root is not writable (%s): %s', exc, support)
    if sys.platform == 'win32' and role != 'service':
        try:
            import ctypes

            ctypes.windll.user32.MessageBoxW(None, message, 'CallPilot', 0x10)  # MB_ICONERROR
        except Exception:
            logger.exception('Could not show portable-directory error dialog')
    stderr = getattr(sys, 'stderr', None)
    if stderr is not None:
        print(message, file=stderr)


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
    """Initial start plus five fast retries, followed by unlimited slow retries."""

    def __init__(self) -> None:
        self.attempts = 0

    def next_delay(self) -> float:
        delay = (FAST_RETRY_DELAYS[self.attempts]
                 if self.attempts < len(FAST_RETRY_DELAYS) else SLOW_RETRY_SECONDS)
        self.attempts += 1
        return delay

    @property
    def mode(self) -> str:
        return 'slow' if self.attempts > len(FAST_RETRY_DELAYS) else 'fast'


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
    _require_frozen()
    import pystray
    from PIL import Image

    from . import windows_autostart

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

    def restart_service(*_):
        restart.set()

    def autostart_enabled(_item=None):
        try:
            return windows_autostart.is_enabled()
        except windows_autostart.AutostartUnavailable:
            return False

    def toggle_autostart(_icon, _item):
        try:
            windows_autostart.set_enabled(not windows_autostart.is_enabled())
        except windows_autostart.AutostartUnavailable:
            logger.exception('Could not toggle autostart')

    def fix_autostart(_icon, _item):
        try:
            windows_autostart.set_enabled(True)
        except windows_autostart.AutostartUnavailable:
            logger.exception('Could not repair autostart')

    def quit_with_autostart_off(_icon, _item):
        try:
            windows_autostart.set_enabled(False)
        except windows_autostart.AutostartUnavailable:
            logger.exception('Could not disable autostart on quit')
        quit_tray()

    def stale_autostart_target():
        try:
            return windows_autostart.stale_run_target()
        except windows_autostart.AutostartUnavailable:
            return None

    def build_menu():
        items = [
            pystray.MenuItem('Open dashboard', open_window, default=True),
            pystray.MenuItem('Restart service', restart_service),
            pystray.MenuItem('开机自动启动', toggle_autostart, checked=autostart_enabled),
        ]
        if stale_autostart_target() is not None:
            items.append(pystray.MenuItem('开机自启指向旧路径，点击修正', fix_autostart))
        items.append(pystray.MenuItem('退出并关闭开机自启', quit_with_autostart_off))
        items.append(pystray.MenuItem('Quit CallPilot', quit_tray))
        return items

    def supervise():
        nonlocal service
        budget = RestartBudget()
        due: float | None = None
        started_at: float | None = None
        delay = 0.0
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
                due = None
                started_at = None
            now = time.monotonic()
            if service is None or service.poll() is not None:
                started_at = None
                if due is None:
                    delay = budget.next_delay()
                    due = now + delay
                if now >= due:
                    if stop.is_set() or (support / 'tray.stop').exists():
                        break
                    logger.info('Starting backend: attempt=%d delay=%g mode=%s',
                                budget.attempts, delay, budget.mode)
                    try:
                        service = subprocess.Popen([sys.executable, '--service'], cwd=support)
                        started_at = time.monotonic()
                    except OSError:
                        logger.exception('Could not start backend')
                    due = None
            elif started_at is not None and now - started_at >= STABLE_RUNTIME_SECONDS:
                logger.info('Stable backend runtime; resetting restart budget after %g seconds',
                            now - started_at)
                budget = RestartBudget()
                started_at = None
            online = tray.probe_online(tray.web_url(), timeout=0.5)
            if online:
                icon.title = 'CallPilot: running'
            elif budget.mode == 'slow':
                icon.title = 'CallPilot: service error, retrying'
            else:
                icon.title = 'CallPilot: starting / offline'
        for role in ('window', 'service'):
            (support / f'{role}.stop').touch()
        # Never release tray ownership while a child still holds installed files.
        for child in (window, service):
            if child is not None:
                child.wait()
        icon.stop()

    icon = pystray.Icon('CallPilot', Image.open(tray.icon_path(True)), 'CallPilot', menu=pystray.Menu(build_menu))

    def setup(icon):
        icon.visible = True
        stale = stale_autostart_target()
        if stale is not None:
            message = f'开机自启仍指向旧路径：\n{stale}\n请在托盘菜单中点「开机自启指向旧路径，点击修正」。'
            icon.notify(message, 'CallPilot')
        if '--login' not in sys.argv:
            open_window()
        supervise()

    icon.run(setup=setup)


def main() -> None:
    _require_frozen()
    if '--selftest' in sys.argv:
        from .windows_selftest import run

        raise SystemExit(run())
    role = 'service' if '--service' in sys.argv else 'window' if '--window' in sys.argv else 'tray'
    support = config.app_support_dir()
    if config.is_portable_mode():
        try:
            config.ensure_dir_writable(support, verify=True)
        except config.DataRootNotWritable as exc:
            _report_portable_error(support, exc, role)
            raise SystemExit(1) from exc
    else:
        support.mkdir(parents=True, exist_ok=True)
    if '--shutdown' in sys.argv:
        raise SystemExit(shutdown(support))
    resources = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parents[2]))
    seed_env(support, resources / '.env.example')
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
