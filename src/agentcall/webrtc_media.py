"""Call-local direct DTLS-SRTP endpoint; signaling never implies media readiness."""

from __future__ import annotations

import asyncio
import importlib
import json
import logging
import math
import threading
import time
from concurrent.futures import Future
from fractions import Fraction
from types import SimpleNamespace
from typing import Any

from . import config
from .audio_bridge import apply_pcm_gain
from .call_timing import MAX_TIMING_BYTES, CallTiming
from .livekit_media import _decode_control_payload, _put_latest
from .pcm_stats import PcmFlowStats
from .remote_dialer import REMOTE_AUDIO_RATE, REMOTE_CONTROL_TOPIC

logger = logging.getLogger(__name__)


def offer_candidate_summary(sdp: str) -> str:
    """Raw offer candidates as typ/transport/family counts (no addresses), e.g. relay/udp/v4:1.

    Compared with the parsed remote counts in the ICE gather log, this shows candidates aioice
    dropped (IPv6 without local IPv6, TCP, ...), which otherwise look like "the phone sent none".
    """
    counts: dict[str, int] = {}
    seen: set[tuple[str, ...]] = set()
    for line in sdp.splitlines():
        if not line.startswith("a=candidate:"):
            continue
        parts = line.split()
        if len(parts) < 8 or "typ" not in parts:
            continue
        kind = parts[parts.index("typ") + 1] if parts.index("typ") + 1 < len(parts) else "?"
        # BUNDLE offers repeat the same candidate in every m-section; count each address once.
        identity = (kind, parts[2].lower(), parts[4], parts[5], parts[1])
        if identity in seen:
            continue
        seen.add(identity)
        family = "v6" if ":" in parts[4] else "v4"
        key = f"{kind}/{parts[2].lower()}/{family}"
        counts[key] = counts.get(key, 0) + 1
    return ",".join(f"{key}:{counts[key]}" for key in sorted(counts)) or "none"

_FRAME_SAMPLES = REMOTE_AUDIO_RATE // 50
_FRAME_BYTES = _FRAME_SAMPLES * 2
_FRAME_SECONDS = _FRAME_SAMPLES / REMOTE_AUDIO_RATE
# Catch up at most this far behind real time; beyond it the backlog is stale.
_MAX_CATCH_UP_SECONDS = 0.1
# On re-anchor, retain three 20 ms frames (60 ms including the next send):
# enough cushion for producer jitter without preserving a call-long stale backlog.
_REANCHOR_KEEP_FRAMES = 3
_BACKLOG_TRIM_LOG_INTERVAL_SECONDS = 5.0
_MAX_CONTROL_BYTES = 4096
_MAX_CONTROL_BUFFER = 64 * 1024
_RTP_STATS_INTERVAL_SECONDS = 2.0
_RTP_STATS_MAX_SAMPLES = 10
_RTP_STATS_READ_MAX_SECONDS = 1.0  # Diagnostics cannot stall teardown or ownership commit.
_warm_lock = threading.Lock()
_warm_future: Future[Any] | None = None
_stack: Any = None
_SETUP_MARKS = (
    "ice_completed", "dtls_handshake_start", "dtls_connected", "sctp_connected", "control_channel_open",
    "first_rtp_received", "first_audio_frame_decoded", "media_ready", "dial_start",
)


