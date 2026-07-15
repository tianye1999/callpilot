"""Offline B3 media handoff tests: one writer, rollback, and final ownership."""

from __future__ import annotations

import asyncio

from fakes import FakeAgent, FakeAudioBridge, FakeModem

from agentcall.call_agent import CallAgentService
from agentcall.remote_dialer import IssuedLiveKitSession, RemoteDialerInvite
from agentcall.takeover_coordinator import (
    ClaimFence,
    InboundTakeoverSession,
    TakeoverOffer,
    TakeoverState,
)


class FakeEndpoint:
    media_ready = True
    browser_connected = True

    def __init__(self, session) -> None:
        self.session = session
        self.closed = False
        self.connected = False
        self.browser_chunks = [b"mobile-to-caller"]
        self.commands: list[dict] = []
        self.events: list[dict] = []

    async def connect(self) -> None:
        self.connected = True

    async def close(self) -> None:
        self.closed = True

    async def next_command(self, timeout: float):
        return self.commands.pop(0) if self.commands else None

    def take_browser_audio(self, max_chunks: int = 10) -> list[bytes]:
        chunks, self.browser_chunks = self.browser_chunks, []
        self.session._active = False
        return chunks[:max_chunks]

    def push_modem_audio(self, pcm: bytes) -> None:
        pass

    async def send_event(self, event: dict) -> None:
        self.events.append(event)


class FakeRecord:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def log_event(self, event_type: str, **fields) -> None:
        self.events.append({"type": event_type, **fields})


def _service() -> CallAgentService:
    return CallAgentService(
        modem_port="unused",
        audio_keyword="unused",
        provider="openai",
        modem=FakeModem(),  # type: ignore[arg-type]
    )


def _issued(expires_at: float) -> IssuedLiveKitSession:
    return IssuedLiveKitSession(
        invite=RemoteDialerInvite("session_takeover_1234", "", expires_at),
        room_name="callpilot-takeover-room",
        browser_identity="web-device-primary",
        edge_identity="edgepart-takeover",
        browser_token="",
        edge_token="edge-token",
        livekit_url="wss://livekit.example",
    )


def _claimed(service: CallAgentService, monkeypatch) -> InboundTakeoverSession:
    monkeypatch.setenv("INBOUND_TAKEOVER_ENABLED", "true")
    session = service.session
    session._active = True
    session._outbound_number = None
    session._session_generation = 7
    session._initialize_takeover_context("inbound")
    service.session._build_tools("inbound")
    service.session._request_owner_takeover(7)
    request = service.next_inbound_takeover_offer()
    assert request is not None
    offer = TakeoverOffer(
        request.offer_id,
        request.nonce,
        request.call_id,
        request.generation,
        "web-device-primary",
        request.expires_at,
    )
    claimed = InboundTakeoverSession(
        offer,
        ClaimFence(request.call_id, request.generation, "claim_primary_1234", "web-device-primary"),
        _issued(request.expires_at),
    )
    assert service.provide_inbound_takeover_session(claimed).accepted
    return claimed


def test_handoff_stops_old_writer_and_pumps_mobile_then_single_finalizer(monkeypatch) -> None:
    service = _service()
    session = service.session
    claimed = _claimed(service, monkeypatch)
    old_bridge = FakeAudioBridge()
    old_bridge.start()
    new_bridge = FakeAudioBridge()
    new_bridge.feed_uplink(b"caller-to-mobile")
    endpoint = FakeEndpoint(session)
    monkeypatch.setattr(
        "agentcall.call_agent.create_audio_bridge", lambda **_kwargs: new_bridge
    )
    session._takeover_endpoint_factory = lambda _issued: endpoint
    session.modem.connected_flag.set()

    result = asyncio.run(
        session._handoff_to_mobile(
            FakeAgent(), old_bridge, claimed, None, session._session_generation
        )
    )

    assert result is new_bridge
    assert old_bridge.stopped
    assert new_bridge.started
    assert new_bridge.downlink == [b"mobile-to-caller"]
    assert endpoint.connected and endpoint.closed
    assert endpoint.events == [{"type": "status", "status": "connected"}]
    assert session.takeover_state is TakeoverState.MOBILE_ACTIVE
    assert session.modem.calls == []


