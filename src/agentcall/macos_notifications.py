"""窗口进程的通知授权桥；导入和构造均不触发系统权限请求。"""

from __future__ import annotations

import platform
import sys
import threading
from typing import Callable
from urllib.parse import urlsplit


def available() -> bool:
    if sys.platform != "darwin" or not getattr(sys, "frozen", False):
        return False
    try:
        from Foundation import NSBundle

        return (int(platform.mac_ver()[0].split(".")[0]) >= 13
                and NSBundle.mainBundle().bundleIdentifier() == "ai.bondings.callpilot")
    except (ImportError, ValueError):
        return False


class NotificationBridge:
    def __init__(self, current_url: Callable[[], str | None], trusted_url: str):
        self._current_url = current_url
        self._origin = urlsplit(trusted_url)
        self._request_lock = threading.Lock()

    def _allowed(self) -> bool:
        current = urlsplit(self._current_url() or "")
        return (available() and self._origin.hostname in {"127.0.0.1", "localhost", "::1"}
                and (current.scheme, current.netloc) == (self._origin.scheme, self._origin.netloc))

    def notification_status(self) -> dict:
        if not self._allowed():
            return {"available": False}
        from UserNotifications import UNUserNotificationCenter

        done = threading.Event()
        result = {"available": True, "status": "unknown"}

        def received(settings):
            status = int(settings.authorizationStatus())
            result["status"] = "allowed" if status in {2, 3, 4} else "denied" if status == 1 else "not_determined"
            done.set()

        UNUserNotificationCenter.currentNotificationCenter().getNotificationSettingsWithCompletionHandler_(received)
        done.wait(5)
        return result

    def request_notifications(self) -> dict:
        # pywebview 在工作线程调用；异步 completion 不阻塞 Cocoa 主线程。
        if not self._allowed() or not self._request_lock.acquire(blocking=False):
            return {"available": self._allowed(), "status": "unknown"}
        try:
            from UserNotifications import (
                UNAuthorizationOptionAlert,
                UNAuthorizationOptionBadge,
                UNAuthorizationOptionSound,
                UNUserNotificationCenter,
            )

            done = threading.Event()
            UNUserNotificationCenter.currentNotificationCenter().requestAuthorizationWithOptions_completionHandler_(
                UNAuthorizationOptionAlert | UNAuthorizationOptionBadge | UNAuthorizationOptionSound,
                lambda granted, error: done.set(),
            )
            done.wait(60)
            return self.notification_status()
        finally:
            self._request_lock.release()

    def open_notification_settings(self) -> dict:
        if not self._allowed():
            return {"available": False}
        from AppKit import NSWorkspace
        from Foundation import NSURL

        # 系统未提供公开的单 App 设置跳转 API，打开通知设置后由用户选择 CallPilot。
        opened = NSWorkspace.sharedWorkspace().openURL_(
            NSURL.URLWithString_("x-apple.systempreferences:com.apple.Notifications-Settings.extension")
        )
        return {"ok": bool(opened)}
