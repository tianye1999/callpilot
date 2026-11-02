import asyncio
from types import SimpleNamespace

import pytest

from agentcall.inbound_direct import InboundDirectRequest, SwitchableInboundEndpoint


def test_direct_answer_and_fallback_share_one_claim_and_seal_rejects_late_switch():
    async def run():
        replies = []
        request = InboundDirectRequest("offer", "claim", "sdp", [], lambda sdp, error: replies.append((sdp, error)))
        direct = FakeEndpoint("answer")
        cold = FakeEndpoint(None)
        endpoint = SwitchableInboundEndpoint(request, direct_factory=lambda *_, **__: direct,
                                             livekit_factory=lambda _: cold)
        await endpoint.connect()
        assert replies == [("answer", None)]
        assert not endpoint.media_ready
        fallback = []
        assert request.fallback(SimpleNamespace(), lambda sdp, error: fallback.append(error))
        assert not request.fallback(SimpleNamespace(), lambda *_: None)
        for _ in range(20):
            if fallback:
                break
            await asyncio.sleep(0)
        assert fallback == [None]
        assert direct.closed
        cold.media_ready = True
        assert endpoint.media_ready
        assert endpoint.seal()
        assert not request.fallback(SimpleNamespace(), lambda *_: None)
        await endpoint.close()
        assert cold.closed

    asyncio.run(run())


def test_close_rejects_late_answer_and_fallback():
    async def run():
        replies = []
        request = InboundDirectRequest("offer", "claim", "sdp", [], lambda sdp, error: replies.append((sdp, error)))
        gate = asyncio.Event()
        direct = FakeEndpoint("late", gate)
        endpoint = SwitchableInboundEndpoint(request, direct_factory=lambda *_, **__: direct)
        connecting = asyncio.create_task(endpoint.connect())
        await direct.started.wait()
        await endpoint.close()
        gate.set()
        await asyncio.gather(connecting, return_exceptions=True)
        assert not any(sdp == "late" for sdp, _ in replies)
        assert not request.fallback(SimpleNamespace(), lambda *_: None)
        assert not endpoint.media_ready

    asyncio.run(run())


class FakeEndpoint:
    def __init__(self, answer, gate=None):
        self.answer = {"sdp": answer}
        self.gate = gate
        self.closed = False
        self.media_ready = False
        self.browser_connected = True
        self.started = asyncio.Event()

    async def connect(self):
        self.started.set()
        if self.gate:
            await self.gate.wait()

    async def close(self):
        self.closed = True


def test_close_during_fallback_finishes_retiring_transport_before_releasing_owner():
    async def run():
        class RetiringEndpoint(FakeEndpoint):
            def __init__(self):
                super().__init__("answer")
                self.close_started = asyncio.Event()
                self.allow_close = asyncio.Event()

            async def close(self):
                self.close_started.set()
                await self.allow_close.wait()
                self.closed = True

        direct = RetiringEndpoint()
        request = InboundDirectRequest("offer", "claim", "sdp", [], lambda *_: None)
        endpoint = SwitchableInboundEndpoint(request, direct_factory=lambda *_, **__: direct,
                                             livekit_factory=lambda _: FakeEndpoint(None))
        await endpoint.connect()
        assert request.fallback(SimpleNamespace(), lambda *_: None)
        await direct.close_started.wait()
        closing = asyncio.create_task(endpoint.close())
        for _ in range(20):
            await asyncio.sleep(0)
        direct.allow_close.set()
        await asyncio.wait_for(closing, 1)
        assert direct.closed, "Closing the wrapper must not lose its retiring transport"
        assert not endpoint.browser_connected and not endpoint.media_ready

    asyncio.run(run())