def test_takeover_hold_is_one_deterministic_line_before_agent_gate(monkeypatch) -> None:
    service = _service()
    session = service.session
    monkeypatch.setenv("INBOUND_TAKEOVER_ENABLED", "true")
    session._active = True
    session._session_generation = 7
    session._initialize_takeover_context("inbound")
    assert session._build_tools("inbound").dispatch(
        "request_owner_takeover", {}
    )["success"]
    agent = FakeAgent()
    bridge = FakeAudioBridge()
    agent._on_audio_out = session._make_agent_audio_handler(agent, bridge, None)

    asyncio.run(session._speak_takeover_hold_if_needed(agent, bridge, 7))

    assert agent.said == ["请稍等，我确认一下，马上帮您转接。"]
    assert bridge.downlink
    assert session._agent_effect_allowed(7) is False


def test_handoff_start_failure_restarts_old_bridge_and_rolls_back(monkeypatch) -> None:
    service = _service()
    session = service.session
    claimed = _claimed(service, monkeypatch)
    old_bridge = FakeAudioBridge()
    old_bridge.start()
    endpoint = FakeEndpoint(session)

    class BrokenBridge(FakeAudioBridge):
        def start(self) -> None:
            raise RuntimeError("new bridge unavailable")

    monkeypatch.setattr(
        "agentcall.call_agent.create_audio_bridge", lambda **_kwargs: BrokenBridge()
    )
    session._takeover_endpoint_factory = lambda _issued: endpoint

    result = asyncio.run(
        session._handoff_to_mobile(
            FakeAgent(), old_bridge, claimed, None, session._session_generation
        )
    )

    assert result is None
    assert old_bridge.stopped and old_bridge.started
    assert session.takeover_state is TakeoverState.AI_ACTIVE
    assert endpoint.closed


def test_takeover_connected_snapshot_repeats_until_call_stops() -> None:
    service = _service()
    session = service.session
    endpoint = FakeEndpoint(session)
    session._active = True
    session.modem.connected_flag.set()

    async def run() -> None:
        task = asyncio.create_task(
            session._takeover_connected_snapshot_loop(endpoint, interval=0.001)
        )
        while len(endpoint.events) < 2:
            await asyncio.sleep(0.001)
        session._active = False
        await asyncio.wait_for(task, timeout=0.1)

    asyncio.run(run())

    assert len(endpoint.events) >= 2
    assert all(
        event == {"type": "status", "status": "connected"}
        for event in endpoint.events
    )


def test_postcommit_media_loss_expires_to_notice_then_hangup(monkeypatch) -> None:
    service = _service()
    session = service.session
    claimed = _claimed(service, monkeypatch)
    coordinator = session._takeover_coordinator
    assert coordinator is not None
    assert coordinator.mark_mobile_media_ready(claimed.fence).accepted
    assert coordinator.commit_mobile(claimed.fence).accepted
    monkeypatch.setenv("REMOTE_DISCONNECT_GRACE_SECONDS", "0")
    endpoint = FakeEndpoint(session)
    endpoint.media_ready = False
    session.modem.connected_flag.set()
    record = FakeRecord()

    asyncio.run(
        session._pump_mobile_media(
            endpoint,
            FakeAudioBridge(),
            record,  # type: ignore[arg-type]
            claimed,
        )
    )

    assert session.takeover_state is TakeoverState.ENDED
    assert coordinator.end_reason == "mobile_reconnect_timeout"
    assert record.events == [{"type": "takeover_notice_then_hangup"}]


def test_owner_hangup_command_ends_without_disconnect_notice(monkeypatch) -> None:
    service = _service()
    session = service.session
    claimed = _claimed(service, monkeypatch)
    coordinator = session._takeover_coordinator
    assert coordinator is not None
    assert coordinator.mark_mobile_media_ready(claimed.fence).accepted
    assert coordinator.commit_mobile(claimed.fence).accepted
    endpoint = FakeEndpoint(session)
    endpoint.browser_chunks = []
    endpoint.commands.append({"type": "hangup"})
    session.modem.connected_flag.set()
    record = FakeRecord()

    asyncio.run(
        asyncio.wait_for(
            session._pump_mobile_media(
                endpoint,
                FakeAudioBridge(),
                record,  # type: ignore[arg-type]
                claimed,
            ),
            timeout=0.2,
        )
    )

    assert session.takeover_state is TakeoverState.ENDED
    assert coordinator.end_reason == "owner_hangup"
    assert record.events == [{"type": "takeover_owner_hangup"}]
    assert session.modem.calls == []
