"""Deferred signaling must reserve ownership without inventing a ready peer."""

import asyncio
from types import SimpleNamespace

import pytest

from agentcall.call_timing import CallTiming
from agentcall.inbound_direct import InboundDirectRequest, SwitchableInboundEndpoint


class Endpoint:
    def __init__(self, gate=None):
        self.answer = {"sdp": "answer"}
        self.media_ready = False
        self.browser_connected = False
        self.closed = False
        self.started = asyncio.Event()
        self.gate = gate

    async def connect(self):
        self.started.set()
        if self.gate:
            await self.gate.wait()

    async def close(self):
        self.closed = True


async def settle():
    for _ in range(10):
        await asyncio.sleep(0)


def test_timing_from_retiring_transport_is_received_before_fallback_finishes():
    async def run():
        entered, release = asyncio.Event(), asyncio.Event()
        batch = {"type": "client_timing", "v": 1, "attempt": "00000000-0000-4000-8000-000000000001",
                 "seq": 1, "marks": {"answer_action": 0, "claim_start": 1, "claim_response": 20}}

        async def next_command(timeout):
            entered.set()
            await release.wait()
            return batch

        request = InboundDirectRequest("offer", "claim", None, [], lambda *_: None)
        endpoint = SwitchableInboundEndpoint(request)
        endpoint._inner = SimpleNamespace(next_command=next_command)
        timing = CallTiming()
        endpoint.set_timing(timing)
        reading = asyncio.create_task(endpoint.next_command(1))
        await entered.wait()
        endpoint._inner = None  # The fallback detached the old transport while its read was in flight.
        release.set()
        assert await reading is None
        assert timing.summary()["client_claim_http_ms"] == 19

    asyncio.run(run())


def test_initial_silence_expiry_fences_late_sdp_and_fallback():
    request = InboundDirectRequest("offer", "claim", None, [], lambda *_: None)
    assert request.expire_initial_sdp_wait()
    assert not request.supply_sdp("v=0", [], lambda *_: pytest.fail("Late SDP revived expired claim"))
    assert not request.fallback(SimpleNamespace(), lambda *_: pytest.fail("Late fallback revived expired claim"))
    assert request.media_offer() is None and not request.can_commit


def test_no_audio_failure_retains_claim_for_fallback_but_cannot_commit_direct():
    async def run():
        events = []
        request = InboundDirectRequest("offer", "claim", "sdp", [], lambda *_: None)
        endpoint = SwitchableInboundEndpoint(request)

        async def send(value):
            events.append(value)

        endpoint._inner = SimpleNamespace(direct_connected_at=100.0, phone_audio_received=False,
                                          media_ready=True, send_event=send)
        assert not endpoint.no_phone_audio_expired(107.999, 8)
        assert endpoint.no_phone_audio_expired(108, 8)
        assert await endpoint.fail_no_phone_audio()
        assert not endpoint.media_ready and not request.can_commit
        assert events == [{"type": "media_failed", "reason": "no_phone_audio"}]
        assert not await endpoint.fail_no_phone_audio()
        assert request.fallback(SimpleNamespace(), lambda *_: None)
        assert not request.supply_sdp("sdp", [], lambda *_: None)

    asyncio.run(run())


def test_any_first_audio_frame_or_existing_fallback_disables_no_audio_deadline():
    request = InboundDirectRequest("offer", "claim", "sdp", [], lambda *_: None)
    endpoint = SwitchableInboundEndpoint(request)
    endpoint._inner = SimpleNamespace(direct_connected_at=100.0, phone_audio_received=True)
    assert not endpoint.no_phone_audio_expired(1000, 8)
    endpoint._inner.phone_audio_received = False
    endpoint._direct_replaced = True
    assert not endpoint.no_phone_audio_expired(1000, 8)


@pytest.mark.parametrize("progress", ["sdp", "fallback_pending", "livekit"])
def test_initial_silence_cap_does_not_expire_media_already_started(progress):
    request = InboundDirectRequest("offer", "claim", None, [], lambda *_: None)
    if progress == "sdp":
        assert request.supply_sdp("v=0", [], lambda *_: None)
    else:
        assert request.fallback(SimpleNamespace(), lambda *_: None)
        if progress == "livekit":
            assert request.switched()
    assert not request.expire_initial_sdp_wait()


