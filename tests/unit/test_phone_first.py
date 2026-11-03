"""Offline phone-first acceptance checks; no serial ports or network clients."""

import asyncio
import json
import threading
from dataclasses import replace
from pathlib import Path
from queue import Full
from types import SimpleNamespace

import pytest
from dotenv import dotenv_values
from fakes import FakeAgent, FakeAudioBridge

from agentcall import config
from agentcall.call_agent import CallAgentService, CallSession
from agentcall.cloud_control import CloudEdgeClient
from agentcall.inbound_direct import InboundDirectRequest
from agentcall.modem import Eg25Modem, Sim7600Modem
from agentcall.remote_dialer import IssuedLiveKitSession, RemoteDialerInvite
from agentcall.takeover_coordinator import InboundTakeoverCoordinator, MediaOwner, TakeoverState


def test_phone_first_configuration_defaults_and_example(monkeypatch):
    example = dotenv_values(Path(__file__).parents[2] / ".env.example", encoding="utf-8")
    for key, default in (
        ("INBOUND_PHONE_FIRST_ENABLED", "false"),
        ("INBOUND_PRECONNECT_ENABLED", "false"),
        ("INBOUND_EARLY_ANSWER_ENABLED", "false"),
        ("INBOUND_PCM_PRE_ATA_PROBE_ENABLED", "false"),
        ("INBOUND_PHONE_FIRST_RING_SECONDS", "15"),
        ("INBOUND_PHONE_FIRST_MEDIA_SECONDS", "20"),
    ):
        monkeypatch.delenv(key, raising=False)
        assert config.get_str(key) == default == example[key]
    for value in ("0", "16", "nan", "1.5"):
        with pytest.raises(ValueError):
            config._check_value(config.get_spec("INBOUND_PHONE_FIRST_RING_SECONDS"), value)
    spec = config.get_spec("INBOUND_PHONE_FIRST_MEDIA_SECONDS")
    assert spec.kind == "int"
    for value in ("5", "20", "30"):
        config._check_value(spec, value)
    for value in ("4", "31", "nan", "1.5", "", "-1"):
        with pytest.raises(ValueError):
            config._check_value(spec, value)


def test_unanswered_coordinator_has_no_ai_owner():
    router = SimpleNamespace(owner=MediaOwner.NONE)
    router.switch_owner = lambda owner: setattr(router, "owner", owner)
    coordinator = InboundTakeoverCoordinator(
        call_id="call_test", generation=1, media_router=router, unanswered=True,
    )
    assert coordinator.state is TakeoverState.RINGING_OWNER
    assert router.owner is MediaOwner.NONE
    assert coordinator.rollback_precommit("timeout").accepted
    assert coordinator.state is TakeoverState.AI_ACTIVE


def test_direct_claim_starts_pcm_without_waiting_for_sdp_and_rejects_postcommit_fallback(rig, monkeypatch):
    monkeypatch.setenv("INBOUND_FASTPATH_V2_ENABLED", "true")
    monkeypatch.setenv("INBOUND_EARLY_ANSWER_ENABLED", "false")
    monkeypatch.setenv("INBOUND_PRECONNECT_ENABLED", "true")
    monkeypatch.setattr("agentcall.call_agent.create_agent", lambda *args: pytest.fail("unexpected AI"))
    pcm_started = threading.Event()
    original_initialize = rig.modem.initialize_for_voice

    def initialize(mode):
        original_initialize(mode)
        pcm_started.set()

    monkeypatch.setattr(rig.modem, "initialize_for_voice", initialize)
    rig.endpoint.media_ready = False
    fence = {}

    async def connect():
        assert await asyncio.to_thread(pcm_started.wait, 2), "PCM must not depend on SDP/media completion"
        rig.endpoint.media_ready = True

    rig.endpoint.connect = connect

    def tick():
        rig.clock.tick = lambda: None
        request = rig.session._takeover_request
        assert request is not None
        fence.update(offerId=request.offer_id, callId=request.call_id, claimId="claim_direct",
                     generation=request.generation, nonce=request.nonce)
        assert not rig.session.prepare_takeover(fence), "V2 must not use LiveKit silent prepare"
        direct = InboundDirectRequest(request.offer_id, "claim_direct", "v=0", [], lambda *_: None)
        result = rig.session.accept_takeover_claim(
            offer_id=request.offer_id, call_id=request.call_id, claim_id="claim_direct",
            generation=request.generation, nonce=request.nonce, issued=replace(rig.issued, direct=direct),
        )
        assert result.accepted
        assert rig.session._phone_first_claim_deadline - rig.session._phone_first_claimed_at == pytest.approx(30)

    async def pump(*args):
        assert rig.commands.count("ATA") == rig.commands.count("PCM") == 1
        assert rig.session.takeover_state is TakeoverState.MOBILE_ACTIVE
        assert not rig.service.fallback_inbound_direct(fence, rig.issued, lambda *_: None)

    monkeypatch.setattr(rig.session, "_pump_mobile_media", pump)
    rig.clock.tick = tick
    rig.ring()
    asyncio.run(rig.session._handle_call())
    assert rig.commands.count("ATA") == rig.commands.count("PCM") == 1


def test_direct_preview_forwards_caller_before_phone_audio_and_commit(rig, monkeypatch):
    from agentcall.inbound_direct import SwitchableInboundEndpoint

    monkeypatch.setenv("INBOUND_FASTPATH_V2_ENABLED", "true")
    monkeypatch.setattr("agentcall.call_agent.create_agent", lambda *_: pytest.fail("preview waited for phone audio"))
    direct = rig.endpoint
    direct.media_ready = False
    direct.browser_connected = True
    direct.answer = {"sdp": "answer"}
    received = []

    def push(pcm):
        assert rig.session.takeover_state is TakeoverState.WAITING_OWNER
        assert not direct.media_ready
        received.append(pcm)
        direct.media_ready = True  # Phone starts its microphone only after hearing the caller.

    direct.push_modem_audio = push
    rig.bridge.feed_uplink(b"caller-before-microphone")
    rig.session._takeover_endpoint_factory = lambda issued: SwitchableInboundEndpoint(
        issued.direct, direct_factory=lambda *_args, **_kwargs: direct)

    def claim():
        rig.clock.tick = lambda: None
        offer = rig.session.next_takeover_offer()
        request = InboundDirectRequest(offer.offer_id, "claim_preview", "sdp", [], lambda *_: None)
        assert rig.session.accept_takeover_claim(
            offer_id=offer.offer_id, call_id=offer.call_id, claim_id="claim_preview",
            generation=offer.generation, nonce=offer.nonce, issued=replace(rig.issued, direct=request),
        ).accepted

    rig.clock.tick = claim
    rig.ring()
    asyncio.run(rig.session._handle_call())
    assert received == [b"caller-before-microphone"]
    assert rig.commands.count("ATA") == rig.commands.count("PCM") == 1


@pytest.mark.parametrize("operation", ["sdp", "fallback"])
def test_direct_media_uses_claim_lease_after_ringing_window(rig, monkeypatch, operation):
    monkeypatch.setenv("INBOUND_FASTPATH_V2_ENABLED", "true")
    session = rig.session

    def claim():
        rig.clock.tick = lambda: None
        offer = session.next_takeover_offer()
        rig.clock.now = 114
        direct = InboundDirectRequest(offer.offer_id, "claim_late", None, [], lambda *_: None)
        assert session.accept_takeover_claim(
            offer_id=offer.offer_id, call_id=offer.call_id, claim_id="claim_late",
            generation=offer.generation, nonce=offer.nonce, issued=replace(rig.issued, direct=direct),
        ).accepted
        fence = dict(offerId=offer.offer_id, callId=offer.call_id, claimId="claim_late",
                     generation=offer.generation, nonce=offer.nonce)
        rig.clock.now = 116
        assert rig.clock.now > session._phone_first_deadline
        assert session._phone_first_claim_deadline == 144

        def submit():
            if operation == "fallback":
                return session.fallback_inbound_direct(fence, rig.issued, lambda *_: None)
            return session.supply_inbound_direct_sdp(
                {**fence, "direct": {"sdpOffer": "sdp", "iceServers": []}}, lambda *_: None)

        assert submit(), "Accepted claim survives the original ringing deadline"
        rig.clock.now = 144
        assert not submit(), "Retry cannot extend the original media lease"
        session._set_active(False)

    rig.clock.tick = claim
    rig.ring()
    asyncio.run(session._handle_call())


@pytest.mark.parametrize("accepted", [False, True])
def test_cloud_claim_then_immediate_sdp_uses_same_fenced_request(rig, monkeypatch, accepted):
    monkeypatch.setenv("INBOUND_FASTPATH_V2_ENABLED", "true")
    session = rig.session
    rig.ring()
    session._initialize_takeover_context("inbound")
    client = CloudEdgeClient("https://relay.example.test", rig.service, SimpleNamespace())
    replies = []
    requests = []

    def tick():
        rig.clock.tick = lambda: None
        offer = session._takeover_request
        command = dict(v=1, type="inbound.claim", commandId="command_abcdefghijkl", offerId=offer.offer_id,
                       callId=offer.call_id, claimId="claim_abcdefghijkl", generation=offer.generation,
                       nonce=offer.nonce if accepted else "nonce_wrong_abcdefghijkl", direct={"deferred": True},
                       session=dict(sessionId="session_abcdefghijkl", roomName="callpilot_abcdefghijkl",
                                    browserIdentity="web_abcdefghijkl", edgeIdentity="edgepart_abcdefghijkl",
                                    livekitUrl="wss://invalid.example", token="test" * 12))
        client.handle_message(json.dumps(command), replies.append)
        direct = session._inbound_direct_request
        client.handle_message(json.dumps({**command, "type": "inbound.media.sdp",
            "commandId": "command_sdp_abcdefghijkl", "direct": {"sdpOffer": "v=0\r\n", "iceServers": []}}),
            replies.append)
        if accepted:
            assert session._inbound_direct_request is direct
            assert direct.media_offer() == ("v=0\r\n", [])
            summary = session._mobile_timing.summary()
            assert summary["edge_claim_ack_ms"] is not None
            assert summary["edge_direct_sdp_handled_ms"] >= summary["edge_claim_ack_ms"]
            requests.append(direct)
        else:
            assert direct is None and session._inbound_direct_request is None
            assert json.loads(replies[-1])["errorCode"] == "MEDIA_SDP_REJECTED"
            session._active = False

    rig.clock.tick = tick
    result = asyncio.run(session._handle_phone_first(None))
    if accepted:
        assert requests and result == "completed"
        assert json.loads(replies[0])["status"] == "accepted"
    else:
        assert result == "not_connected"
        assert not rig.commands, "Rejected SDP must not allocate peer or touch modem"


def test_reserved_incoming_answer_is_once_and_old_lease_cannot_hangup(monkeypatch):
    modem = Eg25Modem("unused")
    commands = []

    def command(cmd):
        commands.append(cmd)
        status = 0 if "ATA" in commands else 4
        return f'+CLCC: 1,1,{status},0,0,"",129\r\nOK' if cmd == "AT+CLCC" else "OK"

    monkeypatch.setattr(modem, "_write_command", command)
    lease = modem.reserve_incoming()
    assert modem.answer_reserved_incoming(lease, lambda: True)
    assert modem.answer_reserved_incoming(lease, lambda: True)
    assert commands.count("ATA") == 1
    modem.reserve_incoming()
    modem.finish_reserved_incoming(lease)
    assert not any(cmd in ("ATH", "AT+CHUP") for cmd in commands)


