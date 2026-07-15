"""Pure inbound AI-to-owner takeover state machine for issue #95 Phase A."""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from .remote_dialer import IssuedLiveKitSession


class TakeoverState(StrEnum):
    AI_ACTIVE = "AI_ACTIVE"
    TAKEOVER_PREPARING = "TAKEOVER_PREPARING"
    WAITING_OWNER = "WAITING_OWNER"
    MOBILE_MEDIA_READY = "MOBILE_MEDIA_READY"
    MOBILE_ACTIVE = "MOBILE_ACTIVE"
    MOBILE_RECONNECTING = "MOBILE_RECONNECTING"
    ENDED = "ENDED"


class MediaOwner(StrEnum):
    AI = "AI"
    HOLD = "HOLD"
    MOBILE = "MOBILE"
    NONE = "NONE"


class TakeoverRejection(StrEnum):
    INVALID_STATE = "INVALID_STATE"
    TAKEOVER_DISABLED = "TAKEOVER_DISABLED"
    STALE_CALL = "STALE_CALL"
    STALE_GENERATION = "STALE_GENERATION"
    STALE_CLAIM = "STALE_CLAIM"
    DEVICE_MISMATCH = "DEVICE_MISMATCH"
    OFFER_NOT_FOUND = "OFFER_NOT_FOUND"
    DUPLICATE_OFFER = "DUPLICATE_OFFER"
    OFFER_SCOPE_MISMATCH = "OFFER_SCOPE_MISMATCH"
    OFFER_EXPIRED = "OFFER_EXPIRED"
    OFFER_NOT_EXPIRED = "OFFER_NOT_EXPIRED"
    CLAIM_CONFLICT = "CLAIM_CONFLICT"
    NO_ACTIVE_CLAIM = "NO_ACTIVE_CLAIM"


class TakeoverAction(StrEnum):
    NONE = "NONE"
    NOTICE_THEN_HANGUP = "NOTICE_THEN_HANGUP"


class MediaRouter(Protocol):
    """Non-blocking, call-local owner switch implemented by the future router."""

    @property
    def owner(self) -> MediaOwner: ...

    def switch_owner(self, owner: MediaOwner) -> None: ...


