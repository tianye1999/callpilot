"""aiohttp 网页服务：仪表盘页面 + WebSocket 实时推送 + 短信/外呼/历史/配置接口。"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import re
import wave
from pathlib import Path

from aiohttp import WSMsgType, web

from .. import config
from ..audio_bridge import apply_pcm_gain
from ..contacts import is_reply_target_allowed
from ..events import EventHub
from ..modem import Eg25Modem

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"

# 通话 ID 白名单字符（call_log 生成的目录名），防路径穿越。
_CALL_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def require_service(request: web.Request):
    """取出注入的 service，缺失时抛 503（middleware 会统一转成 500 JSON）。

    历史行为：service 未注入时各端点返回 500。为保持逐端点一致，middleware
    把这里抛出的 503 统一映射为 500，业务层不再各自写「service is None → 500」。
    """
    service = request.app["service"]
    if service is None:
        raise web.HTTPServiceUnavailable(reason="服务不可用")
    return service


def require_call_logger(request: web.Request):
    """历史类端点：service 与 call_logger 都在才可用，否则 503→500。"""
    service = require_service(request)
    if getattr(service, "call_logger", None) is None:
        raise web.HTTPServiceUnavailable(reason="服务不可用")
    return service


async def read_json(request: web.Request):
    """解析请求体 JSON，非法时抛 400（middleware 统一转成 JSON 错误）。"""
    try:
        return await request.json()
    except Exception:  # noqa: BLE001
        raise web.HTTPBadRequest(reason="请求体不是合法 JSON")


@web.middleware
async def _error_middleware(request: web.Request, handler):
    """把 require_service/read_json 抛出的 HTTP 异常统一转成现有 JSON 错误格式。

    503（服务不可用）保持历史状态码 500；400（非法 JSON/参数）保持 400。
    响应体沿用 {"ok": false, "error": ...}，error 取异常 reason。
    """
    try:
        return await handler(request)
    except web.HTTPServiceUnavailable as exc:
        return web.json_response({"ok": False, "error": exc.reason}, status=500)
    except web.HTTPBadRequest as exc:
        return web.json_response({"ok": False, "error": exc.reason}, status=400)


def build_app(
    hub: EventHub,
    modem: Eg25Modem,
    service=None,
    meta: dict | None = None,
    restart_event=None,
) -> web.Application:
    app = web.Application(middlewares=[_error_middleware])
    app["hub"] = hub
    app["modem"] = modem
    app["service"] = service
    app["meta"] = meta or {}
    # 由 app.py 传入的 threading.Event；置位后主循环停止并 os.execv 自重启。
    app["restart_event"] = restart_event

    app.router.add_get("/", _index)
    app.router.add_get("/api/meta", _meta)
    app.router.add_get("/ws", _websocket)
    app.router.add_get("/ws/audio", _audio_websocket)
    app.router.add_post("/api/sms/send", _send_sms)
    app.router.add_post("/api/call/dial", _dial)
    app.router.add_post("/api/call/hangup", _hangup)
    app.router.add_post("/api/call/dtmf", _dtmf)
    app.router.add_post("/api/call/batch_dial", _batch_dial)
    app.router.add_get("/api/call/queue", _queue_status)
    app.router.add_get("/api/history", _history)
    app.router.add_get("/api/history/{call_id}/events", _history_events)
    app.router.add_get("/api/history/{call_id}/audio/{track}", _history_audio)
    app.router.add_get("/api/config", _get_config)
    app.router.add_post("/api/config", _post_config)
    app.router.add_post("/api/restart", _restart)
    app.router.add_static("/static/", STATIC_DIR)
    return app


async def _index(request: web.Request) -> web.Response:
    index_file = STATIC_DIR / "index.html"
    # 禁缓存：界面迭代频繁，避免浏览器用旧页面（曾致用户看不到新 UI）。
    return web.FileResponse(
        index_file,
        headers={"Cache-Control": "no-cache, must-revalidate"},
    )


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


async def _audio_websocket(request: web.Request) -> web.WebSocketResponse:
    """实时旁听：把通话下行 PCM 二进制帧推给浏览器（Web Audio 播放，绕开 native 音频）。

    先发一条 JSON meta 告知采样率，之后全是 s16le 单声道二进制帧。
    """
    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)
    hub: EventHub = request.app["hub"]

    await ws.send_json({"type": "meta", "rate": hub.audio_rate})
    hub.register_audio(ws)
    logger.info("音频旁听端已连接")
    try:
        async for msg in ws:
            if msg.type == WSMsgType.ERROR:
                break
    finally:
        hub.unregister_audio(ws)
        logger.info("音频旁听端已断开")
    return ws


async def _send_sms(request: web.Request) -> web.Response:
    hub: EventHub = request.app["hub"]
    modem: Eg25Modem = request.app["modem"]
    data = await read_json(request)

    number = (data.get("number") or "").strip()
    text = data.get("text") or ""
    if not number or not text:
        return web.json_response(
            {"ok": False, "error": "号码和内容都不能为空"}, status=400
        )

    # 发短信目标限制:只能回复已联系过的号码(来过电/发过短信)或当前通话对端。
    # 无鉴权的 Web 接口也过这道闸,防 CSRF 被利用群发陌生号码。
    service = request.app.get("service")
    call_logger = getattr(service, "call_logger", None)
    # 当前对端仅在「确有通话进行中」时才作放行例外:current_caller 通话结束不清空,
    # 且 /api/call/dial 会把任意外呼目标写进它——不 gate on is_active 会被 CSRF 利用
    # (先拨号写入 current_caller,再发短信绕过联系人校验)。
    session = getattr(service, "session", None)
    current_caller = (
        session.current_caller
        if session is not None and getattr(session, "is_active", False)
        else None
    )
    if not is_reply_target_allowed(
        number, hub, call_logger, extra_allowed=current_caller
    ):
        return web.json_response(
            {"ok": False, "error": "只能给来过电或发过短信的号码发送短信"},
            status=403,
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
    service = require_service(request)
    data = await read_json(request)

    number = (data.get("number") or "").strip()
    if not number:
        return web.json_response({"ok": False, "error": "号码不能为空"}, status=400)
    task = data.get("task")
    if task is not None and not isinstance(task, str):
        return web.json_response({"ok": False, "error": "task 必须是字符串"}, status=400)

    ok, err = service.dial(number, task=task)
    if not ok:
        return web.json_response({"ok": False, "error": err}, status=409)
    return web.json_response({"ok": True})


async def _hangup(request: web.Request) -> web.Response:
    """挂断进行中的通话（AI 与 IVR 互相不挂断时的人工兜底）。"""
    service = require_service(request)
    ok, err = service.hangup()
    if not ok:
        return web.json_response({"ok": False, "error": err}, status=409)
    return web.json_response({"ok": True})


async def _dtmf(request: web.Request) -> web.Response:
    """通话中人工发送 DTMF 按键（IVR 菜单导航）。"""
    service = require_service(request)
    data = await read_json(request)
    digits = (data.get("digits") or "").strip()
    if not digits or any(ch not in "0123456789*#" for ch in digits):
        return web.json_response({"ok": False, "error": "digits 仅允许 0-9、*、#"}, status=400)

    loop = asyncio.get_running_loop()
    ok, err = await loop.run_in_executor(None, service.send_dtmf, digits)
    # 无通话属状态冲突（旧行为 409）；模组发送失败沿用旧行为返回 200 + {"ok": false}。
    if not ok and err == "当前没有进行中的通话":
        return web.json_response({"ok": False, "error": err}, status=409)
    return web.json_response({"ok": bool(ok)})


async def _batch_dial(request: web.Request) -> web.Response:
    """批量外呼：``{"numbers": [...], "task"?: str}`` → ``service.batch_dial``。"""
    service = require_service(request)
    data = await read_json(request)

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
    # 成功响应统一带 ok=true（前端可据此判断，字段只增不改）。
    return web.json_response({**result, "ok": True})


async def _queue_status(request: web.Request) -> web.Response:
    """外呼队列状态快照（pending/current/done/active）。"""
    service = require_service(request)
    return web.json_response({**service.dial_queue_status(), "ok": True})


async def _history(request: web.Request) -> web.Response:
    """通话历史列表：``?limit=50``（新→旧）。"""
    service = require_call_logger(request)
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
    service = require_call_logger(request)
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


_AUDIO_TRACKS = {"uplink", "downlink"}


async def _history_audio(request: web.Request) -> web.Response:
    """播放某通录音：downlink=AI 下行，uplink=对方上行（WAV，供浏览器 <audio> 播放）。

    浏览器播放走 Chrome→系统音频，绕开本机 PortAudio/ffmpeg 播放通道的已知问题，
    是听 AI 到底说了什么最稳的途径（配合实时转写）。
    """
    service = require_call_logger(request)
    call_id = request.match_info["call_id"]
    track = request.match_info["track"]
    if not _CALL_ID_RE.fullmatch(call_id):
        return web.json_response({"ok": False, "error": "非法的通话 ID"}, status=400)
    if track not in _AUDIO_TRACKS:
        return web.json_response(
            {"ok": False, "error": "track 只能是 downlink/uplink"}, status=400
        )
    wav_path = Path(service.call_logger.base_dir) / call_id / f"{track}.wav"
    if not wav_path.is_file():
        return web.json_response({"ok": False, "error": "录音不存在"}, status=404)
    # 上行（对方）模组采集电平极低（原始 RMS 仅几十），回放前放大到可闻；
    # 下行本身够响，原样发。放大量沿用监听增益 MONITOR_UPLINK_GAIN。
    if track == "uplink":
        gain = config.get_float("MONITOR_UPLINK_GAIN")
        loop = asyncio.get_running_loop()
        body = await loop.run_in_executor(None, _amplified_wav_bytes, wav_path, gain)
        return web.Response(body=body, content_type="audio/wav")
    return web.FileResponse(wav_path, headers={"Content-Type": "audio/wav"})


def _amplified_wav_bytes(wav_path: Path, gain: float) -> bytes:
    """读 WAV、对 PCM 加增益、重新封成 WAV 字节（供上行回放放大到可闻）。"""
    with wave.open(str(wav_path), "rb") as r:
        params = r.getparams()
        frames = r.readframes(r.getnframes())
    amplified = apply_pcm_gain(frames, gain)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(params.nchannels)
        w.setsampwidth(params.sampwidth)
        w.setframerate(params.framerate)
        w.writeframes(amplified)
    return buf.getvalue()


async def _get_config(request: web.Request) -> web.Response:
    """设置面板数据：全部配置项 + 当前生效值（secret 已掩码）。"""
    return web.json_response(config.read_panel_values())


async def _restart(request: web.Request) -> web.Response:
    """重启服务以应用需重启的配置。

    置位 restart_event 后延迟停止事件循环——app.py 主循环随即清理并
    os.execv 原地重启（重读 .env）。延迟 0.4s 是为了让本响应先发回前端。
    """
    restart_event = request.app.get("restart_event")
    if restart_event is None:
        return web.json_response(
            {"ok": False, "error": "当前运行方式不支持自动重启"}, status=501
        )
    restart_event.set()
    loop = asyncio.get_running_loop()
    loop.call_later(0.4, loop.stop)
    return web.json_response({"ok": True})


async def _post_config(request: web.Request) -> web.Response:
    """保存设置：``{key: value, ...}`` → 写回 .env 并同步环境变量。

    响应 ``{"ok": true, "updated": [...], "requires_restart": [...]}``；
    任一项非法则整批拒绝（400），文件与环境均不改动。
    """
    data = await read_json(request)
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
    return web.json_response(
        {"ok": True, "updated": updated, "requires_restart": requires_restart}
    )