def warm_webrtc_stack() -> Future[Any]:
    """Start exactly one background import/codec/certificate warm-up, never block the caller."""
    global _warm_future
    with _warm_lock:
        if _warm_future is not None:
            return _warm_future
        future: Future[Any] = Future()
        _warm_future = future

    def warm() -> None:
        global _stack
        started = time.monotonic()
        try:
            rtc = importlib.import_module("aiortc")
            av = importlib.import_module("av")
            importlib.import_module("pylibsrtp")
            importlib.import_module("aioice")
            streams = importlib.import_module("aiortc.mediastreams")
            sdp = importlib.import_module("aiortc.sdp")
            ice_adapter = importlib.import_module("agentcall.webrtc_ice")

            async def initialize() -> None:
                pc = rtc.RTCPeerConnection(rtc.RTCConfiguration(iceServers=[]))
                try:
                    pc.addTransceiver("audio")
                    pc.createDataChannel(REMOTE_CONTROL_TOPIC)
                    await pc.createOffer()
                    # Exercise libav's resampling and Opus initialization off the call loop.
                    frame = av.AudioFrame(format="s16", layout="mono", samples=_FRAME_SAMPLES)
                    frame.planes[0].update(bytes(_FRAME_BYTES))
                    frame.sample_rate, frame.pts, frame.time_base = REMOTE_AUDIO_RATE, 0, Fraction(1, REMOTE_AUDIO_RATE)
                    opus = importlib.import_module("aiortc.codecs.opus")
                    opus.OpusEncoder().encode(frame)
                finally:
                    await pc.close()

            asyncio.run(initialize())
            _stack = SimpleNamespace(rtc=rtc, av=av, streams=streams, ice=ice_adapter, sdp=sdp)
            logger.info("webrtc stack warmed in %.2fs", time.monotonic() - started)
            future.set_result(_stack)
        except Exception as error:
            logger.warning("WebRTC stack warm-up failed: %s", type(error).__name__)
            future.set_exception(error)

    threading.Thread(target=warm, name="webrtc-warmup", daemon=True).start()
    return future


def ice_servers_config(servers: list[dict[str, Any]]) -> Any:
    # Standalone probe/fake clients may use this helper before service startup.
    stack = _stack if _stack is not None else warm_webrtc_stack().result()
    ice = []
    for server in servers:
        urls = server.get("urls")
        urls = [urls] if isinstance(urls, str) else urls
        if not isinstance(urls, list) or not urls or not all(isinstance(url, str) for url in urls):
            raise ValueError("Invalid ICE server URLs")
        ice.append(stack.rtc.RTCIceServer(
            urls=urls, username=server.get("username"), credential=server.get("credential"),
        ))
    # An explicit empty list disables the SDK's default external STUN server.
    return stack.rtc.RTCConfiguration(iceServers=ice)


