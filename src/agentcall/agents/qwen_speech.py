"""DashScope qwen3 realtime speech transport for the local three-stage pipeline.

The local provider is threaded, so both clients use the synchronous
``websockets.sync.client`` transport and are only ever driven from one worker
thread each (ASR worker / TTS worker). Connections are reused across turns and
warmed at session start; a lost connection is retried once where the caller can
afford it, otherwise the pipeline falls back to its local models.

Security: the API key stays in memory. Logs never contain the key or the spoken
text; only provider names, event types and timing are emitted.
"""

from __future__ import annotations

import base64
import json
import logging
import threading
import time
from collections.abc import Callable
from typing import Any

from websockets.exceptions import ConnectionClosed, InvalidStatus, WebSocketException
from websockets.sync.client import connect

logger = logging.getLogger(__name__)

_HOST = "dashscope.aliyuncs.com"
_REALTIME = f"wss://{_HOST}/api-ws/v1/realtime"

_OPEN_TIMEOUT = 12.0
_CLOSE_TIMEOUT = 3.0
_RECV_TIMEOUT = 0.5
_SESSION_READY_TIMEOUT = 2.0
_TTS_DEADLINE = 30.0
_ASR_DEADLINE = 20.0

# 网络级故障可以换连接重试一次；鉴权/协议错误（如 InvalidStatus）不是瞬态。
_RETRYABLE = (OSError, TimeoutError, ConnectionClosed)

# 保活：websockets 自带 keepalive ping；服务端若仍按空闲时长关闭，超过闲置阈值后
# ensure_ready() 会在可能要说话前主动换新连接，真正合成时不付建连开销。
_PING_INTERVAL = 20.0
_PING_TIMEOUT = 20.0
_IDLE_RECONNECT_AFTER = 30.0


class QwenSpeechError(RuntimeError):
    """云端语音鉴权或协议错误；不重试，由调用方回落本地。"""


class _Cancelled(Exception):
    """TTS 生成被调用方取消（barge-in），不算网络故障。"""


def dashscope_api_key() -> str:
    """读取 Key 到内存；缺失时抛错，绝不落日志。"""
    from .. import config

    key = config.get_str("DASHSCOPE_API_KEY").strip()
    if not key:
        raise QwenSpeechError("缺少 DASHSCOPE_API_KEY")
    return key


def _endpoint(model: str) -> str:
    return f"{_REALTIME}?model={model}"


def _headers(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def _decode_pcm(delta: str | None) -> bytes:
    if not delta:
        return b""
    return base64.b64decode(delta)


def _error_message(obj: dict[str, Any]) -> str:
    error = obj.get("error")
    if isinstance(error, dict):
        fields = {k: error[k] for k in ("code", "message") if isinstance(error.get(k), (str, int, float))}
        if fields:
            return " ".join(f"{k}={v}" for k, v in fields.items())[:200]
    if isinstance(error, str):
        return error[:200]
    return "server error"


class _SyncWs:
    """串行 WS 连接 + session 配置；连接生命周期由子类管理。"""

    def __init__(self, model: str, key: str) -> None:
        self._model = model
        self._key = key
        self._ws: Any = None
        self._lock = threading.Lock()
        self._last_used_at = 0.0
        self._connecting = False
        self._closing = False
        self._ready_thread: threading.Thread | None = None
        self._label = "qwen"

    def _session_update(self) -> dict[str, Any]:
        raise NotImplementedError

    def _connect(self) -> Any:
        try:
            ws = connect(
                _endpoint(self._model),
                additional_headers=_headers(self._key),
                open_timeout=_OPEN_TIMEOUT,
                close_timeout=_CLOSE_TIMEOUT,
                ping_interval=_PING_INTERVAL,
                ping_timeout=_PING_TIMEOUT,
            )
        except InvalidStatus as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            raise QwenSpeechError(f"云端语音鉴权/握手失败 HTTP {status}") from exc
        except (OSError, TimeoutError, WebSocketException) as exc:
            raise QwenSpeechError(f"云端语音连接失败: {type(exc).__name__}") from exc
        return ws

    @staticmethod
    def _safe_close(ws: Any) -> None:
        try:
            ws.close()
        except Exception:  # noqa: BLE001 - 关闭失败不影响后续
            pass

    def _touch(self) -> None:
        self._last_used_at = time.monotonic()

    @staticmethod
    def _is_open(ws: Any) -> bool:
        state = getattr(ws, "state", None)
        if state is None:
            return True
        try:
            return int(state) == 1
        except (TypeError, ValueError):
            return True

    def _discard(self) -> None:
        ws, self._ws = self._ws, None
        if ws is not None:
            self._safe_close(ws)

    def _establish(self, ws: Any) -> None:
        ws.send(json.dumps({"type": "session.update", "session": self._session_update()}))
        self._await_session_ready(ws)

    def _await_session_ready(self, ws: Any) -> None:
        """session.update 后读走初始事件，直到 session.updated 或静默超时。"""
        for _ in range(6):
            try:
                message = ws.recv(timeout=_SESSION_READY_TIMEOUT)
            except TimeoutError:
                return
            except ConnectionClosed as exc:
                raise QwenSpeechError("云端语音会话未就绪即断开") from exc
            if isinstance(message, bytes):
                continue
            try:
                event = json.loads(message)
            except json.JSONDecodeError:
                continue
            if event.get("type") == "session.updated":
                return
            if event.get("type") == "error":
                raise QwenSpeechError(_error_message(event))

    def _ensure_connected(self) -> Any:
        """建立连接并安装；连接在锁外建立，安装时若已有新连接则放弃本次。"""
        with self._lock:
            ws = self._ws
            if ws is not None:
                return ws
        ws = self._connect()
        try:
            self._establish(ws)
        except Exception:
            self._safe_close(ws)
            raise
        with self._lock:
            existing = self._ws
            if existing is not None:
                self._safe_close(ws)
                return existing
            if self._closing:
                self._safe_close(ws)
                raise QwenSpeechError("连接已关闭")
            self._ws = ws
            self._touch()
            return ws

    def ensure_ready(self) -> None:
        """异步预连；VAD 起话 / LLM 首字时调用，不阻塞调用线程。"""
        with self._lock:
            if self._closing:
                return
            ws = self._ws
            if ws is not None and self._is_open(ws) and time.monotonic() - self._last_used_at <= _IDLE_RECONNECT_AFTER:
                return
            if self._connecting:
                return
            if ws is None:
                reason = "missing"
            elif self._is_open(ws):
                reason = "idle"
            else:
                reason = "closed"
            if ws is not None:
                self._discard()
            self._connecting = True
            thread = threading.Thread(
                target=self._ready_worker, args=(reason,), name=f"{self._label}-ready", daemon=True,
            )
            self._ready_thread = thread
        thread.start()

    def _ready_worker(self, reason: str) -> None:
        started = time.monotonic()
        try:
            self._ensure_connected()
            logger.info(
                "[%s] ensure_ready reconnected reason=%s duration_ms=%.1f",
                self._label, reason, (time.monotonic() - started) * 1000,
            )
        except Exception as exc:  # noqa: BLE001 - 真正合成时还会兜底重连
            logger.warning(
                "[%s] ensure_ready reconnect failed reason=%s error=%s",
                self._label, reason, type(exc).__name__,
            )
        finally:
            with self._lock:
                self._connecting = False
                self._ready_thread = None

    def close(self) -> None:
        with self._lock:
            self._closing = True
            self._discard()
            thread = self._ready_thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=_CLOSE_TIMEOUT)


