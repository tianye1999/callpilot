"""Hosted control-plane credentials, strict commands, and HTTP client boundaries."""

from __future__ import annotations

import json
import logging
import time
import urllib.error
from datetime import UTC, datetime
from pathlib import Path

import pytest
from websockets.exceptions import ConnectionClosedError
from websockets.frames import Close

from agentcall.cloud_control import CloudControlApi, CloudEdgeClient
from agentcall.cloud_credentials import (
    CloudCredentialStore,
    EdgeCredential,
    parse_edge_credential,
)
from agentcall.content_sync import ContentSyncError
from agentcall.takeover_coordinator import (
    InboundTakeoverOfferRequest,
    InboundTakeoverRevoke,
    TakeoverRejection,
    TakeoverResult,
)


class _Service:
    modem_connected = True

    def __init__(self) -> None:
        self.commands: list[dict] = []
        self.claims: list[dict] = []
        self.offers: list[InboundTakeoverOfferRequest] = []
        self.revokes: list[InboundTakeoverRevoke] = []

    def remote_dialer_status(self) -> dict:
        return {"active": False}

    def line_busy(self) -> bool:
        return bool(self.remote_dialer_status().get("active"))

    def start_cloud_remote_session(self, command: dict) -> tuple[bool, str | None]:
        self.commands.append(command)
        return True, None

    def next_inbound_takeover_offer(
        self, timeout: float = 0.0
    ) -> InboundTakeoverOfferRequest | None:
        return self.offers.pop(0) if self.offers else None

    def next_inbound_takeover_revoke(
        self, timeout: float = 0.0
    ) -> InboundTakeoverRevoke | None:
        return self.revokes.pop(0) if self.revokes else None

    def accept_inbound_takeover_claim(self, **fields) -> TakeoverResult:
        self.claims.append(fields)
        return TakeoverResult.success()


class _Store:
    def __init__(self, credential: EdgeCredential | None = None) -> None:
        self.credential = credential

    def load(self) -> EdgeCredential | None:
        return self.credential

    def sign(self, _message: bytes) -> str:
        return "proof"


def test_v2_reservation_acknowledges_before_sdp_and_never_reclaims(monkeypatch):
    monkeypatch.setenv("INBOUND_FASTPATH_V2_ENABLED", "true")

    class Service(_Service):
        def mark_inbound_direct(self, command, stage):
            assert len(messages) == 1, "ACK must be sent before its timing mark"
            marked.append(stage)

        def supply_inbound_direct_sdp(self, command, reply):
            direct = self.claims[0]["issued"].direct
            return direct.supply_sdp(command["direct"]["sdpOffer"], command["direct"]["iceServers"], reply)

    service = Service()
    client = CloudEdgeClient("https://relay.example.test", service, _Store())
    messages = []
    marked = []
    reserve = _takeover_claim(direct={"deferred": True})
    client.handle_message(json.dumps(reserve), messages.append)
    assert len(service.claims) == len(messages) == 1
    assert marked == ["claim_ack_sent"]
    ack = json.loads(messages[0])
    assert ack["type"] == "inbound.answer" and ack["status"] == "accepted"
    assert "sdpAnswer" not in ack
    direct = service.claims[0]["issued"].direct
    assert direct.media_offer() is None
    submit = {**reserve, "type": "inbound.media.sdp", "commandId": "command_sdp_abcdefghijkl",
              "direct": {"sdpOffer": "v=0\r\n", "iceServers": [
                  {"urls": ["stun:stun.example.test:3478"]},
                  {"urls": ["turn:turn.example.test:3478"], "username": "test", "credential": "test"},
              ]}}
    client.handle_message(json.dumps(submit), messages.append)
    assert len(messages) == 1 and len(service.claims) == 1
    assert direct.media_offer()[0] == "v=0\r\n"
    direct.reply("v=0\r\nanswer", None)
    answer = json.loads(messages[-1])
    assert answer["commandId"] == submit["commandId"] and answer["sdpAnswer"] == "v=0\r\nanswer"


@pytest.mark.parametrize("enabled", [False, True])
def test_v2_turn_prepare_is_scoped_and_never_claims(monkeypatch, enabled):
    monkeypatch.setenv("INBOUND_FASTPATH_V2_ENABLED", str(enabled).lower())
    service = _Service()
    prepared = []
    warmed = []
    monkeypatch.setattr("agentcall.webrtc_media.warm_webrtc_stack", lambda: warmed.append(True), raising=False)
    monkeypatch.setattr(service, "prepare_inbound_direct", prepared.append, raising=False)
    client = CloudEdgeClient("https://relay.example.test", service, _Store())
    value = _takeover_claim()
    del value["claimId"], value["session"]
    value.update(type="inbound.prepare", mode="phone_first", expiresAtUnixMs=round(time.time() * 1000) + 5000,
                 direct={"iceServers": [{"urls": ["stun:stun.example.test:3478"]}]})
    sent = []
    client.handle_message(json.dumps(value), sent.append)
    assert len(prepared) == int(enabled)
    assert len(warmed) == int(enabled)
    assert not sent and not service.claims
    value["nonce"] = "bad"
    client.handle_message(json.dumps(value), sent.append)
    assert len(prepared) == int(enabled)


def test_reservation_send_failure_does_not_emit_ack_sent_mark(monkeypatch):
    monkeypatch.setenv("INBOUND_FASTPATH_V2_ENABLED", "true")
    service = _Service()
    marks = []
    monkeypatch.setattr(service, "mark_inbound_direct", lambda *_: marks.append(True), raising=False)
    client = CloudEdgeClient("https://relay.example.test", service, _Store())

    def broken_send(_):
        raise OSError("closed")

    client.handle_message(json.dumps(_takeover_claim(direct={"deferred": True})), broken_send)
    assert not marks


@pytest.mark.parametrize("enabled", [False, True])
def test_push_receipt_is_strict_and_diagnostic_only(monkeypatch, enabled):
    monkeypatch.setenv("INBOUND_PCM_PRE_ATA_PROBE_ENABLED", str(enabled).lower())
    service = _Service()
    pushed = []
    monkeypatch.setattr(service, "inbound_offer_pushed", pushed.append, raising=False)
    client = CloudEdgeClient("https://relay.example.test", service, _Store())
    claim = _takeover_claim()
    receipt = {key: claim[key] for key in ("v", "offerId", "callId", "generation", "nonce")}
    receipt["type"] = "inbound.offer.pushed"
    client.handle_message(json.dumps(receipt), lambda *_: pytest.fail("Receipt must not ACK"))
    assert len(pushed) == int(enabled)
    client.handle_message(json.dumps({**receipt, "nonce": "bad"}), lambda *_: None)
    assert len(pushed) == int(enabled)