@pytest.mark.parametrize("ending", ["urc", "clcc", "generation"])
def test_unanswered_end_invalidates_answer_without_ata(monkeypatch, ending):
    modem = Eg25Modem("unused")
    commands = []
    monkeypatch.setattr(modem, "_write_command", lambda cmd: commands.append(cmd) or "OK")
    monkeypatch.setattr(modem, "_request_release_cause", lambda *args: None)
    lease = modem.reserve_incoming()
    if ending == "urc":
        modem._process_response_urcs("NO CARRIER")
    elif ending == "clcc":
        modem._process_clcc_response("OK")
        modem._process_clcc_response("OK")
    else:
        modem._maintenance_generation += 1
    assert not modem.answer_reserved_incoming(lease, lambda: True)
    assert "ATA" not in commands


class Clock:
    now = 100.0

    def __call__(self):
        return self.now

    async def sleep(self, seconds):
        self.sleep_sync(seconds)
        await asyncio.sleep(0)

    def sleep_sync(self, seconds):
        self.now += seconds
        self.tick()

    def tick(self):
        pass


@pytest.mark.parametrize("outcome", [
    "delayed_active", "still_incoming", "invalid_snapshots", "error", "ata_no_carrier",
    "no_carrier", "cend", "absent", "other_call", "stale_generation",
])
def test_reserved_ata_confirmation_is_bounded_and_urc_friendly(monkeypatch, outcome):
    monkeypatch.setenv("MODEM_LOG_RELEASE_CAUSE", "false")
    modem = Eg25Modem("unused")
    clock = Clock()
    commands, sleeps, fast_poll = [], [], []
    snapshots = 0

    def command(cmd):
        nonlocal snapshots
        commands.append(cmd)
        if cmd == "ATA":
            clock.now += 0.1
            return {"error": "ERROR", "ata_no_carrier": "NO CARRIER"}.get(outcome, "OK")
        if "ATA" not in commands:
            return '+CLCC: 1,1,4,0,0,"",129\r\nOK'
        snapshots += 1
        if outcome == "absent":
            return "OK"
        if outcome == "other_call":
            return '+CLCC: 2,1,0,0,0,"",129\r\nOK'
        if outcome == "invalid_snapshots":
            return "ERROR" if snapshots % 2 else '+CLCC: 1,1,0,0,0,"",129'
        state = 0 if outcome == "error" or (outcome == "delayed_active" and snapshots >= 4) else 4
        return f'+CLCC: 1,1,{state},0,0,"",129\r\nOK'

    def sleep(seconds):
        # A different thread must be able to process URCs while confirmation waits.
        acquired = threading.Event()

        def reader():
            with modem._serial_lock:
                acquired.set()
                if outcome in ("no_carrier", "cend"):
                    modem._process_response_urcs("NO CARRIER" if outcome == "no_carrier" else "+CEND: 1,0,0,0\r\n")
                elif outcome == "stale_generation":
                    modem._maintenance_generation += 1

        worker = threading.Thread(target=reader)
        worker.start()
        worker.join(timeout=0.5)
        assert acquired.is_set(), "confirmation sleep must release the serial lock"
        sleeps.append(seconds)
        clock.sleep_sync(seconds)

    monkeypatch.setattr(modem, "_write_command", command)
    monkeypatch.setattr(modem, "_request_release_cause", lambda *args: None)
    monkeypatch.setattr(modem, "_start_fast_clcc_poll", lambda: fast_poll.append(True))
    lease = modem.reserve_incoming()
    connected = modem.answer_reserved_incoming(lease, lambda: True, clock=clock, sleep=sleep)
    assert connected is (outcome in ("delayed_active", "still_incoming", "invalid_snapshots"))
    assert modem.is_call_connected() is connected
    assert commands.count("ATA") == 1
    assert fast_poll == ([True] if connected else [])
    assert not any(cmd in ("ATH", "AT+CHUP") for cmd in commands)
    assert all(seconds == pytest.approx(0.25) for seconds in sleeps)
    if outcome == "delayed_active":
        assert len(sleeps) == 3
        assert lease.ata_ms == pytest.approx(850)
    elif outcome in ("still_incoming", "invalid_snapshots"):
        assert sum(sleeps) == pytest.approx(2)
        assert lease.ata_ms == pytest.approx(2100)
    else:
        modem.finish_reserved_incoming(lease)
        assert not any(cmd in ("ATH", "AT+CHUP") for cmd in commands)
    if outcome in ("error", "ata_no_carrier"):
        assert snapshots == 0
    elif outcome in ("absent", "other_call"):
        assert snapshots == 2


@pytest.fixture
def rig(monkeypatch):
    monkeypatch.delenv("INBOUND_PHONE_FIRST_MEDIA_SECONDS", raising=False)
    monkeypatch.setenv("INBOUND_PHONE_FIRST_ENABLED", "true")
    monkeypatch.setenv("INBOUND_TAKEOVER_ENABLED", "true")
    monkeypatch.setenv("REMOTE_WEB_DIALER_ENABLED", "true")
    monkeypatch.setenv("REMOTE_CLOUD_ENABLED", "true")
    monkeypatch.setenv("MODEM_RESET_AFTER_CALL", "false")
    monkeypatch.setenv("MODEM_LOG_RELEASE_CAUSE", "false")
    monkeypatch.setenv("MODEM_ANSWER_GREETING", "must not play")
    clock = Clock()
    modem = Sim7600Modem("unused")
    modem.POST_ANSWER_SETTLE_SECONDS = 0
    commands = []

    def command(cmd):
        commands.append(cmd)
        if cmd == "AT+CLCC":
            state = 0 if "ATA" in commands else 4
            return f'+CLCC: 1,1,{state},0,0,"",129\r\nOK'
        return "OK"

    monkeypatch.setattr(modem, "_write_command", command)
    monkeypatch.setattr(modem, "initialize_for_voice", lambda mode: commands.append("PCM"))
    monkeypatch.setattr(modem, "hangup", lambda: commands.append("HANGUP"))
    answer = modem.answer_reserved_incoming
    monkeypatch.setattr(modem, "answer_reserved_incoming", lambda lease, current: answer(
        lease, current, clock=clock, sleep=clock.sleep_sync,
    ))
    service = CallAgentService("unused", "unused", provider="openai", modem=modem)
    session = service.session
    session._phone_first_cloud_ready = lambda: True
    session._phone_first_clock = clock
    session._phone_first_sleep = clock.sleep
    monkeypatch.setattr(service, "_reject_if_credentials_missing", lambda: (False, None))
    notifications = []
    monkeypatch.setattr("agentcall.notification_events.publish_notification", lambda *args: notifications.append(args))
    bridge = FakeAudioBridge()
    bridge.writer_count = 0
    bridge.max_writers = 0

    def start_bridge():
        bridge.started = True
        bridge.writer_count += 1
        bridge.max_writers = max(bridge.writer_count, bridge.max_writers)

    def stop_bridge():
        bridge.stopped = True
        bridge.writer_count = max(0, bridge.writer_count - 1)

    monkeypatch.setattr(bridge, "start", start_bridge)
    monkeypatch.setattr(bridge, "stop", stop_bridge)
    monkeypatch.setattr("agentcall.call_agent.create_audio_bridge", lambda **kw: bridge)

    def ring():
        # Exercise the real admission/start reset without launching its worker.
        with monkeypatch.context() as patch:
            patch.setattr("agentcall.call_agent.threading.Thread", lambda **kw: SimpleNamespace(
                start=lambda: None, is_alive=lambda: True,
            ))
            modem._on_ring(None)

    issued = IssuedLiveKitSession(
        invite=RemoteDialerInvite("session_test", "", 1000), room_name="room_test",
        browser_identity="web_test", edge_identity="edgepart_test", browser_token="",
        edge_token="test", livekit_url="wss://invalid.example",
    )

    def claim():
        request = session.next_takeover_offer()
        assert request is not None
        return session.accept_takeover_claim(
            offer_id=request.offer_id, call_id=request.call_id, claim_id="claim_test",
            generation=request.generation, nonce=request.nonce, issued=issued,
        )

    endpoint = SimpleNamespace(media_ready=True, events=[], closed=False)

    async def connect():
        pass

    async def close():
        endpoint.closed = True

    async def event(value):
        endpoint.events.append(value)

    endpoint.connect, endpoint.close, endpoint.send_event = connect, close, event
    async def next_command(timeout):
        return None

    endpoint.next_command = next_command
    session._takeover_endpoint_factory = lambda issued: endpoint

    async def pump(*args):
        assert modem.is_call_connected()
        assert session.takeover_state is TakeoverState.MOBILE_ACTIVE
        return bridge

    monkeypatch.setattr(session, "_pump_mobile_media", pump)
    return SimpleNamespace(
        service=service, session=session, modem=modem, commands=commands, clock=clock,
        ring=ring, claim=claim, issued=issued, endpoint=endpoint, bridge=bridge, notifications=notifications,
    )


@pytest.mark.parametrize("enabled", [False, True])
def test_service_webrtc_warmup_is_v2_only_and_nonblocking(rig, monkeypatch, enabled):
    monkeypatch.setenv("INBOUND_FASTPATH_V2_ENABLED", str(enabled).lower())
    warmed = []
    monkeypatch.setattr("agentcall.webrtc_media.warm_webrtc_stack", lambda: warmed.append(True), raising=False)
    monkeypatch.setattr("agentcall.call_agent.threading.Thread", lambda **_: SimpleNamespace(start=lambda: None))
    rig.service.start()
    assert warmed == ([True] if enabled else [])


@pytest.mark.parametrize("response", ["OK", "ERROR", RuntimeError("serial unavailable")])
def test_pcm_pre_ata_probe_is_once_observational_and_queries_after_ata(rig, monkeypatch, response):
    monkeypatch.setenv("INBOUND_PCM_PRE_ATA_PROBE_ENABLED", "true")
    rig.ring()
    session = rig.session
    lease = session._phone_first_lease
    generation = session._session_generation
    events = []
    record = SimpleNamespace(log_event=lambda event, **fields: events.append((event, fields)),
                             log_latency=lambda *_: None)
    session._record = record
    original = rig.modem._send

    def send(command):
        if command in {"AT+CPCMREG=1", "AT+CPCMREG?"}:
            rig.commands.append(command)
            if command == "AT+CPCMREG?":
                return "+CPCMREG: 1\r\nOK"
            if isinstance(response, Exception):
                raise response
            return response
        return original(command)

    monkeypatch.setattr(rig.modem, "_send", send)
    session._probe_pcm_pre_ata(generation, lease, record)
    session._probe_pcm_pre_ata(generation, lease, record)
    assert rig.commands == ["AT+CPCMREG=1"]
    assert not lease.answered and not lease.ata_sent
    assert asyncio.run(session._answer_phone_first(generation, lease))
    assert rig.commands.index("AT+CPCMREG=1") < rig.commands.index("ATA") < rig.commands.index("AT+CPCMREG?")
    assert events[0][0] == events[1][0] == "pcm_pre_ata_probe"
    assert events[0][1]["result"] == (type(response).__name__ if isinstance(response, Exception) else response)
    assert events[1][1]["phase"] == "after_ata" and events[1][1]["state"] == 1
    assert rig.commands.count("PCM") == 0, "Probe must not mark or replace ordinary PCM initialization"


@pytest.mark.parametrize("blocked", ["disabled", "answered", "stale"])
def test_pcm_pre_ata_probe_cannot_run_outside_opt_in_ringing_lease(rig, monkeypatch, blocked):
    monkeypatch.setenv("INBOUND_PCM_PRE_ATA_PROBE_ENABLED", str(blocked != "disabled").lower())
    rig.ring()
    session = rig.session
    lease = session._phone_first_lease
    generation = session._session_generation
    if blocked == "answered":
        lease.ata_sent = True
    elif blocked == "stale":
        generation -= 1
    session._probe_pcm_pre_ata(generation, lease, None)
    session._probe_pcm_after_ata(generation, lease)
    assert not rig.commands


