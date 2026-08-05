from __future__ import annotations

import numpy as np

from agentcall.turn_taking import TurnArbiter


def _pcm(amplitude: int, ms: int = 20, rate: int = 8000) -> bytes:
    return np.full(rate * ms // 1000, amplitude, dtype="<i2").tobytes()


def test_holds_output_until_remote_has_stayed_quiet() -> None:
    arbiter = TurnArbiter(sample_rate=8000, rms_threshold=400, quiet_ms=2500)

    activity = arbiter.observe(
        _pcm(1200), now=10.0, output_pending=False, agent_playing=False
    )
    assert activity.voiced is True
    assert activity.speech_started is True
    assert arbiter.can_play(12.49) is False
    assert arbiter.can_play(12.50) is True


def test_remote_resume_invalidates_pending_not_playing_output_once() -> None:
    arbiter = TurnArbiter(sample_rate=8000, rms_threshold=400, quiet_ms=2500)

    first = arbiter.observe(
        _pcm(1500), now=20.0, output_pending=True, agent_playing=False
    )
    second = arbiter.observe(
        _pcm(1500), now=20.02, output_pending=True, agent_playing=False
    )

    assert first.resumed_over_pending_output is True
    assert second.resumed_over_pending_output is False
    assert arbiter.remote_resumed_count == 1
    assert arbiter.note_stale_output_dropped(3200, 20.02) == 0
    assert arbiter.metrics(20.02)["stale_output_bytes"] == 3200


def test_does_not_treat_playback_echo_as_safe_pending_collision() -> None:
    arbiter = TurnArbiter(sample_rate=8000, rms_threshold=400, quiet_ms=2500)

    activity = arbiter.observe(
        _pcm(1600), now=30.0, output_pending=True, agent_playing=True
    )

    assert activity.voiced is True
    assert activity.resumed_over_pending_output is False
    assert arbiter.remote_resumed_count == 0


def test_deferred_duration_is_accumulated() -> None:
    arbiter = TurnArbiter(sample_rate=8000, rms_threshold=400, quiet_ms=2500)

    assert arbiter.note_output_pending(40.0) is True
    assert arbiter.note_output_pending(40.5) is False
    assert arbiter.note_output_released(41.25) == 1250
    assert arbiter.metrics(41.25)["output_deferred_ms"] == 1250
