"""Remote Web Dialer POC: state machine, media safety, and LiveKit invite tests."""

from __future__ import annotations

import asyncio
import base64
import inspect
import json
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from livekit import api

from agentcall.audio_bridge import (
    FfmpegAudioBridge,
    ModemAudioBridge,
    SerialPcmAudioBridge,
    create_audio_bridge,
)
from agentcall.call_log import CallLogger
from agentcall.livekit_media import LiveKitRemoteMediaEndpoint, _decode_control_payload
from agentcall.remote_dialer import (
    REMOTE_AUDIO_RATE,
    RemoteDialerRuntimeConfig,
    RemoteWebDialerCoordinator,
    issue_livekit_session,
)


class FakeRemoteEndpoint:
    def __init__(self, *, media_ready: bool = True) -> None:
        self._media_ready = media_ready
        self._browser_connected = media_ready
        self.commands: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.browser_audio: deque[bytes] = deque()
        self.modem_audio: list[bytes] = []
        self.events: list[dict[str, Any]] = []
        self.connected = False
        self.closed = False

    @property
    def media_ready(self) -> bool:
        return self._media_ready

    @property
    def browser_connected(self) -> bool:
        return self._browser_connected

    async def connect(self) -> None:
        self.connected = True

    async def close(self) -> None:
        self.closed = True

    async def next_command(self, timeout: float) -> dict[str, Any] | None:
        try:
            return await asyncio.wait_for(self.commands.get(), timeout)
        except TimeoutError:
            return None

    def take_browser_audio(self, max_chunks: int = 10) -> list[bytes]:
        chunks: list[bytes] = []
        while self.browser_audio and len(chunks) < max_chunks:
            chunks.append(self.browser_audio.popleft())
        return chunks

    def push_modem_audio(self, pcm: bytes) -> None:
        self.modem_audio.append(pcm)

    async def send_event(self, event: dict[str, Any]) -> None:
        self.events.append(dict(event))

    def set_media_ready(self, ready: bool) -> None:
        self._media_ready = ready
        self._browser_connected = ready


class FakeModem:
    def __init__(self, *, auto_connect: bool = True) -> None:
        self.auto_connect = auto_connect
        self.connected = False
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def dial(self, number: str) -> str:
        self.calls.append(("dial", (number,)))
        self.connected = self.auto_connect
        return "OK"

    def is_call_connected(self) -> bool:
        return self.connected

    def initialize_for_voice(self, audio_mode: str) -> None:
        self.calls.append(("initialize_for_voice", (audio_mode,)))

    def send_dtmf(self, digits: str) -> bool:
        self.calls.append(("send_dtmf", (digits,)))
        return True

    def pcm_ready(self) -> bool:
        return True

    def hangup(self) -> None:
        self.calls.append(("hangup", ()))
        self.connected = False


class FakeBridge:
    def __init__(self) -> None:
        self.started = False
        self.stopped = False
        self.written: list[bytes] = []
        self.modem_reads: deque[bytes] = deque()

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True

    def read_modem_chunk(self) -> bytes:
        return self.modem_reads.popleft() if self.modem_reads else b""

    def write_modem_chunks(self, chunks) -> None:
        self.written.extend(chunk for chunk in chunks if chunk)


class _FakeLiveKitStream:
    def __init__(self) -> None:
        self.closed = False
        self._wait = asyncio.Event()

    def __aiter__(self):
        return self

    async def __anext__(self):
        await self._wait.wait()
        raise StopAsyncIteration

    async def aclose(self) -> None:
        self.closed = True
        self._wait.set()


class _FakeLiveKitAudioSource:
    instances: list[_FakeLiveKitAudioSource] = []

    def __init__(self, sample_rate: int, channels: int, *, queue_size_ms: int) -> None:
        self.sample_rate = sample_rate
        self.channels = channels
        self.queue_size_ms = queue_size_ms
        self.frames: list[Any] = []
        self.closed = False
        self.instances.append(self)

    async def capture_frame(self, frame) -> None:
        self.frames.append(frame)

    async def aclose(self) -> None:
        self.closed = True


class _FakeLiveKitAudioStream:
    instances: list[_FakeLiveKitStream] = []

    @classmethod
    def from_track(cls, **_kwargs):
        stream = _FakeLiveKitStream()
        cls.instances.append(stream)
        return stream


