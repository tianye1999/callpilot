"""Least-privilege public HTTP gateway for paired Remote Web Dialer phones."""

from __future__ import annotations

import asyncio
import json
import secrets
import time
from collections import defaultdict, deque
from pathlib import Path
from urllib.parse import urlsplit

from aiohttp import web

from .. import config
from ..remote_pairing import (
    InvalidPairingCodeError,
    PairingCapacityError,
    RemotePairingStore,
)

STATIC_DIR = Path(__file__).parent / "static"
DEVICE_COOKIE = "__Host-callpilot-device"
_COOKIE_MAX_AGE = 180 * 24 * 60 * 60
_SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Permissions-Policy": "microphone=(self), camera=(), geolocation=()",
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self' https://cdn.jsdelivr.net; "
        "style-src 'self'; connect-src 'self' wss:; media-src blob:; worker-src 'self' blob:; "
        "manifest-src 'self'; img-src 'self'; base-uri 'none'; form-action 'self'; "
        "frame-ancestors 'none'"
    ),
}


class _AttemptLimiter:
    def __init__(self, limit: int, window_seconds: float = 300.0) -> None:
        self.limit = max(1, limit)
        self.window_seconds = window_seconds
        self._attempts: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        attempts = self._attempts[key]
        while attempts and attempts[0] <= now - self.window_seconds:
            attempts.popleft()
        if len(attempts) >= self.limit:
            return False
        attempts.append(now)
        return True


def build_remote_gateway(
    service,
    pairing_store: RemotePairingStore,
    *,
    public_url: str,
    max_pair_attempts: int = 10,
) -> web.Application:
    """Build a public app that intentionally has no general AgentCall admin routes."""

    parsed = urlsplit(public_url)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.fragment
    ):
        raise ValueError("REMOTE_CONTROL_URL 必须是 HTTPS URL")
    public_origin = f"{parsed.scheme}://{parsed.netloc}"
    app = web.Application(client_max_size=16 * 1024)
    app["service"] = service
    app["pairing_store"] = pairing_store
    app["public_origin"] = public_origin
    app["pair_limiter"] = _AttemptLimiter(max_pair_attempts)

    app.router.add_get("/", _page)
    app.router.add_get("/remote_dialer.html", _page)
    app.router.add_get("/remote-dialer", _page)
    app.router.add_get("/remote-dialer/", _page)
    app.router.add_get("/remote_dialer.css", _asset)
    app.router.add_get("/remote_dialer.js", _asset)
    app.router.add_get("/manifest.webmanifest", _asset)
    app.router.add_get("/remote_dialer_sw.js", _asset)
    app.router.add_get("/callpilot-192.png", _asset)
    app.router.add_get("/callpilot-512.png", _asset)
    app.router.add_get("/api/device", _device)
    app.router.add_post("/api/pair", _pair)
    app.router.add_post("/api/session", _session)
    app.router.add_post("/api/unpair", _unpair)
    return app


async def _page(_request: web.Request) -> web.StreamResponse:
    return web.FileResponse(STATIC_DIR / "remote_dialer.html", headers=_SECURITY_HEADERS)


async def _asset(request: web.Request) -> web.StreamResponse:
    filename = request.path.rsplit("/", 1)[-1]
    allowed = {
        "remote_dialer.css",
        "remote_dialer.js",
        "manifest.webmanifest",
        "remote_dialer_sw.js",
        "callpilot-192.png",
        "callpilot-512.png",
    }
    if filename not in allowed:
        raise web.HTTPNotFound()
    headers = {
        "Cache-Control": "no-cache",
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
    }
    return web.FileResponse(STATIC_DIR / filename, headers=headers)


async def _device(request: web.Request) -> web.Response:
    device = _authenticated_device(request)
    service = request.app["service"]
    status = service.remote_dialer_status()
    if device is None:
        return _json({"ok": True, "paired": False, "edge": status})
    return _json(
        {
            "ok": True,
            "paired": True,
            "device": _device_payload(device),
            "edge": status,
        }
    )


