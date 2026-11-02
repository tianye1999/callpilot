"""Event-gated V2 ownership tests, independent of real network speed."""

import asyncio
from types import SimpleNamespace

from agentcall.call_timing import CallTiming
from agentcall.inbound_preconnect import InboundPreconnect


def test_v2_adopts_connection_that_is_still_connecting():
    async def run():
        gate = asyncio.Event()
        calls = []

        async def prepare():
            calls.append("prepare")
            await gate.wait()

        async def bind(issued):
            calls.append("bind")

        async def close():
            calls.append("close")

        endpoint = SimpleNamespace(preconnect=prepare, bind_winner=bind, close=close)
        issued = SimpleNamespace(room_name="room", edge_identity="edge")
        preconnect = InboundPreconnect()
        preconnect.start(issued, lambda _: endpoint)
        await asyncio.sleep(0)
        adoption = asyncio.create_task(preconnect.adopt(issued, CallTiming(), retain_inflight=True))
        await asyncio.sleep(0)
        assert calls == ["prepare"]
        assert not adoption.done()
        gate.set()
        assert await adoption is endpoint
        await preconnect.close()
        assert calls == ["prepare", "bind"]

    asyncio.run(run())


def test_v2_summary_never_subtracts_different_clock_domains():
    timing = CallTiming(clock=lambda: 1)
    timing.mark("claim_received")
    timing.receive_client({
        "type": "client_timing", "v": 1,
        "attempt": "00000000-0000-4000-8000-000000000001", "seq": 1,
        "marks": {"answer_action": -5000, "claim_start": 0, "downlink_first_frame": 1200, "cloud_cas_ms": 17},
    })
    result = timing.summary()
    assert result["client_answer_to_track_frame_ms"] == 6200
    assert result["edge_claim_to_modem_frame_ms"] is None
    assert result["cloud_cas_ms"] == 17
    assert "answer_to_carrier_audio_ms" not in result