class QwenTtsClient(_SyncWs):
    """``qwen3-tts-flash-realtime`` 8k PCM 流式合成，复用同一连接。"""

    def __init__(self, model: str, voice: str, key: str, *, sample_rate: int = 8000) -> None:
        super().__init__(model, key)
        self._voice = voice
        self._sample_rate = sample_rate
        self._label = "qwen_tts"

    def _session_update(self) -> dict[str, Any]:
        return {
            "voice": self._voice,
            "mode": "commit",
            "response_format": "pcm",
            "sample_rate": self._sample_rate,
            "language_type": "Chinese",
        }

    def warmup(self) -> None:
        """会话开始就建好连接，首句不付建连开销。失败仅告警，首句再兜底重连。"""
        started = time.monotonic()
        try:
            self._ensure_connected()
        except Exception as exc:  # noqa: BLE001 - warmup 失败不致命
            logger.warning("[qwen_tts] warmup failed error=%s", type(exc).__name__)
            return
        logger.info("[qwen_tts] connected duration_ms=%.1f", (time.monotonic() - started) * 1000)

    def synthesize_stream(
        self, text: str, on_audio: Callable[[bytes], None], should_stop: Callable[[], bool],
    ) -> bool:
        """流式下发音频；True=完整，False=被取消。断线重连一次后抛错回落。"""
        for attempt in (0, 1):
            ws = self._ensure_connected()
            try:
                done = self._generate(ws, text, on_audio, should_stop)
            except _Cancelled:
                self._discard()
                return False
            except _RETRYABLE as exc:
                self._discard()
                if attempt == 0:
                    logger.warning("[qwen_tts] connection lost, reconnecting once error=%s", type(exc).__name__)
                    continue
                raise QwenSpeechError(f"云端 TTS 流式失败: {type(exc).__name__}") from exc
            except QwenSpeechError:
                raise
            self._touch()
            return done
        return False

    def _generate(
        self, ws: Any, text: str, on_audio: Callable[[bytes], None], should_stop: Callable[[], bool],
    ) -> bool:
        ws.send(json.dumps({"type": "input_text_buffer.append", "text": text}))
        ws.send(json.dumps({"type": "input_text_buffer.commit"}))
        # 一次合成必须读到本次响应的 response.done 才算完成；audio.done 只是音频段边界。
        # 用 response id 过滤掉上一响应可能残留的 content_part/output_item/response.done。
        response_id: str | None = None
        audio_bytes = 0
        deadline = time.monotonic() + _TTS_DEADLINE
        while time.monotonic() < deadline:
            if should_stop():
                raise _Cancelled()
            try:
                message = ws.recv(timeout=_RECV_TIMEOUT)
            except TimeoutError:
                continue
            if isinstance(message, bytes):
                on_audio(message)
                audio_bytes += len(message)
                continue
            try:
                event = json.loads(message)
            except json.JSONDecodeError:
                continue
            event_type = event.get("type")
            if event_type == "response.created":
                response_id = (event.get("response") or {}).get("id") or response_id
            elif event_type == "response.audio.delta":
                delta_id = event.get("response_id")
                if response_id is None:
                    response_id = delta_id
                if response_id is not None and delta_id is not None and delta_id != response_id:
                    continue
                pcm = _decode_pcm(event.get("delta"))
                if pcm:
                    on_audio(pcm)
                    audio_bytes += len(pcm)
            elif event_type == "response.audio.done":
                # 不是完成点，继续读到本次 response.done。
                continue
            elif event_type == "response.done":
                done_id = (event.get("response") or {}).get("id")
                if response_id is None:
                    # 还没进入本次响应：这是上一响应残留的完成事件。
                    continue
                if done_id is not None and done_id != response_id:
                    continue
                if text and audio_bytes == 0:
                    raise QwenSpeechError("云端 TTS 返回空音频")
                return True
            elif event_type == "error":
                raise QwenSpeechError(_error_message(event))
        raise QwenSpeechError("云端 TTS 等待音频超时")

    def synthesize(self, text: str) -> bytes:
        """整句合成（非流式回落/开场预合成用），攒完再返回。"""
        chunks: list[bytes] = []
        done = self.synthesize_stream(text, chunks.append, lambda: False)
        if not done:
            raise QwenSpeechError("云端 TTS 合成未完成")
        return b"".join(chunks)