@pytest.mark.parametrize("direct", [{"deferred": False}, {"deferred": "true"}, {"deferred": True, "sdpOffer": "v=0"}])
def test_v2_malformed_reservation_never_reaches_service(monkeypatch, direct):
    monkeypatch.setenv("INBOUND_FASTPATH_V2_ENABLED", "true")
    service = _Service()
    client = CloudEdgeClient("https://relay.example.test", service, _Store())
    sent = []
    client.handle_message(json.dumps(_takeover_claim(direct=direct)), sent.append)
    assert not service.claims and not sent


@pytest.mark.parametrize("enabled, accepted", [(False, True), (True, False), (True, True)])
def test_v2_control_plane_end_has_correlated_reply_without_reclaim(monkeypatch, enabled, accepted):
    monkeypatch.setenv("INBOUND_FASTPATH_V2_ENABLED", str(enabled).lower())
    service = _Service()
    ended = []

    def end(command):
        ended.append(command)
        return accepted

    monkeypatch.setattr(service, "end_inbound_direct", end, raising=False)
    client = CloudEdgeClient("https://relay.example.test", service, _Store())
    sent = []
    command = _takeover_claim(type="inbound.media.end")
    client.handle_message(json.dumps(command), sent.append)
    assert len(ended) == int(enabled) and not service.claims
    reply = json.loads(sent[0])
    assert reply["type"] == "inbound.answer" and reply["commandId"] == command["commandId"]
    assert reply["status"] == ("accepted" if enabled and accepted else "rejected")


class _ContentRepository:
    def __init__(self, body: dict | None = None, error: str | None = None) -> None:
        self.body = body or {
            "v": 1,
            "items": [],
            "nextCursor": None,
            "hasMore": False,
            "collectionRevision": "revision_content_abcdefghijkl",
            "oldestAvailableAt": None,
        }
        self.error = error
        self.reads: list[tuple[str, dict]] = []

    def read(self, resource: str, params: dict) -> dict:
        self.reads.append((resource, dict(params)))
        if self.error:
            raise ContentSyncError(self.error)
        return self.body


def _command(**overrides) -> dict:
    expires = datetime.fromtimestamp(time.time() + 300, UTC).isoformat().replace(
        "+00:00", "Z"
    )
    value = {
        "v": 1,
        "type": "session.start",
        "commandId": "command_abcdefghijkl",
        "callId": "call_abcdefghijkl",
        "expiresAt": expires,
        "session": {
            "sessionId": "session_abcdefghijkl",
            "roomName": "callpilot_abcdefghijkl",
            "browserIdentity": "web_abcdefghijkl",
            "edgeIdentity": "edgepart_abcdefghijkl",
            "livekitUrl": "wss://project.livekit.cloud",
            "token": "x" * 80,
        },
    }
    value.update(overrides)
    return value


def _takeover_claim(**overrides) -> dict:
    value = {
        "v": 1,
        "type": "inbound.claim",
        "commandId": "command_takeover_abcdefghijkl",
        "offerId": "offer_takeover_abcdefghijkl",
        "callId": "call_takeover_abcdefghijkl",
        "claimId": "claim_takeover_abcdefghijkl",
        "generation": 7,
        "nonce": "takeover-nonce-abcdefghijkl",
        "session": {
            "sessionId": "session_takeover_abcdefghijkl",
            "roomName": "callpilot_takeover_abcdefghijkl",
            "browserIdentity": "web_takeover_abcdefghijkl",
            "edgeIdentity": "edgepart_takeover_abcdefghijkl",
            "livekitUrl": "wss://project.livekit.cloud",
            "token": "x" * 80,
        },
    }
    value.update(overrides)
    return value


def _data_request(**overrides) -> dict:
    now = round(time.time() * 1000)
    value = {
        "v": 1,
        "type": "data.request",
        "requestId": "request_content_abcdefghijkl",
        "deviceId": "device_content_abcdefghijkl",
        "resource": "messages.list",
        "params": {"limit": 25, "cursor": None},
        "issuedAtUnixMs": now,
        "expiresAtUnixMs": now + 5_000,
    }
    value.update(overrides)
    return value


def _content_client(
    repository: _ContentRepository,
    *,
    enabled: bool = True,
) -> CloudEdgeClient:
    return CloudEdgeClient(
        "https://api.bondings.ai",
        _Service(),
        _Store(),
        content_repository=repository,
        content_read_enabled=lambda: enabled,
    )


def test_edge_credential_parser_rejects_malformed_values() -> None:
    secret = "s" * 40
    credential = parse_edge_credential(f"edge_abcdefghijkl.{secret}")

    assert credential == EdgeCredential(
        edge_id="edge_abcdefghijkl", value=f"edge_abcdefghijkl.{secret}"
    )
    assert parse_edge_credential("edge_abcdefghijkl.short") is None
    assert parse_edge_credential("device_abcdefghijkl." + secret) is None


def test_keychain_store_keeps_credential_and_private_key_outside_files(monkeypatch) -> None:
    import keyring

    values: dict[tuple[str, str], str] = {}
    monkeypatch.setattr(
        keyring, "get_password", lambda service, account: values.get((service, account))
    )
    monkeypatch.setattr(
        keyring,
        "set_password",
        lambda service, account, value: values.__setitem__((service, account), value),
    )
    store = CloudCredentialStore()
    raw = "edge_abcdefghijkl." + "s" * 40

    assert store.save(raw).value == raw
    assert store.load() == EdgeCredential("edge_abcdefghijkl", raw)
    first_public = store.load_or_create_public_key()
    second_public = store.load_or_create_public_key()
    signature = store.sign(b"device proof")

    assert first_public == second_public
    assert len(first_public) == 43
    assert raw in values.values()
    assert first_public not in values.values()
    assert len(signature) == 86


def test_cloud_client_accepts_only_complete_unexpired_session_commands() -> None:
    service = _Service()
    client = CloudEdgeClient(
        "https://api.bondings.ai", service, _Store(), heartbeat_seconds=15
    )
    sent: list[dict] = []

    client.handle_message(
        json.dumps(_command()), lambda value: sent.append(json.loads(value))
    )

    assert service.commands[0]["expiresAtUnix"] > time.time()
    assert sent == [
        {
            "v": 1,
            "type": "command.ack",
            "commandId": "command_abcdefghijkl",
            "callId": "call_abcdefghijkl",
            "status": "accepted",
        }
    ]

    expired = _command(expiresAt="2020-01-01T00:00:00Z")
    client.handle_message(json.dumps(expired), lambda value: sent.append(json.loads(value)))
    client.handle_message('{"type":"run.shell"}', lambda value: sent.append(json.loads(value)))
    assert len(service.commands) == 1
    assert len(sent) == 1


