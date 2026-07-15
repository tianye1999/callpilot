"""Hosted control-plane credentials, strict commands, and HTTP client boundaries."""

from __future__ import annotations

import json
import time
import urllib.error
from datetime import UTC, datetime

import pytest

from agentcall.cloud_control import CloudControlApi, CloudEdgeClient
from agentcall.cloud_credentials import (
    CloudCredentialStore,
    EdgeCredential,
    parse_edge_credential,
)


class _Service:
    modem_connected = True

    def __init__(self) -> None:
        self.commands: list[dict] = []

    def remote_dialer_status(self) -> dict:
        return {"active": False}

    def start_cloud_remote_session(self, command: dict) -> tuple[bool, str | None]:
        self.commands.append(command)
        return True, None


class _Store:
    def __init__(self, credential: EdgeCredential | None = None) -> None:
        self.credential = credential

    def load(self) -> EdgeCredential | None:
        return self.credential

    def sign(self, _message: bytes) -> str:
        return "proof"


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
