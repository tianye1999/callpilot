"""Pure state-machine spike for inbound AI-to-owner takeover."""

from __future__ import annotations

import threading

import pytest

from agentcall.takeover_coordinator import (
    ClaimFence,
    InboundTakeoverCoordinator,
    MediaOwner,
    TakeoverAction,
    TakeoverOffer,
    TakeoverRejection,
    TakeoverState,
)


class FakeMediaRouter:
    """In-memory router proving that every ownership change is explicit."""

    def __init__(self) -> None:
        self.owner = MediaOwner.AI
        self.transitions = [MediaOwner.AI]

    def switch_owner(self, owner: MediaOwner) -> None:
        self.owner = owner
        self.transitions.append(owner)


class FakeClock:
    def __init__(self, now: float = 100.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def _offer(
    device_id: str = "device-primary",
    *,
    offer_id: str = "offer-primary",
    nonce: str = "nonce-primary",
    call_id: str = "call-current",
    generation: int = 7,
    expires_at: float = 130.0,
) -> TakeoverOffer:
    return TakeoverOffer(
        offer_id=offer_id,
        nonce=nonce,
        call_id=call_id,
        generation=generation,
        target_device_id=device_id,
        expires_at=expires_at,
    )


def _coordinator(
    *,
    router: FakeMediaRouter | None = None,
    clock: FakeClock | None = None,
) -> tuple[InboundTakeoverCoordinator, FakeMediaRouter, FakeClock]:
    router = router or FakeMediaRouter()
    clock = clock or FakeClock()
    coordinator = InboundTakeoverCoordinator(
        call_id="call-current",
        generation=7,
        media_router=router,
        clock=clock,
    )
    return coordinator, router, clock


def _claim_primary(
    coordinator: InboundTakeoverCoordinator,
    *,
    claim_id: str = "claim-primary",
    now: float | None = None,
) -> ClaimFence:
    result = coordinator.claim_offer(
        offer_id="offer-primary",
        nonce="nonce-primary",
        claim_id=claim_id,
        device_id="device-primary",
        now=now,
    )
    assert result.accepted
    fence = coordinator.active_fence
    assert fence is not None
    return fence


def _reach_waiting_owner(
    coordinator: InboundTakeoverCoordinator,
    *offers: TakeoverOffer,
) -> None:
    assert coordinator.begin_takeover().accepted
    assert coordinator.wait_for_owner(offers or (_offer(),)).accepted


def _reach_state(
    coordinator: InboundTakeoverCoordinator,
    target: TakeoverState,
) -> None:
    if target is TakeoverState.AI_ACTIVE:
        return
    assert coordinator.begin_takeover().accepted
    if target is TakeoverState.TAKEOVER_PREPARING:
        return
    assert coordinator.wait_for_owner([_offer()]).accepted
    if target is TakeoverState.WAITING_OWNER:
        return
    fence = _claim_primary(coordinator)
    assert coordinator.mark_mobile_media_ready(fence).accepted
    if target is TakeoverState.MOBILE_MEDIA_READY:
        return
    assert coordinator.commit_mobile(fence).accepted
    if target is TakeoverState.MOBILE_ACTIVE:
        return
    assert coordinator.mark_mobile_disconnected(fence).accepted
    assert target is TakeoverState.MOBILE_RECONNECTING


def test_happy_path_covers_state_table_and_disconnect_notice_then_hangup() -> None:
    coordinator, router, _clock = _coordinator()

    assert coordinator.state is TakeoverState.AI_ACTIVE
    assert coordinator.begin_takeover().accepted
    assert coordinator.state is TakeoverState.TAKEOVER_PREPARING
    assert router.owner is MediaOwner.AI

    assert coordinator.wait_for_owner([_offer()]).accepted
    assert coordinator.state is TakeoverState.WAITING_OWNER
    assert router.owner is MediaOwner.HOLD

    fence = _claim_primary(coordinator)
    assert coordinator.mark_mobile_media_ready(fence).accepted
    assert coordinator.state is TakeoverState.MOBILE_MEDIA_READY

    assert coordinator.commit_mobile(fence).accepted
    assert coordinator.state is TakeoverState.MOBILE_ACTIVE
    assert router.owner is MediaOwner.MOBILE

    assert coordinator.mark_mobile_disconnected(fence).accepted
    assert coordinator.state is TakeoverState.MOBILE_RECONNECTING
    assert router.owner is MediaOwner.HOLD

    assert coordinator.mark_mobile_reconnected(fence).accepted
    assert coordinator.state is TakeoverState.MOBILE_ACTIVE
    assert router.owner is MediaOwner.MOBILE

    assert coordinator.mark_mobile_disconnected(fence).accepted
    result = coordinator.expire_mobile_reconnect(fence)

    assert result.accepted
    assert result.action is TakeoverAction.NOTICE_THEN_HANGUP
    assert coordinator.state is TakeoverState.ENDED
    assert coordinator.end_reason == "mobile_reconnect_timeout"
    assert router.owner is MediaOwner.NONE


@pytest.mark.parametrize(
    "target_state",
    [
        TakeoverState.TAKEOVER_PREPARING,
        TakeoverState.WAITING_OWNER,
        TakeoverState.MOBILE_MEDIA_READY,
    ],
)
def test_commit_preparation_failure_rolls_back_to_ai(target_state: TakeoverState) -> None:
    coordinator, router, _clock = _coordinator()
    assert coordinator.begin_takeover().accepted
    if target_state is not TakeoverState.TAKEOVER_PREPARING:
        assert coordinator.wait_for_owner([_offer()]).accepted
    if target_state is TakeoverState.MOBILE_MEDIA_READY:
        fence = _claim_primary(coordinator)
        assert coordinator.mark_mobile_media_ready(fence).accepted

    result = coordinator.rollback_precommit("media_prepare_failed")

    assert result.accepted
    assert coordinator.state is TakeoverState.AI_ACTIVE
    assert coordinator.active_fence is None
    assert coordinator.last_reason == "media_prepare_failed"
    assert router.owner is MediaOwner.AI


@pytest.mark.parametrize(
    "source_state",
    [
        TakeoverState.AI_ACTIVE,
        TakeoverState.TAKEOVER_PREPARING,
        TakeoverState.WAITING_OWNER,
        TakeoverState.MOBILE_MEDIA_READY,
        TakeoverState.MOBILE_ACTIVE,
        TakeoverState.MOBILE_RECONNECTING,
    ],
)
def test_physical_hangup_ends_call_from_every_live_state(
    source_state: TakeoverState,
) -> None:
    coordinator, router, _clock = _coordinator()
    _reach_state(coordinator, source_state)

    result = coordinator.end_call()

    assert result.accepted
    assert coordinator.state is TakeoverState.ENDED
    assert coordinator.end_reason == "physical_call_ended"
    assert coordinator.active_fence is None
    assert router.owner is MediaOwner.NONE

    repeated = coordinator.end_call("late_duplicate")
    assert repeated.accepted and repeated.idempotent
    assert coordinator.end_reason == "physical_call_ended"


@pytest.mark.parametrize(
    ("fence", "expected_code"),
    [
        (
            ClaimFence("call-old", 7, "claim-primary", "device-primary"),
            TakeoverRejection.STALE_CALL,
        ),
        (
            ClaimFence("call-current", 6, "claim-primary", "device-primary"),
            TakeoverRejection.STALE_GENERATION,
        ),
        (
            ClaimFence("call-current", 7, "claim-old", "device-primary"),
            TakeoverRejection.STALE_CLAIM,
        ),
        (
            ClaimFence("call-current", 7, "claim-primary", "device-other"),
            TakeoverRejection.DEVICE_MISMATCH,
        ),
    ],
)
def test_stale_fence_is_rejected_without_state_or_router_change(
    fence: ClaimFence,
    expected_code: TakeoverRejection,
) -> None:
    coordinator, router, _clock = _coordinator()
    _reach_waiting_owner(coordinator)
    _claim_primary(coordinator)
    before_transitions = list(router.transitions)

    result = coordinator.mark_mobile_media_ready(fence)

    assert not result.accepted
    assert result.code is expected_code
    assert coordinator.state is TakeoverState.WAITING_OWNER
    assert router.transitions == before_transitions


def test_double_claim_is_atomic_and_only_one_device_wins() -> None:
    coordinator, _router, _clock = _coordinator()
    _reach_waiting_owner(
        coordinator,
        _offer(),
        _offer(
            "device-secondary",
            offer_id="offer-secondary",
            nonce="nonce-secondary",
        ),
    )
    barrier = threading.Barrier(3)
    results = []

    def claim(offer_id: str, nonce: str, claim_id: str, device_id: str) -> None:
        barrier.wait()
        results.append(
            coordinator.claim_offer(
                offer_id=offer_id,
                nonce=nonce,
                claim_id=claim_id,
                device_id=device_id,
            )
        )

    threads = [
        threading.Thread(
            target=claim,
            args=(
                "offer-primary",
                "nonce-primary",
                "claim-primary",
                "device-primary",
            ),
        ),
        threading.Thread(
            target=claim,
            args=(
                "offer-secondary",
                "nonce-secondary",
                "claim-secondary",
                "device-secondary",
            ),
        ),
    ]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(timeout=1.0)

    assert all(not thread.is_alive() for thread in threads)
    assert sum(result.accepted for result in results) == 1
    rejected = next(result for result in results if not result.accepted)
    assert rejected.code is TakeoverRejection.CLAIM_CONFLICT
    assert coordinator.active_fence is not None
    assert coordinator.active_fence.claim_id in {"claim-primary", "claim-secondary"}


def test_expired_offer_is_rejected_then_wait_timeout_rolls_back_to_ai() -> None:
    clock = FakeClock(100.0)
    coordinator, router, _clock = _coordinator(clock=clock)
    _reach_waiting_owner(coordinator, _offer(expires_at=105.0))
    clock.now = 106.0

    claim = coordinator.claim_offer(
        offer_id="offer-primary",
        nonce="nonce-primary",
        claim_id="claim-primary",
        device_id="device-primary",
    )
    timeout = coordinator.expire_waiting_owner()

    assert not claim.accepted
    assert claim.code is TakeoverRejection.OFFER_EXPIRED
    assert timeout.accepted
    assert coordinator.state is TakeoverState.AI_ACTIVE
    assert coordinator.last_reason == "owner_offer_timeout"
    assert router.owner is MediaOwner.AI


def test_offer_nonce_and_target_device_are_fail_closed() -> None:
    coordinator, _router, _clock = _coordinator()
    _reach_waiting_owner(coordinator)

    wrong_nonce = coordinator.claim_offer(
        offer_id="offer-primary",
        nonce="nonce-wrong",
        claim_id="claim-primary",
        device_id="device-primary",
    )
    wrong_device = coordinator.claim_offer(
        offer_id="offer-primary",
        nonce="nonce-primary",
        claim_id="claim-primary",
        device_id="device-secondary",
    )

    assert not wrong_nonce.accepted
    assert wrong_nonce.code is TakeoverRejection.OFFER_SCOPE_MISMATCH
    assert not wrong_device.accepted
    assert wrong_device.code is TakeoverRejection.OFFER_SCOPE_MISMATCH
    assert coordinator.active_fence is None


def test_duplicate_offer_id_is_rejected_fail_closed() -> None:
    coordinator, router, _clock = _coordinator()
    assert coordinator.begin_takeover().accepted

    result = coordinator.wait_for_owner(
        [
            _offer(),
            _offer(
                "device-secondary",
                nonce="nonce-secondary",
            ),
        ]
    )

    assert not result.accepted
    assert result.code is TakeoverRejection.DUPLICATE_OFFER
    assert coordinator.state is TakeoverState.TAKEOVER_PREPARING
    assert router.transitions == [MediaOwner.AI]


def test_exact_claim_ready_and_commit_retries_are_idempotent() -> None:
    coordinator, router, _clock = _coordinator()
    _reach_waiting_owner(coordinator)

    first_claim = coordinator.claim_offer(
        offer_id="offer-primary",
        nonce="nonce-primary",
        claim_id="claim-primary",
        device_id="device-primary",
    )
    repeated_claim = coordinator.claim_offer(
        offer_id="offer-primary",
        nonce="nonce-primary",
        claim_id="claim-primary",
        device_id="device-primary",
    )
    fence = coordinator.active_fence
    assert fence is not None
    first_ready = coordinator.mark_mobile_media_ready(fence)
    repeated_ready = coordinator.mark_mobile_media_ready(fence)
    first_commit = coordinator.commit_mobile(fence)
    repeated_commit = coordinator.commit_mobile(fence)

    assert first_claim.accepted and not first_claim.idempotent
    assert repeated_claim.accepted and repeated_claim.idempotent
    assert first_ready.accepted and not first_ready.idempotent
    assert repeated_ready.accepted and repeated_ready.idempotent
    assert first_commit.accepted and not first_commit.idempotent
    assert repeated_commit.accepted and repeated_commit.idempotent
    assert router.transitions.count(MediaOwner.MOBILE) == 1


def test_claim_retry_must_repeat_nonce_exactly() -> None:
    coordinator, _router, _clock = _coordinator()
    _reach_waiting_owner(coordinator)
    _claim_primary(coordinator)

    result = coordinator.claim_offer(
        offer_id="offer-primary",
        nonce="nonce-mutated",
        claim_id="claim-primary",
        device_id="device-primary",
    )

    assert not result.accepted
    assert result.code is TakeoverRejection.OFFER_SCOPE_MISMATCH


def test_invalid_state_transition_is_rejected_without_side_effect() -> None:
    coordinator, router, _clock = _coordinator()
    fence = ClaimFence("call-current", 7, "claim-primary", "device-primary")

    result = coordinator.commit_mobile(fence)

    assert not result.accepted
    assert result.code is TakeoverRejection.INVALID_STATE
    assert coordinator.state is TakeoverState.AI_ACTIVE
    assert router.transitions == [MediaOwner.AI]
