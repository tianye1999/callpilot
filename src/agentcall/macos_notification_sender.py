"""菜单栏的通知策略及 UserNotifications 适配；不申请授权。"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.request
from functools import lru_cache
from typing import Callable
from urllib.parse import urlencode

from .macos_notifications import available
from .notification_events import SETTING_KEYS, valid_target

logger = logging.getLogger(__name__)
CONFIRM_PREFIX = "callpilot.confirm."


def fetch_feed(url: str, after: int, epoch: str | None = None) -> dict | None:
    try:
        query = urlencode({"after": after, "epoch": epoch or ""})
        with urllib.request.urlopen(f"{url.rstrip('/')}/api/notify/feed?{query}", timeout=0.5) as response:
            data = json.loads(response.read(1024 * 1024))
        if (isinstance(data, dict) and isinstance(data.get("epoch"), str)
                and type(data.get("seq")) is int and isinstance(data.get("events"), list)
                and isinstance(data.get("confirmations"), list) and isinstance(data.get("settings"), dict)):
            return data
    except (OSError, ValueError):
        pass
    return None


def claim_update(url: str, version: str) -> bool | None:
    request = urllib.request.Request(f"{url.rstrip('/')}/api/updates/claim-notification",
                                     data=json.dumps({"version": version}).encode(),
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=0.5) as response:
            result = json.loads(response.read(4096))
            return None if result.get("retry") else result.get("ok") is True
    except (OSError, ValueError, AttributeError):
        return None


class TrayNotifications:
    def __init__(self, sender, settings: dict | None = None, *, claim_update=None, busy=lambda: False):
        self.sender = sender
        self.settings = {key: True for key in SETTING_KEYS} if settings is None else settings
        self.seq = 0
        self.epoch: str | None = None
        self._confirmations: dict[str, float] = {}
        self._red_reasons: set[str] = set()
        sender.enabled = self._enabled
        self._claim_update = claim_update
        self._busy = busy
        self._pending_updates: dict[str, dict] = {}

    def _enabled(self, tier: str) -> bool:
        return bool(self.settings.get("MAC_NOTIFICATIONS_ENABLED")
                    and self.settings.get(f"MAC_NOTIFY_{tier.upper()}"))

    def consume(self, data: dict) -> None:
        first = self.epoch != data["epoch"]
        if first:
            self.sender.remove(list(self._confirmations))
            self._confirmations.clear()
            self._pending_updates.clear()
        self.epoch = data["epoch"]
        self.settings = data["settings"]
        current = {CONFIRM_PREFIX + c["id"]: c["deadline"] for c in data["confirmations"]}
        removed = set(self._confirmations) - set(current)
        if not self._enabled("confirm"):
            removed |= set(self._confirmations)
        self.sender.remove(list(removed))
        # 快照独立于环形缓冲；跨重启或关闭事件被淘汰时也能收回旧通知。
        self.sender.reconcile(set(current) if self._enabled("confirm") else set())
        for identifier, deadline in current.items():
            if identifier not in self._confirmations and self._enabled("confirm") and deadline > time.time():
                self.sender.send(identifier, dict(
                    tier="confirm", title="AI 请你确认", body="请打开 CallPilot 处理，超时将按拒绝处理。",
                    target="#confirm/" + identifier.removeprefix(CONFIRM_PREFIX),
                    deadline=deadline,
                ))
        self._confirmations = current
        # 首次接入只取游标，不把服务内旧短信/摘要整批弹出。
        for event in data["events"]:
            if event.get("kind", "").startswith("update:"):
                version = event["kind"].removeprefix("update:")
                self._pending_updates[version] = event
            elif not first:
                if event["seq"] > self.seq and event["tier"] == "info" and self._enabled("info"):
                    self.sender.send(f"callpilot.{self.epoch}.{event['seq']}", event)
        if self._enabled("info") and not self._busy() and self._claim_update is not None:
            for version, event in list(self._pending_updates.items()):
                def claim(version=version):
                    if self._busy():
                        return False
                    claimed = self._claim_update(version)
                    if claimed is not None:
                        self._pending_updates.pop(version, None)
                    return claimed is True
                self.sender.send(f"callpilot.update.{version}", {**event, "claim": claim})
        self.seq = data["seq"]
        self.expire()

    def expire(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        expired = [key for key, deadline in self._confirmations.items() if deadline <= now]
        if expired:
            self.sender.remove(expired)
            for key in expired:
                del self._confirmations[key]

    def status(self, state: str, reason: str, *, suppress: bool = False) -> None:
        # 黄色/通话态不证明故障恢复；只有 ready 才重置本轮已提醒原因。
        if state == "ready":
            self._red_reasons.clear()
        if state != "attention" or reason in self._red_reasons or suppress:
            return
        self._red_reasons.add(reason)
        if self._enabled("alert"):
            self.sender.send("callpilot.alert", dict(tier="alert", title="CallPilot", body=reason, target="#settings"))


class NativeSender:
    def __init__(self, center) -> None:
        self.center = center
        self.delegate = None
        self._lock = threading.RLock()
        self._inflight: dict[str, dict[str, bool]] = {}
        self._confirm_ids: set[str] = set()
        self.enabled: Callable[[str], bool] = lambda tier: True

    def send(self, identifier: str, event: dict) -> None:
        if not valid_target(event.get("target", "")):
            return
        token = {"cancelled": False, "superseded": False}
        with self._lock:
            if len(self._inflight) >= 512:
                self._inflight.pop(next(iter(self._inflight)))["superseded"] = True
            if identifier in self._inflight:
                self._inflight[identifier]["superseded"] = True
            self._inflight[identifier] = token

        def settings_received(settings):
            with self._lock:
                if self._inflight.get(identifier) is not token:
                    return
                if (token["cancelled"] or not self.enabled(event["tier"])
                        or event.get("deadline", float("inf")) <= time.time()
                        or int(settings.authorizationStatus()) not in {2, 3, 4}):
                    self._inflight.pop(identifier, None)
                    return
                from UserNotifications import (
                    UNMutableNotificationContent,
                    UNNotificationInterruptionLevelActive,
                    UNNotificationRequest,
                    UNNotificationSound,
                )

                content = UNMutableNotificationContent.alloc().init()
                if event.get("claim") is not None and not event["claim"]():
                    self._inflight.pop(identifier, None)
                    return
                content.setTitle_(event["title"])
                content.setBody_(event["body"])
                content.setUserInfo_({"target": event["target"], "tier": event["tier"]})
                # 当前签名没有 time-sensitive entitlement；按规格退回 active，不改变授权流程。
                content.setInterruptionLevel_(UNNotificationInterruptionLevelActive)
                if event["tier"] == "confirm":
                    content.setSound_(UNNotificationSound.defaultSound())
                request = UNNotificationRequest.requestWithIdentifier_content_trigger_(identifier, content, None)

                def added(error):
                    with self._lock:
                        if token["cancelled"] and not token["superseded"]:
                            self.center.removePendingNotificationRequestsWithIdentifiers_([identifier])
                            self.center.removeDeliveredNotificationsWithIdentifiers_([identifier])
                        if self._inflight.get(identifier) is token:
                            self._inflight.pop(identifier, None)
                    if error is not None:
                        logger.warning("系统未接受通知请求")

                self.center.addNotificationRequest_withCompletionHandler_(request, added)

        self.center.getNotificationSettingsWithCompletionHandler_(settings_received)

    def remove(self, identifiers) -> None:
        if not identifiers:
            return
        with self._lock:
            for identifier in identifiers:
                if identifier in self._inflight:
                    self._inflight[identifier]["cancelled"] = True
            self.center.removePendingNotificationRequestsWithIdentifiers_(list(identifiers))
            self.center.removeDeliveredNotificationsWithIdentifiers_(list(identifiers))

    def reconcile(self, identifiers: set[str]) -> None:
        with self._lock:
            self._confirm_ids = identifiers

        def received(delivered):
            with self._lock:
                stale = [n.request().identifier() for n in delivered
                         if n.request().identifier().startswith(CONFIRM_PREFIX)
                         and n.request().identifier() not in self._confirm_ids]
                self.remove(stale)

        self.center.getDeliveredNotificationsWithCompletionHandler_(received)


@lru_cache(maxsize=1)
def _delegate_class():
    import objc
    from Foundation import NSObject
    from UserNotifications import (
        UNNotificationDefaultActionIdentifier,
        UNNotificationPresentationOptionBanner,
        UNNotificationPresentationOptionList,
        UNNotificationPresentationOptionSound,
    )

    class CallPilotNotificationDelegate(NSObject, protocols=[objc.protocolNamed("UNUserNotificationCenterDelegate")]):
        def userNotificationCenter_didReceiveNotificationResponse_withCompletionHandler_(self, center, response, done):
            try:
                target = response.notification().request().content().userInfo().get("target", "")
                if response.actionIdentifier() == UNNotificationDefaultActionIdentifier and valid_target(target):
                    self.on_click(target)
            finally:
                done()

        def userNotificationCenter_willPresentNotification_withCompletionHandler_(self, center, notification, done):
            options = UNNotificationPresentationOptionBanner | UNNotificationPresentationOptionList
            if notification.request().content().userInfo().get("tier") == "confirm":
                options |= UNNotificationPresentationOptionSound
            done(options)

    return CallPilotNotificationDelegate


def make_native_sender(on_click: Callable[[str], None]) -> NativeSender | None:
    if not available():
        return None
    try:
        from UserNotifications import UNUserNotificationCenter

        sender = NativeSender(UNUserNotificationCenter.currentNotificationCenter())
        delegate = _delegate_class().alloc().init()
        delegate.on_click = on_click
        sender.delegate = delegate
        sender.center.setDelegate_(delegate)
        return sender
    except Exception:  # noqa: BLE001
        logger.exception("通知中心不可用，保留菜单栏和窗口功能")
        return None
