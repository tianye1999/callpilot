"""Direct media acceptance, including a real DTLS-SRTP aiortc loopback."""

from __future__ import annotations

import asyncio
import json
import math
import struct
import sys
from types import SimpleNamespace
from uuid import uuid4

import pytest
from aiortc import RTCConfiguration, RTCPeerConnection, RTCSessionDescription
from aiortc.mediastreams import AudioStreamTrack
from fakes.webrtc_loopback import loopback_phase

from agentcall.call_timing import CallTiming
from agentcall.remote_dialer import REMOTE_CONTROL_TOPIC
from agentcall.webrtc_media import WebRtcRemoteMediaEndpoint


class ToneTrack(AudioStreamTrack):
    async def recv(self):
        frame = await super().recv()
        pcm = struct.pack("<160h", *(int(6000 * math.sin(2 * math.pi * 440 * n / 8000)) for n in range(160)))
        frame.planes[0].update(pcm)
        return frame


class FakeChannel:
    label = REMOTE_CONTROL_TOPIC
    readyState = "open"
    bufferedAmount = 0

    def __init__(self) -> None:
        self.handlers = {}
        self.sent = []

    def on(self, name):
        def register(handler):
            self.handlers[name] = handler
            return handler
        return register

    def send(self, payload):
        self.sent.append(json.loads(payload))

    def close(self):
        self.readyState = "closed"


class FakePeer:
    connectionState = "new"

    def __init__(self) -> None:
        self.handlers = {}
        self.closed = 0
        self.localDescription = SimpleNamespace(type="answer", sdp="v=0\r\n")
        self.remote_gate = None
        self.entered = asyncio.Event()
        self.tracks = []

    def on(self, name):
        def register(handler):
            self.handlers[name] = handler
            return handler
        return register

    async def setRemoteDescription(self, _description):
        self.entered.set()
        if self.remote_gate is not None:
            await self.remote_gate.wait()

    def addTrack(self, track):
        self.tracks.append(track)

    async def createAnswer(self):
        return self.localDescription

    async def setLocalDescription(self, _description):
        pass

    async def close(self):
        self.closed += 1
        self.connectionState = "closed"


def endpoint(peer, **kwargs):
    return WebRtcRemoteMediaEndpoint(
        {"type": "offer", "sdp": "v=0\r\n"}, [], call_id="test-call", peer_factory=lambda _: peer, **kwargs,
    )


def test_answer_does_not_wait_for_connection_and_close_fences_late_events():
    async def scenario():
        peer = FakePeer()
        media = endpoint(peer)
        await media.connect()
        assert media.answer == {"type": "answer", "sdp": "v=0\r\n"}
        assert not media.browser_connected and not media.media_ready
        await media.close()
        await media.close()
        peer.connectionState = "connected"
        peer.handlers["connectionstatechange"]()
        channel = FakeChannel()
        peer.handlers["datachannel"](channel)
        assert channel.readyState == "closed"
        assert not media.browser_connected and not media.media_ready
        assert peer.closed == 1
    asyncio.run(scenario())


