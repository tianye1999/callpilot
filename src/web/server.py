"""aiohttp 网页服务：仪表盘页面 + WebSocket 实时推送 + 发短信接口。"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from aiohttp import WSMsgType, web

from ..events import EventHub
from ..modem import Eg25Modem

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"


def build_app(
    hub: EventHub,
    modem: Eg25Modem,
    service=None,
    meta: dict | None = None,
) -> web.Application:
    app = web.Application()
    app["hub"] = hub
    app["modem"] = modem
    app["service"] = service
    app["meta"] = meta or {}

    app.router.add_get("/", _index)
    app.router.add_get("/api/meta", _meta)
    app.router.add_get("/ws", _websocket)
    app.router.add_post("/api/sms/send", _send_sms)
    app.router.add_post("/api/call/dial", _dial)
    app.router.add_static("/static/", STATIC_DIR)
    return app


async def _index(request: web.Request) -> web.Response:
    index_file = STATIC_DIR / "index.html"
    return web.FileResponse(index_file)


async def _meta(request: web.Request) -> web.Response:
    return web.json_response(request.app["meta"])


async def _websocket(request: web.Request) -> web.WebSocketResponse:
    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)
    hub: EventHub = request.app["hub"]

    for event in hub.history():
        await ws.send_json(event)

    hub.register(ws)
    logger.info("网页客户端已连接")
    try:
        async for msg in ws:
            if msg.type == WSMsgType.ERROR:
                break
    finally:
        hub.unregister(ws)
        logger.info("网页客户端已断开")
    return ws


async def _send_sms(request: web.Request) -> web.Response:
    hub: EventHub = request.app["hub"]
    modem: Eg25Modem = request.app["modem"]
    try:
        data = await request.json()
    except Exception:  # noqa: BLE001
        return web.json_response({"ok": False, "error": "请求体不是合法 JSON"}, status=400)

    number = (data.get("number") or "").strip()
    text = data.get("text") or ""
    if not number or not text:
        return web.json_response(
            {"ok": False, "error": "号码和内容都不能为空"}, status=400
        )

    loop = asyncio.get_running_loop()
    try:
        ok = await loop.run_in_executor(None, modem.send_sms, number, text)
    except Exception as exc:  # noqa: BLE001
        logger.exception("发送短信异常")
        hub.publish(
            {"type": "sms_out", "number": number, "text": text, "status": "error"}
        )
        return web.json_response({"ok": False, "error": str(exc)}, status=500)

    hub.publish(
        {
            "type": "sms_out",
            "number": number,
            "text": text,
            "status": "sent" if ok else "failed",
        }
    )
    return web.json_response({"ok": bool(ok)})


async def _dial(request: web.Request) -> web.Response:
    service = request.app["service"]
    if service is None:
        return web.json_response({"ok": False, "error": "服务不可用"}, status=500)
    try:
        data = await request.json()
    except Exception:  # noqa: BLE001
        return web.json_response({"ok": False, "error": "请求体不是合法 JSON"}, status=400)

    number = (data.get("number") or "").strip()
    if not number:
        return web.json_response({"ok": False, "error": "号码不能为空"}, status=400)

    ok, err = service.dial(number)
    if not ok:
        return web.json_response({"ok": False, "error": err}, status=409)
    return web.json_response({"ok": True})
