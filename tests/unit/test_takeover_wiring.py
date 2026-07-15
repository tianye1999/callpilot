"""Offline Edge wiring for the #95 offer request and claimed media injection."""

from __future__ import annotations

from fakes import FakeModem

from agentcall.call_agent import CallAgentService
from agentcall.remote_dialer import (
    IssuedLiveKitSession,
    RemoteDialerInvite,
)
from agentcall.takeover_coordinator import (
    ClaimFence,
    InboundTakeoverSession,
    TakeoverOffer,
    TakeoverRejection,
    TakeoverState,
)


def _service() -> CallAgentService:
    return CallAgentService(
        modem_port="unused",
        audio_keyword="unused",
        provider="openai",
        modem=FakeModem(),  # type: ignore[arg-type]
    )


def _prepare_inbound(service: CallAgentService, generation: int = 7) -> None:
    session = service.session
    session._active = True
    session._outbound_number = None
    session._session_generation = generation
    session._initialize_takeover_context("inbound")


def _issued(expires_at: float) -> IssuedLiveKitSession:
    return IssuedLiveKitSession(
        invite=RemoteDialerInvite(
            session_id="session_takeover_1234",
            url="",
            expires_at=expires_at,
        ),
        room_name="callpilot-takeover-room",
        browser_identity="web-device-primary",
        edge_identity="edgepart-takeover",
        browser_token="",
        edge_token="edge-token",
        livekit_url="wss://livekit.example",
    )


def _claim_for_request(request) -> InboundTakeoverSession:
    device_id = "web-device-primary"
    offer = TakeoverOffer(
        offer_id=request.offer_id,
        nonce=request.nonce,
        call_id=request.call_id,
        generation=request.generation,
        target_device_id=device_id,
        expires_at=request.expires_at,
    )
    fence = ClaimFence(
        call_id=request.call_id,
        generation=request.generation,
        claim_id="claim_primary_1234",
        device_id=device_id,
    )
    return InboundTakeoverSession(
        offer=offer,
        fence=fence,
        issued=_issued(request.expires_at),
    )


def _tool_names(service: CallAgentService, direction: str) -> set[str]:
    registry = service.session._build_tools(direction)
    return {spec["function"]["name"] for spec in registry.specs()}


def test_takeover_tool_is_registered_only_for_enabled_inbound(monkeypatch) -> None:
    service = _service()
    _prepare_inbound(service)

    monkeypatch.setenv("INBOUND_TAKEOVER_ENABLED", "false")
    assert "request_owner_takeover" not in _tool_names(service, "inbound")

    monkeypatch.setenv("INBOUND_TAKEOVER_ENABLED", "true")
    assert "request_owner_takeover" in _tool_names(service, "inbound")
    assert "request_owner_takeover" not in _tool_names(service, "outbound")


def test_tool_request_is_opaque_bounded_and_double_gated(monkeypatch) -> None:
    monkeypatch.setenv("INBOUND_TAKEOVER_ENABLED", "true")
    monkeypatch.setenv(
        "INBOUND_TAKEOVER_PREFERENCE",
        "快递也转给我，不要把这段偏好发到云端。",
    )
    service = _service()
    _prepare_inbound(service)
    registry = service.session._build_tools("inbound")

    monkeypatch.setenv("INBOUND_TAKEOVER_ENABLED", "false")
    disabled = registry.dispatch("request_owner_takeover", {})
    assert disabled["success"] is False
    assert disabled["code"] == "TAKEOVER_DISABLED"
    assert service.next_inbound_takeover_offer() is None

    monkeypatch.setenv("INBOUND_TAKEOVER_ENABLED", "true")
    accepted = registry.dispatch("request_owner_takeover", {})
    request = service.next_inbound_takeover_offer()

    assert accepted["success"] is True
    assert request is not None
    assert request.offer_id.startswith("offer_")
    assert request.call_id.startswith("call_")
    assert request.generation == 7
    assert request.expires_at > request.created_at
    serialized = repr(request)
    assert "138" not in serialized
    assert "快递" not in serialized
    assert service.session.takeover_state is TakeoverState.TAKEOVER_PREPARING

    repeated = registry.dispatch("request_owner_takeover", {})
    assert repeated["success"] is False
    assert repeated["code"] == "TAKEOVER_NOT_AI_ACTIVE"
    assert service.next_inbound_takeover_offer() is None


def test_claimed_session_injection_validates_fence_and_queues_media(monkeypatch) -> None:
    monkeypatch.setenv("INBOUND_TAKEOVER_ENABLED", "true")
    service = _service()
    _prepare_inbound(service)
    registry = service.session._build_tools("inbound")
    assert registry.dispatch("request_owner_takeover", {})["success"] is True
    request = service.next_inbound_takeover_offer()
    assert request is not None

    stale_offer = TakeoverOffer(
        offer_id=request.offer_id,
        nonce=request.nonce,
        call_id=request.call_id,
        generation=request.generation + 1,
        target_device_id="web-device-primary",
        expires_at=request.expires_at,
    )
    stale_session = InboundTakeoverSession(
        offer=stale_offer,
        fence=ClaimFence(
            request.call_id,
            request.generation + 1,
            "claim_stale_1234",
            "web-device-primary",
        ),
        issued=_issued(request.expires_at),
    )

    stale = service.provide_inbound_takeover_session(stale_session)
    assert not stale.accepted
    assert stale.code is TakeoverRejection.STALE_GENERATION
    assert service.session.takeover_state is TakeoverState.TAKEOVER_PREPARING
    assert service.take_inbound_takeover_session() is None

    claimed_session = _claim_for_request(request)
    accepted = service.provide_inbound_takeover_session(claimed_session)

    assert accepted.accepted
    assert service.session.takeover_state is TakeoverState.WAITING_OWNER
    assert service.session.takeover_fence == claimed_session.fence
    assert service.take_inbound_takeover_session() is claimed_session


def test_cloud_claim_adapter_rebuilds_offer_from_edge_local_expiry(monkeypatch) -> None:
    monkeypatch.setenv("INBOUND_TAKEOVER_ENABLED", "true")
    service = _service()
    _prepare_inbound(service)
    registry = service.session._build_tools("inbound")
    assert registry.dispatch("request_owner_takeover", {})["success"] is True
    request = service.next_inbound_takeover_offer()
    assert request is not None

    result = service.accept_inbound_takeover_claim(
        offer_id=request.offer_id,
        call_id=request.call_id,
        claim_id="claim_cloud_1234",
        generation=request.generation,
        nonce=request.nonce,
        issued=_issued(request.expires_at + 300),
    )

    assert result.accepted
    claimed = service.take_inbound_takeover_session()
    assert claimed is not None
    assert claimed.offer.expires_at == request.expires_at
    assert claimed.offer.target_device_id == claimed.issued.browser_identity
    assert claimed.fence.device_id == claimed.issued.browser_identity


def test_offer_request_does_not_publish_nonce_or_preference(monkeypatch) -> None:
    monkeypatch.setenv("INBOUND_TAKEOVER_ENABLED", "true")
    monkeypatch.setenv("INBOUND_TAKEOVER_PREFERENCE", "快递也转接")
    service = _service()
    _prepare_inbound(service)

    assert service.session._build_tools("inbound").dispatch(
        "request_owner_takeover", {}
    )["success"] is True
    request = service.next_inbound_takeover_offer()
    assert request is not None

    history = service.hub.history() if service.hub is not None else []
    assert request.nonce not in repr(history)
    assert "快递也转接" not in repr(history)
