"""Hosted control-plane enrollment API and outbound Edge WebSocket client."""

from __future__ import annotations

import json
import logging
import random
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from .cloud_credentials import CloudCredentialStore, EdgeCredential

logger = logging.getLogger(__name__)

_MAX_JSON_BYTES = 16 * 1024
_ID_RE = r"[a-z]+_[A-Za-z0-9_-]{12,80}"


class CloudSessionService(Protocol):
    modem_connected: bool

    def remote_dialer_status(self) -> dict[str, Any]: ...

    def start_cloud_remote_session(self, command: dict[str, Any]) -> tuple[bool, str | None]: ...


@dataclass(frozen=True)
class CloudControlStatus:
    enabled: bool
    enrolled: bool
    connected: bool
    edge_id: str | None
    last_error: str | None


class CloudControlApi:
    def __init__(self, base_url: str, *, timeout: float = 10.0) -> None:
        self.base_url = _validate_cloud_url(base_url)
        self.timeout = max(1.0, min(timeout, 30.0))

    def enroll(self, code: str, display_name: str, public_key: str) -> dict[str, Any]:
        return self._request(
            "POST",
            "/v1/edge-enrollments/claim",
            {"code": code, "displayName": display_name, "publicKey": public_key},
        )

    def create_pairing(self, credential: EdgeCredential) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/v1/edges/{credential.edge_id}/pairing-sessions",
            {"ttlSeconds": 300},
            credential=credential,
        )

    def list_devices(self, credential: EdgeCredential) -> dict[str, Any]:
        return self._request(
            "GET",
            f"/v1/edges/{credential.edge_id}/devices",
            credential=credential,
        )

    def revoke_device(self, credential: EdgeCredential, device_id: str) -> dict[str, Any]:
        if not _valid_id(device_id, "device"):
            raise ValueError("设备 ID 格式不合法")
        return self._request(
            "DELETE",
            f"/v1/devices/{device_id}",
            credential=credential,
        )

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        credential: EdgeCredential | None = None,
    ) -> dict[str, Any]:
        body = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if credential is not None:
            headers["Authorization"] = f"Bearer {credential.value}"
        request = urllib.request.Request(
            urllib.parse.urljoin(self.base_url + "/", path.lstrip("/")),
            data=body,
            headers=headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read(_MAX_JSON_BYTES + 1)
        except urllib.error.HTTPError as exc:
            raw = exc.read(_MAX_JSON_BYTES + 1)
            code = _cloud_error_code(raw) or f"HTTP_{exc.code}"
            raise RuntimeError(f"云端请求失败: {code}") from None
        except (OSError, urllib.error.URLError) as exc:
            raise RuntimeError("无法连接 CallPilot 云控制面") from exc
        if len(raw) > _MAX_JSON_BYTES:
            raise RuntimeError("云端响应过大")
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("云端响应不是合法 JSON") from exc
        if not isinstance(value, dict):
            raise RuntimeError("云端响应格式不合法")
        return value


class CloudEdgeClient:
    """Maintain one outbound WSS and dispatch bounded session commands."""

    def __init__(
        self,
        base_url: str,
        service: CloudSessionService,
        credential_store: CloudCredentialStore,
        *,
        connect: Callable[..., Any] | None = None,
        heartbeat_seconds: float = 15.0,
    ) -> None:
        self.base_url = _validate_cloud_url(base_url)
        self.service = service
        self.credential_store = credential_store
        self._connect = connect
        self._heartbeat_seconds = max(5.0, heartbeat_seconds)
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._connected = False
        self._last_error: str | None = None
        self._state_lock = threading.Lock()

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="callpilot-cloud-edge",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5.0)
        self._thread = None
        self._set_connected(False)

    def status(self) -> CloudControlStatus:
        credential = self.credential_store.load()
        with self._state_lock:
            return CloudControlStatus(
                enabled=True,
                enrolled=credential is not None,
                connected=self._connected,
                edge_id=credential.edge_id if credential else None,
                last_error=self._last_error,
            )

    def handle_message(self, raw: str, send: Callable[[str], None]) -> None:
        command_id = None
        call_id = None
        try:
            command = _parse_session_command(raw)
            command_id = command["commandId"]
            call_id = command["callId"]
            accepted, error = self.service.start_cloud_remote_session(command)
        except ValueError:
            logger.warning("忽略格式不合法的云端控制消息")
            return
        response: dict[str, Any] = {
            "v": 1,
            "type": "command.ack",
            "commandId": command_id,
            "callId": call_id,
            "status": "accepted" if accepted else "rejected",
        }
        if error:
            response["errorCode"] = error
        send(json.dumps(response, separators=(",", ":")))

    def _run(self) -> None:
        delay = 1.0
        while not self._stop_event.is_set():
            credential = self.credential_store.load()
            if credential is None:
                self._set_error("EDGE_NOT_ENROLLED")
                self._stop_event.wait(5.0)
                continue
            try:
                self._run_connection(credential)
                delay = 1.0
            except Exception as exc:  # network and SDK exceptions vary
                self._set_connected(False)
                self._set_error(type(exc).__name__)
                logger.warning(
                    "CallPilot 云控制面连接中断: error_type=%s", type(exc).__name__
                )
                self._stop_event.wait(delay + random.random() * 0.25 * delay)
                delay = min(delay * 2.0, 30.0)

    def _run_connection(self, credential: EdgeCredential) -> None:
        connect = self._connect
        if connect is None:
            from websockets.sync.client import connect as websocket_connect

            connect = websocket_connect
        websocket_url = _websocket_url(self.base_url)
        with connect(
            websocket_url,
            additional_headers={"Authorization": f"Bearer {credential.value}"},
            open_timeout=10.0,
            close_timeout=3.0,
        ) as websocket:
            self._set_connected(True)
            self._set_error(None)
            next_heartbeat = 0.0
            while not self._stop_event.is_set():
                now = time.monotonic()
                if now >= next_heartbeat:
                    websocket.send(self._heartbeat())
                    next_heartbeat = now + self._heartbeat_seconds
                try:
                    message = websocket.recv(timeout=1.0)
                except TimeoutError:
                    continue
                if isinstance(message, str):
                    self.handle_message(message, websocket.send)

    def _heartbeat(self) -> str:
        status = self.service.remote_dialer_status()
        payload = {
            "v": 1,
            "type": "heartbeat",
            "occurredAt": _utc_now(),
            "status": {
                "modemOnline": bool(self.service.modem_connected),
                "lineBusy": bool(status.get("active")),
            },
        }
        return json.dumps(payload, separators=(",", ":"))

    def _set_connected(self, value: bool) -> None:
        with self._state_lock:
            self._connected = value

    def _set_error(self, value: str | None) -> None:
        with self._state_lock:
            self._last_error = value


