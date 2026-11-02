"""Hosted control-plane enrollment API and outbound Edge WebSocket client."""

from __future__ import annotations

import json
import logging
import random
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from . import config
from .cloud_credentials import CloudCredentialStore, EdgeCredential
from .content_sync import ContentSyncError
from .inbound_direct import InboundDirectRequest
from .remote_dialer import IssuedLiveKitSession, RemoteDialerInvite
from .takeover_coordinator import (
    InboundTakeoverOfferRequest,
    InboundTakeoverRevoke,
    TakeoverResult,
)

logger = logging.getLogger(__name__)

_MAX_JSON_BYTES = 16 * 1024
_ID_RE = r"[a-z]+_[A-Za-z0-9_-]{12,80}"
_USER_AGENT = "CallPilot-Edge/1"


class CloudSessionService(Protocol):
    modem_connected: bool

    def line_busy(self) -> bool: ...

    def start_cloud_remote_session(self, command: dict[str, Any]) -> tuple[bool, str | None]: ...

    def next_inbound_takeover_offer(
        self, timeout: float = 0.0
    ) -> InboundTakeoverOfferRequest | None: ...

    def next_inbound_takeover_revoke(
        self, timeout: float = 0.0
    ) -> InboundTakeoverRevoke | None: ...

    def accept_inbound_takeover_claim(
        self,
        *,
        offer_id: str,
        call_id: str,
        claim_id: str,
        generation: int,
        nonce: str,
        issued: IssuedLiveKitSession,
    ) -> TakeoverResult: ...


class CloudContentRepository(Protocol):
    def read(self, resource: str, params: dict[str, Any]) -> dict[str, Any]: ...


@dataclass(frozen=True)
class CloudControlStatus:
    enabled: bool
    enrolled: bool
    connected: bool
    edge_id: str | None
    last_error: str | None