def test_pcm_probe_is_scheduled_only_after_fenced_push_receipt(rig, monkeypatch):
    from agentcall.takeover_coordinator import InboundTakeoverOfferRequest

    monkeypatch.setenv("INBOUND_PCM_PRE_ATA_PROBE_ENABLED", "true")
    rig.ring()
    session = rig.session
    offer = InboundTakeoverOfferRequest("offer_test", "nonce_test_abcdefghijkl", "call_test",
                                       session._session_generation, 1, 1000, phone_first=True)
    session._takeover_request = offer
    jobs = []
    monkeypatch.setattr("agentcall.call_agent.threading.Thread", lambda **kw: SimpleNamespace(
        start=lambda: jobs.append(kw)))
    fence = dict(offerId=offer.offer_id, callId=offer.call_id, generation=offer.generation, nonce=offer.nonce)
    session.inbound_offer_pushed({**fence, "nonce": "stale"})
    assert not jobs and not rig.commands
    session.inbound_offer_pushed(fence)
    assert len(jobs) == 1 and jobs[0]["target"] == session._probe_pcm_pre_ata
    assert not rig.commands, "WebSocket dispatch must not synchronously execute serial commands"


@pytest.mark.parametrize("outcome", ["reuse", "failure", "timeout", "hangup"])
def test_silent_prepare_is_fenced_and_never_touches_modem_before_claim(rig, monkeypatch, outcome):
    monkeypatch.setenv("INBOUND_PRECONNECT_ENABLED", "true")
    rig.ring()
    session = rig.session
    session._initialize_takeover_context("inbound")
    operations = []

    async def prepare():
        operations.append("prepare")
        assert not rig.commands and not rig.bridge.started
        if outcome == "failure":
            raise ConnectionError("synthetic")

    async def bind(issued):
        operations.append("bind")
        assert issued == rig.issued
        assert not rig.commands and not rig.bridge.started

    async def connect():
        operations.append("cold")

    rig.endpoint.preconnect, rig.endpoint.bind_winner, rig.endpoint.connect = prepare, bind, connect
    ticks = 0

    def tick():
        nonlocal ticks
        ticks += 1
        request = session._takeover_request
        if ticks == 1:
            command = {"offerId": request.offer_id, "callId": request.call_id, "generation": request.generation,
                "nonce": request.nonce, "session": {"sessionId": "session_test", "roomName": rig.issued.room_name,
                    "browserIdentity": rig.issued.browser_identity, "edgeIdentity": rig.issued.edge_identity,
                    "livekitUrl": rig.issued.livekit_url, "token": rig.issued.edge_token}}
            for field, value in (("nonce", "wrong"), ("generation", request.generation + 1), ("callId", "wrong")):
                assert not session.prepare_takeover({**command, field: value})
            before = list(rig.commands)
            assert session.prepare_takeover(command)
            assert session.prepare_takeover(command)
            assert rig.commands == before
        elif ticks == 4:
            if outcome in ("reuse", "failure"):
                assert rig.claim().accepted
            elif outcome == "timeout":
                rig.clock.now = session._phone_first_deadline
            else:
                session._active = False
            rig.clock.tick = lambda: None

    rig.clock.tick = tick
    result = asyncio.run(session._handle_phone_first(None))
    assert operations.count("prepare") == 1
    assert rig.endpoint.closed
    if outcome in ("reuse", "failure"):
        assert result == "completed"
        assert rig.commands.count("ATA") == 1
        assert ("bind" in operations) == (outcome == "reuse")
        assert ("cold" in operations) == (outcome == "failure")
    else:
        assert result == ("ai" if outcome == "timeout" else "not_connected")
        assert not rig.commands and not rig.bridge.started


@pytest.mark.parametrize("delay", [8, 12, 19])
def test_delayed_media_ready_records_both_latencies(rig, monkeypatch, delay):
    def forbidden(*args, **kwargs):
        pytest.fail("ready mobile must not start AI")

    monkeypatch.setattr("agentcall.call_agent.create_agent", forbidden)
    rig.endpoint.media_ready = False
    original = rig.modem._write_command

    def command(cmd):
        if cmd == "ATA":
            rig.clock.now += 0.3
        return original(cmd)

    monkeypatch.setattr(rig.modem, "_write_command", command)
    rig.ring()

    def claim():
        assert rig.claim().accepted
        claimed_at = rig.clock.now

        def ready():
            if rig.clock.now >= claimed_at + delay:
                rig.endpoint.media_ready = True
                rig.clock.tick = lambda: None

        rig.clock.tick = ready

    rig.clock.tick = claim
    asyncio.run(rig.session._handle_call())
    assert rig.session._record.source == "remote_web_dialer"
    assert rig.commands.count("ATA") == 1
    events_text = (rig.session._record.path / "events.jsonl").read_text(encoding="utf-8")
    events = [json.loads(line) for line in events_text.splitlines()]
    latencies = {event["stage"]: event["ms"] for event in events if event["type"] == "latency"}
    assert delay * 1000 <= latencies["phone_first_media_ready_ms"] < delay * 1000 + 51
    assert latencies["phone_first_ata_ms"] == pytest.approx(300)
    edge_stages = [event["stage"] for event in events
                   if event["type"] == "latency" and event["stage"].startswith("edge_")]
    assert edge_stages == [
        "edge_claim_received", "edge_endpoint_connect_start", "edge_endpoint_connect_return",
        "edge_media_ready", "edge_ata_start", "edge_ata_return", "edge_pcm_ready",
        "edge_phone_ended_notice_sent", "edge_ath_complete",
    ]
    assert all(latencies[stage] >= 0 for stage in edge_stages)
    assert [latencies[stage] for stage in edge_stages] == sorted(latencies[stage] for stage in edge_stages)


def test_full_offer_queue_falls_back_to_ai(rig, monkeypatch, caplog):
    initialize = rig.session._initialize_takeover_context

    def full(request):
        raise Full

    def initialize_full(*args, **kwargs):
        initialize(*args, **kwargs)
        monkeypatch.setattr(rig.session._takeover_offer_queue, "put_nowait", full)

    monkeypatch.setattr(rig.session, "_initialize_takeover_context", initialize_full)
    agent = FakeAgent()
    monkeypatch.setattr("agentcall.call_agent.create_agent", lambda provider: agent)

    async def loop(*args):
        return rig.bridge

    monkeypatch.setattr(rig.session, "_run_agent_loop", loop)
    rig.ring()
    asyncio.run(rig.session._handle_call())
    assert agent.started and agent.said
    assert rig.commands.count("ATA") == 1
    assert rig.clock.now == 100
    assert "phone-first offer queue full" in caplog.text
    meta = json.loads((rig.session._record.path / "meta.json").read_text(encoding="utf-8"))
    assert meta["status"] != "failed" and meta["answered"]


@pytest.mark.parametrize("provider,parallel", [("openai", False), ("openai", True), ("moss", True)])
def test_mobile_answer_never_starts_ai_or_greeting(rig, monkeypatch, provider, parallel):
    monkeypatch.setenv("PARALLEL_AGENT_CONNECT", str(parallel))
    rig.session.provider = provider

    def forbidden(*args, **kw):
        pytest.fail("phone-first mobile path must not start AI or triage")

    monkeypatch.setattr("agentcall.call_agent.create_agent", forbidden)
    monkeypatch.setattr(rig.session, "_initialize_triage_context", forbidden)
    monkeypatch.setattr(rig.modem, "play_answer_greeting", forbidden)
    rig.ring()

    def claim_once():
        rig.clock.tick = lambda: None
        assert rig.claim().accepted

    rig.clock.tick = claim_once
    asyncio.run(rig.session._handle_call())
    assert rig.commands.count("ATA") == 1
    assert rig.commands.count("PCM") == 1
    assert rig.commands.count("HANGUP") == 1
    assert rig.bridge.started and rig.bridge.stopped
    assert rig.endpoint.closed
    assert rig.bridge.max_writers == 1 and rig.bridge.writer_count == 0
    assert not rig.notifications
    meta = json.loads((rig.session._record.path / "meta.json").read_text(encoding="utf-8"))
    assert meta["answered"] and meta["status"] == "completed"
    assert meta["source"] == "remote_web_dialer"


def test_early_hangup_revokes_one_unanswered_record(rig, monkeypatch):
    rig.ring()
    monkeypatch.setattr("agentcall.call_agent.create_agent", lambda *args: pytest.fail("unexpected AI"))

    def end_once():
        rig.clock.tick = lambda: None
        rig.modem._invalidate_reserved_incoming()
        rig.modem._on_hangup()

    rig.clock.tick = end_once
    asyncio.run(rig.session._handle_call())
    assert "ATA" not in rig.commands and "PCM" not in rig.commands
    assert rig.session.next_takeover_revoke().reason == "CALL_ENDED"
    record = rig.session._record
    assert len(list(record.path.parent.glob("*/meta.json"))) == 1
    meta = json.loads((record.path / "meta.json").read_text(encoding="utf-8"))
    assert meta["status"] == "not_connected" and not meta["answered"]
    assert "source" not in meta
    assert not (record.path / "summary.json").exists()


@pytest.mark.parametrize("cause", [
    "timeout", "disconnect", "no_media", "post_answer_failure", "connect_failure", "disconnect_after_claim",
])
def test_phone_first_falls_back_to_ai_with_one_ata(rig, monkeypatch, cause):
    agent = FakeAgent()
    monkeypatch.setattr("agentcall.call_agent.create_agent", lambda provider: agent)

    async def loop(*args):
        return rig.bridge

    monkeypatch.setattr(rig.session, "_run_agent_loop", loop)
    rig.modem.POST_ANSWER_SETTLE_SECONDS = 0
    rig.ring()

    def tick():
        if cause == "disconnect":
            rig.session._phone_first_cloud_ready = lambda: False
        elif cause in ("no_media", "post_answer_failure", "connect_failure", "disconnect_after_claim"):
            rig.clock.tick = lambda: None
            assert rig.claim().accepted
            if cause == "no_media":
                rig.endpoint.media_ready = False
            elif cause == "post_answer_failure":
                rig.session._takeover_coordinator.commit_mobile = lambda fence: (_ for _ in ()).throw(
                    RuntimeError("commit failed")
                )
            elif cause == "connect_failure":
                async def fail():
                    raise RuntimeError("connect failed")
                rig.endpoint.connect = fail
            else:
                rig.session._phone_first_cloud_ready = lambda: False

    rig.clock.tick = tick
    asyncio.run(rig.session._handle_call())
    assert rig.commands.count("ATA") == 1
    assert agent.started and agent.said
    assert rig.bridge.max_writers == 1 and rig.bridge.writer_count == 0
    limit = 115.1 if cause == "timeout" else 120.2 if cause == "no_media" else 103
    assert rig.clock.now < limit


@pytest.mark.parametrize("configured", [None, "false"])
def test_disabled_path_has_no_new_queries_timers_commands_or_events(rig, monkeypatch, configured):
    if configured is None:
        monkeypatch.delenv("INBOUND_PHONE_FIRST_ENABLED")
    else:
        monkeypatch.setenv("INBOUND_PHONE_FIRST_ENABLED", configured)

    def forbidden(*args, **kw):
        pytest.fail("disabled path touched phone-first dependency")

    rig.session._phone_first_cloud_ready = forbidden
    rig.session._phone_first_clock = forbidden
    rig.session._phone_first_sleep = forbidden
    monkeypatch.setattr(rig.modem, "reserve_incoming", forbidden)
    monkeypatch.setattr("agentcall.cloud_control.CloudControlApi.list_devices", forbidden)
    monkeypatch.setattr("agentcall.call_agent.threading.Timer", forbidden)
    agent = FakeAgent()
    monkeypatch.setattr("agentcall.call_agent.create_agent", lambda provider: agent)

    async def loop(*args):
        return rig.bridge

    monkeypatch.setattr(rig.session, "_run_agent_loop", loop)
    rig.ring()
    asyncio.run(rig.session._handle_call())
    assert rig.commands == ["ATA", "PCM", "HANGUP"]
    assert rig.session._phone_first_lease is None
    assert agent.started and agent.said
    assert rig.session.next_takeover_offer() is None
    assert rig.session.next_takeover_revoke() is None
    events_text = (rig.session._record.path / "events.jsonl").read_text(encoding="utf-8")
    events = [json.loads(line) for line in events_text.splitlines()]
    assert not any(e["type"].startswith("phone_first") for e in events)


