"""One fenced claim, one PCM bridge, and at most one precommit transport fallback."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from .call_timing import CallTiming, attach_endpoint_timing

if TYPE_CHECKING:
    from .remote_dialer import IssuedLiveKitSession

Reply = Callable[[str | None, str | None], None]
DIRECT_MEDIA_LEASE_SECONDS = 30.0


class InboundDirectRequest:
    def __init__(self, offer_id: str, claim_id: str, sdp_offer: str | None,
                 ice_servers: list[dict[str, Any]], reply: Reply) -> None:
        self.offer_id, self.claim_id = offer_id, claim_id
        self.sdp_offer, self.ice_servers = sdp_offer, ice_servers
        self._reply = reply
        self._lock = threading.Lock()
        self._state = "preparing"
        self._answered = False
        self._pending: tuple[IssuedLiveKitSession, Reply] | None = None
        self._fallback_key: tuple[Any, ...] | None = None
        self._fallback_waiters: list[Reply] = []
        self._wake: Callable[[], None] | None = None
        self._media_wake: Callable[[], None] | None = None
        self._sdp_waiters: list[Reply] = []
        self._media_answer: tuple[str | None, str | None] | None = None

    def supply_sdp(self, sdp: str, ice_servers: list[dict[str, Any]], reply: Reply) -> bool:
        with self._lock:
            if self._state != "preparing" or (self.sdp_offer is not None and self.sdp_offer != sdp):
                return False
            answer = self._media_answer
            if self.sdp_offer is None:
                self.sdp_offer, self.ice_servers, self._reply = sdp, ice_servers, reply
            elif answer is None:
                if len(self._sdp_waiters) >= 4:
                    return False
                self._sdp_waiters.append(reply)
            wake = self._media_wake
        if answer is not None:
            reply(*answer)
        if wake is not None:
            wake()
        return True

    def listen_media(self, wake: Callable[[], None]) -> None:
        with self._lock:
            self._media_wake = wake
            ready = self.sdp_offer is not None or self._state != "preparing"
        if ready:
            wake()

    def media_offer(self) -> tuple[str, list[dict[str, Any]]] | None:
        with self._lock:
            if self._state != "preparing" or self.sdp_offer is None:
                return None
            return self.sdp_offer, self.ice_servers

    def expire_initial_sdp_wait(self) -> bool:
        """Fence the silence deadline atomically against SDP or fallback arrival."""
        with self._lock:
            if self.sdp_offer is not None or self._state != "preparing":
                return False
            self._state = "expired"
            return True

    def fail_no_phone_audio(self) -> bool:
        with self._lock:
            if self._state != "preparing":
                return False
            self._state = "direct_failed"
            return True

    def reply(self, sdp: str | None, error: str | None) -> None:
        with self._lock:
            if self._answered:
                return
            self._answered = True
            if self._state == "closed":
                sdp, error = None, "MEDIA_ENDED"
            self._media_answer = (sdp, error)
            reply = self._reply
            waiters, self._sdp_waiters = self._sdp_waiters, []
        reply(sdp, error)
        for waiter in waiters:
            waiter(sdp, error)

    def fallback(self, issued: IssuedLiveKitSession, reply: Reply) -> bool:
        key = tuple(getattr(issued, name, id(issued)) for name in (
            "room_name", "browser_identity", "edge_identity", "livekit_url"))
        with self._lock:
            if self._state in {"switching", "livekit"} and key == self._fallback_key:
                if self._state == "switching":
                    if len(self._fallback_waiters) >= 4:
                        return False
                    self._fallback_waiters.append(reply)
                    return True
                ready = True
                wake = None
            elif self._state not in {"preparing", "direct_failed"}:
                return False
            else:
                ready = False
                self._state = "switching"
                self._fallback_key = key
                self._pending = (issued, reply)
                wake = self._wake
        if ready:
            reply(None, None)
        if wake is not None:
            wake()
        return True

    def listen(self, wake: Callable[[], None]) -> None:
        with self._lock:
            self._wake = wake
            pending = self._pending is not None
        if pending:
            wake()

    def take_fallback(self) -> tuple[IssuedLiveKitSession, Reply] | None:
        with self._lock:
            pending, self._pending = self._pending, None
            return pending

    def seal(self) -> bool:
        with self._lock:
            if self._state not in {"preparing", "livekit"}:
                return False
            self._state = "committed"
            return True

    @property
    def can_commit(self) -> bool:
        with self._lock:
            return self._state in {"preparing", "livekit", "committed"}

    def switched(self) -> bool:
        with self._lock:
            if self._state != "switching":
                return False
            self._state = "livekit"
            waiters, self._fallback_waiters = self._fallback_waiters, []
        for reply in waiters:
            reply(None, None)
        return True

    def switch_failed(self) -> None:
        with self._lock:
            if self._state == "switching":
                self._state = "failed"
            waiters, self._fallback_waiters = self._fallback_waiters, []
        for reply in waiters:
            reply(None, "LIVEKIT_SETUP_FAILED")

    def close(self) -> None:
        with self._lock:
            self._state = "closed"
            self._wake = None
            self._media_wake = None
            pending, self._pending = self._pending, None
            waiters, self._fallback_waiters = self._fallback_waiters, []
        self.reply(None, "MEDIA_ENDED")
        if pending:
            pending[1](None, "MEDIA_ENDED")
        for reply in waiters:
            reply(None, "MEDIA_ENDED")


class SwitchableInboundEndpoint:
    def __init__(self, request: InboundDirectRequest, *,
                 direct_factory: Any = None, livekit_factory: Any = None) -> None:
        self.request = request
        self._direct_factory = direct_factory
        self._livekit_factory = livekit_factory
        self._inner: Any = None
        self._closed = False
        self._switching = False
        self._direct_replaced = False
        self.failed = False
        self.no_phone_audio = False
        self._direct_connect: asyncio.Task[None] | None = None
        self._watch_task: asyncio.Task[None] | None = None
        self._retire_task: asyncio.Task[None] | None = None
        self._stats_finish_task: asyncio.Task[None] | None = None
        self._close_task: asyncio.Task[None] | None = None
        self._timing = CallTiming()

    def set_timing(self, timing: CallTiming) -> None:
        self._timing = timing
        if self._inner is not None:
            attach_endpoint_timing(self._inner, timing)

    @property
    def media_ready(self) -> bool:
        return bool(not self._closed and not self._switching and self.request.can_commit
                    and self._inner is not None and self._inner.media_ready)

    @property
    def browser_connected(self) -> bool:
        return bool(not self._closed and self._inner is not None and self._inner.browser_connected)

    @property
    def direct_preview_ready(self) -> bool:
        return bool(not self._closed and not self._switching and not self._direct_replaced
                    and not self.no_phone_audio and self.browser_connected)

    async def finish_rtp_stats(self) -> None:
        finish = getattr(self._inner, "finish_rtp_stats", None)
        if not callable(finish) or self._stats_finish_task is not None:
            return

        async def sample() -> None:
            try:
                await finish("commit")
            except Exception:
                self._timing.mark("rtp_stats_unavailable")

        # Diagnostics are never a prerequisite for hearing or ownership commit.
        self._stats_finish_task = asyncio.create_task(sample())

    def no_phone_audio_expired(self, now: float, maximum: float) -> bool:
        if self._closed or self._direct_replaced or self.no_phone_audio or self._inner is None:
            return False
        connected_at = getattr(self._inner, "direct_connected_at", None)
        return bool(connected_at is not None and not getattr(self._inner, "phone_audio_received", False)
                    and now >= connected_at + maximum)

    async def fail_no_phone_audio(self) -> bool:
        if not self.request.fail_no_phone_audio():
            return False
        self.no_phone_audio = True
        self._timing.mark("no_phone_audio")
        try:
            await self.send_event({"type": "media_failed", "reason": "no_phone_audio"})
        except Exception:
            # A dead phone cannot hold the caller silent or revoke the claim's
            # remaining HTTP fallback opportunity.
            self._timing.mark("media_failed_delivery_failed")
        return True

    async def connect(self) -> None:
        if self._closed:
            raise ConnectionError("media closed")
        wake = asyncio.Event()
        loop = asyncio.get_running_loop()

        def notify() -> None:
            if not loop.is_closed():
                loop.call_soon_threadsafe(wake.set)

        self.request.listen(notify)
        self._watch_task = asyncio.create_task(self._watch_fallback(wake))
        self._direct_connect = asyncio.create_task(self._connect_direct())
        try:
            await self._direct_connect
        except asyncio.CancelledError:
            if not self._direct_replaced:
                raise
        except Exception:
            # Preserve this claim and its PCM bridge for the phone's fallback.
            self.request.reply(None, "DIRECT_SETUP_FAILED")

    async def _connect_direct(self) -> None:
        from .webrtc_media import WebRtcRemoteMediaEndpoint

        wake = asyncio.Event()
        loop = asyncio.get_running_loop()

        def notify() -> None:
            if not loop.is_closed():
                loop.call_soon_threadsafe(wake.set)

        self.request.listen_media(notify)
        media = self.request.media_offer()
        while media is None:
            await wake.wait()
            wake.clear()
            media = self.request.media_offer()
        if self._closed or self._switching:
            return
        sdp, servers = media
        self._timing.mark("direct_sdp_received")
        self._timing.mark("direct_sdp_connect_start")
        factory = self._direct_factory or WebRtcRemoteMediaEndpoint
        direct = factory({"type": "offer", "sdp": sdp}, servers, call_id=self.request.claim_id)
        self._inner = direct
        attach_endpoint_timing(direct, self._timing)
        await direct.connect()
        if not self._closed and not self._switching:
            answer = direct.answer
            if answer is None:
                raise ConnectionError("No SDP answer")
            self._timing.mark("sdp_answer_ready")
            self.request.reply(answer["sdp"], None)

    async def _watch_fallback(self, wake: asyncio.Event) -> None:
        await wake.wait()
        pending = self.request.take_fallback()
        if pending is None or self._closed:
            return
        issued, reply = pending
        self._switching = True
        self._direct_replaced = True
        self.request.reply(None, "DIRECT_REPLACED")
        if self._direct_connect is not None:
            self._direct_connect.cancel()
        previous, self._inner = self._inner, None
        cold: Any = None
        try:
            if previous is not None:
                # Detach media immediately; native cleanup must not delay fallback.
                self._retire_task = asyncio.create_task(previous.close())
                await asyncio.sleep(0)
            from .livekit_media import LiveKitRemoteMediaEndpoint

            factory = self._livekit_factory or LiveKitRemoteMediaEndpoint
            cold = factory(issued)
            attach_endpoint_timing(cold, self._timing)
            self._inner = cold
            self._timing.mark("livekit_fallback_start")
            await cold.connect()
            if self._closed or not self.request.switched():
                raise ConnectionError("media ended")
            self._switching = False
            self._timing.mark("livekit_fallback_ready")
            reply(None, None)
        except asyncio.CancelledError:
            reply(None, "MEDIA_ENDED")
            raise
        except Exception as error:
            self.failed = True
            self._timing.record_observation("direct_media_transition", {
                "operation": "livekit_connect", "accepted": False, "reason": "LIVEKIT_SETUP_FAILED",
                "errorType": type(error).__name__,
            })
            self.request.switch_failed()
            reply(None, "LIVEKIT_SETUP_FAILED")
            if cold is not None:
                await cold.close()

    def seal(self) -> bool:
        return self.media_ready and self.request.seal()

    async def close(self) -> None:
        if self._close_task is None:
            self._closed = True
            self.request.close()
            self._close_task = asyncio.create_task(self._close())
        await asyncio.shield(self._close_task)

    async def _close(self) -> None:
        if self._stats_finish_task is not None:
            await asyncio.gather(self._stats_finish_task, return_exceptions=True)
        tasks = [task for task in (self._direct_connect, self._watch_task) if task is not None]
        for task in tasks:
            task.cancel()
        if self._inner is not None:
            await self._inner.close()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self._retire_task is not None:
            await asyncio.gather(self._retire_task, return_exceptions=True)

    async def next_command(self, timeout: float) -> dict[str, Any] | None:
        if self._inner is None:
            await asyncio.sleep(timeout)
            return None
        result: dict[str, Any] | None = await self._inner.next_command(timeout)
        if result is not None and result.get("type") == "client_timing":
            self._timing.receive_client(result)
            return None
        if result is not None and result.get("type") == "client_rtp_stats":
            self._timing.receive_rtp(result)
            return None
        return result

    def take_browser_audio(self, max_chunks: int = 10) -> list[bytes]:
        result: list[bytes] = self._inner.take_browser_audio(max_chunks) if self._inner is not None else []
        return result

    def push_modem_audio(self, pcm: bytes) -> None:
        if self._inner is not None:
            self._inner.push_modem_audio(pcm)

    async def send_event(self, event: dict[str, Any]) -> None:
        if self._inner is not None:
            await self._inner.send_event(event)