def test_cloud_ack_preserves_stable_edge_preflight_error_code() -> None:
    class _RejectingService(_Service):
        def start_cloud_remote_session(
            self, command: dict
        ) -> tuple[bool, str | None]:
            self.commands.append(command)
            return False, "SIM_NOT_REGISTERED"

    service = _RejectingService()
    client = CloudEdgeClient("https://api.bondings.ai", service, _Store())
    sent: list[dict] = []

    client.handle_message(
        json.dumps(_command()), lambda value: sent.append(json.loads(value))
    )

    assert sent[0]["status"] == "rejected"
    assert sent[0]["errorCode"] == "SIM_NOT_REGISTERED"


def test_edge_sends_opaque_takeover_offer_then_revoke_with_frozen_schema() -> None:
    service = _Service()
    service.offers.append(
        InboundTakeoverOfferRequest(
            offer_id="offer_takeover_abcdefghijkl",
            call_id="call_takeover_abcdefghijkl",
            generation=7,
            nonce="takeover-nonce-abcdefghijkl",
            created_at=100.0,
            expires_at=130.0,
        )
    )
    service.revokes.append(
        InboundTakeoverRevoke(
            offer_id="offer_takeover_abcdefghijkl",
            call_id="call_takeover_abcdefghijkl",
            reason="CALL_ENDED",
        )
    )
    client = CloudEdgeClient("https://api.bondings.ai", service, _Store())
    sent: list[dict] = []

    client._drain_takeover_events(lambda raw: sent.append(json.loads(raw)))

    assert sent == [
        {
            "v": 1,
            "type": "inbound.offer",
            "offerId": "offer_takeover_abcdefghijkl",
            "callId": "call_takeover_abcdefghijkl",
            "generation": 7,
            "nonce": "takeover-nonce-abcdefghijkl",
            "expiresAtUnixMs": 130000,
        },
        {
            "v": 1,
            "type": "inbound.offer.revoke",
            "offerId": "offer_takeover_abcdefghijkl",
            "callId": "call_takeover_abcdefghijkl",
            "reason": "CALL_ENDED",
        },
    ]
    assert "preference" not in repr(sent).lower()
    assert "number" not in repr(sent).lower()


def test_cloud_client_accepts_strict_inbound_claim_and_acks_offer() -> None:
    service = _Service()
    client = CloudEdgeClient("https://api.bondings.ai", service, _Store())
    sent: list[dict] = []

    client.handle_message(
        json.dumps(_takeover_claim()),
        lambda value: sent.append(json.loads(value)),
    )

    assert len(service.claims) == 1
    claim = service.claims[0]
    assert claim["offer_id"] == "offer_takeover_abcdefghijkl"
    assert claim["generation"] == 7
    assert claim["nonce"] == "takeover-nonce-abcdefghijkl"
    assert claim["issued"].browser_identity == "web_takeover_abcdefghijkl"
    assert claim["issued"].browser_token == ""
    assert sent == [
        {
            "v": 1,
            "type": "command.ack",
            "commandId": "command_takeover_abcdefghijkl",
            "callId": "call_takeover_abcdefghijkl",
            "offerId": "offer_takeover_abcdefghijkl",
            "status": "accepted",
        }
    ]


def test_silent_prepare_has_separate_dispatch_and_no_claim_ack(monkeypatch):
    service = _Service()
    prepared = []
    service.prepare_inbound_takeover = prepared.append
    client = CloudEdgeClient("https://cloud.example.test", service, _Store())
    command = _takeover_claim()
    command.pop("claimId")
    command.update(type="inbound.prepare", mode="phone_first", expiresAtUnixMs=round(time.time() * 1000) + 15000)
    sent = []
    monkeypatch.setenv("INBOUND_PRECONNECT_ENABLED", "false")
    client.handle_message(json.dumps(command), sent.append)
    assert not prepared and not service.claims and not sent
    assert "inboundPreconnectV1" not in json.loads(client._heartbeat())["status"]
    monkeypatch.setenv("INBOUND_PRECONNECT_ENABLED", "true")
    client.handle_message(json.dumps(command), sent.append)
    assert len(prepared) == 1 and not service.claims and not sent
    assert json.loads(client._heartbeat())["status"]["inboundPreconnectV1"] is True
    for mutation in ({"mode": "ai"}, {"expiresAtUnixMs": 0}, {"unexpected": True}):
        client.handle_message(json.dumps({**command, **mutation}), sent.append)
    assert len(prepared) == 1 and not sent


def test_inbound_claim_rejection_preserves_stable_fence_code() -> None:
    class _RejectingService(_Service):
        def accept_inbound_takeover_claim(self, **fields) -> TakeoverResult:
            self.claims.append(fields)
            return TakeoverResult.reject(TakeoverRejection.STALE_GENERATION)

    service = _RejectingService()
    client = CloudEdgeClient("https://api.bondings.ai", service, _Store())
    sent: list[dict] = []

    client.handle_message(
        json.dumps(_takeover_claim()),
        lambda value: sent.append(json.loads(value)),
    )

    assert sent[0]["status"] == "rejected"
    assert sent[0]["offerId"] == "offer_takeover_abcdefghijkl"
    assert sent[0]["errorCode"] == "STALE_GENERATION"


def test_content_request_reads_repository_and_returns_correlated_response() -> None:
    repository = _ContentRepository()
    client = _content_client(repository)
    sent: list[dict] = []

    client.handle_message(
        json.dumps(_data_request()),
        lambda raw: sent.append(json.loads(raw)),
    )

    assert repository.reads == [("messages.list", {"limit": 25, "cursor": None})]
    assert sent == [
        {
            "v": 1,
            "type": "data.response",
            "requestId": "request_content_abcdefghijkl",
            "resource": "messages.list",
            "status": "ok",
            "body": repository.body,
        }
    ]