@pytest.mark.parametrize("value", ["0", "16", "nan", "", "1.5", "-1"])
def test_invalid_wait_uses_original_start_and_warns(rig, monkeypatch, caplog, value):
    monkeypatch.setenv("INBOUND_PHONE_FIRST_RING_SECONDS", value)
    starts = []
    monkeypatch.setattr(rig.session, "start", lambda **kw: starts.append(kw))
    rig.ring()
    assert starts == [{}]
    assert "INBOUND_PHONE_FIRST_RING_SECONDS" in caplog.text
    assert not rig.commands


@pytest.mark.parametrize("unavailable", ["takeover", "hosted", "cloud", "unregistered", "factory"])
def test_local_preflight_failure_uses_original_path(rig, monkeypatch, unavailable):
    keys = {"takeover": "INBOUND_TAKEOVER_ENABLED", "hosted": "REMOTE_WEB_DIALER_ENABLED",
            "cloud": "REMOTE_CLOUD_ENABLED"}
    if unavailable in keys:
        monkeypatch.setenv(keys[unavailable], "false")
    elif unavailable == "unregistered":
        rig.session._phone_first_cloud_ready = lambda: False
    else:
        rig.session._takeover_endpoint_factory = None
    starts = []
    monkeypatch.setattr(rig.session, "start", lambda **kw: starts.append(kw))
    rig.ring()
    assert starts == [{}]
    assert not rig.commands


def test_cloud_availability_is_cached_without_credential_or_pairing_io():
    def forbidden():
        pytest.fail("read credentials on ringing path")

    client = CloudEdgeClient("https://invalid.example", SimpleNamespace(), SimpleNamespace(load=forbidden))
    assert not client.takeover_available()
    client._set_connected(True)
    assert client.takeover_available()
    client._set_connected(False)
    assert not client.takeover_available()


def test_duplicate_ring_clcc_do_not_extend_deadline_or_offer(rig, monkeypatch):
    rig.ring()
    deadline = rig.session._phone_first_deadline
    generation = rig.session._session_generation
    offers = []

    def tick():
        offer = rig.session.next_takeover_offer()
        if offer:
            offers.append(offer)
        rig.ring()
        rig.modem._process_clcc_response('+CLCC: 1,1,4,0,0,"",129\r\nOK')
        assert rig.session._phone_first_deadline == deadline
        assert rig.session._session_generation == generation

    rig.clock.tick = tick
    agent = FakeAgent()
    monkeypatch.setattr("agentcall.call_agent.create_agent", lambda provider: agent)

    async def loop(*args):
        return rig.bridge

    monkeypatch.setattr(rig.session, "_run_agent_loop", loop)
    asyncio.run(rig.session._handle_call())
    assert len(offers) == 1
    assert offers[0].expires_at - offers[0].created_at == pytest.approx(15)
    assert rig.commands.count("ATA") == 1


@pytest.mark.parametrize("when,accepted", [(114.99, True), (115.0, False), (115.01, False)])
def test_claim_deadline_has_one_winner(rig, monkeypatch, when, accepted):
    rig.ring()
    agent = FakeAgent()
    monkeypatch.setattr("agentcall.call_agent.create_agent", lambda provider: agent)

    async def loop(*args):
        return rig.bridge

    monkeypatch.setattr(rig.session, "_run_agent_loop", loop)

    def tick():
        rig.clock.tick = lambda: None
        rig.clock.now = when
        assert rig.claim().accepted is accepted

    rig.clock.tick = tick
    asyncio.run(rig.session._handle_call())
    assert rig.commands.count("ATA") == 1
    assert agent.started is not accepted
    assert rig.bridge.max_writers == 1


def test_old_generation_cannot_answer_or_finish_new_reservation(rig):
    rig.ring()
    old_lease = rig.session._phone_first_lease
    old_generation = rig.session._session_generation
    rig.session._session_generation += 1
    rig.session._phone_first_lease = rig.modem.reserve_incoming()
    assert not asyncio.run(rig.session._answer_phone_first(old_generation, old_lease))
    rig.modem.finish_reserved_incoming(old_lease)
    assert rig.commands == []


@pytest.mark.parametrize("guard", ["maintenance", "recovering", "remote", "credentials"])
def test_original_admission_guards_precede_phone_first(rig, monkeypatch, guard):
    if guard == "maintenance":
        rig.service._hardware_maintenance = True
    elif guard == "recovering":
        rig.modem._reset_in_progress = True
    elif guard == "remote":
        rig.service._remote_call_owner = object()
    else:
        monkeypatch.setattr(rig.service, "_reject_if_credentials_missing", lambda: (True, "missing"))
    rig.ring()
    assert not rig.session.is_busy and not rig.commands
    assert rig.session._phone_first_lease is None


@pytest.mark.parametrize("parallel", [False, True])
def test_post_answer_failure_parallel_and_legacy_have_one_answer_event(rig, monkeypatch, parallel):
    monkeypatch.setenv("PARALLEL_AGENT_CONNECT", str(parallel))
    # The AI fallback may use its existing module greeting; this test isolates ATA.
    monkeypatch.setenv("MODEM_ANSWER_GREETING", "")
    rig.ring()
    agent = FakeAgent()
    monkeypatch.setattr("agentcall.call_agent.create_agent", lambda provider: agent)

    async def loop(*args):
        return rig.bridge

    monkeypatch.setattr(rig.session, "_run_agent_loop", loop)

    def tick():
        rig.clock.tick = lambda: None
        assert rig.claim().accepted
        rig.session._takeover_coordinator.commit_mobile = lambda fence: (_ for _ in ()).throw(RuntimeError())

    rig.clock.tick = tick
    asyncio.run(rig.session._handle_call())
    assert rig.commands.count("ATA") == 1
    assert rig.bridge.max_writers == 1
    assert agent.started and agent.said
    events_text = (rig.session._record.path / "events.jsonl").read_text(encoding="utf-8")
    events = [json.loads(line) for line in events_text.splitlines()]
    assert sum(event["type"] == "answered" for event in events) == 1


def test_duplicate_claim_never_renews_media_budget(rig, monkeypatch):
    rig.ring()
    session = rig.session

    def tick():
        rig.clock.tick = lambda: None
        assert rig.claim().accepted
        request = session._takeover_request
        deadline = session._phone_first_claim_deadline
        assert deadline == pytest.approx(rig.clock.now + 20)
        monkeypatch.setenv("INBOUND_PHONE_FIRST_MEDIA_SECONDS", "30")
        rig.clock.now += 1
        repeated = session.accept_takeover_claim(
            offer_id=request.offer_id, call_id=request.call_id, claim_id="claim_test",
            generation=request.generation, nonce=request.nonce, issued=rig.issued,
        )
        assert repeated.accepted and repeated.idempotent
        assert session._phone_first_claim_deadline == deadline

    rig.clock.tick = tick
    asyncio.run(session._handle_call())
    assert rig.commands.count("ATA") == 1


@pytest.mark.parametrize("configured,seconds", [
    (None, 20), ("5", 5), ("12", 12), ("30", 30),
    ("4", 20), ("31", 20), ("nan", 20), ("1.5", 20), ("", 20), ("-1", 20),
])
@pytest.mark.parametrize("pending_connect", [False, True])
def test_media_timeout_uses_live_config_and_warns_on_invalid_value(
    rig, monkeypatch, caplog, configured, seconds, pending_connect,
):
    if configured is not None:
        monkeypatch.setenv("INBOUND_PHONE_FIRST_MEDIA_SECONDS", configured)
    rig.ring()
    cancelled = []

    async def connect():
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append(True)

    if pending_connect:
        rig.endpoint.connect = connect
    rig.endpoint.media_ready = False
    agent = FakeAgent()
    monkeypatch.setattr("agentcall.call_agent.create_agent", lambda provider: agent)

    async def loop(*args):
        return rig.bridge

    monkeypatch.setattr(rig.session, "_run_agent_loop", loop)

    def tick():
        rig.clock.tick = lambda: None
        assert rig.claim().accepted
        assert rig.session._phone_first_claim_deadline == pytest.approx(rig.clock.now + seconds)

    rig.clock.tick = tick
    asyncio.run(rig.session._handle_call())
    assert cancelled == ([True] if pending_connect else [])
    assert rig.endpoint.closed
    assert 100 + seconds <= rig.clock.now < 100 + seconds + 0.2
    assert agent.started and rig.commands.count("ATA") == 1
    invalid = configured is not None and configured not in {"5", "12", "30"}
    warnings = [record for record in caplog.records if "INBOUND_PHONE_FIRST_MEDIA_SECONDS" in record.message]
    assert len(warnings) == int(invalid)
    if invalid:
        assert warnings[0].levelname == "WARNING"
        assert "20" in warnings[0].message


def test_media_budget_is_reread_for_next_call(rig, monkeypatch):
    session = rig.session

    for seconds in (5, 30):
        monkeypatch.setenv("INBOUND_PHONE_FIRST_MEDIA_SECONDS", str(seconds))
        rig.commands.clear()
        rig.endpoint.closed = False
        rig.ring()

        def claim_once():
            rig.clock.tick = lambda: None
            assert rig.claim().accepted
            assert session._phone_first_claim_deadline == pytest.approx(rig.clock.now + seconds)

        rig.clock.tick = claim_once
        session._run()
        assert session._record.source == "remote_web_dialer"
        assert rig.commands.count("ATA") == 1


def test_hangup_while_media_connects_never_answers_or_runs_ai(rig, monkeypatch):
    rig.ring()

    async def connect():
        rig.modem._invalidate_reserved_incoming()
        rig.modem._on_hangup()

    rig.endpoint.connect = connect
    monkeypatch.setattr("agentcall.call_agent.create_agent", lambda *args: pytest.fail("unexpected AI"))

    def tick():
        rig.clock.tick = lambda: None
        assert rig.claim().accepted

    rig.clock.tick = tick
    asyncio.run(rig.session._handle_call())
    assert "ATA" not in rig.commands and "PCM" not in rig.commands
    assert rig.endpoint.closed
    assert rig.session.next_takeover_revoke().reason == "CALL_ENDED"


@pytest.mark.parametrize("fault", ["ata_error", "gone_before_answer", "pcm_failed"])
def test_modem_failure_never_reanswers_or_creates_ai_on_dead_call(rig, monkeypatch, fault):
    rig.ring()
    original = rig.modem._write_command

    def command(cmd):
        if (fault == "ata_error" and (cmd == "ATA" or "ATA" in rig.commands)) or fault == "gone_before_answer":
            rig.commands.append(cmd)
            return "ERROR" if cmd == "ATA" else "OK"
        return original(cmd)

    monkeypatch.setattr(rig.modem, "_write_command", command)
    if fault == "pcm_failed":
        rig.modem.last_voice_enable = {"ok": False}
    monkeypatch.setattr("agentcall.call_agent.create_agent", lambda *args: pytest.fail("unexpected AI"))

    def tick():
        rig.clock.tick = lambda: None
        assert rig.claim().accepted

    rig.clock.tick = tick
    asyncio.run(rig.session._handle_call())
    assert rig.commands.count("ATA") <= 1
    assert not rig.bridge.started
    meta = json.loads((rig.session._record.path / "meta.json").read_text(encoding="utf-8"))
    assert meta["status"] == ("failed" if fault == "pcm_failed" else "not_connected")