class CloudControlApi:
    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 10.0,
        sign: Callable[[bytes], str] | None = None,
    ) -> None:
        self.base_url = _validate_cloud_url(base_url)
        self.timeout = max(1.0, min(timeout, 30.0))
        self._sign = sign

    def enroll(self, code: str, display_name: str, public_key: str) -> dict[str, Any]:
        return self._request(
            "POST",
            "/v1/edge-enrollments/claim",
            {"code": code, "displayName": display_name, "publicKey": public_key},
        )

    def create_pairing(
        self,
        credential: EdgeCredential,
        *,
        ttl_seconds: int = 300,
        purpose: str = "standard",
    ) -> dict[str, Any]:
        if purpose not in {"standard", "app_review"}:
            raise ValueError("purpose must be standard or app_review")
        max_seconds = 600 if purpose == "standard" else 7 * 24 * 60 * 60
        if not isinstance(ttl_seconds, int) or isinstance(ttl_seconds, bool):
            raise ValueError("ttl_seconds must be an integer")
        if not 60 <= ttl_seconds <= max_seconds:
            raise ValueError("ttl_seconds is outside the allowed range")
        payload: dict[str, Any] = {"ttlSeconds": ttl_seconds}
        if purpose == "app_review":
            payload["purpose"] = purpose
        return self._request(
            "POST",
            f"/v1/edges/{credential.edge_id}/pairing-sessions",
            payload,
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
        headers = {"Accept": "application/json", "User-Agent": _USER_AGENT}
        if payload is not None:
            body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if credential is not None:
            if self._sign is None:
                raise RuntimeError("云端设备签名器未配置")
            headers["Authorization"] = f"Bearer {credential.value}"
            _add_device_proof(
                headers,
                credential.edge_id,
                method,
                urllib.parse.urlparse(path).path,
                self._sign,
            )
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
            raise RuntimeError("无法连接 KinyAI-SIM助手 云控制面") from exc
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
        content_repository: CloudContentRepository | None = None,
        content_read_enabled: Callable[[], bool] | None = None,
    ) -> None:
        self.base_url = _validate_cloud_url(base_url)
        self.service = service
        self.credential_store = credential_store
        self._connect = connect
        self._heartbeat_seconds = max(5.0, heartbeat_seconds)
        self._content_repository = content_repository
        self._content_read_enabled = content_read_enabled or (lambda: False)
        self._seen_data_requests: dict[str, int] = {}
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

    def takeover_available(self) -> bool:
        """Cached only: a connected authenticated WSS implies an enrolled Edge."""
        with self._state_lock:
            return self._connected and not self._stop_event.is_set()

    def handle_message(self, raw: str, send: Callable[[str], None]) -> None:
        try:
            command = _parse_cloud_command(raw)
        except ValueError:
            logger.warning("忽略格式不合法的云端控制消息")
            return

        if command["type"] == "data.request":
            self._handle_data_request(command, send)
            return

        if command["type"] == "inbound.offer.pushed":
            pushed = getattr(self.service, "inbound_offer_pushed", None)
            if config.get_bool("INBOUND_PCM_PRE_ATA_PROBE_ENABLED") and callable(pushed):
                pushed(command)
            return

        if command["type"] == "inbound.prepare":
            if "direct" in command:
                prepare_direct = getattr(self.service, "prepare_inbound_direct", None)
                if config.get_bool("INBOUND_FASTPATH_V2_ENABLED") and callable(prepare_direct):
                    from .webrtc_media import warm_webrtc_stack

                    warm_webrtc_stack()
                    prepare_direct(command)
                return
            prepare = getattr(self.service, "prepare_inbound_takeover", None)
            if (not config.get_bool("INBOUND_FASTPATH_V2_ENABLED")
                    and config.get_bool("INBOUND_PRECONNECT_ENABLED") and callable(prepare)):
                prepare(command)
            # Prepare is not a claim ACK and cannot transition D1 to edge_ready.
            return

        command_id = command["commandId"]
        call_id = command["callId"]
        offer_id: str | None = None
        if command["type"] == "session.start":
            accepted, error = self.service.start_cloud_remote_session(command)
        else:
            offer_id = command["offerId"]
            session = command["session"]

            def reply(sdp: str | None, error_code: str | None) -> None:
                answer = {key: command[key] for key in ("commandId", "offerId", "callId", "claimId")}
                answer.update(v=1, type="inbound.answer", status="rejected" if error_code else "accepted")
                if sdp is not None:
                    answer["sdpAnswer"] = sdp
                if error_code:
                    answer["errorCode"] = error_code
                try:
                    send(json.dumps(answer, separators=(",", ":")))
                    if (command["type"] == "inbound.claim" and error_code is None
                            and command.get("direct", {}).get("deferred")):
                        mark = getattr(self.service, "mark_inbound_direct", None)
                        if callable(mark):
                            mark(command, "claim_ack_sent")
                except Exception:
                    # SDP and TURN credentials must never reach logs on failure.
                    logger.warning("Inbound media answer delivery failed")

            direct = command.get("direct")
            if direct is not None and not config.get_bool("INBOUND_FASTPATH_V2_ENABLED"):
                reply(None, "DIRECT_DISABLED")
                return
            if command["type"] == "inbound.media.end":
                end = getattr(self.service, "end_inbound_direct", None)
                accepted = (config.get_bool("INBOUND_FASTPATH_V2_ENABLED") and callable(end) and end(command))
                reply(None, None if accepted else "MEDIA_END_REJECTED")
                return
            if command["type"] == "inbound.media.sdp":
                supply_sdp = getattr(self.service, "supply_inbound_direct_sdp", None)
                if not callable(supply_sdp) or not supply_sdp(command, reply):
                    reply(None, "MEDIA_SDP_REJECTED")
                return
            issued = IssuedLiveKitSession(
                invite=RemoteDialerInvite(
                    session_id=session["sessionId"],
                    url="",
                    expires_at=time.time() + 600.0,
                ),
                room_name=session["roomName"],
                browser_identity=session["browserIdentity"],
                edge_identity=session["edgeIdentity"],
                browser_token="",
                edge_token=session["token"],
                livekit_url=session["livekitUrl"],
                direct=InboundDirectRequest(offer_id, command["claimId"], direct.get("sdpOffer"),
                                            direct.get("iceServers", []),
                                            (lambda *_: None) if direct.get("deferred") else reply)
                if direct is not None else None,
            )
            if command["type"] == "inbound.media.livekit":
                fallback = getattr(self.service, "fallback_inbound_direct", None)
                if (not config.get_bool("INBOUND_FASTPATH_V2_ENABLED") or not callable(fallback)
                        or not fallback(command, issued, reply)):
                    reply(None, "MEDIA_FALLBACK_REJECTED")
                return
            result = self.service.accept_inbound_takeover_claim(
                offer_id=offer_id,
                call_id=call_id,
                claim_id=command["claimId"],
                generation=command["generation"],
                nonce=command["nonce"],
                issued=issued,
            )
            accepted = result.accepted
            error = result.code.value if result.code is not None else None
            if not accepted and direct is not None:
                reply(None, error or "CLAIM_REJECTED")
            if direct is not None:
                if accepted and direct.get("deferred"):
                    reply(None, None)
                return
        response: dict[str, Any] = {
            "v": 1,
            "type": "command.ack",
            "commandId": command_id,
            "callId": call_id,
            "status": "accepted" if accepted else "rejected",
        }
        if offer_id is not None:
            response["offerId"] = offer_id
        if error:
            response["errorCode"] = error
        send(json.dumps(response, separators=(",", ":")))

    def _handle_data_request(
        self, command: dict[str, Any], send: Callable[[str], None]
    ) -> None:
        now_ms = round(time.time() * 1000)
        self._prune_seen_data_requests(now_ms)
        request_id = command["requestId"]
        resource = command["resource"]
        if request_id in self._seen_data_requests:
            self._send_data_error(send, request_id, resource, "INVALID_REQUEST")
            return
        if len(self._seen_data_requests) >= 2_048:
            self._seen_data_requests.pop(next(iter(self._seen_data_requests)))
        self._seen_data_requests[request_id] = command["expiresAtUnixMs"]

        repository = self._content_repository
        if repository is None or not self._content_read_enabled():
            self._send_data_error(send, request_id, resource, "FEATURE_DISABLED")
            return
        try:
            params = dict(command["params"])
            while True:
                body = repository.read(resource, params)
                response = {
                    "v": 1,
                    "type": "data.response",
                    "requestId": request_id,
                    "resource": resource,
                    "status": "ok",
                    "body": body,
                }
                wire = _compact_json(response)
                if len(wire.encode("utf-8")) <= _MAX_JSON_BYTES:
                    send(wire)
                    return
                items = body.get("items")
                limit = params.get("limit")
                if (
                    not isinstance(items, list)
                    or len(items) <= 1
                    or isinstance(limit, bool)
                    or not isinstance(limit, int)
                    or limit <= 1
                ):
                    self._send_data_error(
                        send, request_id, resource, "PAYLOAD_TOO_LARGE"
                    )
                    return
                params["limit"] = max(
                    1,
                    min(limit - 1, len(items) - 1, limit // 2),
                )
        except ContentSyncError as exc:
            code = (
                exc.code
                if exc.code
                in {
                    "INVALID_REQUEST",
                    "CURSOR_INVALID",
                    "FEATURE_DISABLED",
                    "NOT_FOUND",
                    "PAYLOAD_TOO_LARGE",
                }
                else "INTERNAL_ERROR"
            )
            self._send_data_error(send, request_id, resource, code)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "内容同步读取失败: error_type=%s", type(exc).__name__
            )
            self._send_data_error(send, request_id, resource, "INTERNAL_ERROR")

    def _prune_seen_data_requests(self, now_ms: int) -> None:
        for request_id, expires_at in list(self._seen_data_requests.items()):
            if expires_at <= now_ms:
                self._seen_data_requests.pop(request_id, None)

    @staticmethod
    def _send_data_error(
        send: Callable[[str], None],
        request_id: str,
        resource: str,
        code: str,
    ) -> None:
        send(
            _compact_json(
                {
                    "v": 1,
                    "type": "data.response",
                    "requestId": request_id,
                    "resource": resource,
                    "status": "error",
                    "error": {"code": code},
                }
            )
        )

    def _drain_takeover_events(self, send: Callable[[str], None]) -> None:
        while request := self.service.next_inbound_takeover_offer():
            send(
                json.dumps(
                    {
                        "v": 1,
                        "type": "inbound.offer",
                        "offerId": request.offer_id,
                        "callId": request.call_id,
                        "generation": request.generation,
                        "nonce": request.nonce,
                        "expiresAtUnixMs": round(request.expires_at * 1000),
                        **({"mode": "phone_first"} if request.phone_first
                           and (config.get_bool("INBOUND_PRECONNECT_ENABLED")
                                or config.get_bool("INBOUND_FASTPATH_V2_ENABLED")) else {}),
                    },
                    separators=(",", ":"),
                )
            )
        while revoke := self.service.next_inbound_takeover_revoke():
            send(
                json.dumps(
                    {
                        "v": 1,
                        "type": "inbound.offer.revoke",
                        "offerId": revoke.offer_id,
                        "callId": revoke.call_id,
                        "reason": revoke.reason,
                    },
                    separators=(",", ":"),
                )
            )

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
            additional_headers=self._websocket_headers(credential),
            user_agent_header=_USER_AGENT,
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
                self._drain_takeover_events(websocket.send)
                from .remote_alert_push import drain

                drain(websocket.send)
                try:
                    message = websocket.recv(timeout=1.0)
                except TimeoutError:
                    continue
                if isinstance(message, str):
                    self.handle_message(message, websocket.send)

    def _websocket_headers(self, credential: EdgeCredential) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {credential.value}"}
        _add_device_proof(
            headers,
            credential.edge_id,
            "GET",
            "/v1/edges/connect",
            self.credential_store.sign,
        )
        return headers

    def _heartbeat(self) -> str:
        payload = {
            "v": 1,
            "type": "heartbeat",
            "occurredAt": _utc_now(),
            "status": {
                "modemOnline": bool(self.service.modem_connected),
                "lineBusy": self.service.line_busy(),
                **({"inboundPreconnectV1": True} if config.get_bool("INBOUND_PRECONNECT_ENABLED")
                   and not config.get_bool("INBOUND_FASTPATH_V2_ENABLED") else {}),
                **({"inboundFastpathV2": True} if config.get_bool("INBOUND_FASTPATH_V2_ENABLED") else {}),
            },
        }
        return json.dumps(payload, separators=(",", ":"))

    def _set_connected(self, value: bool) -> None:
        with self._state_lock:
            self._connected = value

    def _set_error(self, value: str | None) -> None:
        with self._state_lock:
            self._last_error = value


