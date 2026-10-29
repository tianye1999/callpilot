"""tray 通知使用假发送器和假 UserNotifications，不调用系统。"""

import sys
from types import SimpleNamespace

import pytest

from agentcall import macos_notification_sender as notifications
from agentcall.notification_events import SETTING_KEYS, NotificationFeed


class FakeSender:
    def __init__(self):
        self.sent = []
        self.removed = []
        self.reconciled = []

    def send(self, identifier, event):
        self.sent.append((identifier, event))

    def remove(self, identifiers):
        self.removed.extend(identifiers)

    def reconcile(self, identifiers):
        self.reconciled.append(set(identifiers))


def controller():
    sender = FakeSender()
    return notifications.TrayNotifications(sender), sender


def test_bootstrap_skips_old_info_and_restart_resets_cursor():
    tray, sender = controller()
    feed = NotificationFeed()
    feed.info("sms", "Title", "old", "#sms")
    tray.consume(feed.read(0))
    assert sender.sent == []
    feed.info("sms", "Title", "new", "#sms")
    tray.consume(feed.read(tray.seq))
    assert len(sender.sent) == 1
    restarted = NotificationFeed()
    tray.consume(restarted.read(tray.seq))
    assert tray.seq == 0
    restarted.info("sms", "Title", "after restart", "#sms")
    tray.consume(restarted.read(tray.seq))
    assert len(sender.sent) == 2


@pytest.mark.parametrize("key", SETTING_KEYS)
def test_master_and_tier_switches(monkeypatch, key):
    tray, sender = controller()
    feed = NotificationFeed()
    tray.consume(feed.read(0))
    monkeypatch.setenv(key, "false")
    feed.confirm_requested("abc", "Proceed?", 60)
    feed.info("sms", "Title", "text", "#sms")
    tray.consume(feed.read(0))
    tray.status("attention", "No SIM")
    tiers = [event["tier"] for _, event in sender.sent]
    if key == "MAC_NOTIFICATIONS_ENABLED":
        assert tiers == []
    else:
        assert key.removeprefix("MAC_NOTIFY_").lower() not in tiers
        assert len(tiers) == 2


def test_red_reason_once_until_recovered():
    tray, sender = controller()
    for state, reason in [("attention", "No SIM"), ("attention", "No SIM"),
                          ("attention", "No modem"), ("attention", "No SIM"),
                          ("transient", "Connecting"), ("attention", "No SIM")]:
        tray.status(state, reason)
    assert len(sender.sent) == 2
    tray.status("ready", "Ready")
    tray.status("attention", "No SIM")
    assert len(sender.sent) == 3


def test_confirmation_closed_and_offline_expiry_remove():
    tray, sender = controller()
    feed = NotificationFeed()
    feed.confirm_requested("abc", "Proceed?", 60)
    tray.consume(feed.read(0))
    assert sender.sent[0][0] == "callpilot.confirm.abc"
    feed.confirm_closed("abc")
    tray.consume(feed.read(tray.seq))
    assert "callpilot.confirm.abc" in sender.removed
    feed.confirm_requested("def", "Proceed?", 60)
    tray.consume(feed.read(tray.seq))
    tray.expire(float("inf"))
    assert "callpilot.confirm.def" in sender.removed


def test_closed_before_poll_is_not_delivered_and_disable_removes(monkeypatch):
    tray, sender = controller()
    feed = NotificationFeed()
    tray.consume(feed.read(0))
    feed.confirm_requested("abc", "Proceed?", 60)
    feed.confirm_closed("abc")
    tray.consume(feed.read(tray.seq))
    assert sender.sent == []
    feed.confirm_requested("def", "Proceed?", 60)
    tray.consume(feed.read(tray.seq))
    monkeypatch.setenv("MAC_NOTIFY_CONFIRM", "false")
    tray.consume(feed.read(tray.seq))
    assert "callpilot.confirm.def" in sender.removed


