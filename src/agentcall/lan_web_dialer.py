"""LAN-only browser dialer POC.

This module intentionally stays separate from the normal AI call pipeline.  It
proves one thing: a phone browser on the same LAN can provide the human audio
while the Mac/Dongle places the real cellular call.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from aiohttp import WSMsgType, web

from .audio_bridge import (
    FfmpegAudioBridge,
    ModemAudioBridge,
    SerialPcmAudioBridge,
    create_audio_bridge,
    resample_pcm,
)

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "web" / "static"

MIN_BROWSER_SAMPLE_RATE = 8000
MAX_BROWSER_SAMPLE_RATE = 96000
MAX_BROWSER_PCM_BYTES = 384_000
_PHONE_NUMBER_RE = re.compile(r"\+?[0-9*#]{1,32}")


AudioBridge = ModemAudioBridge | SerialPcmAudioBridge | FfmpegAudioBridge
BridgeFactory = Callable[..., AudioBridge]
Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]


class LanDialerError(Exception):
    """Base exception for the LAN dialer POC."""


class LanDialerValidationError(LanDialerError):
    """Raised when an untrusted browser payload is invalid."""


class LanDialerBusyError(LanDialerError):
    """Raised when a second browser tries to place a call while one is active."""


class ModemLike(Protocol):
    def dial(self, number: str) -> str: ...

    def initialize_for_voice(self, audio_mode: str = "uac") -> None: ...

    def hangup(self) -> None: ...

    def is_call_connected(self) -> bool: ...

    def send_dtmf(self, digits: str) -> bool: ...

    def pcm_ready(self) -> bool: ...


@dataclass(frozen=True)
class LanDialerSettings:
    audio_mode: str = "uac"
    audio_keyword: str = "Interface"
    pcm_port: str | None = None
    pcm_baudrate: int = 921600
    tx_gain: float = 1.0
    connect_timeout_seconds: float = 45.0
    modem_command_timeout_seconds: float = 12.0


@dataclass(frozen=True)
class StartPayload:
    number: str
    sample_rate: int


def _validate_number(number: str) -> str:
    number = (number or "").strip()
    if not number:
        raise LanDialerValidationError("号码不能为空")
    if not _PHONE_NUMBER_RE.fullmatch(number):
        raise LanDialerValidationError(f"号码格式不合法: {number}")
    return number


def _validate_sample_rate(sample_rate: object) -> int:
    if isinstance(sample_rate, bool):
        raise LanDialerValidationError("sampleRate 必须是整数")
    if isinstance(sample_rate, int):
        value = sample_rate
    elif isinstance(sample_rate, float) and sample_rate.is_integer():
        value = int(sample_rate)
    elif isinstance(sample_rate, str):
        try:
            value = int(sample_rate)
        except ValueError:
            raise LanDialerValidationError("sampleRate 必须是整数") from None
    else:
        raise LanDialerValidationError("sampleRate 必须是整数") from None
    if not MIN_BROWSER_SAMPLE_RATE <= value <= MAX_BROWSER_SAMPLE_RATE:
        raise LanDialerValidationError(
            f"sampleRate 必须在 {MIN_BROWSER_SAMPLE_RATE}..{MAX_BROWSER_SAMPLE_RATE}"
        )
    return value


def parse_start_payload(payload: dict[str, object]) -> StartPayload:
    if payload.get("type") != "start":
        raise LanDialerValidationError("第一条消息必须是 start")
    return StartPayload(
        number=_validate_number(str(payload.get("number") or "")),
        sample_rate=_validate_sample_rate(payload.get("sampleRate")),
    )


def _pcm_peak_abs(pcm: bytes) -> int:
    if len(pcm) < 2:
        return 0
    peak = 0
    for idx in range(0, len(pcm) - 1, 2):
        sample = int.from_bytes(pcm[idx:idx + 2], "little", signed=True)
        peak = max(peak, abs(sample))
    return peak


class LanDialerCall:
    """One browser-controlled cellular call."""

    def __init__(
        self,
        modem: ModemLike,
        *,
        settings: LanDialerSettings,
        bridge_factory: BridgeFactory = create_audio_bridge,
    ) -> None:
        self.modem = modem
        self.settings = settings
        self.bridge_factory = bridge_factory
        self.bridge: AudioBridge | None = None
        self.active = False
        self._ended = False
        self._modem_timed_out = False
        self._browser_pcm_bytes = 0
        self._browser_non_silent_bytes = 0
        self._modem_pcm_bytes = 0
        self._last_stats_at = time.monotonic()

    async def begin(self, number: str) -> None:
        number = _validate_number(number)
        await self._run_modem_command(self.modem.dial, number)
        if not await self._wait_connected():
            await self.aend()
            raise LanDialerError("外呼未接通或等待接通超时")
        await self._run_modem_command(self.modem.initialize_for_voice, self.settings.audio_mode)
        bridge = self.bridge_factory(
            mode=self.settings.audio_mode,
            device_keyword=self.settings.audio_keyword,
            pcm_port=self.settings.pcm_port,
            pcm_baudrate=self.settings.pcm_baudrate,
            tx_gain=self.settings.tx_gain,
        )
        if isinstance(bridge, SerialPcmAudioBridge):
            bridge.set_ready_check(self.modem.pcm_ready)
        await asyncio.to_thread(bridge.start)
        self.bridge = bridge
        self.active = True
        self._ended = False

    async def _run_modem_command(self, command: Callable[..., object], *args: object) -> object:
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(command, *args),
                timeout=self.settings.modem_command_timeout_seconds,
            )
        except TimeoutError:
            self._modem_timed_out = True
            close = getattr(self.modem, "close", None)
            if callable(close):
                await asyncio.to_thread(close)
            raise LanDialerError("模组指令超时，请检查 Dongle USB 桥是否在线") from None

    async def _wait_connected(self) -> bool:
        deadline = asyncio.get_running_loop().time() + self.settings.connect_timeout_seconds
        while asyncio.get_running_loop().time() < deadline:
            if self.modem.is_call_connected():
                return True
            await asyncio.sleep(0.05)
        return False

    def accept_browser_pcm(self, pcm_browser: bytes, *, browser_sample_rate: int) -> None:
        if not self.active or self.bridge is None:
            return
        if len(pcm_browser) > MAX_BROWSER_PCM_BYTES:
            raise LanDialerValidationError("音频帧过大")
        browser_sample_rate = _validate_sample_rate(browser_sample_rate)
        self._browser_pcm_bytes += len(pcm_browser)
        pcm_8k = resample_pcm(pcm_browser, browser_sample_rate, 8000)
        if hasattr(self.bridge, "amplify_for_modem"):
            pcm_8k = self.bridge.amplify_for_modem(pcm_8k)  # type: ignore[attr-defined]
        if pcm_8k:
            if _pcm_peak_abs(pcm_8k) > 300:
                self._browser_non_silent_bytes += len(pcm_8k)
            self.bridge.write_modem_chunks([pcm_8k])
        self._log_audio_stats()

    def read_modem_pcm(self) -> bytes:
        if not self.active or self.bridge is None:
            return b""
        pcm = self.bridge.read_modem_chunk()
        self._modem_pcm_bytes += len(pcm)
        self._log_audio_stats()
        return pcm

    def _log_audio_stats(self) -> None:
        now = time.monotonic()
        if now - self._last_stats_at < 5:
            return
        logger.info(
            "LAN POC 音频统计: phone_mic=%s bytes, phone_mic_non_silent=%s bytes, "
            "modem_to_phone=%s bytes",
            self._browser_pcm_bytes,
            self._browser_non_silent_bytes,
            self._modem_pcm_bytes,
        )
        self._last_stats_at = now

    def end(self) -> None:
        if self._ended:
            return
        self._ended = True
        self.active = False
        bridge = self.bridge
        self.bridge = None
        if bridge is not None:
            try:
                bridge.stop()
            except Exception as exc:  # noqa: BLE001
                logger.warning("关闭 LAN POC 音频桥失败: %s", exc)
        try:
            self.modem.hangup()
        except Exception as exc:  # noqa: BLE001
            logger.warning("挂断 LAN POC 通话失败: %s", exc)

    async def aend(self) -> None:
        if self._ended:
            return
        self._ended = True
        self.active = False
        bridge = self.bridge
        self.bridge = None
        if bridge is not None:
            try:
                await asyncio.to_thread(bridge.stop)
            except Exception as exc:  # noqa: BLE001
                logger.warning("关闭 LAN POC 音频桥失败: %s", exc)
        if self._modem_timed_out:
            logger.warning("跳过 LAN POC 挂断：模组指令已超时，已关闭连接等待重启")
            return
        try:
            await self._run_modem_command(self.modem.hangup)
        except Exception as exc:  # noqa: BLE001
            logger.warning("挂断 LAN POC 通话失败: %s", exc)


class LanDialerController:
    def __init__(
        self,
        modem: ModemLike,
        *,
        settings: LanDialerSettings,
        bridge_factory: BridgeFactory = create_audio_bridge,
    ) -> None:
        self.modem = modem
        self.settings = settings
        self.bridge_factory = bridge_factory
        self._lock = asyncio.Lock()
        self._active_call: LanDialerCall | None = None

    async def reserve_call(self) -> LanDialerCall:
        async with self._lock:
            if self._active_call is not None:
                raise LanDialerBusyError("已有通话进行中")
            call = LanDialerCall(
                self.modem,
                settings=self.settings,
                bridge_factory=self.bridge_factory,
            )
            self._active_call = call
            return call

    def release_call(self, call: LanDialerCall) -> None:
        if self._active_call is call:
            self._active_call = None

    async def handle_ws(self, ws: web.WebSocketResponse) -> None:
        call: LanDialerCall | None = None
        downlink_task: asyncio.Task[None] | None = None
        start: StartPayload | None = None
        try:
            start = await _receive_start(ws)
            call = await self.reserve_call()
            await _send_json(ws, {"type": "status", "status": "dialing"})
            await call.begin(start.number)
            await _send_json(ws, {"type": "status", "status": "connected"})
            downlink_task = asyncio.create_task(_send_downlink(ws, call))
            async for msg in ws:
                if msg.type == WSMsgType.BINARY:
                    call.accept_browser_pcm(
                        bytes(msg.data),
                        browser_sample_rate=start.sample_rate,
                    )
                elif msg.type == WSMsgType.TEXT:
                    data = _loads_json_object(msg.data)
                    if data.get("type") == "hangup":
                        break
                    if data.get("type") == "dtmf":
                        digits = str(data.get("digits") or "")
                        if not digits or any(ch not in "0123456789*#" for ch in digits):
                            await _send_json(ws, {"type": "error", "error": "DTMF 只允许 0-9、*、#"})
                            continue
                        self.modem.send_dtmf(digits)
                elif msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.ERROR):
                    break
        except LanDialerError as exc:
            await _send_json(ws, {"type": "error", "error": str(exc)})
        except Exception:  # noqa: BLE001
            logger.exception("LAN POC websocket 异常")
            await _send_json(ws, {"type": "error", "error": "通话异常"})
        finally:
            if downlink_task is not None:
                downlink_task.cancel()
                try:
                    await downlink_task
                except asyncio.CancelledError:
                    pass
            if call is not None:
                await call.aend()
                self.release_call(call)
            if not ws.closed:
                await _send_json(ws, {"type": "status", "status": "ended"})
                await ws.close()

    def status(self) -> dict[str, object]:
        return {"active": self._active_call is not None}


CONTROLLER_KEY: web.AppKey[LanDialerController] = web.AppKey(
    "lan_dialer_controller",
    LanDialerController,
)


def _loads_json_object(raw: str) -> dict[str, object]:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        raise LanDialerValidationError("消息不是合法 JSON") from None
    if not isinstance(data, dict):
        raise LanDialerValidationError("消息必须是 JSON 对象")
    return data


async def _receive_start(ws: web.WebSocketResponse) -> StartPayload:
    msg = await ws.receive(timeout=10)
    if msg.type != WSMsgType.TEXT:
        raise LanDialerValidationError("第一条消息必须是 JSON 文本")
    return parse_start_payload(_loads_json_object(msg.data))


async def _send_downlink(ws: web.WebSocketResponse, call: LanDialerCall) -> None:
    while call.active and not ws.closed:
        if not call.modem.is_call_connected():
            await _send_json(ws, {"type": "status", "status": "ended"})
            await ws.close()
            break
        pcm = call.read_modem_pcm()
        if pcm:
            await ws.send_bytes(pcm)
        else:
            await asyncio.sleep(0.01)


async def _send_json(ws: web.WebSocketResponse, payload: dict[str, object]) -> None:
    if not ws.closed:
        await ws.send_json(payload)


def _auth_middleware(token: str):
    @web.middleware
    async def middleware(request: web.Request, handler: Handler) -> web.StreamResponse:
        supplied = request.query.get("token", "")
        if not supplied:
            authorization = request.headers.get("Authorization", "")
            if authorization.startswith("Bearer "):
                supplied = authorization[len("Bearer "):].strip()
        if not secrets.compare_digest(supplied, token):
            return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
        return await handler(request)

    return middleware


def build_lan_dialer_app(controller: LanDialerController, *, token: str) -> web.Application:
    app = web.Application(middlewares=[_auth_middleware(token)])
    app[CONTROLLER_KEY] = controller
    app.router.add_get("/", _index)
    app.router.add_get("/api/status", _status)
    app.router.add_get("/ws/call", _ws_call)
    return app


async def _index(request: web.Request) -> web.StreamResponse:
    return web.FileResponse(
        STATIC_DIR / "lan_dialer.html",
        headers={"Cache-Control": "no-cache, must-revalidate"},
    )


async def _status(request: web.Request) -> web.Response:
    controller = request.app[CONTROLLER_KEY]
    return web.json_response({"ok": True, **controller.status()})


async def _ws_call(request: web.Request) -> web.WebSocketResponse:
    ws = web.WebSocketResponse(heartbeat=20, max_msg_size=MAX_BROWSER_PCM_BYTES + 1024)
    await ws.prepare(request)
    controller = request.app[CONTROLLER_KEY]
    await controller.handle_ws(ws)
    return ws