def test_clcc_error_does_not_end_window_but_two_empty_snapshots_do(rig, monkeypatch):
    rig.ring()
    monkeypatch.setattr("agentcall.call_agent.create_agent", lambda *args: pytest.fail("unexpected AI"))

    def tick():
        rig.clock.tick = lambda: None
        for _ in range(3):
            rig.modem._process_clcc_response("ERROR")
        assert rig.session.is_active
        rig.modem._process_clcc_response("OK")
        assert rig.session.is_active
        rig.modem._process_clcc_response("OK")
        assert not rig.session.is_active

    rig.clock.tick = tick
    asyncio.run(rig.session._handle_call())
    assert not rig.commands
    assert rig.session.next_takeover_revoke().reason == "CALL_ENDED"


def test_stop_keeps_admission_closed_until_blocked_answer_worker_finishes(rig, monkeypatch):
    rig.ring()
    lease = rig.session._phone_first_lease
    generation = rig.session._session_generation
    entered, release = threading.Event(), threading.Event()
    original = rig.modem._write_command

    def command(cmd):
        result = original(cmd)
        if cmd == "ATA":
            entered.set()
            assert release.wait(2)
        return result

    monkeypatch.setattr(rig.modem, "_write_command", command)

    async def exercise():
        task = asyncio.create_task(rig.session._answer_phone_first(generation, lease))
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            rig.session.stop()
            assert rig.session.is_busy
            rig.ring()
            assert rig.session._session_generation == generation
        finally:
            release.set()
        assert not await task

    asyncio.run(exercise())
    rig.modem.finish_reserved_incoming(lease)
    rig.session._resources_released = True
    rig.ring()
    before = list(rig.commands)
    rig.modem.finish_reserved_incoming(lease)
    assert not asyncio.run(rig.session._answer_phone_first(generation, lease))
    assert rig.commands == before


def test_mobile_and_early_end_publish_terminal_ui_status(rig):
    rig.ring()
    events = []
    rig.session.hub = SimpleNamespace(publish=events.append)

    def tick():
        rig.clock.tick = lambda: None
        assert rig.claim().accepted

    rig.clock.tick = tick
    asyncio.run(rig.session._handle_call())
    assert [event["status"] for event in events if event["type"] == "call"] == ["answered", "ended"]


def test_media_cleanup_cannot_delay_ai_fallback(rig, monkeypatch):
    rig.ring()
    rig.endpoint.media_ready = False
    agent_started = asyncio.Event()
    agent = FakeAgent()

    def create(provider):
        agent_started.set()
        return agent

    monkeypatch.setattr("agentcall.call_agent.create_agent", create)

    async def close():
        await asyncio.wait_for(agent_started.wait(), timeout=0.1)
        rig.endpoint.closed = True

    rig.endpoint.close = close

    async def loop(*args):
        assert rig.clock.now < 120.2
        return rig.bridge

    monkeypatch.setattr(rig.session, "_run_agent_loop", loop)

    def tick():
        rig.clock.tick = lambda: None
        assert rig.claim().accepted

    rig.clock.tick = tick
    asyncio.run(rig.session._handle_call())
    assert rig.endpoint.closed


@pytest.mark.parametrize("after_pcm", [False, True])
@pytest.mark.parametrize("with_command_id", [False, True])
def test_owner_cancels_media_preparation_then_ai_answers(rig, monkeypatch, after_pcm, with_command_id):
    rig.ring()
    agent = FakeAgent()
    monkeypatch.setattr("agentcall.call_agent.create_agent", lambda provider: agent)

    reads = 0

    async def next_command(timeout):
        nonlocal reads
        reads += 1
        if after_pcm and reads == 1:
            return None
        assert ("ATA" in rig.commands) is after_pcm
        assert ("PCM" in rig.commands) is after_pcm
        return {"type": "hangup", **({"commandId": "32e5a14a-d2ad-4365-9908-7b071ec4db62"}
                                    if with_command_id else {})}

    rig.endpoint.next_command = next_command

    async def loop(*args):
        assert rig.session.takeover_state is TakeoverState.AI_ACTIVE
        return rig.bridge

    monkeypatch.setattr(rig.session, "_run_agent_loop", loop)

    def tick():
        rig.clock.tick = lambda: None
        assert rig.claim().accepted

    rig.clock.tick = tick
    asyncio.run(rig.session._handle_call())
    assert agent.started and rig.commands.count("ATA") == 1
    events_text = (rig.session._record.path / "events.jsonl").read_text(encoding="utf-8")
    events = [json.loads(line) for line in events_text.splitlines()]
    assert sum(event["type"] == "takeover_owner_cancelled_precommit" for event in events) == 1
    assert sum(event.get("stage") == "edge_hangup_during_prepare" for event in events) == 1
    assert not any(event["type"] == "takeover_owner_hangup" for event in events)
    assert not any(event.get("type") == "hangup.ack" for event in rig.endpoint.events)


def test_late_duplicate_claim_cannot_revoke_winner_media_grace(rig, monkeypatch):
    rig.ring()
    session = rig.session
    monkeypatch.setattr("agentcall.call_agent.create_agent", lambda *args: pytest.fail("winner lost to duplicate"))

    def tick():
        rig.clock.tick = lambda: None
        rig.clock.now = 114.99
        assert rig.claim().accepted
        request = session._takeover_request
        fence = session.takeover_fence
        rig.clock.now = 115.1
        repeated = session.accept_takeover_claim(
            offer_id=request.offer_id, call_id=request.call_id, claim_id="claim_test",
            generation=request.generation, nonce=request.nonce, issued=rig.issued,
        )
        assert not repeated.accepted
        assert session.takeover_fence == fence

    rig.clock.tick = tick
    asyncio.run(session._handle_call())
    assert rig.commands.count("ATA") == 1
    assert session._record.source == "remote_web_dialer"


@pytest.mark.parametrize("response", [
    'NO CARRIER\r\n+CLCC: 1,1,4,0,0,"",129\r\nOK',
    '+CLCC: 1,1,4,0,0,"",129',
])
def test_answer_preflight_rejects_ended_or_incomplete_snapshot(monkeypatch, response):
    modem = Eg25Modem("unused")
    commands = []
    monkeypatch.setattr(modem, "_write_command", lambda cmd: commands.append(cmd) or response)
    monkeypatch.setattr(modem, "_request_release_cause", lambda *args: None)
    lease = modem.reserve_incoming()
    assert not modem.answer_reserved_incoming(lease, lambda: True)
    assert commands == ["AT+CLCC"]


@pytest.mark.parametrize("ending", ["hangup", "disconnect"])
def test_direct_mobile_uses_existing_duplex_pump_and_disconnect_policy(rig, monkeypatch, ending):
    rig.ring()
    monkeypatch.setenv("REMOTE_DISCONNECT_GRACE_SECONDS", "0")
    monkeypatch.setattr("agentcall.call_agent.create_agent", lambda *args: pytest.fail("unexpected AI"))
    monkeypatch.setattr(rig.session, "_pump_mobile_media", CallSession._pump_mobile_media.__get__(rig.session))
    rig.bridge.feed_uplink(b"caller-to-owner")
    sent = []
    reads = 0

    async def next_command(timeout):
        nonlocal reads
        if rig.session.takeover_state is TakeoverState.MOBILE_ACTIVE:
            reads += 1
            if reads > 1:
                if ending == "hangup":
                    return {"type": "hangup"}
                rig.endpoint.media_ready = False
        return None

    rig.endpoint.next_command = next_command
    rig.endpoint.take_browser_audio = lambda: [b"owner-to-caller"]
    rig.endpoint.push_modem_audio = sent.append

    def tick():
        rig.clock.tick = lambda: None
        assert rig.claim().accepted

    rig.clock.tick = tick
    asyncio.run(rig.session._handle_call())
    assert rig.bridge.downlink == [b"owner-to-caller"]
    assert sent == [b"caller-to-owner"]
    assert rig.commands.count("ATA") == rig.commands.count("HANGUP") == 1
    assert rig.endpoint.closed and rig.bridge.stopped
    events_text = (rig.session._record.path / "events.jsonl").read_text(encoding="utf-8")
    events = [json.loads(line) for line in events_text.splitlines()]
    stages = [event["stage"] for event in events if event["type"] == "latency"]
    assert stages.count("edge_first_phone_to_modem") == stages.count("edge_first_modem_to_phone") == 1
    assert stages.index("edge_pcm_ready") < stages.index("edge_first_phone_to_modem")
    assert stages.index("edge_first_phone_to_modem") < stages.index("edge_ath_complete")
    assert stages.count("edge_hangup_received") == (1 if ending == "hangup" else 0)


def test_later_ai_takeover_does_not_inherit_expired_phone_first_window(rig):
    rig.ring()
    session = rig.session
    session._initialize_takeover_context("inbound")
    session._phone_first_cloud_ready = lambda: False
    assert asyncio.run(session._handle_phone_first(None)) == "ai"
    rig.clock.now = 116
    session._takeover_endpoint_rejection = lambda: None
    assert session.begin_owner_takeover()["success"]
    assert rig.claim().accepted


def test_next_clcc_only_call_can_reuse_index_after_window_cleanup():
    modem = Eg25Modem("unused")
    rings = []
    modem.on_ring(rings.append)
    incoming = '+CLCC: 1,1,4,0,0,"",129\r\nOK'
    modem._process_clcc_response(incoming)
    lease = modem.reserve_incoming()
    modem._invalidate_reserved_incoming()
    modem.finish_reserved_incoming(lease)
    modem._process_clcc_response(incoming)
    assert rings == [None, None]


def enable_early_answer(rig, monkeypatch):
    monkeypatch.setenv("INBOUND_EARLY_ANSWER_ENABLED", "true")
    monkeypatch.setenv("MODEM_ANSWER_GREETING", "")
    rig.modem.last_voice_enable = {"ok": True, "attempts": 1}
    rig.advance_early_clock = False
    rig.early_bridge_started = threading.Event()
    original_start = rig.bridge.start

    def start():
        original_start()
        rig.early_bridge_started.set()

    monkeypatch.setattr(rig.bridge, "start", start)

    async def sleep(seconds):
        # Only timeout tests advance time, after their prerequisites are ready.
        # Worker scheduling must not consume a call's fake media budget.
        rig.clock.sleep_sync(seconds if rig.advance_early_clock else 0)
        await asyncio.sleep(0)

    rig.session._phone_first_sleep = sleep
    rig.ring()

    def claim_once():
        rig.clock.tick = lambda: None
        assert rig.claim().accepted

    rig.clock.tick = claim_once


async def wait_early_event(event):
    # A deadlock watchdog, not a delay used to establish ordering.
    assert await asyncio.to_thread(event.wait, 5), "early-answer checkpoint was not reached"


def phone_first_events(rig):
    return [json.loads(line) for line in
            (rig.session._record.path / "events.jsonl").read_text(encoding="utf-8").splitlines()]


def test_early_answer_pcm_precedes_late_media_and_discards_waiting_audio(rig, monkeypatch):
    enable_early_answer(rig, monkeypatch)
    monkeypatch.setattr("agentcall.call_agent.create_agent", lambda *args: pytest.fail("unexpected AI"))
    rig.endpoint.media_ready = False
    order = []
    original_start = rig.bridge.start
    original_read = rig.bridge.read_modem_chunk
    drained = threading.Event()

    def start():
        original_start()
        order.append("pcm")
        rig.bridge.feed_uplink(b"old caller audio")
        rig.bridge.feed_uplink(b"more old caller audio")

    def read():
        chunk = original_read()
        if rig.bridge.started and not chunk:
            drained.set()
        return chunk

    monkeypatch.setattr(rig.bridge, "start", start)
    monkeypatch.setattr(rig.bridge, "read_modem_chunk", read)

    async def connect():
        await wait_early_event(drained)
        assert rig.bridge.started, "PCM must start without waiting for phone media"
        assert rig.bridge.read_modem_chunk() == b""
        assert rig.session.takeover_state is not TakeoverState.MOBILE_ACTIVE
        assert rig.commands.count("ATA") == rig.commands.count("PCM") == 1
        order.append("media")
        rig.endpoint.media_ready = True

    async def pump(*args):
        order.append("pump")
        assert rig.endpoint.media_ready
        assert rig.session.takeover_state is TakeoverState.MOBILE_ACTIVE
        assert rig.bridge.read_modem_chunk() == b""

    rig.endpoint.connect = connect
    monkeypatch.setattr(rig.session, "_pump_mobile_media", pump)
    asyncio.run(rig.session._handle_call())
    assert order == ["pcm", "media", "pump"]
    assert rig.commands.count("ATA") == rig.commands.count("PCM") == 1
    events = phone_first_events(rig)
    assert sum(event["type"] == "phone_first_early_answer" for event in events) == 1
    waits = [event for event in events if event.get("stage") == "phone_first_connected_wait_ms"]
    assert len(waits) == 1