def fake_native(monkeypatch, status=2, defer=False):
    class Content:
        @classmethod
        def alloc(cls):
            return cls()

        def init(self):
            return self

        def __getattr__(self, name):
            return lambda value: setattr(self, name, value)

    sent, removed, callbacks = [], [], []
    center = SimpleNamespace(
        getNotificationSettingsWithCompletionHandler_=lambda cb: callbacks.append(cb) if defer else cb(
            SimpleNamespace(authorizationStatus=lambda: status)),
        addNotificationRequest_withCompletionHandler_=lambda request, cb: (sent.append(request), cb(None)),
        removeDeliveredNotificationsWithIdentifiers_=lambda ids: removed.extend(ids),
        removePendingNotificationRequestsWithIdentifiers_=lambda ids: None,
    )
    monkeypatch.setitem(sys.modules, "UserNotifications", SimpleNamespace(
        UNMutableNotificationContent=Content,
        UNNotificationRequest=SimpleNamespace(
            requestWithIdentifier_content_trigger_=lambda identifier, content, trigger: (identifier, content)),
        UNNotificationSound=SimpleNamespace(defaultSound=lambda: "sound"),
        UNNotificationInterruptionLevelActive=1,
    ))
    return center, sent, removed, callbacks


@pytest.mark.parametrize("status", [0, 1])
def test_native_unauthorized_never_sends_or_requests(monkeypatch, status):
    center, sent, _, _ = fake_native(monkeypatch, status)
    notifications.NativeSender(center).send("id", dict(tier="confirm", title="Title", body="Body", target="#sms"))
    assert sent == []


def test_native_confirm_has_sound_active_no_actions_and_cancel_race(monkeypatch):
    center, sent, removed, callbacks = fake_native(monkeypatch, defer=True)
    sender = notifications.NativeSender(center)
    event = dict(tier="confirm", title="Title", body="Body", target="#confirm/abc")
    sender.send("id", event)
    sender.remove(["id"])
    callbacks.pop()(SimpleNamespace(authorizationStatus=lambda: 2))
    assert sent == []
    sender.send("next", event)
    callbacks.pop()(SimpleNamespace(authorizationStatus=lambda: 2))
    content = sent[0][1]
    assert content.setSound_ == "sound"
    assert content.setInterruptionLevel_ == 1
    assert content.setUserInfo_ == {"target": "#confirm/abc", "tier": "confirm"}
    assert "setCategoryIdentifier_" not in content.__dict__
    assert removed == ["id"]


def test_nonbundled_sender_does_not_import_native(monkeypatch):
    monkeypatch.setattr(notifications, "available", lambda: False)
    monkeypatch.setitem(sys.modules, "UserNotifications", None)
    assert notifications.make_native_sender(lambda _: None) is None


def test_old_add_completion_cannot_cancel_new_same_identifier(monkeypatch):
    center, sent, removed, callbacks = fake_native(monkeypatch, defer=True)
    completions = []
    center.addNotificationRequest_withCompletionHandler_ = lambda request, cb: (
        sent.append(request), completions.append(cb))
    sender = notifications.NativeSender(center)
    event = dict(tier="alert", title="Title", body="Body", target="#settings")
    sender.send("alert", event)
    callbacks.pop(0)(SimpleNamespace(authorizationStatus=lambda: 2))
    sender.send("alert", event)
    completions.pop(0)(None)
    callbacks.pop(0)(SimpleNamespace(authorizationStatus=lambda: 2))
    completions.pop(0)(None)
    assert len(sent) == 2 and removed == []


def test_cancel_during_add_cleans_late_delivery(monkeypatch):
    center, sent, removed, _ = fake_native(monkeypatch)
    completions = []
    center.addNotificationRequest_withCompletionHandler_ = lambda request, cb: (
        sent.append(request), completions.append(cb))
    sender = notifications.NativeSender(center)
    sender.send("confirm", dict(tier="confirm", title="Title", body="Body", target="#confirm/abc"))
    sender.remove(["confirm"])
    completions.pop()(None)
    assert removed == ["confirm", "confirm"]