class QwenAsrClient(_SyncWs):
    """``qwen3-asr-flash-realtime`` 手动 commit 模式；判停由本地 VAD 决定。"""

    def __init__(self, model: str, key: str, *, sample_rate: int = 8000) -> None:
        super().__init__(model, key)
        self._sample_rate = sample_rate
        self._label = "qwen_asr"

    def _session_update(self) -> dict[str, Any]:
        # 必须显式关掉 turn_detection：不设时服务端默认 server_vad 自行判停，
        # 手动 commit 会报 “no invalid audio stream”。本地 VAD/判停决定轮末。
        return {
            "input_audio_format": "pcm",
            "sample_rate": self._sample_rate,
            "turn_detection": None,
            "input_audio_transcription": {"language": "zh"},
        }

    def warmup(self) -> None:
        started = time.monotonic()
        try:
            self._ensure_connected()
        except Exception as exc:  # noqa: BLE001 - warmup 失败不致命
            logger.warning("[qwen_asr] warmup failed error=%s", type(exc).__name__)
            return
        logger.info("[qwen_asr] connected duration_ms=%.1f", (time.monotonic() - started) * 1000)

    def _send_append(self, ws: Any, pcm8k: bytes) -> None:
        ws.send(json.dumps({"type": "input_audio_buffer.append", "audio": base64.b64encode(pcm8k).decode()}))

    def append(self, pcm8k: bytes) -> None:
        if not pcm8k:
            return
        ws = self._ensure_connected()
        try:
            self._send_append(ws, pcm8k)
        except _RETRYABLE as exc:
            # 断线时丢弃坏连接，下一轮 append 会重新建连；本轮由管线回落本地。
            self._discard()
            raise QwenSpeechError(f"云端 ASR 送段失败: {type(exc).__name__}") from exc
        self._touch()

    def commit(self) -> str:
        ws = self._ensure_connected()
        try:
            ws.send(json.dumps({"type": "input_audio_buffer.commit"}))
        except _RETRYABLE as exc:
            self._discard()
            raise QwenSpeechError(f"云端 ASR commit 失败: {type(exc).__name__}") from exc
        self._touch()
        deadline = time.monotonic() + _ASR_DEADLINE
        while time.monotonic() < deadline:
            try:
                message = ws.recv(timeout=_RECV_TIMEOUT)
            except TimeoutError:
                continue
            except _RETRYABLE as exc:
                self._discard()
                raise QwenSpeechError(f"云端 ASR 等待 final 失败: {type(exc).__name__}") from exc
            if isinstance(message, bytes):
                continue
            try:
                event = json.loads(message)
            except json.JSONDecodeError:
                continue
            event_type = event.get("type")
            if event_type == "conversation.item.input_audio_transcription.completed":
                self._touch()
                return str(event.get("transcript") or "").strip()
            if event_type == "error":
                raise QwenSpeechError(_error_message(event))
        raise QwenSpeechError("云端 ASR 等待 final 超时")

    def clear(self) -> None:
        if self._ws is None:
            return
        try:
            self._ws.send(json.dumps({"type": "input_audio_buffer.clear"}))
        except Exception:  # noqa: BLE001 - 清理是尽力而为
            self._discard()

    def one_shot(self, pcm8k: bytes) -> str:
        self.append(pcm8k)
        return self.commit()