class _FakeLiveKitParticipant:
    def __init__(self) -> None:
        self.published: list[tuple[Any, Any]] = []
        self.data: list[tuple[bytes, dict[str, Any]]] = []

    async def publish_track(self, track, options):
        self.published.append((track, options))

    async def publish_data(self, payload: bytes, **kwargs) -> None:
        self.data.append((payload, kwargs))


class _FakeLiveKitRoom:
    latest: _FakeLiveKitRoom | None = None

    def __init__(self) -> None:
        self.callbacks: dict[str, Any] = {}
        self.local_participant = _FakeLiveKitParticipant()
        self.remote_participants: dict[str, Any] = {}
        self.connected: tuple[str, str, Any] | None = None
        self.disconnected = False
        type(self).latest = self

    def on(self, event: str):
        def register(callback):
            self.callbacks[event] = callback
            return callback

        return register

    async def connect(self, url: str, token: str, options) -> None:
        self.connected = (url, token, options)

    async def disconnect(self) -> None:
        self.disconnected = True


@dataclass
class _FakeRoomOptions:
    connect_timeout: float | None = None


class _FakeTrackPublishOptions:
    source: str | None = None


class _FakeAudioFrame:
    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs


def _fake_rtc_module():
    _FakeLiveKitAudioSource.instances.clear()
    _FakeLiveKitAudioStream.instances.clear()
    return SimpleNamespace(
        Room=_FakeLiveKitRoom,
        RoomOptions=_FakeRoomOptions,
        AudioSource=_FakeLiveKitAudioSource,
        AudioStream=_FakeLiveKitAudioStream,
        LocalAudioTrack=SimpleNamespace(
            create_audio_track=lambda name, source: (name, source)
        ),
        TrackPublishOptions=_FakeTrackPublishOptions,
        TrackSource=SimpleNamespace(SOURCE_MICROPHONE="microphone"),
        TrackKind=SimpleNamespace(KIND_AUDIO="audio"),
        AudioFrame=_FakeAudioFrame,
    )


