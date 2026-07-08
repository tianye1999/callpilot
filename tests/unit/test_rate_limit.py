"""rate_limit 单测：短信滑动窗口限流的边界行为。"""

from __future__ import annotations

from agentcall.rate_limit import SlidingWindowRateLimiter


def test_sliding_window_allows_until_limit_then_rejects():
    now = 1000.0
    limiter = SlidingWindowRateLimiter(window_seconds=3600, time_fn=lambda: now)

    assert limiter.acquire("sms", 2).allowed is True
    assert limiter.acquire("sms", 2).allowed is True

    denied = limiter.acquire("sms", 2)
    assert denied.allowed is False
    assert denied.retry_after > 0


def test_sliding_window_expires_old_entries():
    now = 1000.0
    limiter = SlidingWindowRateLimiter(window_seconds=10, time_fn=lambda: now)
    assert limiter.acquire("sms", 1).allowed is True
    assert limiter.acquire("sms", 1).allowed is False

    now = 1010.1
    assert limiter.acquire("sms", 1).allowed is True


def test_zero_limit_disables_rate_limit():
    limiter = SlidingWindowRateLimiter(window_seconds=3600, time_fn=lambda: 1000.0)

    for _ in range(20):
        assert limiter.acquire("sms", 0).allowed is True
