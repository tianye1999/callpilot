"""Small, incremental TTS clauses and optional per-clause PCM gain."""

from __future__ import annotations

_PUNCTUATION = frozenset("，。！？；：、,.!?;:．")
_NUMERIC_SEPARATORS = frozenset(".,:．：")


class ClauseSplitter:
    def __init__(self, max_chars: int = 20) -> None:
        self._buffer = ""
        self._max_chars = max_chars

    def feed(self, text: str) -> list[str]:
        clauses: list[str] = []
        for char in text:
            if self._pending_numeric_separator() and not char.isdigit():
                self._emit(clauses)
            self._buffer += char
            if self._pending_numeric_separator():
                continue
            # The size limit is soft at numeric runs; a forced cut must not undo
            # decimal / time / thousands-separator protection at the boundary.
            if char in _PUNCTUATION or (len(self._buffer) >= self._max_chars and not char.isdigit()):
                self._emit(clauses)
        return clauses

    def _pending_numeric_separator(self) -> bool:
        return (len(self._buffer) >= 2 and self._buffer[-1] in _NUMERIC_SEPARATORS
                and self._buffer[-2].isdigit())

    def _emit(self, clauses: list[str]) -> None:
        han = sum("\u3400" <= c <= "\u9fff" for c in self._buffer)
        enough = han >= 2 if han else sum(c.isalnum() for c in self._buffer) >= 2
        if enough or len(self._buffer) >= self._max_chars:
            clauses.append(self._buffer)
            self._buffer = ""

    def finish(self) -> list[str]:
        tail, self._buffer = self._buffer, ""
        return [tail] if any(c.isalnum() for c in tail) else []


def normalize_peak(pcm: bytes, peak: float | None) -> bytes:
    if peak is None or not pcm:
        return pcm
    import numpy as np

    samples = np.frombuffer(pcm, dtype="<i2").astype("float32")
    current = float(np.max(np.abs(samples)))
    if current == 0:
        return pcm
    gain = min(10 ** (12 / 20), peak * 32767 / current)
    return np.clip(samples * gain, -32768, 32767).astype("<i2").tobytes()