async def _pair(request: web.Request) -> web.Response:
    if not config.get_bool("REMOTE_WEB_DIALER_ENABLED"):
        return _error("远程网页拨号未启用", 403)
    if not _same_origin(request):
        return _error("请求来源不受信任", 403)
    limiter: _AttemptLimiter = request.app["pair_limiter"]
    peer = request.remote or "unknown"
    if not limiter.allow(peer):
        return _error("配对尝试过于频繁，请稍后再试", 429)
    data = await _read_json(request)
    if data is None:
        return _error("请求体不是合法 JSON", 400)
    code = data.get("code")
    display_name = data.get("display_name")
    if not isinstance(code, str) or not isinstance(display_name, str):
        return _error("配对码和设备名称不能为空", 400)
    store: RemotePairingStore = request.app["pairing_store"]
    try:
        credential = store.pair(code, display_name)
    except InvalidPairingCodeError:
        return _error("配对码无效或已过期", 401)
    except PairingCapacityError as exc:
        return _error(str(exc), 409)
    except ValueError as exc:
        return _error(str(exc), 400)

    response = _json(
        {"ok": True, "paired": True, "device": _device_payload(credential.device)}
    )
    response.set_cookie(
        DEVICE_COOKIE,
        f"{credential.device.device_id}.{credential.secret}",
        max_age=_COOKIE_MAX_AGE,
        secure=True,
        httponly=True,
        samesite="Strict",
        path="/",
    )
    return response


async def _session(request: web.Request) -> web.Response:
    if not config.get_bool("REMOTE_WEB_DIALER_ENABLED"):
        return _error("远程网页拨号未启用", 403)
    if not _same_origin(request):
        return _error("请求来源不受信任", 403)
    if await _read_json(request) is None:
        return _error("请求体不是合法 JSON", 400)
    if _authenticated_device(request) is None:
        return _error("设备未配对或已撤销", 401)
    service = request.app["service"]
    loop = asyncio.get_running_loop()
    invite, error = await loop.run_in_executor(None, service.create_remote_dialer_invite)
    if invite is None:
        return _error(error or "无法创建远程拨号会话", 409)
    return _json({"ok": True, "invite": invite})


async def _unpair(request: web.Request) -> web.Response:
    if not _same_origin(request):
        return _error("请求来源不受信任", 403)
    if await _read_json(request) is None:
        return _error("请求体不是合法 JSON", 400)
    credential = _cookie_parts(request)
    store: RemotePairingStore = request.app["pairing_store"]
    if credential is not None:
        device_id, secret = credential
        if store.authenticate(device_id, secret) is not None:
            store.revoke(device_id)
    response = _json({"ok": True, "paired": False})
    response.del_cookie(DEVICE_COOKIE, path="/", secure=True, httponly=True, samesite="Strict")
    return response


def _authenticated_device(request: web.Request):
    credential = _cookie_parts(request)
    if credential is None:
        return None
    device_id, secret = credential
    store: RemotePairingStore = request.app["pairing_store"]
    return store.authenticate(device_id, secret)


def _cookie_parts(request: web.Request) -> tuple[str, str] | None:
    value = request.cookies.get(DEVICE_COOKIE, "")
    if "." not in value or len(value) > 256:
        return None
    device_id, secret = value.split(".", 1)
    return device_id, secret


def _same_origin(request: web.Request) -> bool:
    origin = request.headers.get("Origin", "")
    expected = request.app["public_origin"]
    return bool(origin) and secrets.compare_digest(origin, expected)


async def _read_json(request: web.Request) -> dict | None:
    try:
        data = await request.json()
    except (json.JSONDecodeError, ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def _device_payload(device) -> dict:
    return {
        "device_id": device.device_id,
        "display_name": device.display_name,
        "created_at": device.created_at,
        "last_used_at": device.last_used_at,
    }


def _json(payload: dict, *, status: int = 200) -> web.Response:
    return web.json_response(payload, status=status, headers={"Cache-Control": "no-store"})


def _error(message: str, status: int) -> web.Response:
    return _json({"ok": False, "error": message}, status=status)


__all__ = ["DEVICE_COOKIE", "build_remote_gateway"]
