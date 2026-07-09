"""aiohttp 网页服务：仪表盘页面 + WebSocket 实时推送 + 短信/外呼/历史/配置接口。"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import re
import secrets
import subprocess
import sys
import wave
from functools import partial
from pathlib import Path

from aiohttp import WSMsgType, web
from serial.tools import list_ports

from .. import config, platforms
from ..audio_bridge import apply_pcm_gain
from ..contacts import is_reply_target_allowed
from ..events import EventHub
from ..modem import Eg25Modem
from ..number_profiles import (
    ProfileConflictError,
    ProfileNotFoundError,
    ProfileValidationError,
    create_profile,
    delete_profile,
    list_managed_profiles,
    list_profiles,
    update_profile,
)
from ..port_detect import QUECTEL_VID
from ..rate_limit import acquire_sms_send_slot

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"

# 通话 ID 白名单字符（call_log 生成的目录名），防路径穿越。
_CALL_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _ensure_setup_sms_token(app: web.Application) -> str | None:
    token_box = app.get("setup_sms_token")
    if not token_box:
        return None
    if not token_box[0]:
        token_box[0] = secrets.token_urlsafe(24)
    return str(token_box[0])


def _bundled_libusb_path() -> Path | None:
    base = getattr(sys, "_MEIPASS", None)
    if not base:
        return None
    candidate = Path(base) / "lib" / "libusb-1.0.0.dylib"
    return candidate if candidate.is_file() else None


def _pyusb_backend():
    bundled = _bundled_libusb_path()
    if bundled is None:
        return None

    def find_library(_name: str) -> str:
        return str(bundled)

    try:
        import usb.backend.libusb1
    except Exception:  # noqa: BLE001
        return None
    return usb.backend.libusb1.get_backend(find_library=find_library)


def _detect_quectel_usb_pyusb() -> bool:
    try:
        import usb.core
    except Exception as exc:  # noqa: BLE001
        logger.debug("PyUSB unavailable for Quectel scan: %s", exc)
        return False
    try:
        return any(
            True
            for _dev in usb.core.find(
                find_all=True,
                idVendor=QUECTEL_VID,
                backend=_pyusb_backend(),
            )
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("PyUSB Quectel scan failed: %s", exc)
        return False


def _usb_tree_has_quectel(node) -> bool:
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "vendor_id":
                if isinstance(value, int) and value == QUECTEL_VID:
                    return True
                if isinstance(value, str) and "0x2c7c" in value.lower():
                    return True
            if _usb_tree_has_quectel(value):
                return True
    elif isinstance(node, list):
        return any(_usb_tree_has_quectel(item) for item in node)
    return False


def _detect_quectel_usb_system_profiler() -> bool:
    try:
        result = subprocess.run(
            ["system_profiler", "SPUSBDataType", "-json"],
            check=False,
            capture_output=True,
            text=True,
            timeout=8,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("system_profiler Quectel scan failed: %s", exc)
        return False
    if result.returncode != 0:
        logger.debug("system_profiler Quectel scan exited %s", result.returncode)
        return False
    try:
        data = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        logger.debug("system_profiler Quectel scan JSON invalid: %s", exc)
        return False
    return _usb_tree_has_quectel(data)


def detect_quectel_usb_online() -> bool:
    """Best-effort EC20/EG25 USB VID presence check for the setup wizard."""
    if platforms.IS_MACOS:
        if _detect_quectel_usb_pyusb():
            return True
        return _detect_quectel_usb_system_profiler()
    try:
        return any(getattr(port, "vid", None) == QUECTEL_VID for port in list_ports.comports())
    except Exception as exc:  # noqa: BLE001
        logger.debug("Quectel USB scan failed: %s", exc)
        return False


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
    app["setup_sms_token"] = [secrets.token_urlsafe(24)]

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
    app.router.add_get("/api/number_profiles", _number_profiles)
    app.router.add_get("/api/number_profiles/manage", _number_profiles_manage)
    app.router.add_post("/api/number_profiles", _number_profiles_create)
    app.router.add_patch("/api/number_profiles/{profile_id}", _number_profiles_update)
    app.router.add_delete("/api/number_profiles/{profile_id}", _number_profiles_delete)
    app.router.add_get("/api/history", _history)
    app.router.add_delete("/api/history", _history_clear)
    app.router.add_post("/api/history/{call_id}", _history_delete)
    app.router.add_delete("/api/history/{call_id}", _history_delete)
    app.router.add_get("/api/history/{call_id}/events", _history_events)
    app.router.add_get("/api/history/{call_id}/audio/{track}", _history_audio)
    app.router.add_get("/api/config", _get_config)
    app.router.add_post("/api/config", _post_config)
    app.router.add_post("/api/config/validate_key", _validate_key)
    app.router.add_post("/api/setup/complete", _setup_complete)
    app.router.add_post("/api/setup/test_sms", _setup_test_sms)
    app.router.add_post("/api/restart", _restart)
    app.router.add_static("/static/", STATIC_DIR)
    return app


async def _index(request: web.Request) -> web.StreamResponse:
    index_file = STATIC_DIR / "index.html"
    # 禁缓存：界面迭代频繁，避免浏览器用旧页面（曾致用户看不到新 UI）。
    return web.FileResponse(
        index_file,
        headers={"Cache-Control": "no-cache, must-revalidate"},
    )


async def _meta(request: web.Request) -> web.Response:
    meta = dict(request.app["meta"])
    service = request.app.get("service")
    meta["credentials"] = config.credential_status(meta.get("provider"))
    meta["setup_required"] = config.setup_required()
    meta["hardware"] = {
        "usb_online": detect_quectel_usb_online(),
        "modem_connected": bool(getattr(service, "modem_connected", False)),
        "port": meta.get("port") or config.get_str("MODEM_PORT"),
    }
    setup_sms_token = _ensure_setup_sms_token(request.app)
    if setup_sms_token:
        meta["setup_sms_token"] = setup_sms_token
    return web.json_response(meta)


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

    # rate=下行(AI)采样率；uplink_rate=上行(对方)固定 8kHz（模组 PCM 速率）。
    await ws.send_json({"type": "meta", "rate": hub.audio_rate, "uplink_rate": 8000})
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
            {"ok": False, "error": "只能给曾通话（来电/已接通的外呼）或发过短信的号码发送短信"},
            status=403,
        )
    slot = acquire_sms_send_slot(config.get_int("SMS_RATE_LIMIT_PER_HOUR"))
    if not slot.allowed:
        logger.warning(
            "Web 发短信被频控拦截: to=%s retry_after=%.1fs",
            number,
            slot.retry_after,
        )
        return web.json_response(
            {
                "ok": False,
                "error": "短信发送触发频控，请稍后再试",
                "retry_after": round(slot.retry_after, 1),
            },
            status=429,
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
    # preset_task：选中预设时的命中键（事项框改成子主题也不影响命中）；手输时为 None。
    preset_hint = data.get("preset_task")
    if preset_hint is not None and not isinstance(preset_hint, str):
        return web.json_response({"ok": False, "error": "preset_task 必须是字符串"}, status=400)
    preset_id = data.get("preset_id")
    if preset_id is not None and (
        not isinstance(preset_id, str)
        or len(preset_id) > 64
        or not _CALL_ID_RE.fullmatch(preset_id)
    ):
        return web.json_response({"ok": False, "error": "preset_id 格式不合法"}, status=400)

    ok, err = service.dial(
        number,
        task=task,
        preset_hint=preset_hint,
        preset_id=preset_id,
    )
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


async def _number_profiles(request: web.Request) -> web.Response:
    if not config.get_bool("NUMBER_PROFILES_ENABLED"):
        return web.json_response({"profiles": []})
    # UI 语言决定下拉里 label/task 的展示语言；缺省回退通话语言。
    lang = request.query.get("lang", "").strip() or config.get_str("AGENT_LANGUAGE")
    return web.json_response({"profiles": list_profiles(lang=lang, include_id=True)})


async def _number_profiles_manage(request: web.Request) -> web.Response:
    loop = asyncio.get_running_loop()
    profiles = await loop.run_in_executor(None, list_managed_profiles)
    return web.json_response(
        {
            "ok": True,
            "enabled": config.get_bool("NUMBER_PROFILES_ENABLED"),
            "profiles": profiles,
        }
    )


def _profile_error(exc: Exception) -> web.Response:
    if isinstance(exc, ProfileConflictError):
        status = 409
    elif isinstance(exc, ProfileNotFoundError):
        status = 404
    else:
        status = 400
    return web.json_response({"ok": False, "error": str(exc)}, status=status)


async def _number_profiles_create(request: web.Request) -> web.Response:
    data = await read_json(request)
    loop = asyncio.get_running_loop()
    try:
        profile = await loop.run_in_executor(None, partial(create_profile, data))
    except (ProfileValidationError, ProfileConflictError) as exc:
        return _profile_error(exc)
    return web.json_response({"ok": True, "profile": profile}, status=201)


async def _number_profiles_update(request: web.Request) -> web.Response:
    data = await read_json(request)
    profile_id = request.match_info["profile_id"]
    loop = asyncio.get_running_loop()
    try:
        profile = await loop.run_in_executor(
            None, partial(update_profile, profile_id, data)
        )
    except (ProfileValidationError, ProfileConflictError, ProfileNotFoundError) as exc:
        return _profile_error(exc)
    return web.json_response({"ok": True, "profile": profile})


async def _number_profiles_delete(request: web.Request) -> web.Response:
    profile_id = request.match_info["profile_id"]
    loop = asyncio.get_running_loop()
    try:
        deleted = await loop.run_in_executor(None, partial(delete_profile, profile_id))
    except ProfileValidationError as exc:
        return _profile_error(exc)
    if not deleted:
        return _profile_error(ProfileNotFoundError("预设任务不存在或已被删除"))
    return web.json_response({"ok": True})


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


def _active_call_ids(service) -> set[str]:
    session = getattr(service, "session", None)
    if session is None or not getattr(session, "is_active", False):
        return set()
    record = getattr(session, "_record", None)
    call_id = getattr(record, "id", None)
    if isinstance(call_id, str) and _CALL_ID_RE.fullmatch(call_id):
        return {call_id}
    return set()


async def _history_clear(request: web.Request) -> web.Response:
    """删除全部通话历史；正在进行中的通话跳过。"""
    service = require_call_logger(request)
    active_ids = _active_call_ids(service)
    loop = asyncio.get_running_loop()
    try:
        result = await loop.run_in_executor(
            None, partial(service.call_logger.clear_calls, active_ids=active_ids)
        )
    except OSError as exc:
        return web.json_response({"ok": False, "error": str(exc)}, status=500)
    return web.json_response({"ok": True, **result})


async def _history_delete(request: web.Request) -> web.Response:
    """删除单条通话历史；正在进行中的通话跳过。"""
    service = require_call_logger(request)
    call_id = request.match_info["call_id"]
    if not _CALL_ID_RE.fullmatch(call_id):
        return web.json_response({"ok": False, "error": "非法的通话 ID"}, status=400)
    active_ids = _active_call_ids(service)
    loop = asyncio.get_running_loop()
    try:
        status = await loop.run_in_executor(
            None, partial(service.call_logger.delete_call, call_id, active_ids=active_ids)
        )
    except OSError as exc:
        return web.json_response({"ok": False, "error": str(exc)}, status=500)
    if status == "missing":
        return web.json_response({"ok": False, "error": "通话记录不存在"}, status=404)
    deleted = [call_id] if status == "deleted" else []
    skipped = [call_id] if status == "skipped" else []
    return web.json_response({"ok": True, "deleted": deleted, "skipped": skipped})


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


async def _history_audio(request: web.Request) -> web.StreamResponse:
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


async def _validate_key(request: web.Request) -> web.Response:
    """Online provider key check for the first-run wizard; never persists secrets."""
    data = await read_json(request)
    if not isinstance(data, dict):
        return web.json_response(
            {"ok": False, "error": "请求体需为对象"}, status=400
        )
    provider = str(data.get("provider") or "").strip().lower()
    api_key = str(data.get("api_key") or "").strip()
    if provider not in {"qwen", "openai"}:
        return web.json_response(
            {"ok": False, "status": "unsupported", "error": "当前 provider 不支持在线校验"},
            status=400,
        )
    if not api_key:
        return web.json_response(
            {"ok": False, "status": "invalid", "error": "API Key 不能为空"},
            status=400,
        )

    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(
        None,
        partial(config.validate_provider_key_online, provider, api_key, timeout=5.0),
    )
    payload: dict[str, bool | str] = {"ok": result.ok, "status": result.status}
    if result.message:
        payload["message"] = result.message
    return web.json_response(payload)


async def _setup_complete(request: web.Request) -> web.Response:
    """Persist the hidden SETUP_DONE flag after the wizard finishes or is skipped."""
    await read_json(request)
    loop = asyncio.get_running_loop()
    try:
        updated = await loop.run_in_executor(None, config.mark_setup_done)
    except ValueError as exc:
        return web.json_response({"ok": False, "error": str(exc)}, status=400)
    request.app["setup_sms_token"][0] = None
    return web.json_response({"ok": True, "updated": updated})


async def _setup_test_sms(request: web.Request) -> web.Response:
    """One explicit setup-wizard SMS test, separate from the normal reply-only SMS API."""
    hub: EventHub = request.app["hub"]
    modem: Eg25Modem = request.app["modem"]
    data = await read_json(request)
    if not isinstance(data, dict):
        return web.json_response(
            {"ok": False, "error": "请求体需为对象"}, status=400
        )
    number = (data.get("number") or "").strip()
    text = data.get("text") or ""
    token = str(data.get("token") or "")
    token_box = request.app.get("setup_sms_token") or [None]
    expected_token = token_box[0]
    if not expected_token or token != expected_token:
        return web.json_response(
            {"ok": False, "error": "测试短信令牌无效或已使用"}, status=403
        )
    if not number or not text.strip():
        return web.json_response(
            {"ok": False, "error": "号码和内容都不能为空"}, status=400
        )

    loop = asyncio.get_running_loop()
    try:
        ok = await loop.run_in_executor(None, modem.send_sms, number, text)
    except Exception as exc:  # noqa: BLE001
        logger.exception("向导测试短信异常")
        if hub is not None:
            hub.publish(
                {"type": "sms_out", "number": number, "text": text, "status": "error"}
            )
        return web.json_response({"ok": False, "error": str(exc)}, status=500)
    if hub is not None:
        hub.publish(
            {
                "type": "sms_out",
                "number": number,
                "text": text,
                "status": "sent" if ok else "failed",
            }
        )
    token_box[0] = None
    return web.json_response({"ok": bool(ok)})


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
