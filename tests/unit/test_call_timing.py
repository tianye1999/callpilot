import asyncio
import uuid

import pytest

from agentcall.call_timing import CallTiming


class Record:
    def __init__(self):
        self.events = []

    def log_event(self, kind, **fields):
        self.events.append({"type": kind, **fields})


def packet(**changes):
    return {"type": "client_timing", "v": 1, "attempt": str(uuid.uuid4()),
            "seq": 1, "marks": {"claim_start": 0, "answer_action": -50}, **changes}


def engine_snapshot(**changes):
    return {"running": True, "recording": True, "playing": True, "inputEnabled": True,
            "inputRunning": True, "outputEnabled": True, "outputRunning": True, "inputMuted": False,
            "muteMode": 0, **changes}


def rtp_packet(**changes):
    return {"type": "client_rtp_stats", "v": 1, "seq": 1, "elapsedMs": 0,
            "outPackets": 0, "outBytes": 0, "inPackets": None, "inBytes": None,
            "lost": -1, "rttMs": None, "localCandidate": "host", "remoteCandidate": None,
            "pairState": "succeeded", **changes}


def test_rtp_observations_buffer_deduplicate_and_keep_unknown_distinct_from_zero():
    timing = CallTiming()
    timing.receive_rtp(rtp_packet())
    timing.receive_rtp(rtp_packet())
    record = Record()
    timing.bind(record)
    timing.bind(record)
    for seq in range(2, 12):
        timing.receive_rtp(rtp_packet(seq=seq))
    assert len(record.events) == 10
    assert record.events[0] == rtp_packet()
    assert record.events[0]["outPackets"] == 0
    assert record.events[0]["inPackets"] is None


@pytest.mark.parametrize("changes", [
    {"seq": True}, {"seq": 11}, {"v": True}, {"elapsedMs": -1}, {"elapsedMs": 60001},
    {"outPackets": -1}, {"outBytes": 2**53}, {"inBytes": 0.5}, {"lost": -(2**53)},
    {"rttMs": float("nan")}, {"rttMs": float("inf")}, {"rttMs": True},
    {"localCandidate": "192.0.2.1"}, {"pairState": "connected"}, {"address": "private"},
])
def test_invalid_rtp_is_rejected_without_advancing_sequence(changes):
    record = Record()
    timing = CallTiming(record)
    timing.receive_rtp(rtp_packet(**changes))
    assert record.events == []
    timing.receive_rtp(rtp_packet())
    assert record.events == [rtp_packet()]


def test_rtp_envelope_requires_every_field_and_enforces_byte_bound():
    from agentcall.call_timing import valid_client_rtp_stats

    assert not valid_client_rtp_stats({})
    assert not valid_client_rtp_stats({key: value for key, value in rtp_packet().items() if key != "lost"})
    assert not valid_client_rtp_stats(rtp_packet(rttMs=10**1000))


def test_internal_observations_survive_bind_and_reject_unknown_kind():
    timing = CallTiming()
    timing.record_observation("direct_sdp_summary", {"audioSections": 1})
    timing.record_observation("private_event", {"address": "must-not-log"})
    record = Record()
    timing.bind(record)
    assert record.events == [{"type": "direct_sdp_summary", "audioSections": 1}]


def test_internal_observations_snapshot_and_bound_buffer():
    timing = CallTiming()
    sample = {"outPackets": 0}
    timing.record_observation("edge_rtp_stats", sample)
    sample["outPackets"] = 999
    timing.record_observation("edge_rtp_stats", {"type": "private"})
    timing.record_observation("edge_rtp_stats", {"outPackets": float("nan")})
    timing.record_observation("edge_rtp_stats", {"extra": "x" * 4096})
    for _ in range(20):
        timing.record_observation("edge_rtp_stats", {"outPackets": 1})
    record = Record()
    timing.bind(record)
    assert len(record.events) == 12
    assert record.events[0] == {"type": "edge_rtp_stats", "outPackets": 0}