def test_early_answer_media_ready_still_waits_for_pcm_without_serial_lock(rig, monkeypatch):
    enable_early_answer(rig, monkeypatch)
    monkeypatch.setattr("agentcall.call_agent.create_agent", lambda *args: pytest.fail("unexpected AI"))
    entered, release = threading.Event(), threading.Event()
    original_initialize = rig.modem.initialize_for_voice
    original_reserved = rig.modem.incoming_reserved
    original_guard = rig.session._phone_first_guard
    loop_thread = threading.get_ident()
    progress = []
    checked_while_locked = threading.Event()

    def initialize(mode):
        with rig.modem._serial_lock:
            entered.set()
            assert release.wait(5), "event loop blocked behind modem serial lock"
            original_initialize(mode)

    def reserved(lease):
        if entered.is_set() and not release.is_set():
            assert threading.get_ident() != loop_thread, "serial-lock check on event loop"
        return original_reserved(lease)

    def guard():
        if threading.get_ident() == loop_thread and entered.is_set() and not release.is_set():
            assert rig.session.takeover_state is not TakeoverState.MOBILE_ACTIVE
            progress.append(True)
            if len(progress) >= 3:
                checked_while_locked.set()
        return original_guard()

    async def connect():
        try:
            await wait_early_event(entered)
            await wait_early_event(checked_while_locked)
        finally:
            release.set()

    monkeypatch.setattr(rig.modem, "initialize_for_voice", initialize)
    monkeypatch.setattr(rig.modem, "incoming_reserved", reserved)
    monkeypatch.setattr(rig.session, "_phone_first_guard", guard)
    rig.endpoint.connect = connect
    asyncio.run(rig.session._handle_call())
    assert len(progress) >= 3
    assert rig.session._record.source == "remote_web_dialer"
    assert rig.commands.count("ATA") == rig.commands.count("PCM") == 1


@pytest.mark.parametrize("parallel", [False, True])
@pytest.mark.parametrize("failure", [
    "connect_error", "timeout", "claim_timeout", "owner_cancelled", "commit_rejected", "media_lost",
])
def test_early_answer_fallback_reuses_pcm(rig, monkeypatch, parallel, failure):
    enable_early_answer(rig, monkeypatch)
    monkeypatch.setenv("PARALLEL_AGENT_CONNECT", str(parallel))
    if failure == "claim_timeout":
        monkeypatch.setenv("INBOUND_PHONE_FIRST_MEDIA_SECONDS", "5")
    agent = FakeAgent()
    monkeypatch.setattr("agentcall.call_agent.create_agent", lambda provider: agent)
    pcm_at = []
    original_start = rig.bridge.start

    def start():
        if not pcm_at:
            pcm_at.append(rig.clock.now)
        original_start()

    monkeypatch.setattr(rig.bridge, "start", start)

    async def loop(*args):
        assert rig.session.takeover_state is TakeoverState.AI_ACTIVE
        assert rig.commands.count("PCM") == 1
        return rig.bridge

    monkeypatch.setattr(rig.session, "_run_agent_loop", loop)
    if failure in {"connect_error", "timeout", "claim_timeout"}:
        rig.endpoint.media_ready = False

    async def connect():
        await wait_early_event(rig.early_bridge_started)
        if failure == "connect_error":
            # The reuse event must use the original result, not a later snapshot.
            rig.modem.last_voice_enable = {"ok": False, "attempts": 9}
            raise ConnectionError("synthetic")
        if failure == "commit_rejected":
            rig.session._takeover_coordinator.commit_mobile = lambda fence: SimpleNamespace(accepted=False)
        if failure in {"timeout", "claim_timeout"}:
            rig.advance_early_clock = True

    async def next_command(timeout):
        if failure == "owner_cancelled":
            return {"type": "hangup"}
        if failure == "media_lost":
            rig.endpoint.media_ready = False
        return None

    rig.endpoint.connect = connect
    rig.endpoint.next_command = next_command
    asyncio.run(rig.session._handle_call())
    assert agent.started and agent.said
    assert rig.commands.count("ATA") == rig.commands.count("PCM") == 1
    assert rig.bridge.max_writers == 1
    assert rig.endpoint.closed and rig.bridge.stopped
    events = phone_first_events(rig)
    enables = [event for event in events if event["type"] == "pcm_enable"]
    assert len(enables) == 1
    assert enables[0]["reused"] is True and enables[0]["ok"] is True and enables[0]["attempts"] == 1
    assert sum(event["type"] == "answered" for event in events) == 1
    waits = [event for event in events if event.get("stage") == "phone_first_connected_wait_ms"]
    assert len(waits) == 1
    if failure == "timeout":
        assert 8000 <= waits[0]["ms"] < 8051
        assert 8 <= rig.clock.now - pcm_at[0] < 8.1
        assert rig.clock.now < rig.session._phone_first_claim_deadline
    if failure == "claim_timeout":
        expected_ms = (rig.session._phone_first_claim_deadline - pcm_at[0]) * 1000
        assert expected_ms <= waits[0]["ms"] < expected_ms + 51
        assert rig.session._phone_first_claim_deadline <= rig.clock.now < rig.session._phone_first_claim_deadline + 0.1
    if failure == "owner_cancelled":
        assert sum(event["type"] == "takeover_owner_cancelled_precommit" for event in events) == 1
        assert not any(event["type"] == "takeover_owner_hangup" for event in events)
        assert not any(event.get("type") == "hangup.ack" for event in rig.endpoint.events)


def test_early_answer_caller_ends_while_waiting_for_media(rig, monkeypatch):
    enable_early_answer(rig, monkeypatch)
    monkeypatch.setattr("agentcall.call_agent.create_agent", lambda *args: pytest.fail("unexpected AI"))
    rig.endpoint.media_ready = False

    async def connect():
        await wait_early_event(rig.early_bridge_started)
        rig.modem._invalidate_reserved_incoming()
        rig.modem._on_hangup()

    rig.endpoint.connect = connect
    asyncio.run(rig.session._handle_call())
    assert rig.commands.count("ATA") == rig.commands.count("PCM") == 1
    assert rig.endpoint.closed and rig.bridge.stopped
    meta = json.loads((rig.session._record.path / "meta.json").read_text(encoding="utf-8"))
    assert meta["status"] == "completed"


@pytest.mark.parametrize("failure", ["ata", "pcm"])
def test_early_answer_modem_failure_does_not_start_ai(rig, monkeypatch, failure):
    enable_early_answer(rig, monkeypatch)
    monkeypatch.setattr("agentcall.call_agent.create_agent", lambda *args: pytest.fail("unexpected AI"))
    original = rig.modem._write_command

    def command(cmd):
        result = original(cmd)
        return "ERROR" if cmd == "ATA" and failure == "ata" else result

    monkeypatch.setattr(rig.modem, "_write_command", command)
    if failure == "pcm":
        rig.modem.last_voice_enable = {"ok": False}
    asyncio.run(rig.session._handle_call())
    assert rig.commands.count("ATA") == 1
    assert rig.commands.count("PCM") == int(failure == "pcm")
    assert rig.endpoint.closed and not rig.bridge.started
    meta = json.loads((rig.session._record.path / "meta.json").read_text(encoding="utf-8"))
    assert meta["status"] == ("not_connected" if failure == "ata" else "failed")


def test_early_answer_adopts_ready_preconnect_before_pcm(rig, monkeypatch):
    enable_early_answer(rig, monkeypatch)
    monkeypatch.setenv("INBOUND_PRECONNECT_ENABLED", "true")
    monkeypatch.setattr("agentcall.call_agent.create_agent", lambda *args: pytest.fail("unexpected AI"))
    operations = []
    session = rig.session
    bound = threading.Event()
    original_initialize = rig.modem.initialize_for_voice

    def initialize(mode):
        assert bound.wait(5)
        original_initialize(mode)

    monkeypatch.setattr(rig.modem, "initialize_for_voice", initialize)

    async def prepare():
        operations.append("prepare")
        assert not rig.commands

    async def bind(issued):
        operations.append("bind")
        assert "PCM" not in rig.commands
        assert session.takeover_state is not TakeoverState.MOBILE_ACTIVE
        bound.set()

    async def connect():
        pytest.fail("ready preconnect must be reused")

    rig.endpoint.preconnect, rig.endpoint.bind_winner, rig.endpoint.connect = prepare, bind, connect

    def tick():
        request = session._takeover_request
        if not operations:
            assert session.prepare_takeover({
                "offerId": request.offer_id, "callId": request.call_id, "generation": request.generation,
                "nonce": request.nonce, "session": {
                    "sessionId": "session_test", "roomName": rig.issued.room_name,
                    "browserIdentity": rig.issued.browser_identity, "edgeIdentity": rig.issued.edge_identity,
                    "livekitUrl": rig.issued.livekit_url, "token": rig.issued.edge_token,
                },
            })
        else:
            rig.clock.tick = lambda: None
            assert rig.claim().accepted

    rig.clock.tick = tick

    async def pump(*args):
        assert operations == ["prepare", "bind"]
        assert rig.commands.count("PCM") == 1
        assert session.takeover_state is TakeoverState.MOBILE_ACTIVE

    monkeypatch.setattr(session, "_pump_mobile_media", pump)
    asyncio.run(session._handle_call())
    assert session._record.source == "remote_web_dialer"
    assert rig.commands.count("ATA") == rig.commands.count("PCM") == 1


@pytest.mark.parametrize("initial", [False, True])
def test_early_answer_setting_is_frozen_at_claim_and_reset_for_next_call(rig, monkeypatch, initial):
    enable_early_answer(rig, monkeypatch)
    monkeypatch.setattr("agentcall.call_agent.create_agent", lambda *args: pytest.fail("unexpected AI"))
    for enabled in (initial, not initial):
        monkeypatch.setenv("INBOUND_EARLY_ANSWER_ENABLED", str(enabled))
        rig.commands.clear()
        rig.endpoint.closed = False
        rig.ring()

        def claim_once():
            rig.clock.tick = lambda: None
            assert rig.claim().accepted
            monkeypatch.setenv("INBOUND_EARLY_ANSWER_ENABLED", str(not enabled))

        rig.clock.tick = claim_once
        rig.session._run()
        assert rig.session._record.source == "remote_web_dialer"
        events = phone_first_events(rig)
        assert any(event["type"] == "phone_first_early_answer" for event in events) is enabled
        assert (rig.session._phone_first_pcm_enable_info is not None) is enabled


