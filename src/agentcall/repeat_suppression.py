"""Similarity-based suppression for repeated agent speech.

This deliberately compares only the agent's own recent downlink transcripts.
It does not inspect user speech and does not use phrase/keyword lists, so it
stays useful across languages and IVR wording.
"""

from __future__ import annotations

import logging
import unicodedata
from collections import deque
from difflib import SequenceMatcher
from typing import Callable, Iterable

from . import config

logger = logging.getLogger(__name__)

DEFAULT_RECENT_LIMIT = 5
MIN_NORMALIZED_CHARS = 6


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

    def should_suppress(self, text: str) -> bool:
        threshold = self._threshold_getter()
        if is_repetitive(text, self._recent, threshold):
            return True
        if normalize_for_similarity(text):
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
    ) -> None:
        self._provider = provider
        self._emit_audio = emit_audio
        self._suppressor = suppressor or RepeatSuppressor()
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
            logger.info("[%s] 抑制复读: %s", self._provider, transcript)
            return True
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