def test_audio_engine_evidence_survives_binding_and_http_replay():
    timing = CallTiming()
    message = packet(marks={"direct_control_ready": 100, "audio_engine_running": 120},
                     audioEngine=engine_snapshot())
    timing.receive_client(message)
    record = Record()
    timing.bind(record)
    timing.receive_client(message)
    assert timing.summary()["client_audio_engine_running_ms"] == 120
    assert [event for event in record.events if event["type"] == "client_audio_engine_state"] == [
        {"type": "client_audio_engine_state", "seq": 1, "audioEngine": engine_snapshot()},
    ]


@pytest.mark.parametrize("snapshot", [engine_snapshot(running="yes"), engine_snapshot(muteMode=True),
    engine_snapshot(muteMode=3), engine_snapshot(deviceName="must-not-log"), {}, None])
def test_audio_engine_snapshot_is_strict_and_never_logs_free_text(snapshot):
    record = Record()
    timing = CallTiming(record)
    timing.receive_client(packet(marks={"audio_engine_failed": 200}, audioEngine=snapshot))
    assert record.events == []


def test_client_timing_buffers_until_record_and_deduplicates():
    timing = CallTiming()
    message = packet()
    timing.receive_client(message)
    record = Record()
    timing.bind(record)
    timing.receive_client(message)
    timing.receive_client({**message, "seq": 2, "marks": {"connect_return": 1500}})
    timing.receive_client({**message, "seq": 1, "marks": {"mic_enable_return": 1600}})
    assert [(e["stage"], e["ms"]) for e in record.events] == [
        ("client_claim_start", 0), ("client_answer_action", -50), ("client_connect_return", 1500)]


@pytest.mark.parametrize("stage", [
    "claim_dns_start", "claim_dns_end", "claim_connect_start", "claim_connect_end",
    "claim_tls_start", "claim_tls_end", "claim_request_start", "claim_first_byte",
    "claim_connection_reused", "prewarm_start", "prewarm_end",
])
def test_client_timing_accepts_claim_network_and_prewarm_stages(stage):
    record = Record()
    timing = CallTiming(record=record)
    timing.receive_client(packet(marks={stage: 1}))
    assert record.events == [{"type": "latency", "stage": "client_" + stage, "ms": 1}]


def test_client_timing_accepts_audio_lease_wait_marks():
    record = Record()
    timing = CallTiming(record=record)
    timing.receive_client(packet(marks={"audio_lease_start": -6500, "audio_lease_end": -10}))
    assert record.events == [
        {"type": "latency", "stage": "client_audio_lease_start", "ms": -6500},
        {"type": "latency", "stage": "client_audio_lease_end", "ms": -10},
    ]


def test_client_timing_skips_unknown_stage_without_losing_known_marks():
    record = Record()
    timing = CallTiming(record=record)
    message = packet(marks={"claim_start": 0, "future_stage": 10, "claim_response": 20})
    timing.receive_client(message)
    timing.receive_client(message)
    assert record.events == [
        {"type": "latency", "stage": "client_claim_start", "ms": 0},
        {"type": "latency", "stage": "client_claim_response", "ms": 20},
    ]


@pytest.mark.parametrize("value", [
    True, "private-value", None, [], {}, float("nan"), float("inf"),
    -60001, 86400001, 10**1000,
])
def test_client_timing_unknown_stage_still_requires_valid_numeric_value(value):
    record = Record()
    timing = CallTiming(record=record)
    message = packet(marks={"claim_start": 0, "future_stage": value})
    timing.receive_client(message)
    assert record.events == []
    timing.receive_client({**message, "marks": {"claim_start": 0}})
    assert record.events == [{"type": "latency", "stage": "client_claim_start", "ms": 0}]


def test_unknown_only_timing_batch_still_pins_attempt_and_advances_sequence():
    record = Record()
    timing = CallTiming(record=record)
    message = packet(marks={"future_stage": 0})
    timing.receive_client(message)
    timing.receive_client({**message, "marks": {"claim_start": 1}})
    timing.receive_client(packet(seq=2, marks={"claim_response": 2}))
    assert record.events == []
    timing.receive_client({**message, "seq": 2, "marks": {"claim_start": 3}})
    assert record.events == [{"type": "latency", "stage": "client_claim_start", "ms": 3}]