def _require_identifier(value: str, label: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")


@dataclass(frozen=True)
class TakeoverOffer:
    offer_id: str
    nonce: str
    call_id: str
    generation: int
    target_device_id: str
    expires_at: float

    def __post_init__(self) -> None:
        _require_identifier(self.offer_id, "offer_id")
        _require_identifier(self.nonce, "nonce")
        _require_identifier(self.call_id, "call_id")
        _require_identifier(self.target_device_id, "target_device_id")
        if self.generation < 0:
            raise ValueError("generation must be non-negative")
        if not math.isfinite(self.expires_at):
            raise ValueError("expires_at must be finite")


@dataclass(frozen=True)
class ClaimFence:
    call_id: str
    generation: int
    claim_id: str
    device_id: str

    def __post_init__(self) -> None:
        _require_identifier(self.call_id, "call_id")
        _require_identifier(self.claim_id, "claim_id")
        _require_identifier(self.device_id, "device_id")
        if self.generation < 0:
            raise ValueError("generation must be non-negative")


@dataclass(frozen=True)
class InboundTakeoverOfferRequest:
    offer_id: str
    nonce: str
    call_id: str
    generation: int
    created_at: float
    expires_at: float

    def __post_init__(self) -> None:
        _require_identifier(self.offer_id, "offer_id")
        _require_identifier(self.nonce, "nonce")
        _require_identifier(self.call_id, "call_id")
        if not self.offer_id.startswith("offer_"):
            raise ValueError("offer_id must use the offer_ prefix")
        if not self.call_id.startswith("call_"):
            raise ValueError("call_id must use the call_ prefix")
        if not 16 <= len(self.nonce) <= 128:
            raise ValueError("nonce must contain 16-128 characters")
        if self.generation < 0:
            raise ValueError("generation must be non-negative")
        if not math.isfinite(self.created_at) or not math.isfinite(self.expires_at):
            raise ValueError("offer timestamps must be finite")
        if self.expires_at <= self.created_at:
            raise ValueError("expires_at must be later than created_at")


@dataclass(frozen=True)
class InboundTakeoverRevoke:
    offer_id: str
    call_id: str
    reason: str

    def __post_init__(self) -> None:
        _require_identifier(self.offer_id, "offer_id")
        _require_identifier(self.call_id, "call_id")
        _require_identifier(self.reason, "reason")
        if not self.offer_id.startswith("offer_"):
            raise ValueError("offer_id must use the offer_ prefix")
        if not self.call_id.startswith("call_"):
            raise ValueError("call_id must use the call_ prefix")
        if not self.reason.replace("_", "").isupper():
            raise ValueError("reason must be an uppercase underscore code")


@dataclass(frozen=True)
class InboundTakeoverSession:
    offer: TakeoverOffer
    fence: ClaimFence
    issued: IssuedLiveKitSession

    def __post_init__(self) -> None:
        if self.offer.call_id != self.fence.call_id:
            raise ValueError("offer and fence call_id must match")
        if self.offer.generation != self.fence.generation:
            raise ValueError("offer and fence generation must match")
        if self.offer.target_device_id != self.fence.device_id:
            raise ValueError("offer and fence device identity must match")
        if self.offer.target_device_id != self.issued.browser_identity:
            raise ValueError("claim device must match the issued browser identity")


@dataclass(frozen=True)
class TakeoverResult:
    accepted: bool
    code: TakeoverRejection | None = None
    action: TakeoverAction = TakeoverAction.NONE
    idempotent: bool = False

    @classmethod
    def success(
        cls,
        *,
        action: TakeoverAction = TakeoverAction.NONE,
        idempotent: bool = False,
    ) -> TakeoverResult:
        return cls(True, action=action, idempotent=idempotent)

    @classmethod
    def reject(cls, code: TakeoverRejection) -> TakeoverResult:
        return cls(False, code=code)


_ALLOWED_TRANSITIONS: dict[TakeoverState, frozenset[TakeoverState]] = {
    TakeoverState.AI_ACTIVE: frozenset(
        {TakeoverState.TAKEOVER_PREPARING, TakeoverState.ENDED}
    ),
    TakeoverState.TAKEOVER_PREPARING: frozenset(
        {TakeoverState.WAITING_OWNER, TakeoverState.AI_ACTIVE, TakeoverState.ENDED}
    ),
    TakeoverState.WAITING_OWNER: frozenset(
        {
            TakeoverState.MOBILE_MEDIA_READY,
            TakeoverState.AI_ACTIVE,
            TakeoverState.ENDED,
        }
    ),
    TakeoverState.MOBILE_MEDIA_READY: frozenset(
        {TakeoverState.MOBILE_ACTIVE, TakeoverState.AI_ACTIVE, TakeoverState.ENDED}
    ),
    TakeoverState.MOBILE_ACTIVE: frozenset(
        {TakeoverState.MOBILE_RECONNECTING, TakeoverState.ENDED}
    ),
    TakeoverState.MOBILE_RECONNECTING: frozenset(
        {TakeoverState.MOBILE_ACTIVE, TakeoverState.ENDED}
    ),
    TakeoverState.ENDED: frozenset(),
}


class InboundTakeoverCoordinator:
    """Own the pure takeover state and reject stale or competing commands.

    The coordinator is deliberately unaware of modem, LiveKit, cloud, or agent
    implementations. The injected router performs only immediate call-local owner
    switches; external protocol adapters remain responsible for parsing messages.
    """

    def __init__(
        self,
        *,
        call_id: str,
        generation: int,
        media_router: MediaRouter,
        clock: Callable[[], float] = time.time,
    ) -> None:
        _require_identifier(call_id, "call_id")
        if generation < 0:
            raise ValueError("generation must be non-negative")
        if media_router.owner is not MediaOwner.AI:
            raise ValueError("media router must start with AI ownership")
        self.call_id = call_id
        self.generation = generation
        self._router = media_router
        self._clock = clock
        self._lock = threading.RLock()
        self._state = TakeoverState.AI_ACTIVE
        self._offers: dict[str, TakeoverOffer] = {}
        self._active_offer_id: str | None = None
        self._active_fence: ClaimFence | None = None
        self._last_reason: str | None = None
        self._end_reason: str | None = None

    @property
    def state(self) -> TakeoverState:
        with self._lock:
            return self._state

    @property
    def active_fence(self) -> ClaimFence | None:
        with self._lock:
            return self._active_fence

    @property
    def last_reason(self) -> str | None:
        with self._lock:
            return self._last_reason

    @property
    def end_reason(self) -> str | None:
        with self._lock:
            return self._end_reason

    def begin_takeover(self) -> TakeoverResult:
        with self._lock:
            if self._state is TakeoverState.TAKEOVER_PREPARING:
                return TakeoverResult.success(idempotent=True)
            if self._state is not TakeoverState.AI_ACTIVE:
                return TakeoverResult.reject(TakeoverRejection.INVALID_STATE)
            self._last_reason = None
            self._transition(TakeoverState.TAKEOVER_PREPARING)
            return TakeoverResult.success()

    def wait_for_owner(self, offers: Iterable[TakeoverOffer]) -> TakeoverResult:
        offers_by_id: dict[str, TakeoverOffer] = {}
        duplicate_offer = False
        for offer in offers:
            if offer.offer_id in offers_by_id:
                duplicate_offer = True
            offers_by_id[offer.offer_id] = offer
        with self._lock:
            if self._state is not TakeoverState.TAKEOVER_PREPARING:
                return TakeoverResult.reject(TakeoverRejection.INVALID_STATE)
            if not offers_by_id:
                return TakeoverResult.reject(TakeoverRejection.OFFER_NOT_FOUND)
            if duplicate_offer:
                return TakeoverResult.reject(TakeoverRejection.DUPLICATE_OFFER)
            if any(
                offer.call_id != self.call_id or offer.generation != self.generation
                for offer in offers_by_id.values()
            ):
                return TakeoverResult.reject(
                    TakeoverRejection.OFFER_SCOPE_MISMATCH
                )
            self._offers = offers_by_id
            self._router.switch_owner(MediaOwner.HOLD)
            self._transition(TakeoverState.WAITING_OWNER)
            return TakeoverResult.success()

    def claim_offer(
        self,
        *,
        offer_id: str,
        nonce: str,
        claim_id: str,
        device_id: str,
        now: float | None = None,
    ) -> TakeoverResult:
        with self._lock:
            if self._state is not TakeoverState.WAITING_OWNER:
                return TakeoverResult.reject(TakeoverRejection.INVALID_STATE)
            offer = self._offers.get(offer_id)
            if offer is None:
                return TakeoverResult.reject(TakeoverRejection.OFFER_NOT_FOUND)
            if offer.nonce != nonce or offer.target_device_id != device_id:
                return TakeoverResult.reject(
                    TakeoverRejection.OFFER_SCOPE_MISMATCH
                )
            repeated_fence = ClaimFence(
                self.call_id, self.generation, claim_id, device_id
            )
            if self._active_fence is not None:
                if (
                    self._active_offer_id == offer_id
                    and self._active_fence == repeated_fence
                ):
                    return TakeoverResult.success(idempotent=True)
                return TakeoverResult.reject(TakeoverRejection.CLAIM_CONFLICT)

            observed_at = self._clock() if now is None else now
            if observed_at >= offer.expires_at:
                return TakeoverResult.reject(TakeoverRejection.OFFER_EXPIRED)
            self._active_offer_id = offer_id
            self._active_fence = repeated_fence
            return TakeoverResult.success()

    def mark_mobile_media_ready(self, fence: ClaimFence) -> TakeoverResult:
        with self._lock:
            if self._state is TakeoverState.MOBILE_MEDIA_READY:
                rejection = self._validate_fence(fence)
                return (
                    TakeoverResult.reject(rejection)
                    if rejection is not None
                    else TakeoverResult.success(idempotent=True)
                )
            if self._state is not TakeoverState.WAITING_OWNER:
                return TakeoverResult.reject(TakeoverRejection.INVALID_STATE)
            rejection = self._validate_fence(fence)
            if rejection is not None:
                return TakeoverResult.reject(rejection)
            self._transition(TakeoverState.MOBILE_MEDIA_READY)
            return TakeoverResult.success()

    def commit_mobile(self, fence: ClaimFence) -> TakeoverResult:
        with self._lock:
            if self._state is TakeoverState.MOBILE_ACTIVE:
                rejection = self._validate_fence(fence)
                return (
                    TakeoverResult.reject(rejection)
                    if rejection is not None
                    else TakeoverResult.success(idempotent=True)
                )
            if self._state is not TakeoverState.MOBILE_MEDIA_READY:
                return TakeoverResult.reject(TakeoverRejection.INVALID_STATE)
            rejection = self._validate_fence(fence)
            if rejection is not None:
                return TakeoverResult.reject(rejection)
            self._router.switch_owner(MediaOwner.MOBILE)
            self._transition(TakeoverState.MOBILE_ACTIVE)
            return TakeoverResult.success()

    def mark_mobile_disconnected(self, fence: ClaimFence) -> TakeoverResult:
        with self._lock:
            if self._state is TakeoverState.MOBILE_RECONNECTING:
                rejection = self._validate_fence(fence)
                return (
                    TakeoverResult.reject(rejection)
                    if rejection is not None
                    else TakeoverResult.success(idempotent=True)
                )
            if self._state is not TakeoverState.MOBILE_ACTIVE:
                return TakeoverResult.reject(TakeoverRejection.INVALID_STATE)
            rejection = self._validate_fence(fence)
            if rejection is not None:
                return TakeoverResult.reject(rejection)
            self._router.switch_owner(MediaOwner.HOLD)
            self._transition(TakeoverState.MOBILE_RECONNECTING)
            return TakeoverResult.success()

    def mark_mobile_reconnected(self, fence: ClaimFence) -> TakeoverResult:
        with self._lock:
            if self._state is TakeoverState.MOBILE_ACTIVE:
                rejection = self._validate_fence(fence)
                return (
                    TakeoverResult.reject(rejection)
                    if rejection is not None
                    else TakeoverResult.success(idempotent=True)
                )
            if self._state is not TakeoverState.MOBILE_RECONNECTING:
                return TakeoverResult.reject(TakeoverRejection.INVALID_STATE)
            rejection = self._validate_fence(fence)
            if rejection is not None:
                return TakeoverResult.reject(rejection)
            self._router.switch_owner(MediaOwner.MOBILE)
            self._transition(TakeoverState.MOBILE_ACTIVE)
            return TakeoverResult.success()

    def expire_mobile_reconnect(self, fence: ClaimFence) -> TakeoverResult:
        with self._lock:
            if self._state is not TakeoverState.MOBILE_RECONNECTING:
                return TakeoverResult.reject(TakeoverRejection.INVALID_STATE)
            rejection = self._validate_fence(fence)
            if rejection is not None:
                return TakeoverResult.reject(rejection)
            self._end_reason = "mobile_reconnect_timeout"
            self._router.switch_owner(MediaOwner.NONE)
            self._transition(TakeoverState.ENDED)
            self._clear_claim()
            return TakeoverResult.success(
                action=TakeoverAction.NOTICE_THEN_HANGUP
            )

    def expire_waiting_owner(self, *, now: float | None = None) -> TakeoverResult:
        with self._lock:
            if (
                self._state is not TakeoverState.WAITING_OWNER
                or self._active_fence is not None
            ):
                return TakeoverResult.reject(TakeoverRejection.INVALID_STATE)
            observed_at = self._clock() if now is None else now
            if any(observed_at < offer.expires_at for offer in self._offers.values()):
                return TakeoverResult.reject(
                    TakeoverRejection.OFFER_NOT_EXPIRED
                )
            return self._rollback_locked("owner_offer_timeout")

    def rollback_precommit(self, reason: str) -> TakeoverResult:
        _require_identifier(reason, "reason")
        with self._lock:
            if self._state not in {
                TakeoverState.TAKEOVER_PREPARING,
                TakeoverState.WAITING_OWNER,
                TakeoverState.MOBILE_MEDIA_READY,
            }:
                return TakeoverResult.reject(TakeoverRejection.INVALID_STATE)
            return self._rollback_locked(reason)

    def end_call(self, reason: str = "physical_call_ended") -> TakeoverResult:
        _require_identifier(reason, "reason")
        with self._lock:
            if self._state is TakeoverState.ENDED:
                return TakeoverResult.success(idempotent=True)
            self._end_reason = reason
            self._router.switch_owner(MediaOwner.NONE)
            self._transition(TakeoverState.ENDED)
            self._clear_claim()
            return TakeoverResult.success()

    def _rollback_locked(self, reason: str) -> TakeoverResult:
        self._router.switch_owner(MediaOwner.AI)
        self._transition(TakeoverState.AI_ACTIVE)
        self._last_reason = reason
        self._clear_claim()
        return TakeoverResult.success()

    def _validate_fence(self, fence: ClaimFence) -> TakeoverRejection | None:
        active = self._active_fence
        if active is None:
            return TakeoverRejection.NO_ACTIVE_CLAIM
        if fence.call_id != self.call_id:
            return TakeoverRejection.STALE_CALL
        if fence.generation != self.generation:
            return TakeoverRejection.STALE_GENERATION
        if fence.claim_id != active.claim_id:
            return TakeoverRejection.STALE_CLAIM
        if fence.device_id != active.device_id:
            return TakeoverRejection.DEVICE_MISMATCH
        return None

    def _transition(self, target: TakeoverState) -> None:
        if target not in _ALLOWED_TRANSITIONS[self._state]:
            raise RuntimeError(f"illegal takeover transition: {self._state} -> {target}")
        self._state = target

    def _clear_claim(self) -> None:
        self._offers.clear()
        self._active_offer_id = None
        self._active_fence = None