def test_stack_warmup_is_idempotent_and_connect_has_no_local_imports(monkeypatch):
    import builtins
    import sys

    from agentcall.webrtc_media import warm_webrtc_stack

    async def scenario():
        first = warm_webrtc_stack()
        assert warm_webrtc_stack() is first
        await asyncio.wrap_future(first)
        assert all(name in sys.modules for name in ("aiortc", "av", "pylibsrtp", "aioice"))
        seen = []
        original = builtins.__import__

        def observe(name, globals=None, *args, **kwargs):
            if globals and globals.get("__name__") == "agentcall.webrtc_media":
                seen.append(name)
            return original(name, globals, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", observe)
        media = endpoint(FakePeer())
        await media.connect()
        await media.close()
        assert seen == []

    asyncio.run(scenario())


def test_cold_stack_import_runs_off_event_loop(monkeypatch):
    import threading

    from agentcall import webrtc_media

    async def scenario():
        entered, release = threading.Event(), threading.Event()
        original = webrtc_media.importlib.import_module
        loop_thread = threading.get_ident()

        def blocked_import(name):
            if name == "aiortc":
                assert threading.get_ident() != loop_thread
                entered.set()
                assert release.wait(5)
            return original(name)

        monkeypatch.setattr(webrtc_media, "_warm_future", None)
        monkeypatch.setattr(webrtc_media.importlib, "import_module", blocked_import)
        future = webrtc_media.warm_webrtc_stack()
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            assert not future.done()
            assert webrtc_media.warm_webrtc_stack() is future
        finally:
            release.set()
        await asyncio.wrap_future(future)

    asyncio.run(scenario())


def test_close_fence_cannot_bypass_check_drain(monkeypatch):
    from agentcall import webrtc_media

    async def scenario():
        await asyncio.wrap_future(webrtc_media.warm_webrtc_stack())
        draining, release = asyncio.Event(), asyncio.Event()

        async def drain(_pc):
            draining.set()
            await release.wait()

        monkeypatch.setattr(webrtc_media._stack.ice, "stop_peer_checks", drain)
        peer = FakePeer()
        peer.remote_gate = asyncio.Event()
        media = endpoint(peer)
        connecting = asyncio.create_task(media.connect())
        await peer.entered.wait()
        closing = asyncio.create_task(media.close())
        await draining.wait()
        peer.remote_gate.set()
        checkpoint = asyncio.Event()
        asyncio.get_running_loop().call_soon(checkpoint.set)
        await checkpoint.wait()
        assert peer.closed == 0
        release.set()
        await closing
        with pytest.raises(asyncio.CancelledError):
            await connecting
        assert peer.closed == 1

    asyncio.run(scenario())


def test_close_during_uncancellable_connect_never_publishes_answer():
    async def scenario():
        peer = FakePeer()
        peer.remote_gate = asyncio.Event()
        media = endpoint(peer)
        connecting = asyncio.create_task(media.connect())
        await peer.entered.wait()
        await media.close()
        peer.remote_gate.set()
        with pytest.raises(asyncio.CancelledError):
            await connecting
        assert media.answer is None
        assert not peer.tracks
        assert not media.browser_connected and not media.media_ready
    asyncio.run(scenario())


def test_hangup_reserved_slot_and_timing_cannot_be_starved_by_control_flood():
    async def scenario():
        peer = FakePeer()
        media = endpoint(peer)
        records = []
        timing = CallTiming(SimpleNamespace(log_event=lambda kind, **fields: records.append((kind, fields))))
        media.set_timing(timing)
        await media.connect()
        channel = FakeChannel()
        peer.handlers["datachannel"](channel)
        receive = channel.handlers["message"]
        for _ in range(40):
            receive('{"type":"dtmf","digits":"1"}')
        receive(json.dumps({"type": "client_timing", "v": 1, "attempt": str(uuid4()),
                            "seq": 1, "marks": {"answer_action": -10}}))
        receive('{"type":"hangup","commandId":"first"}')
        receive('{"type":"hangup","commandId":"second"}')
        assert await media.next_command(0.1) == {"type": "hangup", "commandId": "first"}
        assert await media.next_command(0.1) == {"type": "dtmf", "digits": "1"}
        assert media._commands.qsize() == 31
        assert ("latency", {"stage": "client_answer_action", "ms": -10}) in records
        receive(json.dumps({"type": "client_rtp_stats", "v": 1, "seq": 1, "elapsedMs": 5,
                            "outPackets": 2, "outBytes": 30, "inPackets": 3, "inBytes": 40,
                            "lost": 0, "rttMs": None, "localCandidate": "host",
                            "remoteCandidate": "relay", "pairState": "succeeded"}))
        assert any(kind == "client_rtp_stats" and fields["outPackets"] == 2 for kind, fields in records)
        receive("[1]")
        receive(b"\xff")
        receive("x" * 5000)
        assert media._commands.qsize() == 31
        await media.send_event({"type": "status", "status": "media_ready"})
        assert channel.sent[-1]["clientTimingVersion"] == 1
        await media.close()
        receive('{"type":"hangup"}')
        assert await media.next_command(0.001) is None
        with pytest.raises(ConnectionError):
            await media.send_event({"type": "hangup.ack"})
    asyncio.run(scenario())


def test_pcm_queues_are_bounded_and_ready_requires_actual_audio():
    async def scenario():
        peer = FakePeer()
        media = endpoint(peer, queue_max_chunks=2)
        await media.connect()
        peer.connectionState = "connected"
        peer.handlers["connectionstatechange"]()
        channel = FakeChannel()
        peer.handlers["datachannel"](channel)
        assert media.browser_connected and not media.media_ready
        for index in range(50):
            media.push_modem_audio(struct.pack("<160h", *([index] * 160)))
        assert media._modem_audio.qsize() == 2
        assert len(media._modem_buffer) < 320
        assert media._modem_audio.get_nowait() == struct.pack("<160h", *([48] * 160))
        await media.close()
    asyncio.run(scenario())


def test_rtp_sampling_bounded_terminal_and_sender_silence_count():
    async def scenario():
        peer = FakePeer()
        media = endpoint(peer)
        observed = []
        media._timing = SimpleNamespace(mark=lambda *_: None,
                                        record_observation=lambda kind, fields: observed.append((kind, fields)))

        async def stats():
            return {"out": SimpleNamespace(type="outbound-rtp", kind="audio", packetsSent=5, bytesSent=100),
                    "in": SimpleNamespace(type="inbound-rtp", kind="audio", packetsReceived=4, packetsLost=0),
                    "transport": SimpleNamespace(type="transport", dtlsState="connected")}

        peer.getStats = stats
        await media.connect()
        await media._local_track.recv()
        for _ in range(12):
            await media._sample_rtp_stats(terminal=False, reason="periodic")
        await media.finish_rtp_stats("commit")
        await media.finish_rtp_stats("close")
        await media.close()
        records = [fields for kind, fields in observed if kind == "edge_rtp_stats"]
        assert len(records) == 10
        assert [record["seq"] for record in records] == list(range(1, 11))
        assert records[-1]["terminal"] and records[-1]["reason"] == "commit"
        assert records[-1]["outPackets"] == 5 and records[-1]["inPackets"] == 4
        assert records[-1]["inBytes"] is None
        assert records[-1]["senderFrames"] == records[-1]["senderSilenceFrames"] == 1
        assert records[-1]["dtlsState"] == "connected"

    asyncio.run(scenario())


def test_modem_sender_logs_pcm_windows_even_when_queue_is_empty(caplog):
    from agentcall import webrtc_media
    from agentcall.pcm_stats import PcmFlowStats

    async def scenario():
        await asyncio.wrap_future(webrtc_media.warm_webrtc_stack())
        source = asyncio.Queue()
        track = webrtc_media._modem_track(source, 1)
        now = [0.0]
        track.stats = PcmFlowStats("downlink1_direct_publish", clock=lambda: now[0])
        caplog.set_level("INFO", logger="agentcall.pcm_stats")
        source.put_nowait(struct.pack("<160h", *([300] * 160)))
        await track.recv()
        assert not caplog.records
        now[0] = 5.0
        track._next_at = None
        await track.recv()
        assert len(caplog.records) == 1
        assert "frames=1 bytes=320 peak=300" in caplog.records[0].message
        assert "senderFrames=2 senderSilenceFrames=1" in caplog.records[0].message
        now[0] = 10.0
        track._next_at = None
        await track.recv()
        assert len(caplog.records) == 2
        assert "frames=0 bytes=0 peak=0" in caplog.records[1].message
        assert "senderFrames=3 senderSilenceFrames=2" in caplog.records[1].message
        track.stop()

    asyncio.run(scenario())


def test_modem_sender_keeps_real_time_cadence_despite_sleep_overshoot_and_encode_time(monkeypatch):
    """11-03 device call: re-anchoring on each wake-up sent ~33 frames/s and dropped caller audio."""
    from agentcall import webrtc_media

    clock = [100.0]

    class FakeTime:
        @staticmethod
        def monotonic():
            return clock[0]

    async def scenario():
        await asyncio.wrap_future(webrtc_media.warm_webrtc_stack())
        real_sleep = asyncio.sleep

        async def overshooting_sleep(delay):
            clock[0] += delay + 0.008  # a busy event loop wakes late
            await real_sleep(0)

        monkeypatch.setattr(webrtc_media, "time", FakeTime)
        monkeypatch.setattr(webrtc_media.asyncio, "sleep", overshooting_sleep)
        source = asyncio.Queue()
        track = webrtc_media._modem_track(source, 1)
        start = clock[0]
        for _ in range(250):
            source.put_nowait(bytes(320))
            await track.recv()
            clock[0] += 0.006  # encoder and RTP send time between pulls
        elapsed = clock[0] - start
        assert track.frames == 250
        assert elapsed < 5.0 + 0.05, f"250 frames took {elapsed:.2f}s instead of 5 s"
        assert track.silence_frames == 0

        clock[0] += 2.0  # a long stall re-anchors instead of bursting 100 stale frames
        before = clock[0]
        await track.recv()
        await track.recv()
        assert clock[0] - before >= 0.02
        track.stop()

    asyncio.run(scenario())


@pytest.mark.parametrize("capacity", [10, 50])
def test_modem_sender_reanchor_trims_oldest_backlog_and_resumes_cadence(monkeypatch, caplog, capacity):
    from agentcall import webrtc_media

    clock = [100.0]

    async def scenario():
        await asyncio.wrap_future(webrtc_media.warm_webrtc_stack())

        async def sleep(delay):
            clock[0] += delay

        monkeypatch.setattr(webrtc_media, "time", SimpleNamespace(monotonic=lambda: clock[0]))
        monkeypatch.setattr(webrtc_media.asyncio, "sleep", sleep)
        caplog.set_level("INFO", logger="agentcall.webrtc_media")
        media = endpoint(FakePeer(), queue_max_chunks=capacity)
        track = webrtc_media._modem_track(media._modem_audio, 1)
        await track.recv()
        clock[0] += 0.5
        # Exercise the real chunking/overflow producer before the sender resumes.
        for value in range(1, capacity + 6):
            media.push_modem_audio(struct.pack("<160h", *([value] * 160)))
        assert media._modem_audio.qsize() == capacity
        resumed_at = clock[0]
        for index, expected in enumerate(range(capacity + 3, capacity + 6)):
            frame = await track.recv()
            assert frame.to_ndarray().flatten().tolist() == [expected] * 160
            assert frame.pts == (index + 1) * 160
            assert clock[0] == pytest.approx(resumed_at + index * 0.02)
            assert media._modem_audio.qsize() == 2 - index
        logs = [r.message for r in caplog.records if "backlog trimmed" in r.message]
        assert len(logs) == 1
        assert f"dropped_frames={capacity - 3}" in logs[0]
        assert f"dropped_ms={(capacity - 3) * 20}" in logs[0]
        # A second stall still trims, but does not flood the log.
        clock[0] += 0.5
        for _ in range(capacity):
            media.push_modem_audio(bytes(320))
        await track.recv()
        assert media._modem_audio.qsize() == 2
        assert len([r for r in caplog.records if "backlog trimmed" in r.message]) == 1
        clock[0] += 5.0
        for _ in range(capacity):
            media.push_modem_audio(bytes(320))
        await track.recv()
        assert media._modem_audio.qsize() == 2
        assert len([r for r in caplog.records if "backlog trimmed" in r.message]) == 2
        track.stop()

    asyncio.run(scenario())


@pytest.mark.parametrize("stall", [0.0, 0.08])
def test_modem_sender_normal_and_short_catchup_preserve_all_frames(monkeypatch, caplog, stall):
    from agentcall import webrtc_media

    clock = [100.0]

    async def scenario():
        await asyncio.wrap_future(webrtc_media.warm_webrtc_stack())

        async def sleep(delay):
            clock[0] += delay

        monkeypatch.setattr(webrtc_media, "time", SimpleNamespace(monotonic=lambda: clock[0]))
        monkeypatch.setattr(webrtc_media.asyncio, "sleep", sleep)
        caplog.set_level("INFO", logger="agentcall.webrtc_media")
        source = asyncio.Queue()
        track = webrtc_media._modem_track(source, 1)
        for value in range(10):
            source.put_nowait(struct.pack("<160h", *([value] * 160)))
        for value in range(10):
            if value == 1:
                clock[0] += stall
            frame = await track.recv()
            assert frame.to_ndarray().flatten().tolist() == [value] * 160
            assert frame.pts == value * 160
            assert source.qsize() == 9 - value
            expected_elapsed = max(stall, value * 0.02) if value else 0.0
            assert clock[0] == pytest.approx(100.0 + expected_elapsed)
        assert track.silence_frames == 0
        assert not [r for r in caplog.records if "backlog trimmed" in r.message]
        track.stop()

    asyncio.run(scenario())


def test_periodic_rtp_stats_stop_at_reserved_terminal_slot(monkeypatch):
    from agentcall import webrtc_media

    async def scenario():
        peer = FakePeer()
        media = endpoint(peer)
        ninth = asyncio.Event()
        samples = []

        def observe(kind, fields):
            if kind == "edge_rtp_stats":
                samples.append(fields)
                if fields["seq"] == 9:
                    ninth.set()

        async def stats():
            return {}

        peer.getStats = stats
        media._timing = SimpleNamespace(mark=lambda *_: None, record_observation=observe)
        monkeypatch.setattr(webrtc_media, "_RTP_STATS_INTERVAL_SECONDS", 0)
        await media.connect()
        peer.connectionState = "connected"
        peer.handlers["connectionstatechange"]()
        await asyncio.wait_for(ninth.wait(), 5)
        await media._stats_task
        assert len(samples) == 9
        await media.close()
        assert len(samples) == 10 and samples[-1]["terminal"]
        assert samples[-1]["outPackets"] is None and samples[-1]["inPackets"] is None

    asyncio.run(scenario())


def test_sdp_summary_does_not_copy_arbitrary_mid_codec_or_credentials():
    from agentcall import webrtc_media

    async def scenario():
        await asyncio.wrap_future(webrtc_media.warm_webrtc_stack())
        peer = RTCPeerConnection(RTCConfiguration(iceServers=[]))
        peer.addTransceiver("audio")
        try:
            description = await peer.createOffer()
            # Parse the generated description, but never set it on the peer or expose its content.
            description.sdp = description.sdp.replace("a=mid:0", "a=mid:private-contact")
            fake = SimpleNamespace(localDescription=description, getTransceivers=lambda: [])
            summary = webrtc_media._sdp_summary(fake)
            encoded = json.dumps(summary)
            assert summary["mLines"][0]["mid"] is None
            assert summary["mLines"][0]["codecs"][0]["mimeType"] == "audio/opus"
            assert not any(value in encoded for value in ("private-contact", "ice-ufrag", "fingerprint", "ssrc"))
        finally:
            await peer.close()

    asyncio.run(scenario())


def test_failed_stats_reads_are_bounded_and_never_block_close():
    async def scenario():
        calls = 0
        peer = FakePeer()

        async def stats():
            nonlocal calls
            calls += 1
            raise RuntimeError("unavailable")

        peer.getStats = stats
        media = endpoint(peer)
        await media.connect()
        for _ in range(12):
            await media._sample_rtp_stats(terminal=False, reason="periodic")
        await media.close()
        assert calls == 10 and peer.closed == 1

    asyncio.run(scenario())


@pytest.mark.skipif(
    sys.platform == "win32" and sys.version_info < (3, 13),
    reason="Windows + Python 3.12 CI: in-process aiortc<->aiortc loopback parks the Proactor loop with no timer armed "
           "(pytest-timeout dump: idle select, no aiortc frames); Windows 3.13 and all other legs run it. BACKLOG C11.",
)
@pytest.mark.parametrize("slow_start", [False, True], ids=["normal", "delayed-gather-dtls-consumer"])
def test_real_loopback_non_silent_pcm_both_ways_dtmf_and_hangup_ack(monkeypatch, slow_start):
    from aioice import ice
    from aiortc import RTCDtlsTransport

    from agentcall import webrtc_ice

    monkeypatch.setenv("REMOTE_DOWNLINK_GAIN", "1")
    # Upstream excludes loopback, while the product adapter enumerates separately.
    # Restrict both sources, not SDP after gathering: no real NIC/VPN/IPv6/mDNS I/O.
    monkeypatch.setattr(ice, "get_host_addresses", lambda **_: ["127.0.0.1"])
    monkeypatch.setattr(webrtc_ice, "physical_host_addresses", lambda: ["127.0.0.1"])
    if slow_start:
        def unexpected_interfaces():
            raise AssertionError("loopback must not enumerate host interfaces")

        monkeypatch.setattr(ice.ifaddr, "get_adapters", unexpected_interfaces)
        gather, start_dtls = ice.Connection.gather_candidates, RTCDtlsTransport.start

        async def delayed_gather(self):
            await asyncio.sleep(0.25)
            return await gather(self)

        async def delayed_dtls(self, parameters):
            await asyncio.sleep(0.25)
            return await start_dtls(self, parameters)

        monkeypatch.setattr(ice.Connection, "gather_candidates", delayed_gather)
        monkeypatch.setattr(RTCDtlsTransport, "start", delayed_dtls)

    async def scenario():
        phone_ice_started = asyncio.Event()

        class AnswerPeer(RTCPeerConnection):
            async def setLocalDescription(self, description):
                # aiortc schedules answerer ICE before the offerer applies the
                # answer. Incoming checks can then start a candidate-pair task
                # with remote_password=None. Sequence signaling before checks,
                # without replacing the real ICE/DTLS implementation.
                transports = {t.receiver.transport.transport for t in self.getTransceivers()}
                if self.sctp is not None:
                    transports.add(self.sctp.transport.transport)
                for transport in transports:
                    original_start = transport.start

                    async def start(parameters, original_start=original_start):
                        await phone_ice_started.wait()
                        return await original_start(parameters)

                    monkeypatch.setattr(transport, "start", start)
                await super().setLocalDescription(description)

        phone = RTCPeerConnection(RTCConfiguration(iceServers=[]))
        peers = [phone]
        phone.addTrack(ToneTrack())
        channel = phone.createDataChannel(REMOTE_CONTROL_TOPIC)
        phone_ice = phone.getTransceivers()[0].receiver.transport.transport

        @phone_ice.on("statechange")
        def on_phone_ice_state():
            if phone_ice.state == "checking":
                phone_ice_started.set()

        if slow_start:
            original_phone_start = phone_ice.start

            async def delayed_phone_start(parameters):
                await asyncio.sleep(0.25)
                return await original_phone_start(parameters)

            monkeypatch.setattr(phone_ice, "start", delayed_phone_start)
        remote_track = asyncio.Future()
        events = asyncio.Queue()
        media = None
        producer = None
        ready, channel_open = asyncio.Event(), asyncio.Event()
        observations = []

        @channel.on("open")
        def on_open():
            channel_open.set()

        def observe(kind, **fields):
            observations.append((kind, fields))
            if kind == "latency" and fields.get("stage") == "edge_media_ready":
                ready.set()

        @phone.on("track")
        def on_track(track):
            if not remote_track.done():
                remote_track.set_result(track)

        @channel.on("message")
        def on_message(payload):
            events.put_nowait(json.loads(payload))

        try:
            async with loopback_phase("offer gathering", peers):
                await phone.setLocalDescription(await phone.createOffer())
            media = WebRtcRemoteMediaEndpoint(
                {"type": "offer", "sdp": phone.localDescription.sdp}, [], call_id="loopback",
                peer_factory=lambda _: AnswerPeer(RTCConfiguration(iceServers=[])),
            )
            media.set_timing(CallTiming(SimpleNamespace(log_event=observe)))
            async with loopback_phase("answer gathering", peers):
                try:
                    await media.connect()
                finally:
                    if media._pc is not None:
                        peers.append(media._pc)
            assert media.answer is not None and not media.media_ready
            for peer in peers:
                candidates = [line.split() for line in peer.localDescription.sdp.splitlines()
                              if line.startswith("a=candidate:")]
                assert candidates and all(c[4] == "127.0.0.1" and c[7] == "host" for c in candidates)
            async with loopback_phase("ICE/DTLS/SCTP and first PCM", peers, timeout=60):
                await phone.setRemoteDescription(RTCSessionDescription(**media.answer))
                await ready.wait()
                await channel_open.wait()
                assert media.media_ready and channel.readyState == "open"

            async with loopback_phase("non-silent uplink PCM", peers):
                while True:
                    uplink = media.take_browser_audio()
                    assert all(len(chunk) == 320 for chunk in uplink)
                    if any(abs(value) > 500 for chunk in uplink for value in struct.unpack("<160h", chunk)):
                        break
                    await asyncio.sleep(0.01)
            stages = {fields["stage"] for kind, fields in observations if kind == "latency"}
            assert {"edge_direct_remote_track", "edge_direct_audio_ready", "edge_phone_first_frame"} <= stages
            assert "edge_phone_track_subscribed" not in stages

            channel.send('{"type":"dtmf","digits":"12#"}')
            async with loopback_phase("DTMF", peers):
                assert await media.next_command(30) == {"type": "dtmf", "digits": "12#"}

            async with loopback_phase("remote track", peers):
                track = await remote_track
            if slow_start:
                # Accumulate >100 decoded silence frames before producing tone.
                async with loopback_phase("delayed consumer backlog", peers):
                    while track._queue.qsize() < 120:
                        await asyncio.sleep(0.01)

            async def feed_modem():
                while True:
                    media.push_modem_audio(struct.pack("<160h", *([3000, -3000] * 80)))
                    await asyncio.sleep(0.02)

            producer = asyncio.create_task(feed_modem())
            async with loopback_phase("non-silent downlink PCM", peers):
                while True:
                    frame = await track.recv()
                    if abs(frame.to_ndarray()).max() > 500:
                        break
            async with loopback_phase("terminal RTP stats", peers):
                await media.finish_rtp_stats("commit")
            rtp = [fields for kind, fields in observations if kind == "edge_rtp_stats"]
            assert rtp[-1]["terminal"] and rtp[-1]["outPackets"] > 0 and rtp[-1]["inPackets"] > 0
            assert rtp[-1]["senderFrames"] > 0 and rtp[-1]["dtlsState"] == "connected"
            assert rtp[-1]["selectedCandidatePair"] == {
                "localCandidate": "host", "remoteCandidate": "host", "state": "succeeded"}
            summaries = [fields for kind, fields in observations if kind == "direct_sdp_summary"]
            audio = next(line for line in summaries[0]["mLines"] if line["kind"] == "audio")
            assert audio["currentDirection"] == "sendrecv" and audio["rtcpMux"] and audio["bundle"]
            assert any(codec["mimeType"] == "audio/opus" and codec["clockRate"] == 48000
                       and codec["channels"] == 2 for codec in audio["codecs"])
            command_id = str(uuid4())
            channel.send(json.dumps({"type": "hangup", "commandId": command_id}))
            async with loopback_phase("hangup.ack", peers):
                command = await media.next_command(30)
                assert command is not None and command["type"] == "hangup"
                assert await media._timing.acknowledge_hangup(command, media.send_event)
                assert await events.get() == {"type": "hangup.ack", "commandId": command_id}
        finally:
            if producer is not None:
                producer.cancel()
                await asyncio.gather(producer, return_exceptions=True)
            try:
                if media is not None:
                    async with loopback_phase("endpoint close", peers):
                        await media.close()
            finally:
                async with loopback_phase("phone close", peers):
                    await phone.close()
        assert not media.media_ready and not media.browser_connected

    asyncio.run(scenario())


def test_loopback_timeout_reports_transport_state_and_drains_waiter():
    async def scenario():
        peer = RTCPeerConnection(RTCConfiguration(iceServers=[]))
        peer.addTransceiver("audio")
        drained = asyncio.Event()

        async def stalled():
            try:
                await asyncio.Event().wait()
            finally:
                drained.set()

        try:
            with pytest.raises(AssertionError) as caught:
                async with loopback_phase("injected stalled transport", [peer], timeout=0.01):
                    await stalled()
            message = str(caught.value)
            assert drained.is_set()
            assert "phase='injected stalled transport'" in message
            assert "'ice': 'new'" in message and "'dtls': 'new'" in message
            assert "localCandidates" in message and "'stats':" in message and "pending=" in message
            assert not any(secret in message for secret in ("ice-pwd", "ice-ufrag", "fingerprint"))
        finally:
            await peer.close()

    asyncio.run(scenario())