def _parse_session_command(raw: str) -> dict[str, Any]:
    if not isinstance(raw, str) or not raw or len(raw.encode("utf-8")) > _MAX_JSON_BYTES:
        raise ValueError("invalid command")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("invalid command") from exc
    if not isinstance(value, dict) or set(value) != {
        "v", "type", "commandId", "callId", "expiresAt", "session"
    }:
        raise ValueError("invalid command")
    if value.get("v") != 1 or value.get("type") != "session.start":
        raise ValueError("invalid command")
    if not _valid_id(value.get("commandId"), "command") or not _valid_id(
        value.get("callId"), "call"
    ):
        raise ValueError("invalid command")
    expires_at = _parse_timestamp(value.get("expiresAt"))
    if expires_at <= time.time() or expires_at > time.time() + 600:
        raise ValueError("invalid command")
    session = value.get("session")
    required = {
        "sessionId", "roomName", "browserIdentity", "edgeIdentity", "livekitUrl", "token"
    }
    if not isinstance(session, dict) or set(session) != required:
        raise ValueError("invalid command")
    if not _valid_id(session.get("sessionId"), "session"):
        raise ValueError("invalid command")
    if not _valid_id(session.get("roomName"), "callpilot"):
        raise ValueError("invalid command")
    if not _valid_id(session.get("browserIdentity"), "web"):
        raise ValueError("invalid command")
    if not _valid_id(session.get("edgeIdentity"), "edgepart"):
        raise ValueError("invalid command")
    livekit_url = session.get("livekitUrl")
    if not isinstance(livekit_url, str) or urllib.parse.urlparse(livekit_url).scheme != "wss":
        raise ValueError("invalid command")
    token = session.get("token")
    if not isinstance(token, str) or len(token) < 40 or len(token) > 8192:
        raise ValueError("invalid command")
    value["expiresAtUnix"] = expires_at
    return value


def _validate_cloud_url(value: str) -> str:
    parsed = urllib.parse.urlparse((value or "").strip())
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("云控制面地址必须是无凭证的 HTTPS URL")
    return urllib.parse.urlunparse(("https", parsed.netloc, parsed.path.rstrip("/"), "", "", ""))


def _websocket_url(base_url: str) -> str:
    parsed = urllib.parse.urlparse(base_url)
    path = f"{parsed.path.rstrip('/')}/v1/edges/connect"
    return urllib.parse.urlunparse(("wss", parsed.netloc, path, "", "", ""))


def _valid_id(value: Any, prefix: str) -> bool:
    import re

    return isinstance(value, str) and re.fullmatch(rf"{prefix}_[A-Za-z0-9_-]{{12,80}}", value) is not None


def _parse_timestamp(value: Any) -> float:
    from datetime import datetime

    if not isinstance(value, str):
        raise ValueError("invalid timestamp")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError as exc:
        raise ValueError("invalid timestamp") from exc


def _cloud_error_code(raw: bytes) -> str | None:
    try:
        value = json.loads(raw.decode("utf-8"))
        code = value.get("error", {}).get("code")
        return code if isinstance(code, str) else None
    except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
        return None


def _utc_now() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


__all__ = [
    "CloudControlApi",
    "CloudControlStatus",
    "CloudEdgeClient",
]