def test_content_relay_preserves_shared_v1_fixture_contract() -> None:
    fixture_dir = (
        Path(__file__).resolve().parents[2] / "docs/fixtures/content-sync/v1"
    )
    request = json.loads((fixture_dir / "edge-data-request.json").read_text(encoding="utf-8"))
    repository = _ContentRepository(
        body=json.loads((fixture_dir / "messages-page.json").read_text(encoding="utf-8"))
    )
    now = round(time.time() * 1000)
    request.update(issuedAtUnixMs=now, expiresAtUnixMs=now + 5_000)
    sent: list[str] = []

    _content_client(repository).handle_message(json.dumps(request), sent.append)

    response = json.loads(sent[0])
    assert response["requestId"] == request["requestId"]
    assert response["resource"] == request["resource"]
    assert response["body"] == repository.body
    assert len(sent[0].encode("utf-8")) <= 16 * 1024


def test_content_request_double_gate_fails_before_repository_read() -> None:
    repository = _ContentRepository()
    sent: list[dict] = []

    _content_client(repository, enabled=False).handle_message(
        json.dumps(_data_request()),
        lambda raw: sent.append(json.loads(raw)),
    )

    assert repository.reads == []
    assert sent[0]["status"] == "error"
    assert sent[0]["error"] == {"code": "FEATURE_DISABLED"}


def test_content_request_replay_and_expiry_never_read_content() -> None:
    repository = _ContentRepository()
    client = _content_client(repository)
    sent: list[dict] = []
    request = _data_request()

    client.handle_message(json.dumps(request), lambda raw: sent.append(json.loads(raw)))
    client.handle_message(json.dumps(request), lambda raw: sent.append(json.loads(raw)))
    expired = _data_request(
        requestId="request_expired_abcdefghijkl",
        issuedAtUnixMs=1_000,
        expiresAtUnixMs=2_000,
    )
    client.handle_message(json.dumps(expired), lambda raw: sent.append(json.loads(raw)))

    assert len(repository.reads) == 1
    assert sent[1]["error"] == {"code": "INVALID_REQUEST"}
    assert all(item["requestId"] != "request_expired_abcdefghijkl" for item in sent)


@pytest.mark.parametrize(
    ("mutation", "params"),
    [
        ({"resource": "filesystem.read"}, None),
        ({"params": {"limit": True, "cursor": None}}, None),
        ({"params": {"limit": 25, "cursor": "not-a-cursor"}}, None),
        ({"expiresAtUnixMs": 0}, None),
        ({}, {"extra": "field"}),
    ],
)
def test_malformed_content_request_is_rejected_before_repository_read(
    mutation, params
) -> None:
    repository = _ContentRepository()
    sent: list[str] = []
    request = _data_request(**mutation)
    if params:
        request.update(params)

    _content_client(repository).handle_message(json.dumps(request), sent.append)

    assert repository.reads == []
    assert sent == []


def test_content_request_ttl_over_ten_seconds_is_rejected_before_read() -> None:
    repository = _ContentRepository()
    sent: list[str] = []
    request = _data_request()
    request["expiresAtUnixMs"] = request["issuedAtUnixMs"] + 10_001

    _content_client(repository).handle_message(json.dumps(request), sent.append)

    assert repository.reads == []
    assert sent == []


def test_content_repository_error_maps_to_stable_content_response() -> None:
    repository = _ContentRepository(error="CURSOR_INVALID")
    sent: list[dict] = []

    _content_client(repository).handle_message(
        json.dumps(_data_request()),
        lambda raw: sent.append(json.loads(raw)),
    )

    assert sent[0]["status"] == "error"
    assert sent[0]["error"] == {"code": "CURSOR_INVALID"}


def test_content_page_reduces_limit_until_full_envelope_fits() -> None:
    class _AdaptiveRepository(_ContentRepository):
        def read(self, resource: str, params: dict) -> dict:
            self.reads.append((resource, dict(params)))
            limit = params["limit"]
            return {
                "v": 1,
                "items": [
                    {
                        "messageId": f"msg_{index:012d}",
                        "revision": f"revision_{index:012d}",
                        "direction": "INBOUND",
                        "address": "+15550100101",
                        "text": "x" * 9_000,
                        "occurredAt": index,
                        "recordedAt": index,
                        "status": "RECEIVED",
                    }
                    for index in range(limit)
                ],
                "nextCursor": "cursor_content_abcdefghijkl" if limit == 1 else None,
                "hasMore": limit == 1,
                "collectionRevision": "revision_content_abcdefghijkl",
                "oldestAvailableAt": 0,
            }

    repository = _AdaptiveRepository()
    sent_raw: list[str] = []
    request = _data_request(params={"limit": 2, "cursor": None})

    _content_client(repository).handle_message(json.dumps(request), sent_raw.append)

    assert [params["limit"] for _, params in repository.reads] == [2, 1]
    assert len(sent_raw[0].encode("utf-8")) <= 16 * 1024
    response = json.loads(sent_raw[0])
    assert len(response["body"]["items"]) == 1
    assert response["body"]["hasMore"] is True


def test_content_single_item_oversize_is_413_code_without_body_log(
    caplog,
) -> None:
    body = {
        "v": 1,
        "items": [{"text": "private sentinel " + "界" * 6_000}],
        "nextCursor": None,
        "hasMore": False,
        "collectionRevision": "revision_content_abcdefghijkl",
        "oldestAvailableAt": 0,
    }
    repository = _ContentRepository(body=body)
    sent: list[dict] = []

    with caplog.at_level(logging.WARNING):
        _content_client(repository).handle_message(
            json.dumps(_data_request(params={"limit": 1, "cursor": None})),
            lambda raw: sent.append(json.loads(raw)),
        )

    assert sent[0]["status"] == "error"
    assert sent[0]["error"] == {"code": "PAYLOAD_TOO_LARGE"}
    assert "private sentinel" not in caplog.text


@pytest.mark.parametrize(
    "mutation",
    [
        {"nonce": "short"},
        {"generation": -1},
        {"generation": True},
        {"offerId": "call_wrong_prefix_abcdefghijkl"},
        {"extra": "field"},
    ],
)
def test_inbound_claim_parser_rejects_malformed_or_mutated_contract(mutation) -> None:
    service = _Service()
    client = CloudEdgeClient("https://api.bondings.ai", service, _Store())
    sent: list[str] = []
    command = _takeover_claim()
    command.update(mutation)

    client.handle_message(json.dumps(command), sent.append)

    assert service.claims == []
    assert sent == []


def test_heartbeat_tracks_live_modem_connection_state() -> None:
    service = _Service()
    client = CloudEdgeClient("https://api.bondings.ai", service, _Store())

    assert json.loads(client._heartbeat())["status"]["modemOnline"] is True
    service.modem_connected = False
    assert json.loads(client._heartbeat())["status"]["modemOnline"] is False


