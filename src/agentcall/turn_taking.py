"""Local turn-taking guard for telephone/IVR conversations.

The modem path is intentionally half duplex to prevent acoustic/USB loopback.
That makes a wrong turn boundary especially costly: once Agent playback starts,
remote audio is suppressed.  This guard therefore arbitrates *before* playback
using the raw modem uplink, where there is no Agent echo yet.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class RemoteActivity:
    voiced: bool
    speech_started: bool
    resumed_over_pending_output: bool
    rms: float
    frame_ms: float


class TurnArbiter:
    """Hold Agent output until the remote side has stayed quiet long enough."""

    def __init__(
        self,
        *,
        sample_rate: int,
        rms_threshold: float,
        quiet_ms: float,
    ) -> None:
        self.sample_rate = max(1, int(sample_rate))
        self.rms_threshold = max(0.0, float(rms_threshold))
        self.quiet_ms = max(0.0, float(quiet_ms))
        self.last_voice_at: float | None = None
        self.remote_speaking = False
        self._pending_collision_latched = False
        self._output_held_at: float | None = None
        self.remote_resumed_count = 0
        self.stale_output_drops = 0
        self.stale_output_bytes = 0
        self.output_deferred_ms = 0.0

    @staticmethod
    def frame_rms(pcm: bytes) -> float:
        aligned = pcm[: len(pcm) - (len(pcm) % 2)]
        if not aligned:
            return 0.0
        samples = np.frombuffer(aligned, dtype="<i2")
        if samples.size == 0:
            return 0.0
        return float(np.sqrt(np.mean(samples.astype(np.float64) ** 2)))

    def observe(
        self,
        pcm: bytes,
        *,
        now: float,
        output_pending: bool,
        agent_playing: bool,
    ) -> RemoteActivity:
        frame_ms = len(pcm) / 2 / self.sample_rate * 1000.0
        rms = self.frame_rms(pcm)
        voiced = rms >= self.rms_threshold
        speech_started = False
        resumed = False

        if voiced:
            speech_started = not self.remote_speaking
            self.remote_speaking = True
            self.last_voice_at = now
            # Only pending (not yet played) output is safe to invalidate.  Raw
            # uplink during playback can contain the Agent's own echo.
            if output_pending and not agent_playing:
                if not self._pending_collision_latched:
                    self.remote_resumed_count += 1
                    resumed = True
                self._pending_collision_latched = True
        elif self.remote_speaking and self.can_play(now):
            self.remote_speaking = False
            self._pending_collision_latched = False

        return RemoteActivity(
            voiced=voiced,
            speech_started=speech_started,
            resumed_over_pending_output=resumed,
            rms=rms,
            frame_ms=frame_ms,
        )

    def can_play(self, now: float) -> bool:
        if self.last_voice_at is None:
            return True
        return (now - self.last_voice_at) * 1000.0 >= self.quiet_ms

    def note_output_pending(self, now: float) -> bool:
        """Return True only when a new deferred-output interval starts."""
        if self._output_held_at is not None:
            return False
        self._output_held_at = now
        return True

    def note_output_released(self, now: float) -> int:
        if self._output_held_at is None:
            return 0
        held_ms = max(0.0, (now - self._output_held_at) * 1000.0)
        self.output_deferred_ms += held_ms
        self._output_held_at = None
        return round(held_ms)

    def note_stale_output_dropped(self, byte_count: int, now: float) -> int:
        self.stale_output_drops += 1
        self.stale_output_bytes += max(0, int(byte_count))
        return self.note_output_released(now)

    def metrics(self, now: float) -> dict[str, int]:
        deferred = self.output_deferred_ms
        if self._output_held_at is not None:
            deferred += max(0.0, (now - self._output_held_at) * 1000.0)
        return {
            "remote_resumed": self.remote_resumed_count,
            "stale_output_drops": self.stale_output_drops,
            "stale_output_bytes": self.stale_output_bytes,
            "output_deferred_ms": round(deferred),
        }
