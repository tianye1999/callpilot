"""Offline B3 media handoff tests: one writer, rollback, and final ownership."""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest
from fakes import FakeAgent, FakeAudioBridge, FakeModem

from agentcall.call_agent import CallAgentService
from agentcall.remote_dialer import IssuedLiveKitSession, RemoteDialerInvite
from agentcall.takeover_coordinator import (
    ClaimFence,
    InboundTakeoverSession,
    TakeoverOffer,
    TakeoverState,
)
from agentcall.triage_judge import TriageVerdict


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
    service = CallAgentService(
        modem_port="unused",
        audio_keyword="unused",
        provider="openai",
        modem=FakeModem(),  # type: ignore[arg-type]
    )
    service.session._takeover_endpoint_rejection = lambda: None
    return service


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


def _claimed(service: CallAgentService, monkeypatch, trigger="agent_tool") -> InboundTakeoverSession:
    monkeypatch.setenv("INBOUND_TAKEOVER_ENABLED", "true")
    session = service.session
    session._active = True
    session._outbound_number = None
    session._session_generation = 7
    session._initialize_takeover_context("inbound")
    service.session._build_tools("inbound")
    if trigger == "triage_judge":
        session._triage_mode = "enforce"
        session._triage_pending = True
        session._triage_results.put_nowait(TriageVerdict(
            category="personal", action="transfer", confidence=0.9,
            reason_code="owner_requested", turn_id=1, call_generation=7,
        ))
        assert asyncio.run(session._consume_triage_results(FakeAgent(), FakeAudioBridge(), 7)) == "transfer"
    else:
        assert service.session._build_tools("inbound").dispatch("request_owner_takeover", {})["success"]
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


@pytest.mark.parametrize("trigger", ["agent_tool", "triage_judge"])
def test_handoff_stops_old_writer_and_pumps_mobile_then_single_finalizer(monkeypatch, trigger) -> None:
    service = _service()
    session = service.session
    claimed = _claimed(service, monkeypatch, trigger)
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


def test_parallel_cancel_after_takeover_stops_replacement_writer(monkeypatch):
    service = _service()
    session = service.session
    session._parallel_agent_connect = True
    claimed = _claimed(service, monkeypatch)
    old_bridge = FakeAudioBridge()
    old_bridge.start()
    new_bridge = FakeAudioBridge()
    endpoint = FakeEndpoint(session)
    session._takeover_endpoint_factory = lambda _issued: endpoint
    monkeypatch.setattr("agentcall.call_agent.create_audio_bridge", lambda **_kw: new_bridge)

    async def run():
        entered = asyncio.Event()

        async def pump(*_args):
            entered.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(session, "_pump_mobile_media", pump)
        task = asyncio.create_task(session._handoff_to_mobile(
            FakeAgent(), old_bridge, claimed, None, session._session_generation,
        ))
        await entered.wait()
        assert session.takeover_state is TakeoverState.MOBILE_ACTIVE
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    assert old_bridge.stopped and new_bridge.stopped
    assert endpoint.closed


def test_takeover_hold_is_one_scenario_instruction_before_agent_gate(monkeypatch) -> None:
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

    assert len(agent.said) == 1
    assert "尚未接通" in agent.said[0]
    assert bridge.downlink
    assert session._agent_effect_allowed(7) is False


@pytest.mark.parametrize("trigger", ["agent_tool", "triage_judge"])
def test_handoff_start_failure_restarts_old_bridge_and_rolls_back(monkeypatch, trigger) -> None:
    service = _service()
    session = service.session
    claimed = _claimed(service, monkeypatch, trigger)
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
    assert session._agent_effect_allowed(7)
    assert not session._triage_terminal and not session._triage_pending
    assert session._takeover_request is None
    assert endpoint.closed


def test_claim_without_media_before_deadline_recovers_and_rejects_late_claim(monkeypatch) -> None:
    service = _service()
    session = service.session
    claimed = _claimed(service, monkeypatch, "triage_judge")
    session._takeover_deadline = time.monotonic() + 0.02
    old_bridge = FakeAudioBridge()
    agent = FakeAgent()
    agent._on_audio_out = session._make_agent_audio_handler(agent, old_bridge, None)

    class BlockedEndpoint(FakeEndpoint):
        async def connect(self) -> None:
            await asyncio.Event().wait()

    endpoint = BlockedEndpoint(session)
    session._takeover_endpoint_factory = lambda _issued: endpoint
    result = asyncio.run(asyncio.wait_for(
        session._handoff_to_mobile(agent, old_bridge, claimed, None, 7), timeout=0.5,
    ))
    assert result is None
    assert session._agent_effect_allowed(7)
    assert session.takeover_state is TakeoverState.AI_ACTIVE
    assert old_bridge.downlink and not old_bridge.stopped
    assert len(agent.said) == 1
    assert endpoint.closed
    assert not service.provide_inbound_takeover_session(claimed).accepted
    assert service.take_inbound_takeover_session() is None
    revoke = service.next_inbound_takeover_revoke()
    assert revoke is not None and revoke.reason == "OWNER_TIMEOUT"
    assert service.modem.calls == []