async def _wait_for(predicate, timeout: float = 1.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("condition was not met before timeout")


def _runtime() -> RemoteDialerRuntimeConfig:
    return RemoteDialerRuntimeConfig(
        audio_mode="uac",
        audio_keyword="Interface",
        pcm_port=None,
        pcm_baudrate=921600,
        tx_gain=1.0,
        disconnect_grace_seconds=0.05,
        outbound_max_seconds=2.0,
        connect_timeout_seconds=0.2,
        dtmf_mode="inband",
    )


def _coordinator(
    endpoint: FakeRemoteEndpoint,
    modem: FakeModem,
    bridge: FakeBridge,
    *,
    call_logger: CallLogger | None = None,
) -> RemoteWebDialerCoordinator:
    return RemoteWebDialerCoordinator(
        session_id="session-test",
        expires_at=time.time() + 60,
        modem=modem,  # type: ignore[arg-type]
        endpoint=endpoint,
        runtime=_runtime(),
        bridge_factory=lambda **_kwargs: bridge,  # type: ignore[arg-type]
        call_logger=call_logger,
        reserve_line=lambda _owner: None,
        release_line=lambda _owner: None,
    )


def test_media_must_be_ready_before_dial() -> None:
    async def run() -> None:
        endpoint = FakeRemoteEndpoint(media_ready=False)
        modem = FakeModem()
        coordinator = _coordinator(endpoint, modem, FakeBridge())
        task = asyncio.create_task(coordinator.run())
        await _wait_for(lambda: endpoint.connected)
        await endpoint.commands.put(
            {"type": "dial", "number": "10000", "idempotency_key": "request-1"}
        )
        await _wait_for(lambda: any(e.get("code") == "media_not_ready" for e in endpoint.events))
        coordinator.request_stop("test_done")
        await task

        assert not any(name == "dial" for name, _args in modem.calls)
        assert endpoint.closed is True

    asyncio.run(run())


def test_duplicate_dial_command_never_dials_twice() -> None:
    async def run() -> None:
        endpoint = FakeRemoteEndpoint()
        modem = FakeModem()
        coordinator = _coordinator(endpoint, modem, FakeBridge())
        task = asyncio.create_task(coordinator.run())
        await _wait_for(lambda: endpoint.connected)
        command = {"type": "dial", "number": "10000", "idempotency_key": "same-key"}
        await endpoint.commands.put(command)
        await endpoint.commands.put(command)
        await _wait_for(lambda: sum(name == "dial" for name, _args in modem.calls) == 1)
        await endpoint.commands.put({"type": "hangup"})
        await task

        assert sum(name == "dial" for name, _args in modem.calls) == 1

    asyncio.run(run())


def test_full_duplex_audio_recording_and_dtmf(tmp_path: Path) -> None:
    async def run() -> None:
        endpoint = FakeRemoteEndpoint()
        endpoint.browser_audio.append(b"\x01\x00" * 160)
        modem = FakeModem()
        bridge = FakeBridge()
        bridge.modem_reads.append(b"\x02\x00" * 160)
        logger = CallLogger(tmp_path / "recordings")
        coordinator = _coordinator(endpoint, modem, bridge, call_logger=logger)
        task = asyncio.create_task(coordinator.run())
        await _wait_for(lambda: endpoint.connected)
        await endpoint.commands.put(
            {"type": "dial", "number": "10000", "idempotency_key": "audio-call"}
        )
        await _wait_for(lambda: any(e.get("status") == "connected" for e in endpoint.events))
        await endpoint.commands.put({"type": "dtmf", "digits": "2"})
        await _wait_for(lambda: bool(endpoint.modem_audio) and len(bridge.written) >= 2)
        await endpoint.commands.put({"type": "hangup"})
        await task

        assert bridge.started and bridge.stopped
        assert bridge.written[0] == b"\x01\x00" * 160
        assert endpoint.modem_audio[0] == b"\x02\x00" * 160

        [meta_path] = list((tmp_path / "recordings").glob("*/meta.json"))
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        assert meta["source"] == "remote_web_dialer"
        assert meta["uplink_bytes"] == 320
        assert meta["downlink_bytes"] > 320  # browser speech plus in-band DTMF

    asyncio.run(run())


def test_media_disconnect_grace_hangs_up_real_call() -> None:
    async def run() -> None:
        endpoint = FakeRemoteEndpoint()
        modem = FakeModem()
        coordinator = _coordinator(endpoint, modem, FakeBridge())
        task = asyncio.create_task(coordinator.run())
        await _wait_for(lambda: endpoint.connected)
        await endpoint.commands.put(
            {"type": "dial", "number": "10000", "idempotency_key": "disconnect"}
        )
        await _wait_for(lambda: any(e.get("status") == "connected" for e in endpoint.events))
        endpoint.set_media_ready(False)
        await task

        assert any(name == "hangup" for name, _args in modem.calls)
        assert any(e.get("reason") == "media_disconnected" for e in endpoint.events)

    asyncio.run(run())


def test_livekit_invite_is_room_scoped_short_lived_and_fragment_only() -> None:
    issued = issue_livekit_session(
        livekit_url="wss://example.livekit.cloud",
        api_key="test-key",
        api_secret="test-secret-with-enough-entropy-32",
        public_url="https://dial.callpilot.example/",
        ttl_seconds=300,
        now=time.time(),
    )

    assert issued.invite.url.startswith("https://dial.callpilot.example/#")
    assert "test-secret" not in issued.invite.url
    fragment = issued.invite.url.split("#", 1)[1]
    padded = fragment + "=" * (-len(fragment) % 4)
    payload = json.loads(base64.urlsafe_b64decode(padded))
    assert payload == {
        "v": 1,
        "url": "wss://example.livekit.cloud",
        "token": issued.browser_token,
        "sessionId": issued.invite.session_id,
    }

    verifier = api.TokenVerifier("test-key", "test-secret-with-enough-entropy-32")
    browser_claims = verifier.verify(issued.browser_token)
    edge_claims = verifier.verify(issued.edge_token)
    assert browser_claims.video is not None
    assert browser_claims.video.room == issued.room_name
    assert browser_claims.video.room_join is True
    assert browser_claims.video.can_publish_sources == ["microphone"]
    assert edge_claims.video is not None
    assert edge_claims.video.room == issued.room_name
    assert issued.invite.expires_at - time.time() <= 301
    assert REMOTE_AUDIO_RATE == 8000


def test_livekit_control_payload_is_bounded_and_must_be_object() -> None:
    assert _decode_control_payload(b'{"type":"dial","number":"10000"}') == {
        "type": "dial",
        "number": "10000",
    }
    assert _decode_control_payload(b"[]") is None
    assert _decode_control_payload(b"not-json") is None
    assert _decode_control_payload(b"x" * 4097) is None
    assert _decode_control_payload(b'{"type":42}') is None


def test_livekit_modem_audio_queue_is_bounded_to_latest_frames() -> None:
    issued = issue_livekit_session(
        livekit_url="wss://example.livekit.cloud",
        api_key="test-key",
        api_secret="test-secret-with-enough-entropy-32",
        public_url="https://dial.callpilot.example/",
    )
    endpoint = LiveKitRemoteMediaEndpoint(issued, queue_max_chunks=2)
    frame = b"\x01\x00" * 160
    endpoint.push_modem_audio(frame)
    endpoint.push_modem_audio(b"\x02\x00" * 160)
    endpoint.push_modem_audio(b"\x03\x00" * 160)

    assert endpoint._modem_audio.qsize() == 2
    assert endpoint._modem_audio.get_nowait() == b"\x02\x00" * 160
    assert endpoint._modem_audio.get_nowait() == b"\x03\x00" * 160


def test_livekit_endpoint_filters_identity_and_topic_and_tracks_media_state() -> None:
    async def run() -> None:
        issued = issue_livekit_session(
            livekit_url="wss://example.livekit.cloud",
            api_key="test-key",
            api_secret="test-secret-with-enough-entropy-32",
            public_url="https://dial.callpilot.example/",
        )
        endpoint = LiveKitRemoteMediaEndpoint(
            issued,
            rtc_module=_fake_rtc_module(),
            connect_timeout_seconds=7,
        )
        await endpoint.connect()
        room = _FakeLiveKitRoom.latest
        assert room is not None and room.connected is not None
        assert room.connected[2].connect_timeout == 7

        expected = SimpleNamespace(identity=issued.browser_identity)
        intruder = SimpleNamespace(identity="not-the-browser")
        packet = lambda participant, topic: SimpleNamespace(  # noqa: E731
            participant=participant,
            topic=topic,
            data=b'{"type":"dial","number":"10000"}',
        )
        room.callbacks["data_received"](packet(intruder, "callpilot.control"))
        room.callbacks["data_received"](packet(expected, "wrong.topic"))
        assert await endpoint.next_command(0.001) is None

        room.callbacks["data_received"](packet(expected, "callpilot.control"))
        assert await endpoint.next_command(0.01) == {
            "type": "dial",
            "number": "10000",
        }

        audio_track = SimpleNamespace(kind="audio")
        publication = SimpleNamespace(track=audio_track)
        room.callbacks["track_subscribed"](audio_track, publication, intruder)
        assert endpoint.media_ready is False
        room.callbacks["track_subscribed"](audio_track, publication, expected)
        assert endpoint.media_ready is True
        room.callbacks["track_muted"](expected, publication)
        assert endpoint.media_ready is False
        room.callbacks["track_unmuted"](expected, publication)
        assert endpoint.media_ready is True
        room.callbacks["participant_disconnected"](expected)
        assert endpoint.browser_connected is False
        assert endpoint.media_ready is False

        await endpoint.close()
        assert room.disconnected is True

    asyncio.run(run())


def test_installed_livekit_sdk_exposes_used_runtime_signatures() -> None:
    from livekit import rtc

    stream_params = inspect.signature(rtc.AudioStream.from_track).parameters
    source_params = inspect.signature(rtc.AudioSource).parameters
    publish_params = inspect.signature(rtc.LocalParticipant.publish_data).parameters
    connect_params = inspect.signature(rtc.Room.connect).parameters

    assert {"track", "sample_rate", "num_channels", "frame_size_ms", "capacity"} <= set(
        stream_params
    )
    assert "queue_size_ms" in source_params
    assert {"destination_identities", "topic", "reliable"} <= set(publish_params)
    assert "options" in connect_params


def test_all_real_audio_bridges_satisfy_remote_full_duplex_contract() -> None:
    required = {"start", "stop", "read_modem_chunk", "write_modem_chunks"}
    for bridge_type in (ModemAudioBridge, SerialPcmAudioBridge, FfmpegAudioBridge):
        assert required <= set(dir(bridge_type)), bridge_type.__name__

    factory_params = inspect.signature(create_audio_bridge).parameters
    assert {"mode", "device_keyword", "pcm_port", "pcm_baudrate", "tx_gain"} <= set(
        factory_params
    )
