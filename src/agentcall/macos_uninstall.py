"""Independent Launch Services instance owns uninstall; never the tray's job."""

from __future__ import annotations

import logging
import plistlib
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import macos_launchd as ld
from . import macos_service_management as sm

logger = logging.getLogger(__name__)


def launch(layout, runner=subprocess.run):
    bundle = ld.bundle_root_for_executable(layout.executable)
    if bundle is None:
        raise RuntimeError("Uninstall requires the installed CallPilot.app")
    runner(["/usr/bin/open", "-n", str(bundle), "--args", "--uninstall-worker"],
           check=True, capture_output=True, text=True, timeout=10)


def validate_data_directory(layout):
    expected = Path.home() / "Library/Application Support/CallPilot"
    if layout.support_dir != expected or any(p.is_symlink() for p in (expected, *expected.parents)):
        raise RuntimeError("Refusing to delete a redirected CallPilot data directory")


def execute(layout, *, delete_data, stop, close_shell, cleanup, trash, delete):
    bundle = ld.bundle_root_for_executable(layout.executable)
    if bundle is None:
        raise RuntimeError("Uninstall requires CallPilot.app")
    for label in (ld.APP_LABEL, ld.BRIDGE_LABEL):
        stop(label)
    shell_lock = close_shell()
    try:
        stop(ld.TRAY_LABEL)
        cleanup()
        trash(bundle)
        if delete_data is True:
            delete()
    finally:
        if shell_lock is not None:
            shell_lock.close()


def _trash(bundle):
    from Foundation import NSURL, NSFileManager

    ok, _, error = NSFileManager.defaultManager().trashItemAtURL_resultingItemURL_error_(
        NSURL.fileURLWithPath_(str(bundle)), None, None)
    if not ok:
        raise RuntimeError(f"Could not move CallPilot to Trash: {error}")


def _prepare_data_delete(layout):
    import keyring
    from keyring.errors import PasswordDeleteError

    from .cloud_credentials import _ACCOUNT, _KEY_ACCOUNT, _SERVICE

    validate_data_directory(layout)
    backend = keyring.get_keyring()
    # Load the backend/native dependencies while the frozen bundle still exists.
    accounts = [account for account in (_ACCOUNT, _KEY_ACCOUNT) if backend.get_password(_SERVICE, account)]

    def delete():
        validate_data_directory(layout)
        for account in accounts:
            try:
                backend.delete_password(_SERVICE, account)
            except PasswordDeleteError:
                pass
        shutil.rmtree(layout.support_dir)
    return delete


def _close_shell(layout, close_window, acquire_tray_lock):
    from .macos_shell import send_command

    close_window(layout.support_dir)
    send_command("replace", layout.support_dir)
    deadline = time.monotonic() + 10
    while True:
        lock = acquire_tray_lock()
        if lock is not None:
            return lock
        if time.monotonic() >= deadline:
            raise RuntimeError("CallPilot menu bar has not exited; please retry")
        time.sleep(0.1)


def _pause_shell(layout, acquire_tray_lock):
    from .macos_shell import send_command

    ready = layout.support_dir / "uninstall-ready"
    ready.unlink(missing_ok=True)
    send_command("pause", layout.support_dir)
    deadline = time.monotonic() + 5
    while not ready.exists():
        lock = acquire_tray_lock()
        if lock is not None:
            lock.close()
            return
        if time.monotonic() >= deadline:
            raise RuntimeError("CallPilot is starting or busy; please retry later")
        time.sleep(0.1)


def worker_main(url, close_window, acquire_tray_lock) -> None:
    if sys.platform != "darwin":
        raise RuntimeError("Uninstall is only available on macOS")
    import fcntl

    layout = ld.make_layout(sys.executable)
    layout.support_dir.mkdir(parents=True, exist_ok=True)
    with (layout.support_dir / "uninstall.lock").open("a") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return  # Another confirmed worker owns the pause/resume handshake.
        _worker_main(url, close_window, acquire_tray_lock)


