"""本机通知的有界内存事件流；不写盘，也不依赖 macOS。"""

from __future__ import annotations

import logging
import re
import threading
import time
import unicodedata
import uuid
from collections import deque
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Callable

from . import config

SETTING_KEYS = ("MAC_NOTIFICATIONS_ENABLED", "MAC_NOTIFY_CONFIRM", "MAC_NOTIFY_INFO", "MAC_NOTIFY_ALERT")
_backfill = ContextVar("notification_sms_backfill", default=False)


@contextmanager
def sms_backfill():
    # 标记只在当前回调上下文有效，不会吞掉另一线程同时收到的实时短信。
    token = _backfill.set(True)
    try:
        yield
    finally:
        _backfill.reset(token)


def sms_preview(text: str) -> str:
    # 数字（含 Unicode 数字）或拉丁字母可能构成验证码；无法可靠判别则不展示正文。
    if re.search(r"\d|[a-zA-Z]", unicodedata.normalize("NFKC", text)):
        return "收到一条短信"
    text = " ".join(text.split())
    return text[:20] + ("…" if len(text) > 20 else "") or "收到一条短信"


def valid_target(target: str) -> bool:
    pattern = r"#(?:sms|live|settings|history(?:/[A-Za-z0-9_+-]+)?|confirm/[A-Za-z0-9_-]+)"
    return re.fullmatch(pattern, target) is not None


class NotificationFeed:
    def __init__(self, limit: int = 256, clock: Callable[[], float] = time.time):
        self._events: deque[dict] = deque(maxlen=limit)
        self._confirmations: dict[str, dict] = {}
        self._lock = threading.RLock()
        self._seq = 0
        self._epoch = uuid.uuid4().hex
        self._clock = clock

    def _append(self, kind: str, tier: str, title: str, body: str, target: str) -> None:
        with self._lock:
            self._seq += 1
            self._events.append(dict(seq=self._seq, kind=kind, tier=tier,
                                     title=title[:120], body=body[:240], target=target))

    def info(self, kind: str, title: str, body: str, target: str = "#settings") -> None:
        """普通档通用入口；WP6 可调用，不负责查新或打开外部 URL。"""
        if not valid_target(target):
            raise ValueError("无效通知目标")
        self._append(kind, "info", title, body, target)

    def confirm_requested(self, confirm_id: str, question: str, timeout: float) -> None:
        with self._lock:
            self._prune()
            # 工具层同一时刻只有一张确认卡；额外设上限防止异常生产者占满内存。
            if len(self._confirmations) >= 32:
                self._confirmations.pop(next(iter(self._confirmations)))
            self._confirmations[confirm_id] = dict(
                id=confirm_id, question=question[:500], deadline=self._clock() + timeout,
            )
            self._append("confirm", "confirm", "AI 请你确认", "请打开 CallPilot 处理，超时将按拒绝处理。",
                         f"#confirm/{confirm_id}")

    def confirm_closed(self, confirm_id: str) -> None:
        with self._lock:
            self._confirmations.pop(confirm_id, None)
            self._append("confirm_closed", "confirm", "", "", f"#confirm/{confirm_id}")

    def sms_received(self, sender: str | None, text: str) -> None:
        if not _backfill.get():
            self.info("sms", f"收到短信 · {sender or '未知号码'}", sms_preview(text), "#sms")

    def summary_written(self, call_id: str, number: str | None, summary: dict) -> None:
        text = str(summary.get("summary") or "").strip()
        if summary.get("ok") is True and text:
            sentence = re.split(r"(?<=[。！？!?])|(?<=\.)\s|[\r\n]", text, maxsplit=1)[0]
            self.info("summary", f"通话摘要 · {number or '未知号码'}", sentence, f"#history/{call_id}")

    def _prune(self) -> None:
        self._confirmations = {key: value for key, value in self._confirmations.items()
                               if value["deadline"] > self._clock()}

    def read(self, after: int) -> dict:
        with self._lock:
            self._prune()
            return dict(epoch=self._epoch, seq=self._seq,
                        truncated=bool(self._events and after < self._events[0]["seq"] - 1),
                        events=[dict(e) for e in self._events if e["seq"] > after],
                        confirmations=[dict(e) for e in self._confirmations.values()],
                        settings={key: config.get_bool(key) for key in SETTING_KEYS})


feed = NotificationFeed()


def publish_notification(kind: str, *args: object) -> None:
    # 通知是旁路；任何通知错误都不能改变接听、确认决定、摘要保存或短信转发。
    try:
        getattr(feed, kind)(*args)
    except Exception:  # noqa: BLE001
        logging.getLogger(__name__).warning("通知事件未发布: %s", kind)
