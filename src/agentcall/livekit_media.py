"""LiveKit implementation of the Remote Web Dialer media/control endpoint."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

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
        self._stream_close_tasks: set[asyncio.Task[None]] = set()
        self._browser_connected = False
        self._media_ready = False
        self._closed = False
        # 上行第一段观测：浏览器（手机）→ LiveKit → Edge 的入站帧统计。
        # 打点由 take_browser_audio（remote pump 每 10ms 调）驱动，
        # 因此 LiveKit 一帧未推时也会按期打出 frames=0。
        self._browser_in_stats = PcmFlowStats("uplink1_lk_in")

    @property
    def media_ready(self) -> bool:
        return self._media_ready

    @property
    def browser_connected(self) -> bool:
        return self._browser_connected

    async def connect(self) -> None:
        rtc = self._rtc
        if rtc is None:
            from livekit import rtc as livekit_rtc

            rtc = livekit_rtc
            self._rtc = rtc

        room = rtc.Room()
        self._room = room

        @room.on("participant_connected")
        def on_participant_connected(participant) -> None:
            if participant.identity == self._issued.browser_identity:
                self._browser_connected = True

        @room.on("participant_disconnected")
        def on_participant_disconnected(participant) -> None:
            if participant.identity == self._issued.browser_identity:
                self._browser_connected = False
                self._media_ready = False
                self._cancel_browser_audio_task()

        @room.on("track_subscribed")
        def on_track_subscribed(track, _publication, participant) -> None:
            if (
                participant.identity != self._issued.browser_identity
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
            self._browser_audio_task = asyncio.create_task(
                self._pump_browser_audio(stream)
            )

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
                participant is None
                or participant.identity != self._issued.browser_identity
                or packet.topic != REMOTE_CONTROL_TOPIC
            ):
                return
            command = _decode_control_payload(packet.data)
            if command is None or self._commands.full():
                return
            self._commands.put_nowait(command)

        @room.on("disconnected")
        def on_disconnected(_reason) -> None:
            self._browser_connected = False
            self._media_ready = False

        await room.connect(
            self._issued.livekit_url,
            self._issued.edge_token,
            rtc.RoomOptions(connect_timeout=self._connect_timeout_seconds),
        )

        source = rtc.AudioSource(
            REMOTE_AUDIO_RATE,
            1,
            queue_size_ms=self._queue_max_chunks * _PCM_FRAME_MS,
        )
        self._audio_source = source
        track = rtc.LocalAudioTrack.create_audio_track("phone-downlink", source)
        options = rtc.TrackPublishOptions()
        options.source = rtc.TrackSource.SOURCE_MICROPHONE
        await room.local_participant.publish_track(track, options)
        self._publisher_task = asyncio.create_task(self._publish_modem_audio())

        participant = room.remote_participants.get(self._issued.browser_identity)
        if participant is not None:
            self._browser_connected = True

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        browser_audio_task = self._browser_audio_task
        self._cancel_browser_audio_task()
        tasks = [
            task
            for task in (browser_audio_task, self._publisher_task)
            if task is not None
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._browser_audio_task = None
        self._publisher_task = None

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
            return await asyncio.wait_for(self._commands.get(), timeout)
        except TimeoutError:
            return None

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
        if not pcm:
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
            return
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
                frame = rtc.AudioFrame(
                    data=pcm,
                    sample_rate=REMOTE_AUDIO_RATE,
                    num_channels=1,
                    samples_per_channel=len(pcm) // 2,
                )
                await source.capture_frame(frame)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("发布 LiveKit 模组音频失败: %s", type(exc).__name__)
            self._media_ready = False

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