def test_heartbeat_reports_local_agent_call_as_line_busy() -> None:
    class _LocallyBusyService(_Service):
        def line_busy(self) -> bool:
            return True

    service = _LocallyBusyService()
    assert service.remote_dialer_status()["active"] is False
    client = CloudEdgeClient("https://api.bondings.ai", service, _Store())

    assert json.loads(client._heartbeat())["status"]["lineBusy"] is True


def test_cloud_api_never_includes_bearer_in_url_or_error(monkeypatch) -> None:
    captured = []
    credential = EdgeCredential(
        "edge_abcdefghijkl", "edge_abcdefghijkl." + "s" * 40
    )

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit):
            return b'{"devices":[]}'

    def fake_urlopen(request, timeout):
        captured.append((request, timeout))
        return _Response()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    result = CloudControlApi(
        "https://api.bondings.ai", sign=lambda _message: "proof"
    ).list_devices(credential)

    request, _timeout = captured[0]
    assert result == {"devices": []}
    assert credential.value not in request.full_url
    assert request.headers["Authorization"] == f"Bearer {credential.value}"
    assert request.headers["X-callpilot-signature"] == "proof"
    assert request.headers["User-agent"] == "CallPilot-Edge/1"


def test_cloud_api_preserves_standard_pairing_wire_contract(monkeypatch) -> None:
    captured = []
    credential = EdgeCredential(
        "edge_abcdefghijkl", "edge_abcdefghijkl." + "s" * 40
    )

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit):
            return b'{"pairingId":"pairing_abcdefghijkl","code":"ABCD-EFGH"}'

    def fake_urlopen(request, timeout):
        captured.append((request, timeout))
        return _Response()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    CloudControlApi(
        "https://api.bondings.ai", sign=lambda _message: "proof"
    ).create_pairing(credential)

    request, _timeout = captured[0]
    assert json.loads(request.data) == {"ttlSeconds": 300}


def test_cloud_api_creates_bounded_app_review_pairing(monkeypatch) -> None:
    captured = []
    credential = EdgeCredential(
        "edge_abcdefghijkl", "edge_abcdefghijkl." + "s" * 40
    )

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit):
            return b'{"pairingId":"pairing_abcdefghijkl","code":"ABCD-EFGH"}'

    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda request, timeout: captured.append((request, timeout)) or _Response(),
    )
    api = CloudControlApi(
        "https://api.bondings.ai", sign=lambda _message: "proof"
    )

    api.create_pairing(
        credential,
        ttl_seconds=7 * 24 * 60 * 60,
        purpose="app_review",
    )

    request, _timeout = captured[0]
    assert json.loads(request.data) == {
        "ttlSeconds": 604800,
        "purpose": "app_review",
    }
    with pytest.raises(ValueError, match="ttl_seconds"):
        api.create_pairing(
            credential,
            ttl_seconds=604801,
            purpose="app_review",
        )
    with pytest.raises(ValueError, match="ttl_seconds"):
        api.create_pairing(credential, ttl_seconds=300.5)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="ttl_seconds"):
        api.create_pairing(credential, ttl_seconds=True)
    with pytest.raises(ValueError, match="purpose"):
        api.create_pairing(credential, purpose="unexpected")


@pytest.mark.parametrize(
    "change, expected_times, expected_states",
    [
        ("idle", [100.0, 101.0, 116.0], [(True, True), (True, False), (True, False)]),
        ("unchanged", [100.0, 115.0], [(True, True), (True, True)]),
        ("offline", [100.0, 101.0, 116.0], [(True, True), (False, True), (False, True)]),
        ("both", [100.0, 101.0, 116.0], [(True, True), (False, False), (False, False)]),
        ("error", [100.0, 102.0, 117.0], [(True, True), (True, False), (True, False)]),
        ("initial_error", [101.0, 116.0], [(True, False), (True, False)]),
    ],
)
def test_cloud_websocket_reports_presence_changes_without_waiting_for_heartbeat(
    monkeypatch, change, expected_times, expected_states,
) -> None:
    credential = EdgeCredential("edge_abcdefghijkl", "edge_abcdefghijkl." + "s" * 40)
    clock = [100.0]
    sent = []
    monkeypatch.setattr("agentcall.cloud_control.time.monotonic", lambda: clock[0])

    class Service(_Service):
        busy = True

        def line_busy(self):
            if (change == "error" and clock[0] == 101.0
                    or change == "initial_error" and clock[0] == 100.0):
                raise RuntimeError("temporary status failure")
            return self.busy

    service = Service()

    class Socket:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def send(self, message):
            sent.append((clock[0], json.loads(message)))

        def recv(self, *, timeout):
            assert timeout == 1.0
            clock[0] += timeout
            if change in {"idle", "both", "error", "initial_error"}:
                service.busy = False
            if change in {"offline", "both"}:
                service.modem_connected = False
            if clock[0] >= 118.0:
                client._stop_event.set()
            raise TimeoutError(timeout)

    client = CloudEdgeClient(
        "https://relay.example.test", service, _Store(credential),
        connect=lambda *_args, **_kwargs: Socket(), heartbeat_seconds=15,
    )
    client._run_connection(credential)

    assert [at for at, _ in sent] == expected_times
    assert [(message["status"]["modemOnline"], message["status"]["lineBusy"])
            for _, message in sent] == expected_states
    for _, message in sent:
        assert set(message) == {"v", "type", "occurredAt", "status"}
        assert message["v"] == 1 and message["type"] == "heartbeat"


@pytest.mark.parametrize("failure_start", [100.0, 101.0])
def test_cloud_heartbeat_sampling_failure_expires_and_propagates(monkeypatch, caplog, failure_start):
    credential = EdgeCredential("edge_abcdefghijkl", "edge_abcdefghijkl." + "s" * 40)
    clock = [100.0]
    samples = []
    sent = []
    closed = []
    monkeypatch.setattr("agentcall.cloud_control.time.monotonic", lambda: clock[0])

    class Service(_Service):
        def line_busy(self):
            samples.append(clock[0])
            if clock[0] >= failure_start:
                raise RuntimeError("private detail")
            return False

    class Socket:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, *_args):
            closed.append(exc_type)
            return False

        def send(self, message):
            sent.append(clock[0])

        def recv(self, *, timeout):
            clock[0] += timeout
            if clock[0] >= 120.0:
                client._stop_event.set()
            raise TimeoutError(timeout)

    client = CloudEdgeClient(
        "https://relay.example.test", Service(), _Store(credential),
        connect=lambda *_args, **_kwargs: Socket(), heartbeat_seconds=15,
    )
    with caplog.at_level(logging.INFO), pytest.raises(RuntimeError, match="private detail"):
        client._run_connection(credential)

    assert clock[0] == failure_start + 16.0
    assert failure_start + 15.0 in samples
    assert closed == [RuntimeError]
    assert sent == ([] if failure_start == 100.0 else [100.0])
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "RuntimeError" in warnings[0].message
    assert warnings[0].exc_info is None
    assert "private detail" not in caplog.text
    assert "心跳状态采样已恢复" not in caplog.text