def test_deferred_connect_waits_without_constructing_peer_and_uses_one_supplied_offer():
    async def run():
        created, replies, reservation = [], [], []
        request = InboundDirectRequest("offer", "claim", None, [], lambda *args: reservation.append(args))
        direct = Endpoint()

        def factory(offer, servers, *, call_id):
            created.append((offer, servers, call_id))
            return direct

        endpoint = SwitchableInboundEndpoint(request, direct_factory=factory)
        connecting = asyncio.create_task(endpoint.connect())
        await settle()
        assert not created and not connecting.done() and not endpoint.media_ready
        assert request.supply_sdp("offer-sdp", [{"urls": ["stun:fixture.invalid"]}],
                                  lambda *args: replies.append(args))
        await asyncio.wait_for(connecting, 1)
        assert created == [({"type": "offer", "sdp": "offer-sdp"},
                            [{"urls": ["stun:fixture.invalid"]}], "claim")]
        assert replies == [("answer", None)] and not reservation
        assert request.supply_sdp("offer-sdp", [], lambda *args: replies.append(args))
        assert replies == [("answer", None), ("answer", None)]
        assert len(created) == 1
        await endpoint.close()
        assert direct.closed
        assert not request.supply_sdp("offer-sdp", [], lambda *_: pytest.fail("Late SDP accepted"))

    asyncio.run(run())


def test_deferred_duplicate_sdp_waiters_are_bounded_and_receive_same_answer():
    replies = []
    request = InboundDirectRequest("offer", "claim", None, [], lambda *_: None)
    for index in range(5):
        assert request.supply_sdp("same", [], lambda *args, index=index: replies.append((index, args)))
    assert not request.supply_sdp("same", [], lambda *_: pytest.fail("Waiter limit bypassed"))
    assert not request.supply_sdp("changed", [], lambda *_: pytest.fail("SDP replaced"))
    request.reply("answer", None)
    request.reply("late-answer", None)
    assert replies == [(index, ("answer", None)) for index in range(5)]


def test_close_without_sdp_cancels_wait_without_constructing_peer():
    async def run():
        replies = []
        request = InboundDirectRequest("offer", "claim", None, [], lambda *args: replies.append(args))
        endpoint = SwitchableInboundEndpoint(request,
            direct_factory=lambda *_args, **_kwargs: pytest.fail("PC created before SDP"))
        connecting = asyncio.create_task(endpoint.connect())
        await settle()
        await asyncio.wait_for(endpoint.close(), 1)
        await asyncio.gather(connecting, return_exceptions=True)
        assert replies == [(None, "MEDIA_ENDED")]
        assert not request.supply_sdp("late", [], lambda *_: pytest.fail("Closed SDP accepted"))

    asyncio.run(run())


def test_fallback_without_sdp_starts_livekit_and_rejects_late_offer():
    async def run():
        replies = []
        request = InboundDirectRequest("offer", "claim", None, [], lambda *_: None)
        cold = Endpoint()
        endpoint = SwitchableInboundEndpoint(request,
            direct_factory=lambda *_args, **_kwargs: pytest.fail("Deferred PC created during fallback"),
            livekit_factory=lambda _: cold)
        connecting = asyncio.create_task(endpoint.connect())
        await settle()
        assert request.fallback(SimpleNamespace(), lambda *args: replies.append(args))
        await asyncio.wait_for(cold.started.wait(), 1)
        await asyncio.wait_for(connecting, 1)
        assert replies == [(None, None)]
        assert not request.supply_sdp("late", [], lambda *_: pytest.fail("Fallback undone by late SDP"))
        cold.media_ready = True
        assert endpoint.seal()
        await endpoint.close()
        assert cold.closed

    asyncio.run(run())


def test_close_during_deferred_answer_rejects_pending_retries_and_late_success():
    async def run():
        replies = []
        request = InboundDirectRequest("offer", "claim", None, [], lambda *_: None)
        direct = Endpoint(asyncio.Event())
        endpoint = SwitchableInboundEndpoint(request, direct_factory=lambda *_args, **_kwargs: direct)
        assert request.supply_sdp("offer", [], lambda *args: replies.append(args))
        assert request.supply_sdp("offer", [], lambda *args: replies.append(args))
        connecting = asyncio.create_task(endpoint.connect())
        await direct.started.wait()
        await endpoint.close()
        direct.gate.set()
        await asyncio.gather(connecting, return_exceptions=True)
        request.reply("late", None)
        assert replies == [(None, "MEDIA_ENDED"), (None, "MEDIA_ENDED")]
        assert direct.closed and not endpoint.media_ready

    asyncio.run(run())
