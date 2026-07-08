"""事件枢纽：把模组/Agent 的实时事件线程安全地广播给网页 WebSocket。"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

from aiohttp import web

logger = logging.getLogger(__name__)

# 需要持久化到磁盘的事件类型（短信收发记录）。
_PERSISTED_TYPES = {"sms_in", "sms_out"}


class EventHub:
    """线程安全的事件发布/订阅中心。

    模组回调运行在子线程，网页 WebSocket 运行在 asyncio loop 线程，
    通过 ``loop.call_soon_threadsafe`` 把广播动作调度回 loop 线程执行。
    """

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        history_limit: int = 500,
        store_path: str | Path | None = None,
    ) -> None:
        self._loop = loop
        self._clients: set[web.WebSocketResponse] = set()
        self._history: deque[dict[str, Any]] = deque(maxlen=history_limit)
        self._lock = threading.Lock()
        # 推送 task 需持强引用直至完成，否则可能被 GC 提前回收导致 WS 丢事件。
        self._tasks: set[asyncio.Task[None]] = set()
        # 实时旁听：下行 PCM 二进制帧的订阅端（与 JSON 事件端分开，无监听者时零成本）。
        self._audio_clients: set[web.WebSocketResponse] = set()
        self._audio_tasks: set[asyncio.Task[None]] = set()
        self._audio_rate = 24000
        self._store_path = Path(store_path) if store_path else None
        if self._store_path:
            self._load_persisted()

    # ---- 订阅端（WebSocket）----

    def register(self, ws: web.WebSocketResponse) -> None:
        self._clients.add(ws)

    def unregister(self, ws: web.WebSocketResponse) -> None:
        self._clients.discard(ws)

    # ---- 实时旁听（二进制音频）----

    @property
    def audio_rate(self) -> int:
        return self._audio_rate

    def set_audio_rate(self, rate: int) -> None:
        if rate and rate > 0:
            self._audio_rate = int(rate)

    def register_audio(self, ws: web.WebSocketResponse) -> None:
        self._audio_clients.add(ws)

    def unregister_audio(self, ws: web.WebSocketResponse) -> None:
        self._audio_clients.discard(ws)

    def broadcast_audio(self, pcm: bytes) -> None:
        """把下行 PCM 帧广播给旁听端（任意线程调用，非阻塞、满即丢）。

        无旁听端时立即返回（零成本）；在途发送积压时丢帧不堆积（旁听可丢）。
        """
        if not self._audio_clients or not pcm:
            return
        try:
            self._loop.call_soon_threadsafe(self._broadcast_audio, pcm)
        except RuntimeError:
            pass

    def _broadcast_audio(self, pcm: bytes) -> None:
        # 丢帧保护：在途发送远超监听端数说明浏览器跟不上，丢这帧而非堆积。
        if len(self._audio_tasks) > len(self._audio_clients) * 4:
            return
        for ws in list(self._audio_clients):
            task = asyncio.create_task(self._safe_send_audio(ws, pcm))
            self._audio_tasks.add(task)
            task.add_done_callback(self._audio_tasks.discard)

    async def _safe_send_audio(self, ws: web.WebSocketResponse, pcm: bytes) -> None:
        try:
            await ws.send_bytes(pcm)
        except (ConnectionResetError, RuntimeError):
            self.unregister_audio(ws)
        except Exception as exc:  # noqa: BLE001
            logger.debug("推送音频失败: %s", exc)
            self.unregister_audio(ws)

    def history(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._history)

    # ---- 发布端（任意线程）----

    def publish(self, event: dict[str, Any]) -> None:
        event.setdefault("ts", time.time())
        with self._lock:
            self._history.append(event)
            if self._store_path and event.get("type") in _PERSISTED_TYPES:
                self._persist_locked()
        try:
            self._loop.call_soon_threadsafe(self._broadcast, event)
        except RuntimeError:
            # loop 已关闭（服务正在退出），忽略。
            pass

    def _broadcast(self, event: dict[str, Any]) -> None:
        # 经 call_soon_threadsafe 调度，始终在 loop 线程内执行，故操作 _tasks 无需加锁。
        for ws in list(self._clients):
            task = asyncio.create_task(self._safe_send(ws, event))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def _safe_send(self, ws: web.WebSocketResponse, event: dict[str, Any]) -> None:
        try:
            await ws.send_json(event)
        except (ConnectionResetError, RuntimeError):
            self.unregister(ws)
        except Exception as exc:  # noqa: BLE001
            logger.debug("推送事件失败: %s", exc)
            self.unregister(ws)

    # ---- 持久化 ----

    def _load_persisted(self) -> None:
        assert self._store_path is not None
        if not self._store_path.exists():
            return
        try:
            data = json.loads(self._store_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("读取短信历史失败: %s", exc)
            return
        if isinstance(data, list):
            for event in data:
                if isinstance(event, dict):
                    self._repair_sms_event(event)
                    self._history.append(event)

    @staticmethod
    def _repair_sms_event(event: dict[str, Any]) -> None:
        """修正历史里遗留的未解码 PDU 短信（sender 为空且正文是 PDU 十六进制）。"""
        if event.get("type") != "sms_in" or event.get("sender"):
            return
        text = event.get("text")
        if not isinstance(text, str):
            return
        try:
            from .modem import _looks_like_pdu, parse_sms_pdu
        except Exception:  # noqa: BLE001
            return
        if not _looks_like_pdu(text):
            return
        parsed = parse_sms_pdu(text)
        if parsed is not None:
            sender, _timestamp, body = parsed
            event["sender"] = sender
            event["text"] = body

    def _persist_locked(self) -> None:
        assert self._store_path is not None
        persisted = [
            e for e in self._history if e.get("type") in _PERSISTED_TYPES
        ]
        try:
            self._store_path.parent.mkdir(parents=True, exist_ok=True)
            self._store_path.write_text(
                json.dumps(persisted, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError as exc:
            logger.warning("写入短信历史失败: %s", exc)