def test_fallback_progresses_while_retirement_blocks_and_cancelled_close_keeps_cleanup_owned():
    async def run():
        class CountedEndpoint(FakeEndpoint):
            def __init__(self, answer, close_gate=None):
                super().__init__(answer)
                self.close_gate = close_gate
                self.close_calls = 0

            async def close(self):
                self.close_calls += 1
                if self.close_gate is not None:
                    await self.close_gate.wait()
                self.closed = True

        gate = asyncio.Event()
        direct, cold = CountedEndpoint("answer", gate), CountedEndpoint(None)
        request = InboundDirectRequest("offer", "claim", "sdp", [], lambda *_: None)
        endpoint = SwitchableInboundEndpoint(request, direct_factory=lambda *_, **__: direct,
                                             livekit_factory=lambda _: cold)
        await endpoint.connect()
        assert request.fallback(SimpleNamespace(), lambda *_: None)
        await asyncio.wait_for(cold.started.wait(), 1)
        assert not direct.closed, "Fallback must not wait for old native teardown"
        first, second = asyncio.create_task(endpoint.close()), asyncio.create_task(endpoint.close())
        for _ in range(5):
            await asyncio.sleep(0)
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)
        assert not second.done(), "One caller cancelling must not abandon shared cleanup"
        gate.set()
        await asyncio.wait_for(second, 1)
        await endpoint.close()
        assert direct.closed and cold.closed
        assert direct.close_calls == cold.close_calls == 1

    asyncio.run(run())


def fallback_session(**overrides):
    fields = dict(room_name="room", browser_identity="phone", edge_identity="edge",
                  livekit_url="wss://fixture.invalid", edge_token="first-token")
    fields.update(overrides)
    return SimpleNamespace(**fields)


def test_same_claim_fallback_retries_join_one_switch_and_replay_success():
    replies, wakes = [], []
    request = InboundDirectRequest("offer", "claim", "sdp", [], lambda *_: None)
    request.listen(lambda: wakes.append(True))
    first = fallback_session()
    assert request.fallback(first, lambda *value: replies.append(("first", value)))
    for index in range(4):
        assert request.fallback(fallback_session(edge_token="refreshed-token"),
                                lambda *value, index=index: replies.append((index, value)))
    assert not request.fallback(fallback_session(), lambda *_: pytest.fail("Over-limit callback retained"))
    assert wakes == [True], "Retries must not create another transport switch"
    pending = request.take_fallback()
    assert pending is not None and pending[0] is first
    assert request.take_fallback() is None
    assert request.switched()
    pending[1](None, None)
    assert sorted(str(key) for key, _ in replies) == ["0", "1", "2", "3", "first"]
    assert all(value == (None, None) for _, value in replies)
    assert request.fallback(fallback_session(), lambda *value: replies.append(("retry", value)))
    assert replies[-1] == ("retry", (None, None))
    assert wakes == [True]
    assert request.seal()
    assert not request.fallback(fallback_session(), lambda *_: pytest.fail("Committed retry accepted"))


@pytest.mark.parametrize("field", ["room_name", "browser_identity", "edge_identity", "livekit_url"])
def test_fallback_different_transport_identity_cannot_join_or_replace_switch(field):
    request = InboundDirectRequest("offer", "claim", "sdp", [], lambda *_: None)
    assert request.fallback(fallback_session(), lambda *_: None)
    other = fallback_session(**{field: "different"})
    assert not request.fallback(other, lambda *_: pytest.fail("Conflicting fallback accepted"))
    assert request.switched()
    assert not request.fallback(other, lambda *_: pytest.fail("Conflicting fallback replayed"))


@pytest.mark.parametrize("terminal,error", [("close", "MEDIA_ENDED"),
                                            ("switch_failed", "LIVEKIT_SETUP_FAILED")])
def test_fallback_retry_waiters_receive_terminal_failure_once(terminal, error):
    replies = []
    request = InboundDirectRequest("offer", "claim", "sdp", [], lambda *_: None)
    assert request.fallback(fallback_session(), lambda *_: None)
    assert request.fallback(fallback_session(), lambda *value: replies.append(value))
    getattr(request, terminal)()
    assert replies == [(None, error)]
    getattr(request, terminal)()
    assert replies == [(None, error)]
    assert not request.fallback(fallback_session(), lambda *_: pytest.fail("Terminal retry accepted"))
