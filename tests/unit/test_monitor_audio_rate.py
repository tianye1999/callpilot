"""Live monitor metadata follows the actual output rate, before its PCM."""

from __future__ import annotations

import asyncio
import threading
import time

import pytest
from fakes import FakeAgent, FakeAudioBridge
from test_dtmf_guard_drop_event import SpyRecord, drops, make_session

from agentcall.events import EventHub


class AudioWS:
    def __init__(self):
        self.frames = []
        self.threads = []

    async def send_json(self, data):
        self.threads.append(threading.get_ident())
        self.frames.append(data)

    async def send_bytes(self, data):
        self.threads.append(threading.get_ident())
        self.frames.append(data)


async def drain(hub):
    await asyncio.sleep(0)
    while hub._audio_tasks:
        await asyncio.gather(*list(hub._audio_tasks))
        await asyncio.sleep(0)


def meta(rate):
    return {"type": "meta", "rate": rate, "uplink_rate": 8000}


def test_rate_changes_notify_all_listeners_only_once():
    async def scenario():
        hub = EventHub(asyncio.get_running_loop())
        hub.set_audio_rate(22050)  # No listeners yet; registration sends the current rate.
        listeners = [AudioWS(), AudioWS()]
        for ws in listeners:
            hub.register_audio(ws)
        hub.set_audio_rate(8000)
        hub.set_audio_rate(8000)
        hub.set_audio_rate(0)
        hub.set_audio_rate(-1)
        hub.broadcast_audio(b"pcm")
        await drain(hub)
        for ws in listeners:
            assert ws.frames == [meta(22050), meta(8000), b"\x00pcm"]

    asyncio.run(scenario())


def test_worker_rate_change_meta_cannot_be_overtaken_by_audio():
    async def scenario():
        hub = EventHub(asyncio.get_running_loop())
        entered, release = asyncio.Event(), asyncio.Event()

        class SlowMetaWS(AudioWS):
            async def send_json(self, data):
                if data["rate"] == 8000:
                    entered.set()
                    await release.wait()
                await super().send_json(data)

        ws = SlowMetaWS()
        hub.register_audio(ws)
        await drain(hub)

        def produce():
            hub.set_audio_rate(8000)
            hub.broadcast_audio(b"new-rate")

        await asyncio.to_thread(produce)
        try:
            await asyncio.wait_for(entered.wait(), timeout=1)
            await asyncio.sleep(0)
            assert ws.frames == [meta(24000)]
        finally:
            release.set()
            await drain(hub)
        assert ws.frames == [meta(24000), meta(8000), b"\x00new-rate"]
        assert set(ws.threads) == {threading.get_ident()}

    asyncio.run(scenario())


def test_initial_meta_precedes_audio_and_has_no_subscription_race():
    async def scenario():
        hub = EventHub(asyncio.get_running_loop())
        # A frame queued before registration must not reach the new listener.
        first, second = AudioWS(), AudioWS()
        hub.register_audio(first)
        hub.broadcast_audio(b"old")
        hub.set_audio_rate(8000)
        hub.register_audio(second)
        hub.broadcast_audio(b"new")
        await drain(hub)
        assert first.frames == [meta(24000), b"\x00old", meta(8000), b"\x00new"]
        assert second.frames == [meta(8000), b"\x00new"]

    asyncio.run(scenario())


@pytest.mark.parametrize("guarded", [False, True])
def test_agent_rate_loaded_at_start_updates_hub_and_guard_accounting(guarded):
    async def scenario():
        hub = EventHub(asyncio.get_running_loop())
        session = make_session()
        session.hub = hub
        record = SpyRecord()

        class LoadingAgent(FakeAgent):
            output_rate = 22050

            async def start(self, on_audio_out):
                await super().start(on_audio_out)
                self.output_rate = 8000

        agent, ws = LoadingAgent(), AudioWS()
        handler = session._make_agent_audio_handler(agent, FakeAudioBridge(), record)
        hub.register_audio(ws)
        await drain(hub)
        await agent.start(handler)
        if guarded:
            session._dtmf_guard_until = time.monotonic() + 10
        await asyncio.to_thread(handler, b"\x00\x10" * 800)
        await drain(hub)
        assert hub.audio_rate == session._agent_output_rate == 8000
        assert ws.frames[:2] == [meta(22050), meta(8000)]
        if guarded:
            session._finalize_record(record, "completed", [], "outbound", "test-peer")
            assert drops(record)[0]["duration_ms"] == 100
            assert len(ws.frames) == 2
        else:
            assert ws.frames[2] == b"\x00" + b"\x00\x10" * 800

    asyncio.run(scenario())


def test_constant_24k_agent_sends_no_extra_meta():
    async def scenario():
        hub = EventHub(asyncio.get_running_loop())
        session, ws = make_session(), AudioWS()
        session.hub = hub
        hub.register_audio(ws)
        agent = FakeAgent()
        handler = session._make_agent_audio_handler(agent, FakeAudioBridge(), None)
        await agent.start(handler)
        handler(b"\x00\x10" * 240)
        handler(b"\x00\x10" * 240)
        await drain(hub)
        assert [frame for frame in ws.frames if isinstance(frame, dict)] == [meta(24000)]

    asyncio.run(scenario())


def test_audio_endpoint_sends_initial_meta_once_then_live_updates(monkeypatch):
    from types import SimpleNamespace

    from agentcall.web import server

    async def scenario():
        hub = EventHub(asyncio.get_running_loop())
        finish = asyncio.Event()

        class Socket(AudioWS):
            async def prepare(self, request):
                pass

            def __aiter__(self):
                return self

            async def __anext__(self):
                await finish.wait()
                raise StopAsyncIteration

        ws = Socket()
        monkeypatch.setattr(server.web, "WebSocketResponse", lambda **kwargs: ws)
        request = SimpleNamespace(app={"hub": hub})
        task = asyncio.create_task(server._audio_websocket(request))
        try:
            await asyncio.sleep(0)
            await drain(hub)
            assert ws.frames == [meta(24000)]
            await asyncio.to_thread(hub.set_audio_rate, 8000)
            hub.broadcast_audio(b"pcm")
            await drain(hub)
            assert ws.frames == [meta(24000), meta(8000), b"\x00pcm"]
        finally:
            finish.set()
            assert await task is ws
        assert not hub._audio_clients
        assert not hub._audio_send_locks

    asyncio.run(scenario())