@pytest.mark.parametrize("ending", ["connect_error", "cancel", "hangup"])
@pytest.mark.parametrize("order", ["abandon_first", "pcm_first"])
def test_early_answer_joins_pcm_worker_before_cleanup_and_never_builds_late_bridge(rig, monkeypatch, ending, order):
    enable_early_answer(rig, monkeypatch)
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    original_initialize = rig.modem.initialize_for_voice
    original_sleep = rig.session._phone_first_sleep
    original_threaded = rig.session._threaded_modem_call
    original_gather = asyncio.gather
    original_close = rig.endpoint.close
    agent = FakeAgent()
    rig.endpoint.media_ready = False
    main_task = answer_task = None
    checkpoints = asyncio.Queue()
    fail_connect, connect_failed = asyncio.Event(), asyncio.Event()
    abandoned, early_started = asyncio.Event(), asyncio.Event()
    bridges = {}
    lifecycle = []
    writers = max_writers = 0

    def create_agent(provider):
        assert ending == "connect_error" and finished.is_set()
        return agent

    def initialize(mode):
        with rig.modem._serial_lock:
            entered.set()
            assert release.wait(5)
            original_initialize(mode)

    async def threaded(operation, *args):
        nonlocal answer_task
        if operation == rig.modem.prepare_reserved_voice:
            answer_task = asyncio.current_task()
            try:
                return await original_threaded(operation, *args)
            finally:
                lifecycle.append("pcm_finished")
                finished.set()
        return await original_threaded(operation, *args)

    def gather(*tasks, **kwargs):
        # Both failure and cancellation join the answer task only after setting
        # abandoned. Observe that real boundary, not the earlier error signal.
        if asyncio.current_task() is main_task and answer_task is not None and answer_task in tasks:
            if not abandoned.is_set():
                lifecycle.append("abandoned")
                abandoned.set()
        return original_gather(*tasks, **kwargs)

    async def sleep(seconds):
        await original_sleep(seconds)
        if asyncio.current_task() is main_task and rig.session._phone_first_claim_deadline is not None:
            checkpoint = asyncio.get_running_loop().create_future()
            checkpoints.put_nowait(checkpoint)
            await checkpoint

    def create_bridge(**kwargs):
        owner = "ai" if rig.session.takeover_state is TakeoverState.AI_ACTIVE else "early"
        lifecycle.append(f"{owner}_create")
        assert finished.is_set(), "a bridge was created before PCM worker completion"
        bridge = FakeAudioBridge()
        assert owner not in bridges
        bridges[owner] = bridge

        def start():
            nonlocal writers, max_writers
            lifecycle.append(f"{owner}_start")
            assert finished.is_set(), "a bridge started before PCM worker completion"
            if owner == "early":
                assert not abandoned.is_set(), "early bridge started after abandonment"
                early_started.set()
            else:
                assert abandoned.is_set()
                assert "early" not in bridges or bridges["early"].stopped
            bridge.started = True
            writers += 1
            max_writers = max(writers, max_writers)
            assert writers <= 1

        def stop():
            nonlocal writers
            lifecycle.append(f"{owner}_stop")
            assert finished.is_set()
            if bridge.started and not bridge.stopped:
                writers -= 1
            bridge.stopped = True

        bridge.start, bridge.stop = start, stop
        return bridge

    async def close():
        lifecycle.append("endpoint_close")
        assert finished.is_set(), "endpoint closed before PCM worker completion"
        assert "early" not in bridges or bridges["early"].stopped
        await original_close()

    async def loop(agent, bridge, *args):
        return bridge

    async def connect():
        if ending == "connect_error":
            await fail_connect.wait()
            connect_failed.set()
            raise ConnectionError("synthetic")
        await asyncio.Event().wait()

    async def exercise():
        nonlocal main_task
        main_task = asyncio.create_task(rig.session._handle_call())
        try:
            async with asyncio.timeout(5):
                # First release the ringing loop, then hold the claimed main
                # loop while its independent answer task enters PCM preparation.
                (await checkpoints.get()).set_result(None)
                waiting = await checkpoints.get()
                await wait_early_event(entered)
                assert not finished.is_set() and not bridges and not rig.endpoint.closed
                if ending == "connect_error":
                    fail_connect.set()
                    await connect_failed.wait()
                if order == "pcm_first":
                    release.set()
                    await early_started.wait()
                    assert finished.is_set() and not abandoned.is_set()
                    assert not bridges["early"].stopped and not rig.endpoint.closed
                if ending == "cancel":
                    main_task.cancel()
                else:
                    if ending == "hangup":
                        rig.session._set_active(False)
                    waiting.set_result(None)
                await abandoned.wait()
                if order == "abandon_first":
                    assert not finished.is_set() and not bridges and not rig.endpoint.closed
                    assert not main_task.done()
                    release.set()
                if ending == "cancel":
                    with pytest.raises(asyncio.CancelledError):
                        await main_task
                else:
                    await main_task
        finally:
            release.set()
            if not main_task.done():
                main_task.cancel()
            await original_gather(main_task, return_exceptions=True)

    monkeypatch.setattr("agentcall.call_agent.create_agent", create_agent)
    monkeypatch.setattr("agentcall.call_agent.create_audio_bridge", create_bridge)
    monkeypatch.setattr(rig.modem, "initialize_for_voice", initialize)
    monkeypatch.setattr(rig.session, "_threaded_modem_call", threaded)
    monkeypatch.setattr(rig.session, "_phone_first_sleep", sleep)
    monkeypatch.setattr(rig.session, "_run_agent_loop", loop)
    monkeypatch.setattr(asyncio, "gather", gather)
    rig.endpoint.connect = connect
    rig.endpoint.close = close
    asyncio.run(exercise())
    assert finished.is_set() and rig.endpoint.closed
    assert all(bridge.stopped for bridge in bridges.values())
    assert lifecycle.index("pcm_finished") < lifecycle.index("endpoint_close")
    for owner in bridges:
        assert lifecycle.index("pcm_finished") < lifecycle.index(f"{owner}_create")
    if order == "pcm_first":
        assert lifecycle.index("early_start") < lifecycle.index("abandoned") < lifecycle.index("early_stop")
        assert lifecycle.index("early_stop") < lifecycle.index("endpoint_close")
        if ending == "connect_error":
            assert lifecycle.index("early_stop") < lifecycle.index("ai_start")
    else:
        assert lifecycle.index("abandoned") < lifecycle.index("pcm_finished")
        assert "early_create" not in lifecycle and "early_start" not in lifecycle
    assert agent.started is (ending == "connect_error")
    assert rig.commands.count("ATA") == rig.commands.count("PCM") == 1
    assert max_writers <= 1 and writers == 0


@pytest.mark.parametrize("ready", [False, True])
def test_early_answer_continuous_uplink_cannot_block_media_or_timeout(rig, monkeypatch, ready):
    enable_early_answer(rig, monkeypatch)
    rig.endpoint.media_ready = False
    agent = FakeAgent()
    monkeypatch.setattr("agentcall.call_agent.create_agent", lambda provider: agent)
    loop_thread = threading.get_ident()
    reads = []
    first_read, connect_observed = threading.Event(), threading.Event()

    def read():
        assert threading.get_ident() != loop_thread
        reads.append(True)
        first_read.set()
        assert connect_observed.wait(5)
        rig.clock.now += 0.01
        assert len(reads) < 1000, "continuous PCM must not trap the drain loop"
        return b"live caller audio"

    async def connect():
        await wait_early_event(first_read)
        rig.endpoint.media_ready = ready
        connect_observed.set()

    async def loop(*args):
        return rig.bridge

    monkeypatch.setattr(rig.bridge, "read_modem_chunk", read)
    monkeypatch.setattr(rig.session, "_run_agent_loop", loop)
    rig.endpoint.connect = connect
    asyncio.run(rig.session._handle_call())
    assert reads and rig.endpoint.closed and rig.bridge.stopped
    assert agent.started is not ready
    assert (rig.session._record.source == "remote_web_dialer") is ready
    assert rig.commands.count("ATA") == rig.commands.count("PCM") == 1
    if not ready:
        waits = [event for event in phone_first_events(rig)
                 if event.get("stage") == "phone_first_connected_wait_ms"]
        assert len(waits) == 1 and 8000 <= waits[0]["ms"] < 8101


@pytest.mark.parametrize("hangup_read", [1, 2, 3])
def test_v2_owner_hangup_before_commit_ends_call_instead_of_starting_ai(rig, monkeypatch, hangup_read):
    from dataclasses import replace

    from agentcall.inbound_direct import InboundDirectRequest

    monkeypatch.setenv("INBOUND_FASTPATH_V2_ENABLED", "true")
    rig.ring()
    session = rig.session
    session._initialize_takeover_context("inbound")
    rig.endpoint.media_ready = hangup_read != 1
    command_id = "00000000-0000-4000-8000-000000000001"
    reads = 0

    async def next_command(timeout):
        nonlocal reads
        reads += 1
        if reads == hangup_read:
            return {"type": "hangup", "commandId": command_id}
        return None

    rig.endpoint.next_command = next_command

    def claim():
        rig.clock.tick = lambda: None
        request = session.next_takeover_offer()
        assert request is not None
        direct = InboundDirectRequest(request.offer_id, "claim_test", "v=0", [], lambda *_: None)
        accepted = session.accept_takeover_claim(
            offer_id=request.offer_id, call_id=request.call_id, claim_id="claim_test",
            generation=request.generation, nonce=request.nonce, issued=replace(rig.issued, direct=direct),
        )
        assert accepted.accepted

    rig.clock.tick = claim
    result = asyncio.run(session._handle_phone_first(None))
    assert result in {"completed", "not_connected"}, "Explicit owner hangup must never select AI fallback"
    assert session.takeover_state is TakeoverState.ENDED
    assert {"type": "hangup.ack", "commandId": command_id} in rig.endpoint.events


def test_v2_client_timing_survives_failure_before_media_ready(rig, monkeypatch):
    monkeypatch.setenv("INBOUND_FASTPATH_V2_ENABLED", "true")
    rig.endpoint.media_ready = False
    commands = [
        {"type": "client_timing", "v": 1, "attempt": "00000000-0000-4000-8000-000000000001",
         "seq": 1, "marks": {"answer_action": 0, "claim_start": 1, "claim_response": 50}},
        {"type": "hangup", "commandId": "00000000-0000-4000-8000-000000000002"},
    ]

    async def next_command(timeout):
        return commands.pop(0) if commands else None

    rig.endpoint.next_command = next_command
    rig.ring()
    rig.session._initialize_takeover_context("inbound")

    def claim():
        rig.clock.tick = lambda: None
        offer = rig.session.next_takeover_offer()
        direct = InboundDirectRequest(offer.offer_id, "claim_timing", "v=0", [], lambda *_: None)
        assert rig.session.accept_takeover_claim(
            offer_id=offer.offer_id, call_id=offer.call_id, claim_id="claim_timing",
            generation=offer.generation, nonce=offer.nonce, issued=replace(rig.issued, direct=direct),
        ).accepted

    rig.clock.tick = claim
    assert asyncio.run(rig.session._handle_phone_first(None)) in {"completed", "not_connected"}
    assert rig.session._mobile_timing.summary()["client_claim_http_ms"] == 49


def test_http_client_timing_is_same_winner_fenced_and_received_before_end(rig, monkeypatch):
    monkeypatch.setenv("INBOUND_FASTPATH_V2_ENABLED", "true")
    session = rig.session
    rig.ring()
    session._initialize_takeover_context("inbound")
    batch = {"type": "client_timing", "v": 1, "attempt": "00000000-0000-4000-8000-000000000001",
             "seq": 1, "marks": {"claim_start": 1, "claim_response": 20}}

    def claim():
        rig.clock.tick = lambda: None
        offer = session.next_takeover_offer()
        direct = InboundDirectRequest(offer.offer_id, "claim_timing", None, [], lambda *_: None)
        assert session.accept_takeover_claim(
            offer_id=offer.offer_id, call_id=offer.call_id, claim_id="claim_timing",
            generation=offer.generation, nonce=offer.nonce, issued=replace(rig.issued, direct=direct),
        ).accepted
        command = dict(offerId=offer.offer_id, callId=offer.call_id, claimId="claim_timing",
                       generation=offer.generation, nonce=offer.nonce, clientTiming=[batch])
        rig.service.receive_inbound_client_timing({**command, "claimId": "wrong-claim"})
        assert session._mobile_timing.summary()["client_claim_http_ms"] is None
        rig.service.receive_inbound_client_timing(command)
        assert session._mobile_timing.summary()["client_claim_http_ms"] == 19
        assert rig.service.end_inbound_direct(command)
        rig.service.receive_inbound_client_timing({**command, "clientTiming": [
            {**batch, "seq": 2, "marks": {"connect_start": 1, "connect_return": 2}},
        ]})
        assert session._mobile_timing.summary()["client_connect_ms"] is None

    rig.clock.tick = claim
    assert asyncio.run(session._handle_phone_first(None)) == "not_connected"