@pytest.mark.parametrize("marks", [
    {"claim_start": 0, **{f"future_stage_{index}": index for index in range(16)}},
    {"claim_start": 0, "future_" + "x" * 2048: 1},
])
def test_unknown_timing_marks_do_not_bypass_batch_count_or_byte_limits(marks):
    record = Record()
    timing = CallTiming(record=record)
    message = packet(marks=marks)
    timing.receive_client(message)
    assert record.events == []
    timing.receive_client({**message, "marks": {"claim_start": 0}})
    assert record.events == [{"type": "latency", "stage": "client_claim_start", "ms": 0}]


@pytest.mark.parametrize("changes", [
    {"v": True}, {"seq": True}, {"seq": 0}, {"seq": 65}, {"attempt": "private-number"},
    {"marks": {"claim_start": True}},
    {"marks": {"claim_start": float("nan")}}, {"marks": {"claim_start": 86400001}},
    {"marks": {"claim_start": 10**1000}},
    {"marks": {"claim_start": -60001}}, {"marks": {"claim_start": "secret"}},
    {"recordId": "untrusted"}, {"marks": {}},
    {"marks": {"claim_start": 0, 1: 10}},
])
def test_client_timing_rejects_invalid_batch_atomically(changes):
    record = Record()
    timing = CallTiming(record=record)
    timing.receive_client(packet(**changes))
    assert record.events == []


def test_client_timing_pins_attempt_and_first_mark():
    record = Record()
    timing = CallTiming(record=record)
    message = packet()
    timing.receive_client(message)
    timing.receive_client(packet(seq=2, marks={"connect_return": 500}))
    timing.receive_client({**message, "seq": 2, "marks": {"claim_start": 800}})
    assert len(record.events) == 2


def test_new_client_marks_leave_room_for_edge_and_teardown_observations():
    from agentcall.call_timing import CLIENT_MARKS, MAX_TIMING_MARKS

    timing = CallTiming()
    names = sorted(CLIENT_MARKS)
    attempt = str(uuid.uuid4())
    for index in range(0, len(names), 16):
        timing.receive_client(packet(attempt=attempt, seq=index // 16 + 1,
                                     marks={name: 0 for name in names[index:index + 16]}))
    for index in range(32):
        timing.mark(f"local_{index}")
    assert len(timing._marks) == len(names) + 32 <= MAX_TIMING_MARKS


def test_edge_marks_use_local_clock_and_only_emit_once():
    record = Record()
    clock = [10.0]
    timing = CallTiming(record=record, clock=lambda: clock[0])
    clock[0] = 10.125
    timing.mark("media_ready")
    clock[0] = 12
    timing.mark("media_ready")
    assert record.events == [{"type": "latency", "stage": "edge_media_ready", "ms": 125.0}]


def test_no_audio_summary_uses_only_edge_clock():
    clock = [10.0]
    timing = CallTiming(clock=lambda: clock[0])
    timing.mark("direct_connected")
    clock[0] = 18.0
    timing.mark("no_phone_audio")
    clock[0] = 18.25
    timing.mark("provisional_ai_opening")
    assert timing.summary()["edge_connected_to_no_audio_ms"] == 8000
    assert timing.summary()["edge_no_audio_to_ai_ms"] == 250


def test_ack_timeout_is_bounded_and_duplicate_is_not_another_hangup(monkeypatch):
    monkeypatch.setattr("agentcall.call_timing.HANGUP_ACK_TIMEOUT", 0.01)
    record = Record()
    timing = CallTiming(record=record)

    async def blocked_send(_event):
        await asyncio.Event().wait()

    async def run():
        command = {"type": "hangup", "commandId": str(uuid.uuid4())}
        assert await asyncio.wait_for(timing.acknowledge_hangup(command, blocked_send), 0.2)
        assert not await timing.acknowledge_hangup(command, blocked_send)

    asyncio.run(run())
    assert [event["stage"] for event in record.events] == ["edge_hangup_received", "edge_hangup_ack_failed"]