def test_cloud_heartbeat_sampling_logs_once_per_failure_streak_and_recovery(monkeypatch, caplog):
    credential = EdgeCredential("edge_abcdefghijkl", "edge_abcdefghijkl." + "s" * 40)
    clock = [100.0]
    monkeypatch.setattr("agentcall.cloud_control.time.monotonic", lambda: clock[0])

    class Service(_Service):
        def line_busy(self):
            if 101.0 <= clock[0] < 104.0 or 106.0 <= clock[0] < 108.0:
                raise ValueError("private detail")
            return False

    class Socket:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def send(self, message):
            pass

        def recv(self, *, timeout):
            clock[0] += timeout
            if clock[0] >= 110.0:
                client._stop_event.set()
            raise TimeoutError(timeout)

    client = CloudEdgeClient(
        "https://relay.example.test", Service(), _Store(credential),
        connect=lambda *_args, **_kwargs: Socket(), heartbeat_seconds=15,
    )
    with caplog.at_level(logging.INFO):
        client._run_connection(credential)

    records = [r for r in caplog.records if "心跳状态采样" in r.message]
    assert [r.levelno for r in records] == [logging.WARNING, logging.INFO] * 2
    for warning, recovery in zip(records[::2], records[1::2], strict=True):
        assert "ValueError" in warning.message
        assert warning.exc_info is None
        assert recovery.message == "心跳状态采样已恢复"
    assert "private detail" not in caplog.text


def test_cloud_websocket_uses_product_user_agent() -> None:
    credential = EdgeCredential(
        "edge_abcdefghijkl", "edge_abcdefghijkl." + "s" * 40
    )

    class _WebSocket:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def send(self, _message):
            client._stop_event.set()

        def recv(self, *, timeout):
            raise TimeoutError(timeout)

    captured = {}

    def connect(_url, **kwargs):
        captured.update(kwargs)
        return _WebSocket()

    client = CloudEdgeClient(
        "https://api.bondings.ai",
        _Service(),
        _Store(credential),
        connect=connect,
    )
    client._run_connection(credential)

    assert captured["user_agent_header"] == "CallPilot-Edge/1"
    assert captured["ping_interval"] == 20.0
    assert captured["ping_timeout"] == 20.0


@pytest.mark.parametrize(
    "rcvd,sent,order,code,keepalive",
    [
        (None, Close(1011, "keepalive ping timeout"), None, 1011, True),
        (Close(4001, "Replaced"), Close(4001, "Replaced"), True, 4001, False),
        (Close(1011, "peer"), Close(1011, "keepalive ping timeout"), False, 1011, True),
        (None, None, None, 1006, False),
    ],
)
def test_cloud_disconnect_logs_close_frames(monkeypatch, caplog, rcvd, sent, order, code, keepalive):
    credential = EdgeCredential("edge_abcdefghijkl", "edge_abcdefghijkl." + "s" * 40)
    clock = [100.0]
    monkeypatch.setattr("agentcall.cloud_control.time.monotonic", lambda: clock[0])

    class Socket:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def send(self, _message):
            pass

        def recv(self, *, timeout):
            clock[0] = 145.0
            client._stop_event.set()
            raise ConnectionClosedError(rcvd, sent, order)

    client = CloudEdgeClient("https://relay.example.test", _Service(), _Store(credential),
                             connect=lambda *_args, **_kwargs: Socket())
    with caplog.at_level(logging.INFO):
        client._run()
    record = next(record for record in caplog.records if "连接中断" in record.message)
    fields = json.loads(record.message.split(": ", 1)[1])
    assert fields == {
        "error_type": "ConnectionClosedError", "code": code,
        "rcvd": None if rcvd is None else {"code": rcvd.code, "reason": rcvd.reason},
        "sent": None if sent is None else {"code": sent.code, "reason": sent.reason},
        "rcvd_then": order, "alive_s": 45.0, "since_disconnect_s": None,
        "keepalive_timeout": keepalive,
    }
    assert client.status().last_error == f"ConnectionClosedError:{code}"


def test_cloud_reconnect_logs_attempts_and_elapsed_without_changing_backoff(monkeypatch, caplog):
    credential = EdgeCredential("edge_abcdefghijkl", "edge_abcdefghijkl." + "s" * 40)
    clock = [100.0]
    attempts = [0]
    waits = []
    monkeypatch.setattr("agentcall.cloud_control.time.monotonic", lambda: clock[0])
    monkeypatch.setattr("agentcall.cloud_control.random.random", lambda: 0.0)

    class Socket:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def send(self, _message):
            pass

        def recv(self, *, timeout):
            clock[0] += 10.0
            if attempts[0] == 3:
                client._stop_event.set()
            raise ConnectionClosedError(None, None)

    def connect(*_args, **_kwargs):
        attempts[0] += 1
        if attempts[0] == 2:
            clock[0] += 4.0
            raise OSError("handshake failed")
        return Socket()

    def wait(delay):
        waits.append(delay)
        clock[0] += delay

    client = CloudEdgeClient("https://relay.example.test", _Service(), _Store(credential), connect=connect)
    monkeypatch.setattr(client._stop_event, "wait", wait)
    with caplog.at_level(logging.INFO):
        client._run()
    connected = [json.loads(r.message.split(": ", 1)[1]) for r in caplog.records if "已连接" in r.message]
    assert connected == [{"reconnect_s": 0.0, "attempt": 1}, {"reconnect_s": 7.0, "attempt": 2}]
    disconnected = [json.loads(r.message.split(": ", 1)[1]) for r in caplog.records if "连接中断" in r.message]
    assert disconnected[1]["alive_s"] is None
    assert disconnected[1]["since_disconnect_s"] == 5.0
    assert disconnected[2]["alive_s"] == 10.0
    assert disconnected[2]["since_disconnect_s"] == 17.0
    assert waits == [1.0, 2.0, 4.0]


