"""Local workerd + production CloudEdgeClient/CallSession + aiortc phone acceptance.

Run: .venv/bin/python tests/fakes/inbound_direct_e2e.py
Modes: inline SDP (default), --deferred-sdp, --cancel-before-sdp, --cancel-during-ice.
Only TURN credential generation and physical modem/PCM are fixtures. This is
host-candidate loopback evidence, not a TURN, carrier, or iPhone acceptance test.
Generated enrollment keys/cookies stay in memory; temporary D1 is removed.
Every mode checks callback errors and retired STUN retry timers before loop shutdown.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import json
import logging
import math
import os
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import aiohttp
from aioice.stun import Transaction
from aiortc import RTCPeerConnection, RTCSessionDescription
from aiortc.mediastreams import AudioStreamTrack
from aiortc.sdp import SessionDescription
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from websockets.sync.client import connect as websocket_connect

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from agentcall.call_agent import CallAgentService  # noqa: E402
from agentcall.cloud_control import CloudEdgeClient  # noqa: E402
from agentcall.cloud_credentials import CloudCredentialStore, EdgeCredential  # noqa: E402
from agentcall.modem import Sim7600Modem  # noqa: E402
from agentcall.takeover_coordinator import TakeoverState  # noqa: E402
from agentcall.webrtc_media import ice_servers_config  # noqa: E402
from fakes import FakeAudioBridge  # noqa: E402

CLOUD = ROOT / "cloud"
ORIGIN = "https://direct.test"
ADMIN = "synthetic-local-admin-token-not-a-secret"


class Timeline:
    def __init__(self) -> None:
        self.origin = time.monotonic()
        self.marks: dict[str, float] = {}
        self.lock = threading.Lock()

    def mark(self, name: str) -> None:
        with self.lock:
            self.marks.setdefault(name, round((time.monotonic() - self.origin) * 1000, 3))


class MemoryCredentials(CloudCredentialStore):
    def __init__(self, credential: EdgeCredential, key: Ed25519PrivateKey) -> None:
        self.credential, self.key = credential, key

    def load(self) -> EdgeCredential:
        return self.credential

    def sign(self, message: bytes) -> str:
        return base64.urlsafe_b64encode(self.key.sign(message)).decode().rstrip("=")

    def headers(self, method: str, path: str) -> dict[str, str]:
        timestamp = str(round(time.time() * 1000))
        message = f"{self.credential.edge_id}\n{timestamp}\n{method}\n{path}".encode()
        return {"Authorization": f"Bearer {self.credential.value}", "X-CallPilot-Timestamp": timestamp,
                "X-CallPilot-Signature": self.sign(message)}


class SyntheticSim7600(Sim7600Modem):
    POST_ANSWER_SETTLE_SECONDS = 0

    def __init__(self, timeline: Timeline) -> None:
        super().__init__("never-open-a-serial-port")
        self.commands: list[str] = []
        self.timeline = timeline

    def _write_command(self, command: str) -> str:
        self.commands.append(command)
        if command == "ATA":
            self.timeline.mark("modem_ata")
        if command == "AT+CLCC":
            state = 0 if "ATA" in self.commands else 4
            return f'+CLCC: 1,1,{state},0,0,"",129\r\nOK'
        return "OK"

    def initialize_for_voice(self, _mode: str) -> None:
        self.commands.append("PCM")
        self.last_voice_enable = {"ok": True}
        self.timeline.mark("modem_pcm_ready")

    def hangup(self) -> None:
        self.commands.append("HANGUP")
        self._call_connected_event.clear()
        self.timeline.mark("modem_hangup")


class SyntheticMicrophone(AudioStreamTrack):
    async def recv(self):
        frame = await super().recv()
        frame.planes[0].update(tone(440))
        return frame


class IceBlackhole(asyncio.DatagramProtocol):
    """Receipt proves a real ICE transaction is in flight; never answer it."""

    def __init__(self) -> None:
        self.check_received = asyncio.Event()
        self.closed = asyncio.Event()

    def datagram_received(self, _data: bytes, _addr: tuple) -> None:
        self.check_received.set()

    def connection_lost(self, _exc: Exception | None) -> None:
        self.closed.set()


def blackhole_candidates(sdp: str, port: int) -> str:
    description = SessionDescription.parse(sdp)
    for media in description.media:
        for candidate in media.ice_candidates:
            candidate.ip, candidate.port = "127.0.0.1", port
    return str(description)


def tone(frequency: int) -> bytes:
    return struct.pack("<160h", *(int(6000 * math.sin(2 * math.pi * frequency * n / 8000)) for n in range(160)))


class TracedSocket:
    def __init__(self, connection, timeline: Timeline) -> None:
        self.connection, self.timeline = connection, timeline

    def __enter__(self):
        self.connection.__enter__()
        return self

    def __exit__(self, *args):
        return self.connection.__exit__(*args)

    def send(self, raw: str) -> None:
        value = json.loads(raw)
        name = {"inbound.offer": "edge_offer_sent",
                "inbound.offer.revoke": "edge_revoke_sent"}.get(value.get("type"))
        if value.get("type") == "inbound.answer" and value.get("status") == "accepted":
            name = "edge_answer_sent" if "sdpAnswer" in value else "edge_reservation_ack"
        if name:
            self.timeline.mark(name)
        self.connection.send(raw)

    def recv(self, **kwargs):
        return self.connection.recv(**kwargs)


@contextlib.contextmanager
def local_worker():
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        port = reserved.getsockname()[1]
    with tempfile.TemporaryDirectory(prefix="callpilot-direct-e2e-") as directory:
        binary = CLOUD / "node_modules" / ".bin" / "wrangler"
        args = ["-c", "test/wrangler.direct-test.jsonc", "--local", "--persist-to", directory,
                "--env-file", "test/empty-vars"]
        result = subprocess.run([str(binary), "d1", "migrations", "apply", "DB", *args], cwd=CLOUD,
                                capture_output=True, timeout=60, check=False)
        if result.returncode:
            raise RuntimeError("LOCAL_D1_MIGRATION_FAILED")
        process = subprocess.Popen([str(binary), "dev", *args, "--ip", "127.0.0.1", "--port", str(port)],
                                   cwd=CLOUD, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        output: deque[bytes] = deque(maxlen=100)

        def drain() -> None:
            assert process.stdout is not None
            for line in process.stdout:
                output.append(line)

        reader = threading.Thread(target=drain, daemon=True)
        reader.start()
        try:
            yield f"http://127.0.0.1:{port}"
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            reader.join(timeout=2)
            if process.stdout is not None:
                process.stdout.close()


async def eventually(predicate, *, timeout: float = 15) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


async def run(base: str, *, deferred_sdp: bool = False, cancel_before_sdp: bool = False,
              cancel_during_ice: bool = False) -> dict:
    timeline = Timeline()
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=45)) as http:
        async def request(method: str, path: str, body=None, headers=None, expected=200):
            async with http.request(method, base + path, json=body, headers=headers) as response:
                if response.status != expected:
                    raise RuntimeError(f"LOCAL_HTTP_{method}_{path.split('/')[2]}_{response.status}")
                return await response.json(), response.headers

        async with asyncio.timeout(30):
            while True:
                try:
                    await request("GET", "/healthz")
                    break
                except (aiohttp.ClientError, RuntimeError):
                    await asyncio.sleep(0.1)
        invite, _ = await request("POST", "/v1/admin/enrollment-invites", {"ttlSeconds": 600},
                                  {"Authorization": f"Bearer {ADMIN}"}, 201)
        key = Ed25519PrivateKey.generate()
        public = base64.urlsafe_b64encode(key.public_key().public_bytes_raw()).decode().rstrip("=")
        enrollment, _ = await request("POST", "/v1/edge-enrollments/claim", {
            "code": invite["code"], "displayName": "Synthetic Edge", "publicKey": public,
        }, expected=201)
        credentials = MemoryCredentials(EdgeCredential(enrollment["edgeId"], enrollment["credential"]), key)
        path = f"/v1/edges/{enrollment['edgeId']}/pairing-sessions"
        pairing, _ = await request("POST", path, {"ttlSeconds": 300}, credentials.headers("POST", path), 201)
        _, headers = await request("POST", "/v1/pairing-sessions/claim", {
            "code": pairing["code"], "displayName": "Synthetic Phone",
        }, {"Origin": ORIGIN}, 201)
        phone_headers = {"Cookie": headers["Set-Cookie"].split(";", 1)[0], "Origin": ORIGIN}
        timeline.mark("enrolled_and_paired")

        modem = SyntheticSim7600(timeline)
        bridge = FakeAudioBridge()
        service = CallAgentService("unused", "unused", provider="openai", modem=modem,
                                   call_logger=SimpleNamespace())
        session = service.session
        session.call_logger = None

        def connect(url, **kwargs):
            assert url == "wss://fixture.test/v1/edges/connect"
            local = base.replace("http://", "ws://") + "/v1/edges/connect"
            return TracedSocket(websocket_connect(local, **kwargs), timeline)

        control = CloudEdgeClient("https://fixture.test", service, credentials, connect=connect)
        session._phone_first_cloud_ready = control.takeover_available
        phone = None
        call_task = feed_task = receive_task = None
        cloud_timing: dict[str, float] = {}
        phone_peak = 0
        events: asyncio.Queue[dict] = asyncio.Queue()
        blackhole = IceBlackhole()
        blackhole_transport = None
        if cancel_during_ice:
            blackhole_transport, _ = await asyncio.get_running_loop().create_datagram_endpoint(
                lambda: blackhole, local_addr=("127.0.0.1", 0))
        ai_attempts = 0

        def no_ai(*_args, **_kwargs):
            nonlocal ai_attempts
            ai_attempts += 1
            raise AssertionError("AI_MUST_NOT_START_FOR_OWNER_CLAIM")

        with patch("agentcall.call_agent.create_audio_bridge", return_value=bridge), \
             patch("agentcall.call_agent.create_agent", side_effect=no_ai):
            try:
                control.start()
                await eventually(lambda: control.status().connected)
                async with asyncio.timeout(10):
                    while True:
                        device, _ = await request("GET", "/v1/device", headers=phone_headers)
                        if device["features"].get("inboundFastpathV2"):
                            break
                        await asyncio.sleep(0.02)
                timeline.mark("edge_authenticated")
                # Use real session admission/reset, but run its coroutine on this test loop.
                with patch.object(session, "_run", return_value=None):
                    session.start(phone_first_seconds=15)
                    session._thread.join(timeout=1)
                timeline.mark("ring")
                call_task = asyncio.create_task(session._handle_call())
                async with asyncio.timeout(10):
                    while True:
                        offers, _ = await request("GET", "/v1/inbound-offers", headers=phone_headers)
                        if offers["offers"]:
                            break
                        await asyncio.sleep(0.02)
                offer_id = offers["offers"][0]["offerId"]
                timeline.mark("phone_offer_received")
                ice, _ = await request("POST", f"/v1/inbound-offers/{offer_id}/ice", {}, phone_headers)
                timeline.mark("phone_ice_received")
                phone = RTCPeerConnection(ice_servers_config(ice["iceServers"]))
                phone.addTrack(SyntheticMicrophone())
                channel = phone.createDataChannel("callpilot.control")

                @channel.on("message")
                def on_message(payload):
                    events.put_nowait(json.loads(payload))

                @phone.on("track")
                def on_track(track):
                    nonlocal receive_task

                    async def receive():
                        nonlocal phone_peak
                        while True:
                            frame = await track.recv()
                            phone_peak = max(phone_peak, int(abs(frame.to_ndarray()).max()))
                            if phone_peak > 500:
                                timeline.mark("phone_non_silent_audio")

                    receive_task = asyncio.create_task(receive())

                if not deferred_sdp:
                    await phone.setLocalDescription(await phone.createOffer())
                    timeline.mark("phone_prepare_complete")
                assert "ATA" not in modem.commands and not bridge.started
                timeline.mark("phone_answer")
                claim_body = {"offerId": offer_id, "mediaTransport": "webrtc"}
                if not deferred_sdp:
                    assert phone.localDescription is not None
                    claim_body["sdpOffer"] = phone.localDescription.sdp
                    if blackhole_transport is not None:
                        claim_body["sdpOffer"] = blackhole_candidates(
                            claim_body["sdpOffer"], blackhole_transport.get_extra_info("sockname")[1])
                claim, response_headers = await request("POST", "/v1/inbound-offers/claim",
                                                        claim_body, phone_headers, 202)
                timeline.mark("phone_claim_response")
                for item in response_headers.get("Server-Timing", "").split(","):
                    name, _, value = item.strip().partition(";dur=")
                    if value:
                        cloud_timing[name] = float(value)
                if deferred_sdp:
                    assert claim.get("directAttempted") is True and claim.get("sdpPending") is True
                    assert phone.localDescription is None, "PHONE_GATHER_MUST_NOT_PRECEDE_RESERVATION"
                    await eventually(lambda: "modem_pcm_ready" in timeline.marks)
                    assert modem.commands.count("ATA") == modem.commands.count("PCM") == 1
                    if cancel_before_sdp:
                        end_path = f"/v1/inbound-offers/{offer_id}/media/end"
                        await request("POST", end_path, {"claimId": "claim_wrong00000000"}, phone_headers, 409)
                        assert session.is_active and not call_task.done()
                        timeline.mark("phone_control_end_start")
                        for _ in range(2):
                            ended, _ = await request("POST", end_path, {"claimId": claim["claimId"]},
                                                     phone_headers, 202)
                            assert ended["accepted"] is True
                        timeline.mark("phone_control_end_response")
                        await asyncio.wait_for(call_task, 10)
                        assert session.takeover_state is TakeoverState.ENDED and not session.is_active
                        assert ai_attempts == 0
                        assert modem.commands.count("HANGUP") == 1
                        assert bridge.stopped and channel.readyState != "open"
                        await phone.setLocalDescription(await phone.createOffer())
                        await request("POST", f"/v1/inbound-offers/{offer_id}/media/sdp", {
                            "claimId": claim["claimId"], "sdpOffer": phone.localDescription.sdp,
                        }, phone_headers, 409)
                        timeline.mark("late_sdp_rejected")
                        await eventually(lambda: "edge_revoke_sent" in timeline.marks)
                        return {"type": "inbound_direct_e2e", "status": "passed",
                                "path": "host_candidates_only", "signaling": "cancel_before_sdp",
                                "milestones_ms": timeline.marks, "cloud_claim_ms": cloud_timing,
                                "edge_summary": session._mobile_timing.summary(),
                                "ata_count": 1, "pcm_prepare_count": 1, "hangup_count": 1,
                                "ai_start_count": ai_attempts, "late_sdp_status": 409}
                    timeline.mark("phone_gather_start")
                    await phone.setLocalDescription(await phone.createOffer())
                    timeline.mark("phone_prepare_complete")
                    assert timeline.marks["modem_pcm_ready"] < timeline.marks["phone_gather_start"]
                    assert phone.localDescription is not None
                    timeline.mark("phone_sdp_submit_start")
                    claim, _ = await request("POST", f"/v1/inbound-offers/{offer_id}/media/sdp", {
                        "claimId": claim["claimId"], "sdpOffer": phone.localDescription.sdp,
                    }, phone_headers, 202)
                    timeline.mark("phone_sdp_response")
                direct = next((value for value in claim.get("transports", []) if value["kind"] == "webrtc"), None)
                assert claim.get("directAttempted") is True and direct is not None, "DIRECT_SDP_RELAY_NOT_COMPLETED"
                if cancel_during_ice:
                    await asyncio.wait_for(blackhole.check_received.wait(), 10)
                    timeline.mark("edge_ice_check_in_flight")
                    assert channel.readyState != "open"
                    assert session.takeover_state is TakeoverState.WAITING_OWNER
                    timeline.mark("phone_control_end_start")
                    await request("POST", f"/v1/inbound-offers/{offer_id}/media/end",
                                  {"claimId": claim["claimId"]}, phone_headers, 202)
                    timeline.mark("phone_control_end_response")
                    await asyncio.wait_for(call_task, 10)
                    assert session.takeover_state is TakeoverState.ENDED and not session.is_active
                    assert ai_attempts == 0 and bridge.stopped
                    assert (modem.commands.count("ATA") == modem.commands.count("PCM")
                            == modem.commands.count("HANGUP") == 1)
                    await eventually(lambda: "edge_revoke_sent" in timeline.marks)
                    return {"type": "inbound_direct_e2e", "status": "passed",
                            "path": "host_candidates_only", "signaling": "cancel_during_ice",
                            "milestones_ms": timeline.marks, "cloud_claim_ms": cloud_timing,
                            "edge_summary": session._mobile_timing.summary(),
                            "ata_count": 1, "pcm_prepare_count": 1, "hangup_count": 1,
                            "ai_start_count": ai_attempts}
                await phone.setRemoteDescription(RTCSessionDescription(type="answer", sdp=direct["sdpAnswer"]))

                async def feed():
                    while True:
                        bridge.feed_uplink(tone(660))
                        await asyncio.sleep(0.02)

                feed_task = asyncio.create_task(feed())
                await eventually(lambda: session.takeover_state is TakeoverState.MOBILE_ACTIVE
                                 and channel.readyState == "open")
                timeline.mark("owner_media_committed")
                await eventually(lambda: phone_peak > 500 and bool(bridge.downlink))
                modem_peak = max(abs(value) for chunk in bridge.downlink for value in struct.unpack(
                    "<" + "h" * (len(chunk) // 2), chunk))
                assert modem_peak > 500, "MICROPHONE_PCM_IS_SILENT"
                timeline.mark("bidirectional_non_silent_audio")
                channel.send(json.dumps({"type": "client_timing", "v": 1, "attempt": str(uuid4()), "seq": 1,
                    "marks": {"answer_action": 0, "claim_start": 0,
                              "claim_response": timeline.marks["phone_claim_response"] - timeline.marks["phone_answer"],
                              "downlink_first_frame": timeline.marks["phone_non_silent_audio"]
                              - timeline.marks["phone_answer"],
                              **({"sdp_submit_start": timeline.marks["phone_sdp_submit_start"]
                                  - timeline.marks["phone_answer"],
                                  "sdp_submit_response": timeline.marks["phone_sdp_response"]
                                  - timeline.marks["phone_answer"]} if deferred_sdp else {}),
                              **{f"cloud_{stage}_ms": cloud_timing[stage]
                                 for stage in ("cas", "edge_reserve", "turn", "sdp_relay", "claim_finalize", "total")
                                 if stage in cloud_timing}}}))
                hangup_id = str(uuid4())
                timeline.mark("phone_hangup")
                channel.send(json.dumps({"type": "hangup", "commandId": hangup_id}))
                async with asyncio.timeout(5):
                    while True:
                        event = await events.get()
                        if event.get("type") == "hangup.ack" and event.get("commandId") == hangup_id:
                            break
                timeline.mark("phone_hangup_ack")
                await asyncio.wait_for(call_task, 10)
                assert modem.commands.count("ATA") == modem.commands.count("PCM") == modem.commands.count("HANGUP") == 1
                assert bridge.started and bridge.stopped
                assert ai_attempts == 0
                await eventually(lambda: "edge_revoke_sent" in timeline.marks)
                offers, _ = await request("GET", "/v1/inbound-offers", headers=phone_headers)
                assert not offers["offers"]
                assert session._mobile_timing is not None
                return {"type": "inbound_direct_e2e", "status": "passed", "path": "host_candidates_only",
                        "signaling": "deferred_sdp" if deferred_sdp else "inline_sdp",
                        "milestones_ms": timeline.marks, "cloud_claim_ms": cloud_timing,
                        "edge_summary": session._mobile_timing.summary(),
                        "ata_count": 1, "pcm_prepare_count": 1, "hangup_count": 1,
                        "phone_pcm_peak": phone_peak, "modem_pcm_peak": modem_peak}
            finally:
                session.stop()
                for task in (feed_task, receive_task, call_task):
                    if task is not None and not task.done():
                        task.cancel()
                await asyncio.gather(*(task for task in (feed_task, receive_task, call_task) if task is not None),
                                     return_exceptions=True)
                if phone is not None:
                    await phone.close()
                if blackhole_transport is not None:
                    blackhole_transport.close()
                    await blackhole.closed.wait()
                await asyncio.to_thread(control.stop)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="Write the content-free validation result as one JSONL line")
    parser.add_argument("--deferred-sdp", action="store_true",
                        help="Claim before gathering; send SDP only after real Edge ATA/PCM readiness")
    parser.add_argument("--cancel-before-sdp", action="store_true",
                        help="Claim without SDP, then cancel over authenticated control HTTP")
    parser.add_argument("--cancel-during-ice", action="store_true",
                        help="Cancel only after an actual Edge ICE check reaches a blackhole UDP socket")
    args = parser.parse_args()
    if args.cancel_during_ice and (args.deferred_sdp or args.cancel_before_sdp):
        parser.error("--cancel-during-ice is an independent inline SDP mode")
    failures: list[str] = []

    class ErrorCapture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            if record.exc_info or "Traceback" in record.getMessage():
                failures.append(record.name)

    logging.basicConfig(level=logging.ERROR, handlers=[ErrorCapture()])
    transactions: list[Transaction] = []
    transaction_run = Transaction.run

    async def traced_transaction(transaction: Transaction):
        transactions.append(transaction)
        return await transaction_run(transaction)

    async def checked_run(base: str) -> dict:
        asyncio.get_running_loop().set_exception_handler(lambda _loop, _context: failures.append("asyncio"))
        result = await run(base, deferred_sdp=args.deferred_sdp or args.cancel_before_sdp,
                           cancel_before_sdp=args.cancel_before_sdp, cancel_during_ice=args.cancel_during_ice)
        # Assert before asyncio.run cancels remaining tasks: teardown itself must retire retries.
        for transaction in transactions:
            assert getattr(transaction, "_Transaction__future").done(), "ICE_TRANSACTION_LEFT_PENDING"
            timer = getattr(transaction, "_Transaction__timeout_handle")
            assert timer is None or timer.cancelled(), "ICE_RETRY_TIMER_LEFT_ACTIVE"
        result["active_ice_transactions"] = 0
        return result
    environment = {"INBOUND_FASTPATH_V2_ENABLED": "true", "INBOUND_PRECONNECT_ENABLED": "false",
                   "INBOUND_PHONE_FIRST_ENABLED": "true", "INBOUND_TAKEOVER_ENABLED": "true",
                   "REMOTE_WEB_DIALER_ENABLED": "true", "REMOTE_CLOUD_ENABLED": "true",
                   "MODEM_RESET_AFTER_CALL": "false", "MODEM_LOG_RELEASE_CAUSE": "false",
                   "MONITOR_AI_PLAYBACK": "false", "MONITOR_MODEM_UPLINK": "false",
                   "REMOTE_DOWNLINK_GAIN": "1", "PARALLEL_AGENT_CONNECT": "false"}
    with (patch.dict(os.environ, environment), patch.object(Transaction, "run", traced_transaction),
          local_worker() as base):
        result = asyncio.run(checked_run(base))
    assert not failures, f"ASYNC_OR_LOGGED_EXCEPTIONS: {failures}"
    result["traceback_count"] = len(failures)
    summary = json.dumps({"type": "inbound_timing_summary", **result["edge_summary"]}, separators=(",", ":"))
    line = json.dumps(result, separators=(",", ":"), allow_nan=False)
    if args.output:
        args.output.write_text(summary + "\n" + line + "\n", encoding="utf-8")
    print(summary)
    print(line)


if __name__ == "__main__":
    main()