def _parse_cloud_command(raw: str) -> dict[str, Any]:
    if not isinstance(raw, str) or not raw or len(raw.encode("utf-8")) > 64 * 1024:
        raise ValueError("invalid command")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("invalid command") from exc
    if not isinstance(value, dict):
        raise ValueError("invalid command")
    if "direct" not in value and len(raw.encode("utf-8")) > _MAX_JSON_BYTES:
        raise ValueError("invalid command")
    command_type = value.get("type")
    if command_type == "inbound.offer.pushed":
        if (set(value) != {"v", "type", "offerId", "callId", "generation", "nonce"}
                or value.get("v") != 1
                or not _valid_id(value.get("offerId"), "offer")
                or not _valid_id(value.get("callId"), "call")
                or type(value.get("generation")) is not int or value["generation"] < 0
                or not isinstance(value.get("nonce"), str) or not 16 <= len(value["nonce"]) <= 128):
            raise ValueError("invalid push receipt")
        return value
    if command_type == "session.start":
        return _validate_session_command(value)
    if command_type == "inbound.claim":
        return _validate_inbound_claim(value)
    if command_type == "inbound.media.sdp":
        _validate_inbound_claim({**value, "type": "inbound.claim"})
        if not isinstance(value.get("direct"), dict) or "sdpOffer" not in value["direct"]:
            raise ValueError("invalid SDP command")
        return value
    if command_type in {"inbound.media.livekit", "inbound.media.end"}:
        _validate_inbound_claim({**value, "type": "inbound.claim"})
        if "direct" in value:
            raise ValueError("invalid fallback")
        return value
    if command_type == "inbound.prepare":
        if value.get("mode") != "phone_first":
            raise ValueError("invalid prepare mode")
        expiry = value.get("expiresAtUnixMs")
        if type(expiry) is not int or not time.time() * 1000 < expiry <= (time.time() + 60) * 1000:
            raise ValueError("invalid prepare expiry")
        if "direct" in value:
            if set(value) != {"v", "type", "commandId", "offerId", "callId", "generation", "nonce",
                              "mode", "expiresAtUnixMs", "direct"}:
                raise ValueError("invalid direct prepare")
            if value.get("v") != 1 or any(not _valid_id(value.get(key), prefix) for key, prefix in (
                    ("commandId", "command"), ("offerId", "offer"), ("callId", "call"))):
                raise ValueError("invalid direct prepare scope")
            if (type(value.get("generation")) is not int or value["generation"] < 0
                    or not isinstance(value.get("nonce"), str) or not 16 <= len(value["nonce"]) <= 128):
                raise ValueError("invalid direct prepare fence")
            if not isinstance(value["direct"], dict) or set(value["direct"]) != {"iceServers"}:
                raise ValueError("invalid direct prepare media")
            _validate_ice_servers(value["direct"]["iceServers"])
            return value
        claim = {k: v for k, v in value.items() if k not in {"expiresAtUnixMs", "mode"}}
        claim.update(type="inbound.claim", claimId="claim_prepareplaceholder")
        _validate_inbound_claim(claim)
        if "claimId" in value:
            raise ValueError("invalid prepare")
        return value
    if command_type == "data.request":
        return _validate_data_request(value)
    raise ValueError("invalid command")


