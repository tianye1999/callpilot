"""Scoped Windows multimedia timer request; no effect on other platforms."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from . import platforms

logger = logging.getLogger(__name__)


def _winmm() -> Any:
    import ctypes

    return getattr(ctypes, "WinDLL")("winmm")


@contextmanager
def audio_timer_resolution() -> Iterator[None]:
    timer = None
    if platforms.IS_WINDOWS:
        try:
            candidate = _winmm()
            if candidate.timeBeginPeriod(1) == 0:
                timer = candidate
                logger.info("Windows audio timer: requested_ms=1 active=True")
            else:
                logger.warning("Windows audio timer: requested_ms=1 active=False")
        except (OSError, AttributeError):
            logger.warning("Windows audio timer unavailable")
    try:
        yield
    finally:
        if timer is not None:
            timer.timeEndPeriod(1)
            logger.info("Windows audio timer: released_ms=1")
