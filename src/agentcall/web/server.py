"""aiohttp 网页服务：仪表盘页面 + WebSocket 实时推送 + 短信/外呼/历史/配置接口。"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path

from aiohttp import WSMsgType, web

from .. import config
from ..events import EventHub
from ..modem import Eg25Modem

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"

# 通话 ID 白名单字符（call_log 生成的目录名），防路径穿越。
_CALL_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


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
    app.router.add_post("/api/call/hangup", _hangup)
    app.router.add_post("/api/call/batch_dial", _batch_dial)
    app.router.add_get("/api/call/queue", _queue_status)
    app.router.add_get("/api/history", _history)
    app.router.add_get("/api/history/{call_id}/events", _history_events)
    app.router.add_get("/api/config", _get_config)
    app.router.add_post("/api/config", _post_config)
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


async def _hangup(request: web.Request) -> web.Response:
    """挂断进行中的通话（AI 与 IVR 互相不挂断时的人工兜底）。"""
    service = request.app["service"]
    if service is None:
        return web.json_response({"ok": False, "error": "服务不可用"}, status=500)
    session = service.session
    if not session.is_active:
        return web.json_response({"ok": False, "error": "当前没有进行中的通话"}, status=409)
    session.stop()
    return web.json_response({"ok": True})


async def _batch_dial(request: web.Request) -> web.Response:
    """批量外呼：``{"numbers": [...], "task"?: str}`` → ``service.batch_dial``。"""
    service = request.app["service"]
    if service is None:
        return web.json_response({"ok": False, "error": "服务不可用"}, status=500)
    try:
        data = await request.json()
    except Exception:  # noqa: BLE001
        return web.json_response({"ok": False, "error": "请求体不是合法 JSON"}, status=400)

    numbers = data.get("numbers") if isinstance(data, dict) else None
    if not isinstance(numbers, list) or not numbers:
        return web.json_response(
            {"ok": False, "error": "numbers 需为非空号码列表"}, status=400
        )
    if not all(isinstance(item, str) for item in numbers):
        return web.json_response(
            {"ok": False, "error": "numbers 每一项都需为字符串"}, status=400
        )
    task = data.get("task")
    if task is not None and not isinstance(task, str):
        return web.json_response({"ok": False, "error": "task 需为字符串"}, status=400)
    task = (task or "").strip() or None

    result = service.batch_dial(numbers, task)
    return web.json_response(result)


async def _queue_status(request: web.Request) -> web.Response:
    """外呼队列状态快照（pending/current/done/active）。"""
    service = request.app["service"]
    if service is None:
        return web.json_response({"ok": False, "error": "服务不可用"}, status=500)
    return web.json_response(service.dial_queue_status())


async def _history(request: web.Request) -> web.Response:
    """通话历史列表：``?limit=50``（新→旧）。"""
    service = request.app["service"]
    if service is None or getattr(service, "call_logger", None) is None:
        return web.json_response({"ok": False, "error": "服务不可用"}, status=500)
    raw_limit = request.query.get("limit", "50")
    try:
        limit = int(raw_limit)
    except ValueError:
        return web.json_response({"ok": False, "error": "limit 需为整数"}, status=400)
    if limit < 1:
        return web.json_response({"ok": False, "error": "limit 需大于 0"}, status=400)

    loop = asyncio.get_running_loop()
    calls = await loop.run_in_executor(None, service.call_logger.list_calls, limit)
    return web.json_response(calls)


async def _history_events(request: web.Request) -> web.Response:
    """单通通话的事件时间线：读 ``events.jsonl`` 返回 JSON 列表。"""
    service = request.app["service"]
    if service is None or getattr(service, "call_logger", None) is None:
        return web.json_response({"ok": False, "error": "服务不可用"}, status=500)
    call_id = request.match_info["call_id"]
    if not _CALL_ID_RE.fullmatch(call_id):
        return web.json_response({"ok": False, "error": "非法的通话 ID"}, status=400)

    events_path = Path(service.call_logger.base_dir) / call_id / "events.jsonl"
    if not events_path.is_file():
        return web.json_response({"ok": False, "error": "通话记录不存在"}, status=404)

    def read_events() -> list[dict]:
        events: list[dict] = []
        for line in events_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # 单行损坏容错跳过
        return events

    loop = asyncio.get_running_loop()
    events = await loop.run_in_executor(None, read_events)
    return web.json_response(events)


async def _get_config(request: web.Request) -> web.Response:
    """设置面板数据：全部配置项 + 当前生效值（secret 已掩码）。"""
    return web.json_response(config.read_panel_values())


async def _post_config(request: web.Request) -> web.Response:
    """保存设置：``{key: value, ...}`` → 写回 .env 并同步环境变量。

    响应 ``{"updated": [...], "requires_restart": [...]}``；
    任一项非法则整批拒绝（400），文件与环境均不改动。
    """
    try:
        data = await request.json()
    except Exception:  # noqa: BLE001
        return web.json_response({"ok": False, "error": "请求体不是合法 JSON"}, status=400)
    if not isinstance(data, dict):
        return web.json_response(
            {"ok": False, "error": "请求体需为 {key: value} 对象"}, status=400
        )

    updates: dict[str, str] = {}
    for key, value in data.items():
        # 宽容常见 JSON 标量：bool/数字自动转为字符串，其余类型拒绝。
        if isinstance(value, bool):
            value = "true" if value else "false"
        elif isinstance(value, (int, float)):
            value = str(value)
        elif not isinstance(value, str):
            return web.json_response(
                {"ok": False, "error": f"配置 {key} 的值类型不支持"}, status=400
            )
        updates[str(key)] = value

    loop = asyncio.get_running_loop()
    try:
        updated = await loop.run_in_executor(None, config.update_env_file, updates)
    except ValueError as exc:
        return web.json_response({"ok": False, "error": str(exc)}, status=400)

    requires_restart = [
        key for key in updated if config.get_spec(key).requires_restart
    ]
    return web.json_response({"updated": updated, "requires_restart": requires_restart})
