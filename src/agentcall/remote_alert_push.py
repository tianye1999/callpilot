"""Best-effort, content-free alerts drained by the existing cloud thread."""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable
from typing import Literal

from . import config

AlertKind = Literal["sms", "call_digest", "missed_call"]
logger = logging.getLogger(__name__)


class AlertQueue:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._items: deque[tuple[float, str]] = deque(maxlen=128)
        self._lock = threading.Lock()

    def put(self, kind: AlertKind, event_id: str | None = None) -> None:
        if not config.get_bool("REMOTE_ALERT_PUSH_ENABLED"):
            return
        # Never send a local call directory ID (it can include a phone number).
        opaque = hashlib.sha256((event_id or uuid.uuid4().hex).encode("utf-8")).hexdigest()
        payload = json.dumps({"v": 1, "type": "device.notify", "kind": kind,
                              "eventId": f"event_{opaque}"}, separators=(",", ":"))
        with self._lock:
            self._items.append((self._clock(), payload))

    def drain(self, send: Callable[[str], None]) -> None:
        with self._lock:
            pending = list(self._items)
            self._items.clear()
        if not config.get_bool("REMOTE_ALERT_PUSH_ENABLED"):
            return
        for created, payload in pending:
            if self._clock() - created > 300:
                continue
            try:
                send(payload)
            except Exception:  # noqa: BLE001
                logger.warning("Alert push send failed")
                return


queue = AlertQueue()


def publish(kind: AlertKind, event_id: str | None = None) -> None:
    try:
        queue.put(kind, event_id)
    except Exception:  # noqa: BLE001
        logger.warning("Alert push enqueue failed")


def drain(send: Callable[[str], None]) -> None:
    try:
        queue.drain(send)
    except Exception:  # noqa: BLE001
        logger.warning("Alert push drain failed")
