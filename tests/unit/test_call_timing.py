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


@pytest.mark.parametrize("changes", [
    {"v": True}, {"seq": True}, {"seq": 0}, {"seq": 65}, {"attempt": "private-number"},
    {"marks": {"phone_number": 1}}, {"marks": {"claim_start": True}},
    {"marks": {"claim_start": float("nan")}}, {"marks": {"claim_start": 86400001}},
    {"marks": {"claim_start": 10**1000}},
    {"marks": {"claim_start": -60001}}, {"marks": {"claim_start": "secret"}},
    {"recordId": "untrusted"}, {"marks": {}},
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


def test_edge_marks_use_local_clock_and_only_emit_once():
    record = Record()
    clock = [10.0]
    timing = CallTiming(record=record, clock=lambda: clock[0])
    clock[0] = 10.125
    timing.mark("media_ready")
    clock[0] = 12
    timing.mark("media_ready")
    assert record.events == [{"type": "latency", "stage": "edge_media_ready", "ms": 125.0}]


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