def test_disable_while_authorization_query_pending_prevents_send(monkeypatch):
    center, sent, _, callbacks = fake_native(monkeypatch, defer=True)
    sender = notifications.NativeSender(center)
    tray = notifications.TrayNotifications(sender)
    sender.send("info", dict(tier="info", title="Title", body="Body", target="#sms"))
    tray.settings["MAC_NOTIFY_INFO"] = False
    callbacks.pop()(SimpleNamespace(authorizationStatus=lambda: 2))
    assert sent == []


def test_native_reconcile_only_removes_stale_confirmations(monkeypatch):
    center, _, removed, _ = fake_native(monkeypatch)
    delivered = [SimpleNamespace(request=lambda identifier=identifier: SimpleNamespace(identifier=lambda: identifier))
                 for identifier in ["callpilot.confirm.old", "callpilot.confirm.new", "callpilot.info"]]
    center.getDeliveredNotificationsWithCompletionHandler_ = lambda callback: callback(delivered)
    notifications.NativeSender(center).reconcile({"callpilot.confirm.new"})
    assert removed == ["callpilot.confirm.old"]


def test_delegate_click_routes_and_foreground_sound_with_fakes(monkeypatch):
    class NSObject:
        def __init_subclass__(cls, **kwargs):
            pass

    monkeypatch.setitem(sys.modules, "Foundation", SimpleNamespace(NSObject=NSObject))
    monkeypatch.setitem(sys.modules, "objc", SimpleNamespace(protocolNamed=lambda name: name))
    monkeypatch.setitem(sys.modules, "UserNotifications", SimpleNamespace(
        UNNotificationDefaultActionIdentifier="default", UNNotificationPresentationOptionBanner=1,
        UNNotificationPresentationOptionList=2, UNNotificationPresentationOptionSound=4,
    ))
    notifications._delegate_class.cache_clear()
    try:
        delegate = notifications._delegate_class()()
        clicked, completed, options = [], [], []
        delegate.on_click = clicked.append
        for target in ["#confirm/abc", "#history/call_abc", "#sms", "https://evil.example"]:
            content = SimpleNamespace(userInfo=lambda: {"target": target, "tier": "confirm"})
            notification = SimpleNamespace(request=lambda: SimpleNamespace(content=lambda: content))
            response = SimpleNamespace(notification=lambda: notification, actionIdentifier=lambda: "default")
            delegate.userNotificationCenter_didReceiveNotificationResponse_withCompletionHandler_(
                None, response, lambda: completed.append(True))
            delegate.userNotificationCenter_willPresentNotification_withCompletionHandler_(
                None, notification, options.append)
        assert clicked == ["#confirm/abc", "#history/call_abc", "#sms"]
        assert completed == [True] * 4
        assert options == [7] * 4
    finally:
        notifications._delegate_class.cache_clear()


def test_update_replays_first_poll_and_defers_until_native_callback_is_idle(monkeypatch):
    center, sent, _, callbacks = fake_native(monkeypatch, defer=True)
    center.getDeliveredNotificationsWithCompletionHandler_ = lambda callback: callback([])
    busy, claimed = [False], []
    sender = notifications.NativeSender(center)
    tray = notifications.TrayNotifications(sender, claim_update=lambda version: (claimed.append(version), True)[1],
                                            busy=lambda: busy[0])
    feed = NotificationFeed()
    feed.info("update:26.11.1", "New version", "Notes", "#settings")
    tray.consume(feed.read(0))
    busy[0] = True
    callbacks.pop()(SimpleNamespace(authorizationStatus=lambda: 2))
    assert not sent and not claimed
    busy[0] = False
    tray.consume(feed.read(tray.seq))
    callbacks.pop()(SimpleNamespace(authorizationStatus=lambda: 2))
    assert claimed == ["26.11.1"] and len(sent) == 1
    assert sent[0][0] == "callpilot.update.26.11.1"
    tray.consume(feed.read(tray.seq))
    assert not callbacks


def test_denied_notification_does_not_claim_update(monkeypatch):
    center, sent, _, _ = fake_native(monkeypatch, status=1)
    claimed = []
    sender = notifications.NativeSender(center)
    sender.send("update", dict(tier="info", title="Update", body="Notes", target="#settings",
                               claim=lambda: claimed.append(True)))
    assert not sent and not claimed