def _parse_session_command(raw: str) -> dict[str, Any]:
    value = _parse_cloud_command(raw)
    if value["type"] != "session.start":
        raise ValueError("invalid command")
    return value


def _validate_session_command(value: dict[str, Any]) -> dict[str, Any]:
    if set(value) != {
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
    _validate_issued_session(value.get("session"))
    value["expiresAtUnix"] = expires_at
    return value


def _validate_inbound_claim(value: dict[str, Any]) -> dict[str, Any]:
    if set(value) - {"direct"} != {
        "v",
        "type",
        "commandId",
        "offerId",
        "callId",
        "claimId",
        "generation",
        "nonce",
        "session",
    }:
        raise ValueError("invalid command")
    if value.get("v") != 1 or value.get("type") != "inbound.claim":
        raise ValueError("invalid command")
    identifiers = {
        "commandId": "command",
        "offerId": "offer",
        "callId": "call",
        "claimId": "claim",
    }
    if any(not _valid_id(value.get(key), prefix) for key, prefix in identifiers.items()):
        raise ValueError("invalid command")
    generation = value.get("generation")
    if isinstance(generation, bool) or not isinstance(generation, int) or generation < 0:
        raise ValueError("invalid command")
    nonce = value.get("nonce")
    if not isinstance(nonce, str) or not 16 <= len(nonce) <= 128:
        raise ValueError("invalid command")
    _validate_issued_session(value.get("session"))
    if "direct" in value:
        direct = value["direct"]
        if isinstance(direct, dict) and set(direct) == {"deferred"} and direct["deferred"] is True:
            return value
        if not isinstance(direct, dict) or set(direct) != {"sdpOffer", "iceServers"}:
            raise ValueError("invalid direct media")
        sdp = direct["sdpOffer"]
        if not isinstance(sdp, str) or not sdp.startswith("v=0") or len(sdp.encode("utf-8")) > 16 * 1024:
            raise ValueError("invalid SDP")
        _validate_ice_servers(direct["iceServers"])
    return value


def _validate_ice_servers(servers: Any) -> None:
    if not isinstance(servers, list) or len(servers) > 16:
        raise ValueError("invalid ICE servers")
    for server in servers:
        if not isinstance(server, dict) or set(server) - {"urls", "username", "credential"}:
            raise ValueError("invalid ICE server")
        urls = server.get("urls")
        if (not isinstance(urls, list) or not 1 <= len(urls) <= 16
                or any(not isinstance(url, str) or len(url) > 512
                       or not url.startswith(("stun:", "stuns:", "turn:", "turns:")) for url in urls)):
            raise ValueError("invalid ICE URLs")
        if any(not isinstance(server.get(key, ""), str) or len(server.get(key, "")) > 1024
               for key in ("username", "credential")):
            raise ValueError("invalid ICE credentials")


def _validate_data_request(value: dict[str, Any]) -> dict[str, Any]:
    if set(value) != {
        "v",
        "type",
        "requestId",
        "deviceId",
        "resource",
        "params",
        "issuedAtUnixMs",
        "expiresAtUnixMs",
    }:
        raise ValueError("invalid command")
    if value.get("v") != 1 or value.get("type") != "data.request":
        raise ValueError("invalid command")
    if not _valid_id(value.get("requestId"), "request") or not _valid_id(
        value.get("deviceId"), "device"
    ):
        raise ValueError("invalid command")
    resource = value.get("resource")
    if resource not in {
        "messages.list",
        "call_records.list",
        "call_records.get",
        "call_timeline.list",
    }:
        raise ValueError("invalid command")
    issued_at = value.get("issuedAtUnixMs")
    expires_at = value.get("expiresAtUnixMs")
    if (
        isinstance(issued_at, bool)
        or not isinstance(issued_at, int)
        or issued_at < 0
        or isinstance(expires_at, bool)
        or not isinstance(expires_at, int)
        or expires_at <= issued_at
        or expires_at - issued_at > 10_000
        or expires_at <= round(time.time() * 1000)
    ):
        raise ValueError("invalid command")
    params = value.get("params")
    if not isinstance(params, dict):
        raise ValueError("invalid command")
    if resource in {"messages.list", "call_records.list"}:
        if set(params) != {"limit", "cursor"}:
            raise ValueError("invalid command")
        _validate_data_list_params(params)
    elif resource == "call_records.get":
        if set(params) != {"callId"} or not _valid_id(params.get("callId"), "call"):
            raise ValueError("invalid command")
    else:
        if set(params) != {"callId", "limit", "cursor"} or not _valid_id(
            params.get("callId"), "call"
        ):
            raise ValueError("invalid command")
        _validate_data_list_params(params)
    return value


def _validate_data_list_params(params: dict[str, Any]) -> None:
    limit = params.get("limit")
    cursor = params.get("cursor")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise ValueError("invalid command")
    if cursor is not None and (
        not isinstance(cursor, str)
        or not 8 <= len(cursor) <= 2_048
        or re.fullmatch(r"cursor_[A-Za-z0-9_-]+", cursor) is None
    ):
        raise ValueError("invalid command")


def _validate_issued_session(session: Any) -> None:
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
    parsed_url = urllib.parse.urlparse(livekit_url) if isinstance(livekit_url, str) else None
    if (
        parsed_url is None
        or parsed_url.scheme != "wss"
        or not parsed_url.netloc
        or parsed_url.username
        or parsed_url.password
    ):
        raise ValueError("invalid command")
    token = session.get("token")
    if not isinstance(token, str) or len(token) < 40 or len(token) > 8192:
        raise ValueError("invalid command")


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
    return isinstance(value, str) and re.fullmatch(rf"{prefix}_[A-Za-z0-9_-]{{12,80}}", value) is not None


def _compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


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


def _add_device_proof(
    headers: dict[str, str],
    edge_id: str,
    method: str,
    path: str,
    signer: Callable[[bytes], str] | None,
) -> None:
    if signer is None:
        return
    timestamp = str(int(time.time() * 1000))
    message = f"{edge_id}\n{timestamp}\n{method.upper()}\n{path}".encode("utf-8")
    headers["X-CallPilot-Timestamp"] = timestamp
    headers["X-CallPilot-Signature"] = signer(message)


__all__ = [
    "CloudControlApi",
    "CloudControlStatus",
    "CloudEdgeClient",
]
