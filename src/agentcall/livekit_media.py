"""LiveKit implementation of the Remote Web Dialer media/control endpoint."""

from __future__ import annotations

import asyncio
import json
import logging
import math
from typing import Any

from . import config
from .audio_bridge import apply_pcm_gain
from .call_timing import MAX_TIMING_BYTES, CallTiming
from .pcm_stats import PcmFlowStats
from .remote_dialer import (
    REMOTE_AUDIO_RATE,
    REMOTE_CONTROL_TOPIC,
    REMOTE_STATUS_TOPIC,
    IssuedLiveKitSession,
)

logger = logging.getLogger(__name__)

_PCM_FRAME_MS = 20
_PCM_FRAME_BYTES = REMOTE_AUDIO_RATE * 2 * _PCM_FRAME_MS // 1000
_MAX_CONTROL_BYTES = 4096
_MAX_COMMANDS = 32
_AUDIO_STATS_IDLE_TICK_SECONDS = 0.5


def _decode_control_payload(payload: bytes) -> dict[str, Any] | None:
    if not payload or len(payload) > _MAX_CONTROL_BYTES:
        return None
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict):
        return None
    command_type = value.get("type")
    if not isinstance(command_type, str) or len(command_type) > 32:
        return None
    return value


def _put_latest(queue: asyncio.Queue[bytes], value: bytes) -> None:
    """Bound latency: discard the oldest frame instead of growing without limit."""

    if queue.full():
        try:
            queue.get_nowait()
        except asyncio.QueueEmpty:
            pass
    queue.put_nowait(value)