def test_cloud_websocket_drains_content_free_alerts(monkeypatch) -> None:
    from agentcall import remote_alert_push

    monkeypatch.setenv("REMOTE_ALERT_PUSH_ENABLED", "true")
    monkeypatch.setattr(remote_alert_push, "queue", remote_alert_push.AlertQueue())
    remote_alert_push.publish("sms")
    sent = []
    credential = EdgeCredential("edge_abcdefghijkl", "edge_abcdefghijkl." + "s" * 40)

    class Socket:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def send(self, message):
            sent.append(json.loads(message))

        def recv(self, *, timeout):
            client._stop_event.set()
            raise TimeoutError(timeout)

    client = CloudEdgeClient("https://relay.example.test", _Service(), _Store(credential),
                             connect=lambda *_args, **_kwargs: Socket())
    client._run_connection(credential)
    assert [message["type"] for message in sent] == ["heartbeat", "device.notify"]
    assert set(sent[1]) == {"v", "type", "kind", "eventId"}


@pytest.mark.parametrize(
    "url",
    [
        "http://api.bondings.ai",
        "https://user:pass@api.bondings.ai",
        "https://api.bondings.ai/?token=secret",
    ],
)
def test_cloud_api_requires_plain_https_origin(url: str) -> None:
    with pytest.raises(ValueError):
        CloudControlApi(url)


def test_cloud_api_redacts_structured_http_failure(monkeypatch) -> None:
    response = b'{"error":{"code":"EDGE_REVOKED","message":"sensitive detail"}}'
    error = urllib.error.HTTPError(
        "https://api.bondings.ai/v1/test", 401, "Unauthorized", {}, None
    )
    error.read = lambda _limit: response  # type: ignore[method-assign]
    monkeypatch.setattr("urllib.request.urlopen", lambda *_args, **_kwargs: (_ for _ in ()).throw(error))

    with pytest.raises(RuntimeError, match="EDGE_REVOKED") as caught:
        CloudControlApi("https://api.bondings.ai")._request("GET", "/v1/test")
    assert "sensitive detail" not in str(caught.value)


@pytest.mark.parametrize("enabled", [False, True])
def test_http_timing_is_dispatched_before_end_only_for_v2(monkeypatch, enabled):
    monkeypatch.setenv("INBOUND_FASTPATH_V2_ENABLED", str(enabled).lower())
    service = _Service()
    order = []
    service.receive_inbound_client_timing = lambda command: order.append(("timing", command["clientTiming"]))
    service.end_inbound_direct = lambda _command: order.append(("end", None)) or True
    batch = {"type": "client_timing", "v": 1, "attempt": "00000000-0000-4000-8000-000000000001",
             "seq": 1, "marks": {"audio_engine_failed": 200}}
    client = CloudEdgeClient("https://relay.example.test", service, _Store())
    client.handle_message(json.dumps(_takeover_claim(type="inbound.media.end", clientTiming=[batch])), lambda _: None)
    assert order == ([("timing", [batch]), ("end", None)] if enabled else [])


@pytest.mark.parametrize("batches", [None, {}, [{}], [None] * 9,
    [{"type": "client_timing", "v": 1, "attempt": "00000000-0000-4000-8000-000000000001",
      "seq": 1, "marks": {"device_name": 1}}],
    [{"type": "client_timing", "v": 1, "attempt": "00000000-0000-4000-8000-000000000001",
      "seq": 1, "marks": {"audio_engine_failed": True}}],
])
def test_invalid_http_timing_rejects_entire_control_command(batches):
    from agentcall.cloud_control import _parse_cloud_command

    with pytest.raises(ValueError, match="client timing"):
        _parse_cloud_command(json.dumps(_takeover_claim(type="inbound.media.end", clientTiming=batches)))


def test_http_timing_cannot_be_smuggled_onto_claim():
    from agentcall.cloud_control import _parse_cloud_command

    with pytest.raises(ValueError):
        _parse_cloud_command(json.dumps(_takeover_claim(clientTiming=[])))


@pytest.mark.parametrize("enabled", [False, True])
def test_http_rtp_dispatches_without_timing_before_end(monkeypatch, enabled):
    monkeypatch.setenv("INBOUND_FASTPATH_V2_ENABLED", str(enabled).lower())
    service = _Service()
    order = []
    stats = {"type": "client_rtp_stats", "v": 1, "seq": 1, "elapsedMs": 0,
             "outPackets": 0, "outBytes": 0, "inPackets": None, "inBytes": None,
             "lost": None, "rttMs": None, "localCandidate": None, "remoteCandidate": None, "pairState": None}
    service.receive_inbound_client_timing = lambda command: order.append(("rtp", command["clientRtpStats"]))
    service.end_inbound_direct = lambda _command: order.append(("end", None)) or True
    client = CloudEdgeClient("https://relay.example.test", service, _Store())
    client.handle_message(json.dumps(_takeover_claim(type="inbound.media.end", clientRtpStats=stats)), lambda _: None)
    assert order == ([("rtp", stats), ("end", None)] if enabled else [])


def test_invalid_http_rtp_rejected_and_not_allowed_on_claim():
    from agentcall.cloud_control import _parse_cloud_command

    with pytest.raises(ValueError, match="client RTP"):
        _parse_cloud_command(json.dumps(_takeover_claim(type="inbound.media.end", clientRtpStats={})))
    with pytest.raises(ValueError):
        _parse_cloud_command(json.dumps(_takeover_claim(clientRtpStats={})))


def test_cloud_reconnect_resets_backoff_after_a_healthy_connection_and_caps_it(monkeypatch):
    credential = EdgeCredential("edge_abcdefghijkl", "edge_abcdefghijkl." + "s" * 40)
    clock = [100.0]
    attempts = [0]
    waits = []
    monkeypatch.setattr("agentcall.cloud_control.time.monotonic", lambda: clock[0])
    monkeypatch.setattr("agentcall.cloud_control.random.random", lambda: 0.0)

    class Socket:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def send(self, _message):
            pass

        def recv(self, *, timeout):
            # The 6th attempt connects and stays up for 2 minutes before dropping.
            clock[0] += 120.0
            raise ConnectionClosedError(None, None)

    def connect(*_args, **_kwargs):
        attempts[0] += 1
        if attempts[0] == 7:
            client._stop_event.set()
        if attempts[0] < 6:
            raise OSError("network down")
        return Socket()

    def wait(delay):
        waits.append(delay)
        clock[0] += delay

    client = CloudEdgeClient("https://relay.example.test", _Service(), _Store(credential), connect=connect)
    monkeypatch.setattr(client._stop_event, "wait", wait)
    client._run()
    # Failures double up to the 10 s cap; a drop after a healthy connection retries after 1 s.
    assert waits[:5] == [1.0, 2.0, 4.0, 8.0, 10.0]
    assert waits[5] == 1.0