@pytest.mark.parametrize("outcome", ["lease", "repair", "repair_failed", "hangup"])
def test_no_phone_audio_starts_ai_at_eight_seconds_and_preserves_same_claim_pcm(rig, monkeypatch, outcome):
    from agentcall.inbound_direct import SwitchableInboundEndpoint

    monkeypatch.setenv("INBOUND_FASTPATH_V2_ENABLED", "true")
    monkeypatch.setenv("MODEM_ANSWER_GREETING", "")
    session = rig.session
    fence, requests, ai_times = {}, [], []
    failed, spoken, finalized = asyncio.Event(), asyncio.Event(), asyncio.Event()
    agent = FakeAgent()
    created = []

    def create(_):
        created.append(agent)
        return agent

    monkeypatch.setattr("agentcall.call_agent.create_agent", create)
    original_say = agent.say

    async def say(instructions):
        ai_times.append(rig.clock.now)
        await original_say(instructions)
        spoken.set()

    agent.say = say
    original_fallback = session._phone_first_fallback

    def finalize(reason, generation):
        original_fallback(reason, generation)
        if reason == "NO_PHONE_AUDIO":
            finalized.set()

    monkeypatch.setattr(session, "_phone_first_fallback", finalize)

    async def ai_loop(*_):
        if outcome in {"repair", "hangup"}:
            await asyncio.Event().wait()
        await finalized.wait()
        assert session.takeover_state is TakeoverState.AI_ACTIVE
        assert len(created) == len(agent.said) == 1 and not agent.stopped
        session._set_active(False)
        return rig.bridge

    monkeypatch.setattr(session, "_run_agent_loop", ai_loop)
    direct = rig.endpoint
    direct.media_ready = False
    direct.phone_audio_received = False
    direct.browser_connected = True
    direct.answer = {"sdp": "answer"}

    async def connect():
        direct.direct_connected_at = rig.clock.now

    direct.connect = connect
    original_send = direct.send_event

    async def send(value):
        await original_send(value)
        if value.get("type") == "media_failed":
            failed.set()

    direct.send_event = send
    cold = SimpleNamespace(media_ready=True, browser_connected=True, closed=False)

    async def cold_connect():
        assert agent.started and agent.said and rig.bridge.downlink
        if outcome == "repair_failed":
            raise ConnectionError("fake LiveKit failure")

    async def cold_close():
        cold.closed = True

    cold.connect, cold.close, cold.send_event, cold.next_command = (
        cold_connect, cold_close, original_send, direct.next_command,
    )
    session._takeover_endpoint_factory = lambda issued: SwitchableInboundEndpoint(
        issued.direct, direct_factory=lambda *_args, **_kw: direct, livekit_factory=lambda _: cold,
    )

    async def pump(*_):
        assert agent.stopped and session.takeover_state is TakeoverState.MOBILE_ACTIVE
        assert rig.bridge.max_writers == 1

    monkeypatch.setattr(session, "_pump_mobile_media", pump)

    async def sleep(seconds):
        rig.clock.sleep_sync(seconds if rig.bridge.started else 0)
        await asyncio.sleep(0)

    monkeypatch.setattr(session, "_phone_first_sleep", sleep)
    rig.ring()

    def claim():
        rig.clock.tick = lambda: None
        offer = session.next_takeover_offer()
        fence.update(offerId=offer.offer_id, callId=offer.call_id, claimId="claim_no_audio",
                     generation=offer.generation, nonce=offer.nonce)
        request = InboundDirectRequest(offer.offer_id, fence["claimId"], "sdp", [], lambda *_: None)
        requests.append(request)
        assert session.accept_takeover_claim(
            offer_id=offer.offer_id, call_id=offer.call_id, claim_id=fence["claimId"],
            generation=offer.generation, nonce=offer.nonce, issued=replace(rig.issued, direct=request),
        ).accepted

    rig.clock.tick = claim

    async def scenario():
        running = asyncio.create_task(session._handle_call())
        await failed.wait()
        await spoken.wait()
        assert 8 <= ai_times[0] - direct.direct_connected_at < 8.2
        assert session.takeover_state is TakeoverState.WAITING_OWNER
        if outcome in {"repair", "repair_failed"}:
            assert rig.service.fallback_inbound_direct(fence, rig.issued, lambda *_: None)
        elif outcome == "hangup":
            assert rig.service.end_inbound_direct(fence)
        await asyncio.wait_for(running, 5)

    asyncio.run(scenario())
    assert len(created) == len(agent.said) == 1 and agent.stopped
    assert rig.commands.count("ATA") == rig.commands.count("PCM") == 1
    assert rig.bridge.max_writers == 1 and rig.bridge.downlink
    if outcome == "lease":
        assert finalized.is_set() and rig.clock.now >= session._phone_first_claim_deadline
    elif outcome == "repair_failed":
        assert finalized.is_set() and rig.clock.now < session._phone_first_claim_deadline


@pytest.mark.parametrize("media_state", ["no_sdp", "ice_stalled", "livekit_connecting"])
def test_v2_connected_silence_is_state_bounded_and_ai_reuses_pcm(rig, monkeypatch, media_state):
    from agentcall.inbound_direct import SwitchableInboundEndpoint

    monkeypatch.setenv("INBOUND_FASTPATH_V2_ENABLED", "true")
    monkeypatch.setenv("MODEM_ANSWER_GREETING", "")
    rig.modem.last_voice_enable = {"ok": True, "attempts": 1}
    agent, endpoints, requests = FakeAgent(), [], []
    monkeypatch.setattr("agentcall.call_agent.create_agent", lambda _: agent)
    rig.endpoint.media_ready = False
    rig.endpoint.answer = {"sdp": "v=0\r\nanswer"}

    async def stalled_fallback():
        await asyncio.Event().wait()

    if media_state == "livekit_connecting":
        rig.endpoint.connect = stalled_fallback

    def direct_factory(*_args, **_kwargs):
        assert media_state == "ice_stalled", "No SDP must not create a peer"
        return rig.endpoint

    def factory(issued):
        endpoint = SwitchableInboundEndpoint(issued.direct,
            direct_factory=direct_factory, livekit_factory=lambda _: rig.endpoint)
        endpoints.append(endpoint)
        return endpoint

    rig.session._takeover_endpoint_factory = factory

    async def sleep(seconds):
        rig.clock.sleep_sync(seconds if rig.bridge.started else 0)
        await asyncio.sleep(0)

    async def loop(*_args):
        assert rig.session.takeover_state is TakeoverState.AI_ACTIVE
        assert rig.commands.count("ATA") == rig.commands.count("PCM") == 1
        return rig.bridge

    monkeypatch.setattr(rig.session, "_phone_first_sleep", sleep)
    monkeypatch.setattr(rig.session, "_run_agent_loop", loop)
    rig.ring()

    def claim():
        rig.clock.tick = lambda: None
        offer = rig.session.next_takeover_offer()
        assert offer is not None
        sdp = "v=0\r\n" if media_state == "ice_stalled" else None
        direct = InboundDirectRequest(offer.offer_id, "claim_deferred", sdp, [], lambda *_: None)
        requests.append(direct)
        assert rig.session.accept_takeover_claim(
            offer_id=offer.offer_id, call_id=offer.call_id, claim_id="claim_deferred",
            generation=offer.generation, nonce=offer.nonce, issued=replace(rig.issued, direct=direct),
        ).accepted
        assert rig.session._phone_first_claim_deadline - rig.session._phone_first_claimed_at == pytest.approx(30)
        if media_state == "livekit_connecting":
            assert direct.fallback(rig.issued, lambda *_: None)

    rig.clock.tick = claim
    asyncio.run(rig.session._handle_call())
    assert agent.started and agent.said
    assert rig.commands.count("ATA") == rig.commands.count("PCM") == 1
    assert rig.bridge.max_writers == 1 and rig.bridge.stopped
    assert len(endpoints) == 1 and not endpoints[0].media_ready
    assert not requests[0].supply_sdp("too-late", [], lambda *_: pytest.fail("Expired SDP resurrected claim"))
    waits = [event["ms"] for event in phone_first_events(rig)
             if event.get("stage") == "phone_first_connected_wait_ms"]
    assert len(waits) == 1
    if media_state == "no_sdp":
        assert 8000 <= waits[0] < 8101
        assert rig.clock.now < rig.session._phone_first_claim_deadline
    else:
        assert 29000 <= waits[0] < 30101, "SDP or fallback progress must not be cut off at eight seconds"
        assert rig.clock.now >= rig.session._phone_first_claim_deadline


@pytest.mark.parametrize("after_pcm", [False, True])
def test_deferred_control_hangup_is_fenced_idempotent_and_never_starts_ai(rig, monkeypatch, after_pcm):
    from agentcall.inbound_direct import SwitchableInboundEndpoint

    monkeypatch.setenv("INBOUND_FASTPATH_V2_ENABLED", "true")
    monkeypatch.setattr("agentcall.call_agent.create_agent", lambda *_: pytest.fail("Owner hangup started AI"))
    fence, endings = {}, []
    rig.session._takeover_endpoint_factory = lambda issued: SwitchableInboundEndpoint(
        issued.direct, direct_factory=lambda *_args, **_kwargs: pytest.fail("Unexpected peer without SDP"))

    async def sleep(seconds):
        rig.clock.sleep_sync(0)
        await asyncio.sleep(0)

    def end():
        if after_pcm and not rig.bridge.started:
            return
        rig.clock.tick = lambda: None
        assert not rig.service.end_inbound_direct({**fence, "claimId": "claim_wrong"})
        assert rig.session.is_active and rig.session.takeover_state is TakeoverState.WAITING_OWNER
        assert rig.service.end_inbound_direct(fence)
        assert rig.service.end_inbound_direct(fence)
        assert not rig.service.end_inbound_direct({**fence, "claimId": "claim_wrong"})
        endings.append(True)

    def claim():
        offer = rig.session.next_takeover_offer()
        assert offer is not None
        fence.update(offerId=offer.offer_id, callId=offer.call_id, claimId="claim_cancel",
                     generation=offer.generation, nonce=offer.nonce)
        direct = InboundDirectRequest(offer.offer_id, "claim_cancel", None, [], lambda *_: None)
        assert rig.session.accept_takeover_claim(
            offer_id=offer.offer_id, call_id=offer.call_id, claim_id="claim_cancel",
            generation=offer.generation, nonce=offer.nonce, issued=replace(rig.issued, direct=direct),
        ).accepted
        rig.clock.tick = end
        if not after_pcm:
            end()

    monkeypatch.setattr(rig.session, "_phone_first_sleep", sleep)
    rig.ring()
    rig.clock.tick = claim
    asyncio.run(rig.session._handle_call())
    assert endings == [True]
    assert rig.session.takeover_state is TakeoverState.ENDED
    assert not rig.session.is_active
    assert rig.commands.count("ATA") <= 1 and rig.commands.count("PCM") <= 1
    if after_pcm:
        assert rig.commands.count("ATA") == rig.commands.count("PCM") == 1
        assert rig.bridge.stopped and rig.bridge.max_writers == 1