@pytest.fixture
def windows_monotonic(monkeypatch):
    clock = SimpleNamespace(now=1024.0, resolution=1 / 64)
    original_info = time.get_clock_info

    def clock_info(name):
        info = original_info(name)
        if name != "monotonic":
            return info
        return SimpleNamespace(
            implementation=info.implementation, monotonic=info.monotonic,
            adjustable=info.adjustable, resolution=clock.resolution,
        )

    monkeypatch.setattr(time, "monotonic", lambda: clock.now)
    monkeypatch.setattr(time, "get_clock_info", clock_info)
    return clock


@pytest.mark.parametrize(("budget", "endpoint_timeout", "slow_cancel"), [
    pytest.param("owner", False, False, id="owner-deadline"),
    pytest.param("media", False, False, id="media-deadline"),
    pytest.param("media", False, True, id="media-deadline-slow-cancel"),
    pytest.param("owner", True, False, id="endpoint-timeout"),
])
def test_handoff_connect_timeout_uses_limiting_deadline(
    monkeypatch, windows_monotonic, budget, endpoint_timeout, slow_cancel,
) -> None:
    clock = windows_monotonic
    service = _service()
    session = service.session
    claimed = _claimed(service, monkeypatch, "triage_judge")
    owner_deadline = clock.now + (0.02 if budget == "owner" else 1.0)
    session._takeover_deadline = owner_deadline
    media_timeout = 0.02 if budget == "media" else 1.0
    monkeypatch.setattr("agentcall.call_agent._INBOUND_TAKEOVER_MEDIA_TIMEOUT_SECONDS", media_timeout)
    deadline = min(owner_deadline, clock.now + media_timeout)
    old_bridge = FakeAudioBridge()
    agent = FakeAgent()
    agent._on_audio_out = session._make_agent_audio_handler(agent, old_bridge, None)
    cancelled_at = []

    class BlockedEndpoint(FakeEndpoint):
        async def connect(self) -> None:
            assert asyncio.get_running_loop()._clock_resolution == clock.resolution
            if endpoint_timeout:
                raise TimeoutError("endpoint's own connection failure")
            # One Windows tick leaves now < deadline, inside asyncio's lookahead.
            clock.now += clock.resolution
            try:
                await asyncio.Event().wait()
            finally:
                cancelled_at.append(clock.now)
                if slow_cancel:
                    clock.now = owner_deadline + clock.resolution

    endpoint = BlockedEndpoint(session)
    session._takeover_endpoint_factory = lambda _issued: endpoint

    async def run():
        result = await asyncio.wait_for(
            session._handoff_to_mobile(agent, old_bridge, claimed, None, 7), timeout=0.5,
        )
        assert not [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]
        return result

    assert asyncio.run(run()) is None
    if not endpoint_timeout:
        assert len(cancelled_at) == 1 and cancelled_at[0] < deadline
    assert session.takeover_state is TakeoverState.AI_ACTIVE
    assert session._agent_effect_allowed(7)
    assert old_bridge.downlink and not old_bridge.stopped
    assert len(agent.said) == 1 and endpoint.closed
    assert not service.provide_inbound_takeover_session(claimed).accepted
    assert service.take_inbound_takeover_session() is None
    revoke = service.next_inbound_takeover_revoke()
    expected = "OWNER_TIMEOUT" if budget == "owner" and not endpoint_timeout else "MEDIA_PREPARE_FAILED"
    assert revoke is not None and revoke.reason == expected
    assert service.modem.calls == []


@pytest.mark.parametrize("budget", ["owner", "media"])
def test_expired_handoff_budget_never_starts_endpoint(monkeypatch, windows_monotonic, budget):
    service = _service()
    session = service.session
    claimed = _claimed(service, monkeypatch)
    session._takeover_deadline = windows_monotonic.now + (-1.0 if budget == "owner" else 1.0)
    media_timeout = 0.0 if budget == "media" else 1.0
    monkeypatch.setattr("agentcall.call_agent._INBOUND_TAKEOVER_MEDIA_TIMEOUT_SECONDS", media_timeout)
    endpoint = FakeEndpoint(session)
    session._takeover_endpoint_factory = lambda _issued: endpoint

    assert asyncio.run(session._handoff_to_mobile(FakeAgent(), FakeAudioBridge(), claimed, None, 7)) is None
    assert not endpoint.connected and endpoint.closed
    assert session.takeover_state is TakeoverState.AI_ACTIVE
    revoke = service.next_inbound_takeover_revoke()
    expected = "OWNER_TIMEOUT" if budget == "owner" else "MEDIA_PREPARE_FAILED"
    assert revoke is not None and revoke.reason == expected
    assert service.modem.calls == []


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