_DIRECT_OFFER = {"sdpOffer": "v=0\r\ns=phone\r\n", "iceServers": [
    {"urls": ["turn:turn.example.test:3478"], "username": "test", "credential": "test"}]}


class _OutboundService(_Service):
    def __init__(self, *, answer: tuple[str | None, str | None] | None = ("v=0\r\nanswer", None),
                 result: tuple[bool, str | None] = (True, None)) -> None:
        super().__init__()
        self.answer, self.result = answer, result
        self.fallbacks: list[tuple[str, object]] = []

    def start_cloud_remote_session(self, command, reply=None):
        self.commands.append(command)
        if self.answer is not None and reply is not None:
            reply(*self.answer)
        return self.result

    def fallback_outbound_direct(self, call_id, issued, reply):
        self.fallbacks.append((call_id, issued))
        reply(None, None)
        return True


def _fallback_command(**overrides) -> dict:
    value = {key: _command()[key] for key in ("v", "commandId", "callId", "session")}
    value.update(type="call.media.livekit", commandId="command_fallbackabcdef", **overrides)
    return value


def test_outbound_direct_start_answers_once_with_sdp_and_no_ack(monkeypatch) -> None:
    monkeypatch.setenv("OUTBOUND_DIRECT_ENABLED", "true")
    service = _OutboundService()
    client = CloudEdgeClient("https://relay.example.test", service, _Store())
    sent: list[dict] = []
    client.handle_message(json.dumps(_command(direct=_DIRECT_OFFER)), lambda v: sent.append(json.loads(v)))
    assert service.commands[0]["direct"] == _DIRECT_OFFER
    assert sent == [{"v": 1, "type": "call.answer", "commandId": "command_abcdefghijkl",
                     "callId": "call_abcdefghijkl", "status": "accepted", "sdpAnswer": "v=0\r\nanswer"}]


def test_outbound_direct_rejection_is_a_single_correlated_answer(monkeypatch) -> None:
    monkeypatch.setenv("OUTBOUND_DIRECT_ENABLED", "true")
    service = _OutboundService(answer=(None, "MEDIA_ENDED"), result=(False, "MEDIA_FAILED"))
    client = CloudEdgeClient("https://relay.example.test", service, _Store())
    sent: list[dict] = []
    client.handle_message(json.dumps(_command(direct=_DIRECT_OFFER)), lambda v: sent.append(json.loads(v)))
    assert [(m["type"], m["status"], m["errorCode"]) for m in sent] == [("call.answer", "rejected", "MEDIA_ENDED")]

    service = _OutboundService(answer=None, result=(False, "LINE_BUSY"))
    client = CloudEdgeClient("https://relay.example.test", service, _Store())
    sent.clear()
    client.handle_message(json.dumps(_command(direct=_DIRECT_OFFER)), lambda v: sent.append(json.loads(v)))
    assert [(m["type"], m["errorCode"]) for m in sent] == [("call.answer", "LINE_BUSY")]


def test_outbound_direct_disabled_never_starts_a_session(monkeypatch) -> None:
    monkeypatch.setenv("OUTBOUND_DIRECT_ENABLED", "false")
    service = _OutboundService()
    client = CloudEdgeClient("https://relay.example.test", service, _Store())
    sent: list[dict] = []
    for message in (_command(direct=_DIRECT_OFFER), _fallback_command()):
        client.handle_message(json.dumps(message), lambda v: sent.append(json.loads(v)))
    assert service.commands == [] and service.fallbacks == []
    assert [(m["type"], m["errorCode"]) for m in sent] == [("call.answer", "DIRECT_DISABLED")] * 2


def test_outbound_fallback_passes_the_server_room_to_the_service(monkeypatch) -> None:
    monkeypatch.setenv("OUTBOUND_DIRECT_ENABLED", "true")
    service = _OutboundService()
    client = CloudEdgeClient("https://relay.example.test", service, _Store())
    sent: list[dict] = []
    client.handle_message(json.dumps(_fallback_command()), lambda v: sent.append(json.loads(v)))
    call_id, issued = service.fallbacks[0]
    assert call_id == "call_abcdefghijkl"
    assert issued.room_name == "callpilot_abcdefghijkl" and issued.edge_token == "x" * 80
    assert sent == [{"v": 1, "type": "call.answer", "commandId": "command_fallbackabcdef",
                     "callId": "call_abcdefghijkl", "status": "accepted"}]


@pytest.mark.parametrize("message", [
    _command(direct={"sdpOffer": "not-sdp", "iceServers": []}),
    _command(direct={"sdpOffer": "v=0\r\n"}),
    _command(direct={**_DIRECT_OFFER, "extra": True}),
    {**_fallback_command(), "direct": _DIRECT_OFFER},
    {**_fallback_command(), "callId": "nope"},
])
def test_outbound_direct_parser_rejects_malformed_commands(monkeypatch, message) -> None:
    monkeypatch.setenv("OUTBOUND_DIRECT_ENABLED", "true")
    service = _OutboundService()
    client = CloudEdgeClient("https://relay.example.test", service, _Store())
    sent: list[str] = []
    client.handle_message(json.dumps(message), sent.append)
    assert sent == [] and service.commands == [] and service.fallbacks == []


def test_heartbeat_advertises_outbound_direct_only_when_enabled(monkeypatch) -> None:
    client = CloudEdgeClient("https://relay.example.test", _Service(), _Store())
    monkeypatch.setenv("OUTBOUND_DIRECT_ENABLED", "false")
    assert "outboundDirectV1" not in json.loads(client._heartbeat())["status"]
    monkeypatch.setenv("OUTBOUND_DIRECT_ENABLED", "true")
    assert json.loads(client._heartbeat())["status"]["outboundDirectV1"] is True


def test_heartbeat_advertises_outbound_direct_by_default(monkeypatch) -> None:
    monkeypatch.delenv("OUTBOUND_DIRECT_ENABLED", raising=False)
    client = CloudEdgeClient("https://relay.example.test", _Service(), _Store())
    assert json.loads(client._heartbeat())["status"]["outboundDirectV1"] is True
