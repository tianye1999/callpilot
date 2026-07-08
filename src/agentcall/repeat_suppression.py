"""Similarity-based suppression for repeated agent speech.

This deliberately compares only the agent's own recent downlink transcripts.
It does not inspect user speech and does not use phrase/keyword lists, so it
stays useful across languages and IVR wording.
"""

from __future__ import annotations

import logging
import time
import unicodedata
from collections import deque
from difflib import SequenceMatcher
from typing import Callable, Iterable

from . import config

logger = logging.getLogger(__name__)

DEFAULT_RECENT_LIMIT = 5
MIN_NORMALIZED_CHARS = 6
DEFAULT_NUDGE_COOLDOWN_SECONDS = 8.0
DEFAULT_STUCK_LIMIT = 3


def normalize_for_similarity(text: str) -> str:
    """Normalize text for similarity: case-fold and drop separators/punctuation."""
    normalized: list[str] = []
    for ch in (text or "").casefold():
        category = unicodedata.category(ch)
        if category[0] in {"P", "Z"} or ch.isspace():
            continue
        normalized.append(ch)
    return "".join(normalized)


def is_repetitive(
    new_text: str,
    recent_texts: Iterable[str],
    threshold: float,
    *,
    min_chars: int = MIN_NORMALIZED_CHARS,
) -> bool:
    """Return whether ``new_text`` is highly similar to any recent agent text."""
    if threshold <= 0:
        return False
    new_norm = normalize_for_similarity(new_text)
    if len(new_norm) < min_chars:
        return False
    for old_text in recent_texts:
        old_norm = normalize_for_similarity(old_text)
        if len(old_norm) < min_chars:
            continue
        if SequenceMatcher(None, new_norm, old_norm).ratio() >= threshold:
            return True
    return False


class RepeatSuppressor:
    """Keeps one call's recent agent transcripts and judges repeated responses."""

    def __init__(
        self,
        *,
        recent_limit: int = DEFAULT_RECENT_LIMIT,
        threshold_getter: Callable[[], float] | None = None,
    ) -> None:
        self._recent: deque[str] = deque(maxlen=recent_limit)
        self._threshold_getter = threshold_getter or (
            lambda: config.get_float("REPEAT_SUPPRESS_SIMILARITY")
        )
        self._repeat_hits = 0

    def should_suppress(self, text: str) -> bool:
        threshold = self._threshold_getter()
        normalized = normalize_for_similarity(text)
        if not normalized:
            return False
        if is_repetitive(text, self._recent, threshold):
            self._repeat_hits += 1
            if self._repeat_hits >= 2:
                return True
            self._recent.append(text)
            return False
        self._repeat_hits = 0
        self._recent.append(text)
        return False

    @property
    def disabled(self) -> bool:
        return self._threshold_getter() <= 0


class ResponseAudioGate:
    """Buffer response audio until transcript is known, then flush or drop it.

    Qwen/OpenAI both stream audio before final downlink transcript. Instead of
    provider-specific response.cancel timing, we hold per-response audio locally
    and only release it after the transcript passes the repeat check; repeated
    responses are never handed to the modem queue.
    """

    def __init__(
        self,
        provider: str,
        emit_audio: Callable[[bytes], None],
        suppressor: RepeatSuppressor | None = None,
        on_suppressed: Callable[[str], None] | None = None,
        on_stuck: Callable[[int, str], None] | None = None,
        time_fn: Callable[[], float] = time.monotonic,
        nudge_cooldown_seconds: float = DEFAULT_NUDGE_COOLDOWN_SECONDS,
        stuck_limit: int = DEFAULT_STUCK_LIMIT,
    ) -> None:
        self._provider = provider
        self._emit_audio = emit_audio
        self._suppressor = suppressor or RepeatSuppressor()
        self._on_suppressed = on_suppressed
        self._on_stuck = on_stuck
        self._time_fn = time_fn
        self._nudge_cooldown_seconds = nudge_cooldown_seconds
        self._stuck_limit = stuck_limit
        self._last_nudge_at: float | None = None
        self._consecutive_suppressed = 0
        self._stuck_notified = False
        self._pending: dict[str, list[bytes]] = {}
        self._allowed: set[str] = set()
        self._suppressed: set[str] = set()

    def push_audio(self, response_id: str | None, chunk: bytes) -> None:
        if not chunk:
            return
        if not response_id or self._suppressor.disabled:
            self._emit_audio(chunk)
            return
        if response_id in self._suppressed:
            return
        if response_id in self._allowed:
            self._emit_audio(chunk)
            return
        self._pending.setdefault(response_id, []).append(chunk)

    def complete_transcript(self, response_id: str | None, transcript: str) -> bool:
        if not response_id or self._suppressor.disabled:
            return False
        if self._suppressor.should_suppress(transcript):
            self._pending.pop(response_id, None)
            self._allowed.discard(response_id)
            self._suppressed.add(response_id)
            self._consecutive_suppressed += 1
            logger.info("[%s] 抑制复读: %s", self._provider, transcript)
            self._notify_suppressed(transcript)
            if (
                self._stuck_limit > 0
                and self._consecutive_suppressed >= self._stuck_limit
                and not self._stuck_notified
            ):
                self._stuck_notified = True
                if self._on_stuck is not None:
                    try:
                        self._on_stuck(self._consecutive_suppressed, transcript)
                    except Exception:  # noqa: BLE001
                        logger.exception("[%s] 复读卡死回调失败", self._provider)
            return True
        self._consecutive_suppressed = 0
        self._stuck_notified = False
        self._flush(response_id)
        return False

    def complete_response(self, response_id: str | None) -> None:
        if not response_id:
            return
        self._flush(response_id)
        self._suppressed.discard(response_id)

    def _flush(self, response_id: str) -> None:
        chunks = self._pending.pop(response_id, [])
        self._allowed.add(response_id)
        self._suppressed.discard(response_id)
        for chunk in chunks:
            self._emit_audio(chunk)

    def _notify_suppressed(self, transcript: str) -> None:
        if self._on_suppressed is None:
            return
        now = self._time_fn()
        if (
            self._last_nudge_at is not None
            and now - self._last_nudge_at < self._nudge_cooldown_seconds
        ):
            return
        self._last_nudge_at = now
        try:
            self._on_suppressed(transcript)
        except Exception:  # noqa: BLE001
            logger.exception("[%s] 复读换说法提示失败", self._provider)
