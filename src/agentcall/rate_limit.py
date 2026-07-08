"""进程内滑动窗口频控。

当前用于短信发送闸：AI 工具与 Web API 共用同一个模块级限流器。
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class RateLimitResult:
    allowed: bool
    retry_after: float = 0.0


class SlidingWindowRateLimiter:
    """按 key 记录时间戳的进程内滑动窗口限流器。"""

    def __init__(
        self,
        *,
        window_seconds: float,
        time_fn: Callable[[], float] = time.time,
    ) -> None:
        self.window_seconds = window_seconds
        self._time_fn = time_fn
        self._events: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def acquire(self, key: str, limit: int) -> RateLimitResult:
        if limit <= 0:
            return RateLimitResult(True)
        now = self._time_fn()
        cutoff = now - self.window_seconds
        with self._lock:
            bucket = self._events[key]
            while bucket and bucket[0] <= cutoff:
                bucket.popleft()
            if len(bucket) >= limit:
                retry_after = max(0.0, bucket[0] + self.window_seconds - now)
                return RateLimitResult(False, retry_after)
            bucket.append(now)
            return RateLimitResult(True)

    def reset(self) -> None:
        with self._lock:
            self._events.clear()


_SMS_LIMITER = SlidingWindowRateLimiter(window_seconds=3600)


def acquire_sms_send_slot(limit_per_hour: int) -> RateLimitResult:
    """为一次短信发送占用共享频控额度；``limit_per_hour <= 0`` 表示不限。"""
    return _SMS_LIMITER.acquire("sms_send", limit_per_hour)


def reset_sms_rate_limit_state() -> None:
    """测试/维护用：清空进程内短信频控状态。"""
    _SMS_LIMITER.reset()