def _worker_main(url, close_window, acquire_tray_lock) -> None:
    if sys.platform != "darwin":
        raise RuntimeError("Uninstall is only available on macOS")
    import fcntl

    from AppKit import NSAlert, NSApplication, NSButton, NSButtonTypeSwitch

    from . import macos_shell, macos_upgrade

    layout = ld.make_layout(sys.executable)
    macos_shell.configure_shell_logging("uninstall", layout.log_dir)
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(0)
    app.activateIgnoringOtherApps_(True)
    lang = macos_shell.ui_language()

    def text(zh, en):
        return macos_shell.ui_text(zh, en, lang)

    prompt = NSAlert.alloc().init()
    prompt.setMessageText_(text("卸载 CallPilot？", "Uninstall CallPilot?"))
    prompt.setInformativeText_(text(
        "将停止接打电话，并把 CallPilot 移到废纸篓。通话或队列忙碌时不会卸载。",
        "CallPilot will stop taking calls and move to Trash. Uninstall is blocked during calls."))
    # NSAlert 首按钮默认绑定回车；破坏性操作必须显式点击，Esc 始终安全取消。
    prompt.addButtonWithTitle_(text("卸载", "Uninstall")).setKeyEquivalent_("")
    prompt.addButtonWithTitle_(text("取消", "Cancel")).setKeyEquivalent_("\x1b")
    checkbox = NSButton.alloc().initWithFrame_(((0, 0), (500, 40)))
    checkbox.setButtonType_(NSButtonTypeSwitch)
    checkbox.setTitle_(text("同时删除通话录音、短信和设置（不可恢复）",
                           "Also delete recordings, messages and settings (irreversible)"))
    checkbox.setState_(0)
    prompt.setAccessoryView_(checkbox)
    if prompt.runModal() != 1000:
        return
    delete_data = checkbox.state() == 1
    release = None
    try:
        bundle = ld.bundle_root_for_executable(layout.executable)
        if bundle is None or bundle.is_symlink():
            raise RuntimeError("Invalid CallPilot bundle")
        with (bundle / "Contents/Info.plist").open("rb") as stream:
            if plistlib.load(stream).get("CFBundleIdentifier") != "ai.bondings.callpilot":
                raise RuntimeError("Not a CallPilot bundle")
        if delete_data:
            validate_data_directory(layout)
        _pause_shell(layout, acquire_tray_lock)
        # Same lock order as WP6: upgrade -> startup. Never race a reconciler.
        with (layout.support_dir / "upgrade.lock").open("a") as upgrade_lock, \
                (layout.support_dir / "startup.lock").open("a") as startup_lock:
            fcntl.flock(upgrade_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(startup_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            meta = macos_shell.fetch_meta(url)
            if meta:
                if meta.get("call_active") is not False or meta.get("dial_queue_busy") or not meta.get("upgrade_guard"):
                    raise RuntimeError(text("请在通话和队列结束后重试。", "Wait for calls and the queue to finish."))
                release = macos_upgrade.acquire_lease(url)
                if release is None:
                    raise RuntimeError(text("服务忙碌，请稍后重试。", "Service is busy; please retry."))
            else:
                if sm.agent_running(ld.APP_LABEL, layout):
                    raise RuntimeError("Cannot confirm call state; please retry")
            services = sm.NativeServices()
            delete = _prepare_data_delete(layout) if delete_data else lambda: None
            execute(layout, delete_data=delete_data,
                    stop=lambda label: sm.stop(label, layout, services=services),
                    close_shell=lambda: _close_shell(layout, close_window, acquire_tray_lock),
                    cleanup=lambda: sm.cleanup_legacy(layout),
                    trash=_trash, delete=delete)
            logger.info("CallPilot moved to Trash; delete_data=%s", delete_data)
    except Exception as exc:  # noqa: BLE001
        logger.exception("CallPilot uninstall incomplete")
        alert = NSAlert.alloc().init()
        alert.setMessageText_(text("卸载未完成", "Uninstall incomplete"))
        alert.setInformativeText_(str(exc))
        alert.runModal()
    finally:
        if release is not None:
            release()
        # Success removed the app; do not recreate a deleted support directory.
        if layout.executable.exists():
            macos_shell.send_command("resume", layout.support_dir)
            (layout.support_dir / "uninstall-ready").unlink(missing_ok=True)