def _modem_track(source: asyncio.Queue[bytes], gain: float) -> Any:
    class ModemTrack(_stack.rtc.MediaStreamTrack):
        kind = "audio"

        def __init__(self) -> None:
            super().__init__()
            self._next_at: float | None = None
            self._pts = 0
            self._last_trim_log_at: float | None = None
            self.stats = PcmFlowStats("downlink1_direct_publish")
            self.frames = 0
            self.silence_frames = 0
            self._late_ms: list[float] = []

        async def recv(self) -> Any:
            if self.readyState != "live":
                raise _stack.streams.MediaStreamError
            now = time.monotonic()
            if self._next_at is None:
                self._next_at = now
            else:
                # Fixed 20 ms cadence from the previous slot, not from the wake-up time:
                # re-anchoring on every wake lost each frame's sleep overshoot and
                # encode time, so a busy loop sent ~33 frames/s and the modem queue
                # overflowed (11-03 device call: choppy caller audio). Short stalls
                # are caught up; a long stall re-anchors instead of bursting stale audio.
                self._next_at += _FRAME_SECONDS
                if self._next_at < now - _MAX_CATCH_UP_SECONDS:
                    self._next_at = now
                    dropped = 0
                    while source.qsize() > _REANCHOR_KEEP_FRAMES:
                        source.get_nowait()
                        dropped += 1
                    if dropped and (self._last_trim_log_at is None
                                    or now - self._last_trim_log_at >= _BACKLOG_TRIM_LOG_INTERVAL_SECONDS):
                        logger.info("Direct downlink backlog trimmed: dropped_frames=%d dropped_ms=%.0f kept_frames=%d",
                                    dropped, dropped * _FRAME_SECONDS * 1000, source.qsize())
                        self._last_trim_log_at = now
                elif self._next_at > now:
                    await asyncio.sleep(self._next_at - now)
            if self.readyState != "live":
                raise _stack.streams.MediaStreamError
            self._late_ms.append(max(0.0, (time.monotonic() - self._next_at) * 1000))
            try:
                pcm = apply_pcm_gain(source.get_nowait(), gain, self.stats)
                self.stats.add(pcm)
            except asyncio.QueueEmpty:
                pcm = bytes(_FRAME_BYTES)
                self.silence_frames += 1
            frame = _stack.av.AudioFrame(format="s16", layout="mono", samples=_FRAME_SAMPLES)
            frame.planes[0].update(pcm)
            frame.sample_rate = REMOTE_AUDIO_RATE
            frame.pts = self._pts
            frame.time_base = Fraction(1, REMOTE_AUDIO_RATE)
            self._pts += _FRAME_SAMPLES
            self.frames += 1
            if self.stats.due():
                late = sorted(self._late_ms)
                self.stats.maybe_log(senderFrames=self.frames, senderSilenceFrames=self.silence_frames,
                                     queued=source.qsize(), late_p50_ms=round(late[len(late) // 2], 3),
                                     late_p95_ms=round(late[min(len(late) - 1, int(len(late) * .95))], 3))
                self._late_ms.clear()
            return frame

    return ModemTrack()


def _sdp_summary(pc: Any) -> dict[str, Any]:
    """Extract only negotiation shape, never addresses, SSRC, fingerprints or credentials."""
    description = _stack.sdp.SessionDescription.parse(pc.localDescription.sdp)
    directions = {t.mid: t.currentDirection for t in pc.getTransceivers()}
    bundled = {mid for group in description.group if group.semantic == "BUNDLE" for mid in group.items}
    lines = []
    for index, media in enumerate(description.media[:8]):
        mid = media.rtp.muxId
        direction = directions.get(mid)
        codecs = [{"mimeType": codec.mimeType if codec.mimeType.lower() in {
            "audio/opus", "audio/pcmu", "audio/pcma", "audio/g722", "audio/telephone-event",
            "video/vp8", "video/vp9", "video/h264", "video/rtx"} else "other",
                   "pt": codec.payloadType, "clockRate": codec.clockRate, "channels": codec.channels}
                  for codec in media.rtp.codecs[:16]]
        lines.append({"mLine": index,
                      "mid": mid if isinstance(mid, str) and mid.isascii() and mid.isdecimal()
                      and len(mid) <= 8 else None,
                      "kind": media.kind if media.kind in {"audio", "video", "application"} else "other",
                      "currentDirection": direction if direction in {
                          "sendrecv", "sendonly", "recvonly", "inactive"} else None,
                      "codecs": codecs, "rtcpMux": bool(media.rtcp_mux),
                      "bundle": mid in bundled, "extmapCount": len(media.rtp.headerExtensions)})
    return {"v": 1, "mLines": lines}


def _selected_pair(pc: Any) -> dict[str, str] | None:
    for connection in _stack.ice.peer_connections(pc):
        for pair in getattr(connection, "_nominated", {}).values():
            local, remote = pair.local_candidate.type, pair.remote_candidate.type
            types = {"host", "srflx", "prflx", "relay"}
            if local in types and remote in types and pair.state.name == "SUCCEEDED":
                return {"localCandidate": local, "remoteCandidate": remote, "state": "succeeded"}
    return None


class WebRtcRemoteMediaEndpoint:
    """One authenticated claim owns one peer connection and bounded PCM/control queues."""

    transport = "webrtc"

    def __init__(self, offer: dict[str, str], ice_servers: list[dict[str, Any]], *, call_id: str,
                 queue_max_chunks: int = 10, peer_factory: Any = None) -> None:
        if offer.get("type") != "offer" or not isinstance(offer.get("sdp"), str):
            raise ValueError("Expected an SDP offer")
        if not 0 < len(offer["sdp"].encode("utf-8")) <= 16 * 1024:
            raise ValueError("Invalid SDP offer size")
        self.call_id = call_id
        self._offer = dict(offer)
        self._ice_servers = ice_servers
        self._peer_factory = peer_factory
        size = max(2, min(queue_max_chunks, 50))
        self._browser_audio: asyncio.Queue[bytes] = asyncio.Queue(maxsize=size)
        self._modem_audio: asyncio.Queue[bytes] = asyncio.Queue(maxsize=size)
        self._modem_buffer = bytearray()
        self._commands: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=32)
        self._hangup_command: dict[str, Any] | None = None
        self._command_ready = asyncio.Event()
        self._timing = CallTiming()
        self._setup_marks: dict[str, float] = {}
        self._setup_logged = False
        self._pc: Any = None
        self._channel: Any = None
        self._local_track: Any = None
        self._phone_track: Any = None
        self._track_task: asyncio.Task[None] | None = None
        self._close_task: asyncio.Task[None] | None = None
        self._stats_task: asyncio.Task[None] | None = None
        self._stats_finish_task: asyncio.Task[None] | None = None
        self._stats_lock = asyncio.Lock()
        self._stats_seq = 0
        self._stats_stopped = False
        self._stats_terminal = False
        self._connected = False
        self._received_audio = False
        self._first_audio_received = False
        self.direct_connected_at: float | None = None
        self._closed = False
        self._started = False
        self._answer: dict[str, str] | None = None
        self._browser_in_stats = PcmFlowStats("uplink1_direct_in")
        gain = config.get_float("REMOTE_DOWNLINK_GAIN")
        self._downlink_gain = gain if math.isfinite(gain) and gain > 0 else 16.0

    @property
    def answer(self) -> dict[str, str] | None:
        return dict(self._answer) if self._answer is not None else None

    @property
    def media_ready(self) -> bool:
        return (not self._closed and self._connected and self._received_audio
                and self._channel is not None and self._channel.readyState == "open")

    @property
    def browser_connected(self) -> bool:
        return not self._closed and self._connected

    @property
    def phone_audio_received(self) -> bool:
        return self._first_audio_received

    def set_timing(self, timing: CallTiming) -> None:
        self._timing = timing

    def _mark_ready(self) -> None:
        if self.media_ready:
            self._mark_setup("media_ready")
            self._timing.mark("media_ready")

    def _mark_setup(self, name: str) -> None:
        try:
            if not self._setup_logged and name not in self._setup_marks:
                self._setup_marks[name] = time.monotonic()
        except Exception:
            pass

    def note_dial_start(self) -> None:
        """Observe the existing dial boundary; never participate in its readiness gate."""
        self._mark_setup("dial_start")
        self._log_setup()

    def _log_setup(self) -> None:
        if self._setup_logged:
            return
        self._setup_logged = True
        try:
            origin = self._setup_marks.get("ice_completed")
            fields: dict[str, Any] = {"origin": "ice_completed", "unit": "ms", "peer": id(self._pc)}
            for name in _SETUP_MARKS:
                stamp = self._setup_marks.get(name)
                fields[name] = round((stamp - origin) * 1000, 3) if (
                    stamp is not None and origin is not None) else -1
            pair = None
            try:
                pair = _selected_pair(self._pc)
            except Exception:
                pass
            fields["local_candidate"] = pair["localCandidate"] if pair else "unknown"
            fields["remote_candidate"] = pair["remoteCandidate"] if pair else "unknown"
            logger.info("Direct setup timing %s", json.dumps(fields, separators=(",", ":")))
        except Exception:
            pass

    async def _check_current(self, pc: Any) -> None:
        if self._closed or self._pc is not pc:
            await self.close()
            raise asyncio.CancelledError

    async def connect(self) -> None:
        """Gather an answer; ICE/DTLS and microphone readiness continue asynchronously."""
        if self._closed:
            raise asyncio.CancelledError
        if self._started:
            raise RuntimeError("A direct endpoint can connect only once")
        self._started = True
        self._timing.mark("endpoint_connect_start")
        # A first claim without prepare still loads on a worker thread, not the event loop.
        stack = await asyncio.shield(asyncio.wrap_future(warm_webrtc_stack()))
        if self._closed:
            raise asyncio.CancelledError
        pc = (self._peer_factory(self._ice_servers) if self._peer_factory is not None
              else stack.rtc.RTCPeerConnection(ice_servers_config(self._ice_servers)))
        self._pc = pc

        @pc.on("connectionstatechange")
        def on_state() -> None:
            if self._closed or self._pc is not pc:
                return
            self._connected = pc.connectionState == "connected"
            if self._connected:
                if self.direct_connected_at is None:
                    self.direct_connected_at = time.monotonic()
                self._timing.mark("direct_connected")
                self._timing.mark("phone_joined")
                if self._stats_task is None and not self._stats_stopped:
                    self._stats_task = asyncio.create_task(self._sample_rtp_loop())
            elif pc.connectionState == "failed" and not self._stats_stopped and self._stats_finish_task is None:
                self._stats_finish_task = asyncio.create_task(self.finish_rtp_stats("failed"))
            self._mark_ready()

        @pc.on("datachannel")
        def on_channel(channel: Any) -> None:
            if self._closed or self._channel is not None or channel.label != REMOTE_CONTROL_TOPIC:
                channel.close()
                return
            self._channel = channel

            @channel.on("open")
            def on_open() -> None:
                if not self._closed:
                    self._mark_setup("control_channel_open")
                    self._timing.mark("direct_control_ready")
                    self._mark_ready()

            @channel.on("message")
            def on_message(message: str | bytes) -> None:
                if self._closed or self._channel is not channel:
                    return
                try:
                    payload = message.encode("utf-8") if isinstance(message, str) else message
                except UnicodeEncodeError:
                    return
                command = _decode_control_payload(payload)
                if command is None:
                    return
                if command.get("type") == "client_timing":
                    if len(payload) <= MAX_TIMING_BYTES:
                        self._timing.receive_client(command)
                    return
                if command.get("type") == "client_rtp_stats":
                    if len(payload) <= 512:
                        self._timing.receive_rtp(command)
                    return
                if command.get("type") == "hangup":
                    if self._hangup_command is None:
                        self._hangup_command = command
                elif not self._commands.full():
                    self._commands.put_nowait(command)
                self._command_ready.set()

            if channel.readyState == "open":
                on_open()

        @pc.on("track")
        def on_track(track: Any) -> None:
            if self._closed or track.kind != "audio" or self._phone_track is not None:
                track.stop()
                return
            self._phone_track = track
            self._timing.mark("direct_remote_track")
            self._track_task = asyncio.create_task(self._pump_phone_audio(track))

        try:
            stage_started = time.monotonic()
            await pc.setRemoteDescription(stack.rtc.RTCSessionDescription(**self._offer))
            logger.info("Direct answer peer=%s stage=remote_description duration_ms=%.1f offer_candidates=%s",
                        id(pc), (time.monotonic() - stage_started) * 1000,
                        offer_candidate_summary(str(self._offer.get("sdp", ""))))
            await self._check_current(pc)
            self._local_track = _modem_track(self._modem_audio, self._downlink_gain)
            pc.addTrack(self._local_track)
            stack.ice.configure_peer(pc)
            stack.ice.observe_setup(pc, self._mark_setup)
            stage_started = time.monotonic()
            answer = await pc.createAnswer()
            logger.info("Direct answer peer=%s stage=create_answer duration_ms=%.1f",
                        id(pc), (time.monotonic() - stage_started) * 1000)
            await self._check_current(pc)
            # aiortc gathers local candidates here, but does not wait for peer ICE.
            stage_started = time.monotonic()
            await pc.setLocalDescription(answer)
            logger.info("Direct answer peer=%s stage=local_description_gather duration_ms=%.1f",
                        id(pc), (time.monotonic() - stage_started) * 1000)
            await self._check_current(pc)
            self._answer = {"type": pc.localDescription.type, "sdp": pc.localDescription.sdp}
            if callable(getattr(pc, "getTransceivers", None)):
                try:
                    self._timing.record_observation("direct_sdp_summary", _sdp_summary(pc))
                except Exception as error:
                    logger.debug("Direct SDP summary unavailable: %s", type(error).__name__)
            self._timing.mark("direct_answer_ready")
            self._timing.mark("endpoint_connect_return")
        except BaseException:
            await self.close()
            raise

    async def _sample_rtp_loop(self) -> None:
        while not self._stats_stopped and self._stats_seq < _RTP_STATS_MAX_SAMPLES - 1:
            await asyncio.sleep(_RTP_STATS_INTERVAL_SECONDS)
            if not self._stats_stopped:
                await self._sample_rtp_stats(terminal=False, reason="periodic")

    async def _sample_rtp_stats(self, *, terminal: bool, reason: str) -> None:
        async with self._stats_lock:
            if (self._stats_terminal or self._pc is None
                    or self._stats_seq >= _RTP_STATS_MAX_SAMPLES - (0 if terminal else 1)
                    or not callable(getattr(self._pc, "getStats", None))):
                return
            self._stats_seq += 1
            self._stats_terminal = terminal
            try:
                try:
                    async with asyncio.timeout(_RTP_STATS_READ_MAX_SECONDS):
                        stats = list((await self._pc.getStats()).values())
                except Exception as error:
                    logger.debug("Direct RTP stats read unavailable: %s", type(error).__name__)
                    stats = []
                outgoing = [s for s in stats if s.type == "outbound-rtp" and s.kind == "audio"]
                incoming = [s for s in stats if s.type == "inbound-rtp" and s.kind == "audio"]
                remote = [s for s in stats if s.type == "remote-inbound-rtp" and s.kind == "audio"]
                states = [s.dtlsState for s in stats if s.type == "transport" and s.dtlsState in {
                    "new", "connecting", "connected", "closed", "failed"}]
                rtts = [s.roundTripTime * 1000 for s in remote
                        if isinstance(getattr(s, "roundTripTime", None), (int, float))
                        and math.isfinite(s.roundTripTime) and s.roundTripTime >= 0]
                origin = self.direct_connected_at
                self._timing.record_observation("edge_rtp_stats", {
                    "v": 1, "seq": self._stats_seq, "terminal": terminal,
                    "reason": reason if reason in {"periodic", "commit", "close", "failed"} else "close",
                    "elapsedMs": min(60_000, max(0, round((time.monotonic() - origin) * 1000))) if origin else 0,
                    "outPackets": sum(s.packetsSent for s in outgoing) if outgoing else None,
                    "outBytes": sum(s.bytesSent for s in outgoing) if outgoing else None,
                    "inPackets": sum(s.packetsReceived for s in incoming) if incoming else None,
                    "inBytes": None,  # aiortc inbound-rtp does not expose bytesReceived.
                    "lost": sum(s.packetsLost for s in incoming) if incoming else None,
                    "rttMs": max(rtts) if rtts else None,
                    "dtlsState": "connected" if "connected" in states else next(iter(states), None),
                    "selectedCandidatePair": _selected_pair(self._pc),
                    "senderFrames": getattr(self._local_track, "frames", 0),
                    "senderSilenceFrames": getattr(self._local_track, "silence_frames", 0),
                })
            except Exception as error:
                logger.debug("Direct RTP stats unavailable: %s", type(error).__name__)

    async def finish_rtp_stats(self, reason: str = "commit") -> None:
        """Capture the fast-success/failure sample before disposing sender counters."""
        self._stats_stopped = True
        task = self._stats_task
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await self._sample_rtp_stats(terminal=True, reason=reason)

    async def _pump_phone_audio(self, track: Any) -> None:
        resampler = _stack.av.AudioResampler(format="s16", layout="mono", rate=REMOTE_AUDIO_RATE,
                                           frame_size=_FRAME_SAMPLES)
        try:
            while not self._closed:
                frame = await track.recv()
                self._mark_setup("first_audio_frame_decoded")
                if self._closed:
                    return
                for out in resampler.resample(frame):
                    pcm = bytes(out.planes[0])[:out.samples * 2]
                    if len(pcm) != _FRAME_BYTES:
                        continue
                    self._received_audio = True
                    self._first_audio_received = True
                    self._timing.mark("phone_first_frame")
                    self._timing.mark("direct_audio_ready")
                    self._browser_in_stats.add(pcm)
                    _put_latest(self._browser_audio, pcm)
                self._mark_ready()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.info("Direct phone audio ended: %s", type(error).__name__)
        finally:
            self._received_audio = False

    async def close(self) -> None:
        if self._close_task is None:
            self._log_setup()
            self._closed = True
            self._connected = False
            self._received_audio = False
            self._answer = None
            self._command_ready.set()
            self._close_task = asyncio.create_task(self._close())
        await asyncio.shield(self._close_task)

    async def _close(self) -> None:
        await self.finish_rtp_stats("close")
        if self._stats_finish_task is not None:
            await asyncio.gather(self._stats_finish_task, return_exceptions=True)
        if self._track_task is not None:
            self._track_task.cancel()
        if self._local_track is not None:
            self._local_track.stop()
        if self._phone_track is not None:
            self._phone_track.stop()
        if self._channel is not None:
            self._channel.close()
        if self._pc is not None:
            if _stack is not None:
                await _stack.ice.stop_peer_checks(self._pc)
            await self._pc.close()
        if self._track_task is not None:
            await asyncio.gather(self._track_task, return_exceptions=True)
        self._modem_buffer.clear()
        self._hangup_command = None
        for queue in (self._browser_audio, self._modem_audio, self._commands):
            while not queue.empty():
                queue.get_nowait()
        self._timing.mark("direct_closed")

    async def next_command(self, timeout: float) -> dict[str, Any] | None:
        if self._closed:
            return None
        try:
            await asyncio.wait_for(self._command_ready.wait(), timeout)
        except TimeoutError:
            return None
        if self._closed:
            return None
        if self._hangup_command is not None:
            command, self._hangup_command = self._hangup_command, None
        else:
            try:
                command = self._commands.get_nowait()
            except asyncio.QueueEmpty:
                command = None
        if self._hangup_command is None and self._commands.empty():
            self._command_ready.clear()
        return command

    def take_browser_audio(self, max_chunks: int = 10) -> list[bytes]:
        chunks: list[bytes] = []
        while len(chunks) < max_chunks and not self._closed:
            try:
                chunks.append(self._browser_audio.get_nowait())
            except asyncio.QueueEmpty:
                break
        self._browser_in_stats.maybe_log(queued=self._browser_audio.qsize(), media_ready=self.media_ready)
        return chunks

    def push_modem_audio(self, pcm: bytes) -> None:
        if self._closed or not pcm:
            return
        self._modem_buffer.extend(pcm[:len(pcm) - len(pcm) % 2])
        while len(self._modem_buffer) >= _FRAME_BYTES:
            frame = bytes(self._modem_buffer[:_FRAME_BYTES])
            del self._modem_buffer[:_FRAME_BYTES]
            _put_latest(self._modem_audio, frame)

    async def send_event(self, event: dict[str, Any]) -> None:
        channel = self._channel
        if self._closed or channel is None or channel.readyState != "open":
            if event.get("type") == "hangup.ack":
                raise ConnectionError("Direct control channel unavailable")
            return
        if event.get("type") == "status":
            event = {**event, "clientTimingVersion": 1}
        payload = json.dumps(event, separators=(",", ":"), ensure_ascii=False)
        if len(payload.encode("utf-8")) > _MAX_CONTROL_BYTES:
            raise ValueError("Direct control message too large")
        if channel.bufferedAmount > _MAX_CONTROL_BUFFER:
            raise BufferError("Direct control channel backpressure")
        channel.send(payload)
