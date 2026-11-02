"""The replay must preserve uncertainty instead of manufacturing a speedup."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "inbound_fastpath_replay.py"
spec = importlib.util.spec_from_file_location("inbound_fastpath_replay", SCRIPT)
assert spec is not None and spec.loader is not None
replay = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = replay
spec.loader.exec_module(replay)


def observations() -> dict[str, float]:
    return {
        "edge_claim_received": 0.0,
        "edge_endpoint_connect_start": 10.0,
        "edge_endpoint_connect_return": 3010.0,
        "edge_media_ready": 4010.0,
        "edge_ata_start": 4020.0,
        "edge_pcm_ready": 6820.0,
        "edge_first_modem_to_phone": 6840.0,
        "client_claim_start": 0.0,
        "client_claim_response": 1000.0,
        "client_answer_action": -100.0,
        "client_downlink_first_frame": 5000.0,
    }


def test_parallel_dag_uses_longest_path_not_sum() -> None:
    assert replay.finish_times({
        "room": (3000.0, ()),
        "pcm": (2800.0, ()),
        "subscription": (1000.0, ("room",)),
        "frame": (20.0, ("subscription", "pcm")),
    })["frame"] == 4020.0


def test_replay_moves_ata_in_parallel_but_preserves_every_duration() -> None:
    result = replay.analyze(observations(), "call-01")
    assert result["edge_observed_ms"] == 6840.0
    assert result["edge_parallel_model_ms"] == 4020.0
    assert result["phone_track_first_frame_ms"] == 5100.0
    assert result["answer_to_carrier_audio_ms"] is None
    assert result["answer_to_carrier_audio_model_ms"] is None
    assert result["legacy_cross_clock_value_ms"] == 6940.0


def test_missing_first_frame_is_unknown_not_zero() -> None:
    marks = observations()
    del marks["edge_first_modem_to_phone"]
    result = replay.analyze(marks, "call-02")
    assert result["edge_observed_ms"] is None
    assert result["edge_parallel_model_ms"] is None
    assert "edge_first_modem_to_phone" in result["missing"]


def test_contradictory_observations_do_not_get_clamped_to_fabricate_non_regression() -> None:
    marks = observations()
    marks["edge_first_modem_to_phone"] = 500.0
    result = replay.analyze(marks, "call-03")
    assert result["edge_parallel_model_ms"] is None
    assert result["invalid"]


def test_cross_clock_interval_is_rejected() -> None:
    with pytest.raises(ValueError, match="clock"):
        replay.span(observations(), "client_answer_action", "edge_first_modem_to_phone")


def test_dag_rejects_cycles_and_negative_costs() -> None:
    with pytest.raises(ValueError):
        replay.finish_times({"loop": (1.0, ("loop",))})
    with pytest.raises(ValueError):
        replay.finish_times({"negative": (-1.0, ())})


def test_direct_model_requires_explicit_ice_and_keeps_claim_delivery_uncertainty() -> None:
    assert replay.direct_model(observations(), None) is None
    assert replay.direct_model(observations(), (200.0, 1000.0)) == {
        "lower_ms": 2800.0, "upper_ms": 3800.0,
        "ice_assumption_ms": [200.0, 1000.0], "claim_forward_assumption_ms": [0.0, 1000.0],
    }
    marks = observations()
    del marks["client_claim_response"]
    assert replay.direct_model(marks, (200.0, 1000.0)) is None
    with pytest.raises(ValueError):
        replay.direct_model(observations(), (1000.0, 200.0))


def test_log_reader_rejects_nonfinite_boolean_and_conflicting_marks(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    path.write_text(
        '{"type":"latency","stage":"edge_claim_received","ms":0}\n'
        '{"type":"latency","stage":"edge_claim_received","ms":1}\n'
        '{"type":"latency","stage":"edge_media_ready","ms":true}\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        replay.load_marks(path)
