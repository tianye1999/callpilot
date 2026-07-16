"""Hosted control-plane credentials, strict commands, and HTTP client boundaries."""

from __future__ import annotations

import json
import logging
import time
import urllib.error
from datetime import UTC, datetime
from pathlib import Path

import pytest

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
    request = json.loads((fixture_dir / "edge-data-request.json").read_text())
    repository = _ContentRepository(
        body=json.loads((fixture_dir / "messages-page.json").read_text())
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