class LiveKitRemoteMediaEndpoint:
    """Programmatic room participant bridging browser audio to 8 kHz PCM."""

    def __init__(
        self,
        issued: IssuedLiveKitSession,
        *,
        queue_max_chunks: int = 10,
        connect_timeout_seconds: float = 8.0,
        rtc_module: Any = None,
    ) -> None:
        self._issued = issued
        self._queue_max_chunks = max(2, min(queue_max_chunks, 50))
        self._connect_timeout_seconds = max(1.0, connect_timeout_seconds)
        self._rtc = rtc_module
        self._commands: asyncio.Queue[dict[str, Any]] = asyncio.Queue(
            maxsize=_MAX_COMMANDS
        )
        self._hangup_command: dict[str, Any] | None = None
        self._command_ready = asyncio.Event()
        self._timing = CallTiming()
        self._browser_audio: asyncio.Queue[bytes] = asyncio.Queue(
            maxsize=self._queue_max_chunks
        )
        self._modem_audio: asyncio.Queue[bytes] = asyncio.Queue(
            maxsize=self._queue_max_chunks
        )
        self._modem_buffer = bytearray()
        self._room: Any = None
        self._audio_source: Any = None
        self._audio_stream: Any = None
        self._browser_audio_task: asyncio.Task[None] | None = None
        self._publisher_task: asyncio.Task[None] | None = None
        self._modem_stats_task: asyncio.Task[None] | None = None
        self._stream_close_tasks: set[asyncio.Task[None]] = set()
        self._browser_connected = False
        self._media_ready = False
        self._closed = False
        self._silent = False
        self._connected = False
        self._subscribe_track: Any = None
        self._downlink_gain = config.get_float("REMOTE_DOWNLINK_GAIN")
        if not math.isfinite(self._downlink_gain) or self._downlink_gain <= 0:
            logger.warning(
                "REMOTE_DOWNLINK_GAIN=%r 非法，当前通话回落为 16.0",
                self._downlink_gain,
            )
            self._downlink_gain = 16.0
        # 上行第一段观测：浏览器（手机）→ LiveKit → Edge 的入站帧统计。
        # 打点由 take_browser_audio（remote pump 每 10ms 调）驱动，
        # 因此 LiveKit 一帧未推时也会按期打出 frames=0。
        self._browser_in_stats = PcmFlowStats("uplink1_lk_in")
        # 下行发布观测：模组 → Edge 队列 → LiveKit AudioSource。只统计成功
        # capture 的帧数/字节/峰值与瞬时队列深度，不记录 PCM 内容。
        self._modem_publish_stats = PcmFlowStats("downlink1_lk_publish")

    @property
    def media_ready(self) -> bool:
        return self._media_ready

    @property
    def browser_connected(self) -> bool:
        return self._browser_connected

    def set_timing(self, timing: CallTiming) -> None:
        self._timing = timing

    async def connect(self) -> None:
        await self._connect_room()
        await self.bind_winner(self._issued)

    async def preconnect(self) -> None:
        self._silent = True
        await self._connect_room()
        self._timing.mark("preconnect_ready")

    async def _connect_room(self) -> None:
        self._timing.mark("endpoint_connect_start")
        rtc = self._rtc
        if rtc is None:
            from livekit import rtc as livekit_rtc

            rtc = livekit_rtc
            self._rtc = rtc

        room = rtc.Room()
        self._room = room

        @room.on("participant_connected")
        def on_participant_connected(participant) -> None:
            if not self._silent and participant.identity == self._issued.browser_identity:
                self._browser_connected = True
                self._timing.mark("phone_joined")

        @room.on("participant_disconnected")
        def on_participant_disconnected(participant) -> None:
            if participant.identity == self._issued.browser_identity:
                self._browser_connected = False
                self._media_ready = False
                self._cancel_browser_audio_task()

        @room.on("track_subscribed")
        def on_track_subscribed(track, _publication, participant) -> None:
            if (
                self._closed or self._silent or participant.identity != self._issued.browser_identity
                or track.kind != rtc.TrackKind.KIND_AUDIO
            ):
                return
            self._cancel_browser_audio_task()
            stream = rtc.AudioStream.from_track(
                track=track,
                sample_rate=REMOTE_AUDIO_RATE,
                num_channels=1,
                frame_size_ms=_PCM_FRAME_MS,
                capacity=self._queue_max_chunks,
            )
            self._audio_stream = stream
            self._browser_connected = True
            self._media_ready = True
            self._timing.mark("phone_track_subscribed")
            self._timing.mark("media_ready")
            self._browser_audio_task = asyncio.create_task(
                self._pump_browser_audio(stream)
            )

        self._subscribe_track = on_track_subscribed

        @room.on("track_published")
        def on_track_published(publication, participant) -> None:
            if not self._closed and not self._silent and participant.identity == self._issued.browser_identity:
                publication.set_subscribed(True)

        @room.on("track_unsubscribed")
        def on_track_unsubscribed(_track, _publication, participant) -> None:
            if participant.identity == self._issued.browser_identity:
                self._media_ready = False
                self._cancel_browser_audio_task()

        @room.on("track_muted")
        def on_track_muted(participant, _publication) -> None:
            if participant.identity == self._issued.browser_identity:
                self._media_ready = False

        @room.on("track_unmuted")
        def on_track_unmuted(participant, publication) -> None:
            if (
                participant.identity == self._issued.browser_identity
                and publication.track is not None
            ):
                on_track_subscribed(publication.track, publication, participant)

        @room.on("data_received")
        def on_data_received(packet) -> None:
            participant = packet.participant
            if (
                self._closed or self._silent or participant is None
                or participant.identity != self._issued.browser_identity
                or packet.topic != REMOTE_CONTROL_TOPIC
            ):
                return
            command = _decode_control_payload(packet.data)
            if command is None:
                return
            if command.get("type") == "client_timing":
                if len(packet.data) <= MAX_TIMING_BYTES:
                    self._timing.receive_client(command)
                return
            if command.get("type") == "hangup":
                # A reserved slot cannot be displaced by ordinary controls or telemetry.
                if self._hangup_command is None:
                    self._hangup_command = command
            elif not self._commands.full():
                self._commands.put_nowait(command)
            self._command_ready.set()

        @room.on("disconnected")
        def on_disconnected(_reason) -> None:
            self._connected = False
            self._browser_connected = False
            self._media_ready = False

        await room.connect(
            self._issued.livekit_url,
            self._issued.edge_token,
            rtc.RoomOptions(connect_timeout=self._connect_timeout_seconds, auto_subscribe=not self._silent),
        )
        if self._closed:
            await room.disconnect()
            raise asyncio.CancelledError()
        self._connected = True
        self._timing.mark("room_connect_return")

    async def bind_winner(self, issued: IssuedLiveKitSession) -> None:
        if self._closed or not self._connected or issued.room_name != self._issued.room_name:
            raise ConnectionError("prepared room unavailable")
        self._issued = issued
        if self._silent:
            self._timing.mark("winner_bound")
        self._silent = False
        room, rtc = self._room, self._rtc
        participant = room.remote_participants.get(issued.browser_identity)
        if participant is not None:
            self._browser_connected = True
            self._timing.mark("phone_joined")
            for publication in participant.track_publications.values():
                publication.set_subscribed(True)
                if publication.track is not None:
                    self._subscribe_track(publication.track, publication, participant)
        source = rtc.AudioSource(
            REMOTE_AUDIO_RATE,
            1,
            queue_size_ms=self._queue_max_chunks * _PCM_FRAME_MS,
        )
        self._audio_source = source
        track = rtc.LocalAudioTrack.create_audio_track("phone-downlink", source)
        options = rtc.TrackPublishOptions()
        options.source = rtc.TrackSource.SOURCE_MICROPHONE
        self._timing.mark("downlink_publish_start")
        await room.local_participant.publish_track(track, options)
        self._timing.mark("downlink_published")
        self._publisher_task = asyncio.create_task(self._publish_modem_audio())
        self._modem_stats_task = asyncio.create_task(self._log_modem_publish_stats())

        participant = room.remote_participants.get(self._issued.browser_identity)
        if participant is not None:
            self._browser_connected = True
            self._timing.mark("phone_joined")
        self._timing.mark("endpoint_connect_return")

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._connected = False
        self._timing.mark("preconnect_closed")
        browser_audio_task = self._browser_audio_task
        self._cancel_browser_audio_task()
        tasks = [
            task
            for task in (
                browser_audio_task,
                self._publisher_task,
                self._modem_stats_task,
            )
            if task is not None
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._browser_audio_task = None
        self._publisher_task = None
        self._modem_stats_task = None

        if self._audio_stream is not None:
            try:
                await self._audio_stream.aclose()
            except Exception as exc:  # noqa: BLE001
                logger.debug("关闭 LiveKit 浏览器音轨失败: %s", type(exc).__name__)
            self._audio_stream = None
        if self._stream_close_tasks:
            await asyncio.gather(*self._stream_close_tasks, return_exceptions=True)
            self._stream_close_tasks.clear()
        if self._audio_source is not None:
            try:
                await self._audio_source.aclose()
            except Exception as exc:  # noqa: BLE001
                logger.debug("关闭 LiveKit 音频源失败: %s", type(exc).__name__)
            self._audio_source = None
        if self._room is not None:
            try:
                await self._room.disconnect()
            except Exception as exc:  # noqa: BLE001
                logger.debug("断开 LiveKit 房间失败: %s", type(exc).__name__)
            self._room = None
        self._browser_connected = False
        self._media_ready = False

    async def next_command(self, timeout: float) -> dict[str, Any] | None:
        try:
            await asyncio.wait_for(self._command_ready.wait(), timeout)
        except TimeoutError:
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
        while len(chunks) < max_chunks:
            try:
                chunks.append(self._browser_audio.get_nowait())
            except asyncio.QueueEmpty:
                break
        self._browser_in_stats.maybe_log(
            queued=self._browser_audio.qsize(),
            media_ready=self._media_ready,
        )
        return chunks

    def push_modem_audio(self, pcm: bytes) -> None:
        if self._silent or self._closed or not pcm:
            return
        # Keep int16 samples aligned and publish fixed 20 ms frames.
        self._modem_buffer.extend(pcm[: len(pcm) - (len(pcm) % 2)])
        while len(self._modem_buffer) >= _PCM_FRAME_BYTES:
            frame = bytes(self._modem_buffer[:_PCM_FRAME_BYTES])
            del self._modem_buffer[:_PCM_FRAME_BYTES]
            _put_latest(self._modem_audio, frame)

    async def send_event(self, event: dict[str, Any]) -> None:
        room = self._room
        if room is None or not self._browser_connected:
            if event.get("type") == "hangup.ack":
                raise ConnectionError("control channel unavailable")
            return
        if event.get("type") == "status":
            event = {**event, "clientTimingVersion": 1}
        payload = json.dumps(event, separators=(",", ":"), ensure_ascii=False).encode(
            "utf-8"
        )
        if len(payload) > _MAX_CONTROL_BYTES:
            raise ValueError("远程状态消息过大")
        await room.local_participant.publish_data(
            payload,
            reliable=True,
            destination_identities=[self._issued.browser_identity],
            topic=REMOTE_STATUS_TOPIC,
        )

    async def _pump_browser_audio(self, stream: Any) -> None:
        try:
            async for event in stream:
                pcm = bytes(event.frame.data)
                if pcm:
                    self._timing.mark("phone_first_frame")
                    self._browser_in_stats.add(pcm)
                    _put_latest(self._browser_audio, pcm)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("读取 LiveKit 浏览器音频失败: %s", type(exc).__name__)
            self._media_ready = False

    async def _publish_modem_audio(self) -> None:
        rtc = self._rtc
        if rtc is None:
            raise RuntimeError("LiveKit RTC module is not initialized")

        source = self._audio_source
        if source is None:
            return
        try:
            while True:
                pcm = await self._modem_audio.get()
                amplified = apply_pcm_gain(pcm, self._downlink_gain)
                frame = rtc.AudioFrame(
                    data=amplified,
                    sample_rate=REMOTE_AUDIO_RATE,
                    num_channels=1,
                    samples_per_channel=len(amplified) // 2,
                )
                await source.capture_frame(frame)
                self._modem_publish_stats.add(amplified)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("发布 LiveKit 模组音频失败: %s", type(exc).__name__)
            self._media_ready = False

    async def _log_modem_publish_stats(self) -> None:
        try:
            while True:
                await asyncio.sleep(_AUDIO_STATS_IDLE_TICK_SECONDS)
                self._modem_publish_stats.maybe_log(
                    queued=self._modem_audio.qsize(),
                    gain=self._downlink_gain,
                )
        except asyncio.CancelledError:
            raise

    def _cancel_browser_audio_task(self) -> None:
        task = self._browser_audio_task
        if task is not None and not task.done():
            task.cancel()
        self._browser_audio_task = None
        stream = self._audio_stream
        self._audio_stream = None
        if stream is not None:
            close_task = asyncio.create_task(stream.aclose())
            self._stream_close_tasks.add(close_task)
            close_task.add_done_callback(self._stream_close_tasks.discard)
