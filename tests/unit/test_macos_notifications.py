"""不访问真实通知中心，不产生授权弹窗。"""

import sys
from types import SimpleNamespace

import pytest

from agentcall import macos_notifications as notifications


def test_notification_bridge_queries_without_requesting(monkeypatch):
    calls = []
    center = SimpleNamespace(
        getNotificationSettingsWithCompletionHandler_=lambda callback: callback(
            SimpleNamespace(authorizationStatus=lambda: 2)),
        requestAuthorizationWithOptions_completionHandler_=lambda options, callback: (
            calls.append(options), callback(True, None)),
    )
    monkeypatch.setattr(notifications, "available", lambda: True)
    monkeypatch.setitem(sys.modules, "UserNotifications", SimpleNamespace(
        UNUserNotificationCenter=SimpleNamespace(currentNotificationCenter=lambda: center),
        UNAuthorizationOptionAlert=1, UNAuthorizationOptionBadge=2, UNAuthorizationOptionSound=4,
    ))
    bridge = notifications.NotificationBridge(lambda: "http://127.0.0.1:47100/", "http://127.0.0.1:47100")
    assert bridge.notification_status() == {"available": True, "status": "allowed"}
    assert calls == []
    assert bridge.request_notifications()["status"] == "allowed"
    assert calls == [7]


def test_notification_bridge_rejects_external_page_and_browser(monkeypatch):
    monkeypatch.setattr(notifications, "available", lambda: True)
    bridge = notifications.NotificationBridge(lambda: "https://platform.deepseek.com/", "http://127.0.0.1:47100")
    assert bridge.request_notifications() == {"available": False, "status": "unknown"}
    monkeypatch.setattr(notifications, "available", lambda: False)
    assert bridge.notification_status() == {"available": False}


@pytest.mark.parametrize("authorization,expected", [(0, "not_determined"), (1, "denied"), (2, "allowed")])
def test_notification_status_comes_from_native_center(monkeypatch, authorization, expected):
    monkeypatch.setattr(notifications, "available", lambda: True)
    center = SimpleNamespace(getNotificationSettingsWithCompletionHandler_=lambda callback: callback(
        SimpleNamespace(authorizationStatus=lambda: authorization)))
    monkeypatch.setitem(sys.modules, "UserNotifications", SimpleNamespace(
        UNUserNotificationCenter=SimpleNamespace(currentNotificationCenter=lambda: center)))
    bridge = notifications.NotificationBridge(lambda: "http://localhost:47100/", "http://localhost:47100")
    assert bridge.notification_status()["status"] == expected
