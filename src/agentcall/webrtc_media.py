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

_FRAME_SAMPLES = REMOTE_AUDIO_RATE // 50
_FRAME_BYTES = _FRAME_SAMPLES * 2
_MAX_CONTROL_BYTES = 4096
_MAX_CONTROL_BUFFER = 64 * 1024
_warm_lock = threading.Lock()
_warm_future: Future[Any] | None = None
_stack: Any = None


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
            _stack = SimpleNamespace(rtc=rtc, av=av, streams=streams, ice=ice_adapter)
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
            self.stats = PcmFlowStats("downlink1_direct_publish")

        async def recv(self) -> Any:
            if self.readyState != "live":
                raise _stack.streams.MediaStreamError
            now = time.monotonic()
            if self._next_at is not None:
                await asyncio.sleep(max(0, self._next_at - now))
            if self.readyState != "live":
                raise _stack.streams.MediaStreamError
            # Do not burst stale frames after an event-loop stall.
            self._next_at = time.monotonic() + 0.02
            try:
                pcm = apply_pcm_gain(source.get_nowait(), gain)
                self.stats.add(pcm)
            except asyncio.QueueEmpty:
                pcm = bytes(_FRAME_BYTES)
            frame = _stack.av.AudioFrame(format="s16", layout="mono", samples=_FRAME_SAMPLES)
            frame.planes[0].update(pcm)
            frame.sample_rate = REMOTE_AUDIO_RATE
            frame.pts = self._pts
            frame.time_base = Fraction(1, REMOTE_AUDIO_RATE)
            self._pts += _FRAME_SAMPLES
            return frame

    return ModemTrack()


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
        self._pc: Any = None
        self._channel: Any = None
        self._local_track: Any = None
        self._phone_track: Any = None
        self._track_task: asyncio.Task[None] | None = None
        self._close_task: asyncio.Task[None] | None = None
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
            self._timing.mark("media_ready")

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
            await pc.setRemoteDescription(stack.rtc.RTCSessionDescription(**self._offer))
            await self._check_current(pc)
            self._local_track = _modem_track(self._modem_audio, self._downlink_gain)
            pc.addTrack(self._local_track)
            stack.ice.configure_peer(pc)
            answer = await pc.createAnswer()
            await self._check_current(pc)
            # aiortc gathers local candidates here, but does not wait for peer ICE.
            await pc.setLocalDescription(answer)
            await self._check_current(pc)
            self._answer = {"type": pc.localDescription.type, "sdp": pc.localDescription.sdp}
            self._timing.mark("direct_answer_ready")
            self._timing.mark("endpoint_connect_return")
        except BaseException:
            await self.close()
            raise

    async def _pump_phone_audio(self, track: Any) -> None:
        resampler = _stack.av.AudioResampler(format="s16", layout="mono", rate=REMOTE_AUDIO_RATE,
                                           frame_size=_FRAME_SAMPLES)
        try:
            while not self._closed:
                frame = await track.recv()
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
            self._closed = True
            self._connected = False
            self._received_audio = False
            self._answer = None
            self._command_ready.set()
            self._close_task = asyncio.create_task(self._close())
        await asyncio.shield(self._close_task)

    async def _close(self) -> None:
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
