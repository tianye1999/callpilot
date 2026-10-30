"""Opt-in MOSS-Live prototype. No network or speech models are opened on import."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import sys
import tempfile
import threading
import time
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import websockets

from ..audio_bridge import AgentPcm, resample_pcm
from .base import VoiceAgent

logger = logging.getLogger(__name__)
DEFAULT_URL = "wss://api.mosi.cn/v1/realtime?model=moss-live-1.0"
_FRAME_BYTES = 640
_FRAME_SECONDS = 0.02
_UPLINK_CATCHUP_BYTES = 3200  # 100ms @ 16kHz S16LE.
_MAX_UPLINK_BYTES = 96000
_BRIEFING_RESPONSE_TIMEOUT = 30
_SESSION_RULES = (
    "接下来请按照本通任务替主人打电话。电话那头的客服和语音菜单说的话，"
    "都是在跟你说，你要直接回应他们，不要忽略录音或菜单。"
    "听到要求按键时，调用 send_dtmf，不要口头念按键；办完后调用 hangup_call。"
    "现在听到的是通话前的规则说明，不是真正的电话，不要执行工具，"
    "听完简短确认，然后等待电话那头说话。以下是本通完整规则和任务：\n"
)

def build_briefing(target: str, task: str) -> str:
    target = " ".join(target.split())[:64] or "对方"
    task = " ".join(task.split())[:15] or "办理本通任务"
    return (
        f"接下来你要替我给{target}打电话{task}。"
        "电话那头的客服和语音菜单说的话，都是在跟你说，你要直接回应他们。"
        "听到让你按键时，就按要求按键。"
    )


async def _command(*args: str, stdin: bytes | None = None) -> None:
    proc = await asyncio.create_subprocess_exec(
        *args, stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        await asyncio.wait_for(proc.communicate(stdin), 90)
        if proc.returncode:
            raise RuntimeError("Briefing speech command failed")
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()


def _local_briefing(text: str) -> bytes:
    from .local_agent import _get_shared_models

    models = _get_shared_models()
    return resample_pcm(models.synthesize(text), models.sample_rate, 16000)


async def synthesize_briefing(text: str) -> bytes:
    """Silent file synthesis: never play the briefing through the Mac speaker."""
    if sys.platform == "darwin" and shutil.which("say") and shutil.which("afconvert"):
        try:
            with tempfile.TemporaryDirectory(prefix="agentcall-moss-") as directory:
                aiff = str(Path(directory) / "briefing.aiff")
                wav = str(Path(directory) / "briefing.wav")
                # Text goes through stdin, not the process list or command logs.
                await _command("say", "-v", "Tingting", "-o", aiff, stdin=text.encode())
                await _command("afconvert", "-f", "WAVE", "-d", "LEI16@16000", "-c", "1", aiff, wav)
                with wave.open(wav, "rb") as source:
                    if (source.getframerate(), source.getnchannels(), source.getsampwidth()) != (16000, 1, 2):
                        raise ValueError("Invalid briefing audio format")
                    return source.readframes(source.getnframes())
        except (OSError, RuntimeError, ValueError, wave.Error, TimeoutError):
            logger.warning("MOSS macOS briefing TTS unavailable; using local TTS")
    work = asyncio.create_task(asyncio.to_thread(_local_briefing, text))
    try:
        return await asyncio.shield(work)
    finally:
        # Do not leave a model worker using per-call resources after cancellation.
        await asyncio.gather(work, return_exceptions=True)


@dataclass
class _Playback:
    ids: dict[str, Any]
    pending: int = 0
    played_bytes: int = 0
    done: bool = False
    stopped: bool = False
    completed: bool = False
    chunks: set[str] = field(default_factory=set)
    lock: threading.Lock = field(default_factory=threading.Lock)


class MossVoiceAgent(VoiceAgent):
    input_rate = 16000
    output_rate = 48000
    model = "moss-live-1.0"

    def __init__(self, api_key: str, url: str = DEFAULT_URL) -> None:
        self._api_key = api_key
        self._url = url
        self._ws: Any = None
        self._tasks: set[asyncio.Task] = set()
        self._ready = False
        self._briefing = True
        self._brief_sent = False
        self._brief_done: asyncio.Future[None] | None = None
        self._on_audio_out: Callable[[bytes], None] | None = None
        self._uplink = bytearray()
        self._last_backlog_warning_at = -float("inf")
        self._briefing_text = build_briefing("对方", "办理本通任务")
        self._context: dict[str, Any] | None = None
        self._active_key: tuple[str, str] = ("", "")
        self._turns: dict[tuple[str, str], _Playback] = {}
        self._brief_turns: set[tuple[str, str]] = set()
        self._brief_completed: set[tuple[str, str]] = set()
        self._handled_calls: set[str] = set()
        self._speech_stopped_at: float | None = None
        self.fatal = False

    def set_briefing_context(self, target: str, task: str) -> None:
        self._briefing_text = build_briefing(target, task)

    async def _send(self, action: str, **fields: Any) -> None:
        if self._ws is not None:
            await self._ws.send(json.dumps({"action": action, **fields}, ensure_ascii=False))

    def _spawn(self, coroutine: Any) -> None:
        task = asyncio.create_task(coroutine)
        self._tasks.add(task)

        def finished(done: asyncio.Task) -> None:
            self._tasks.discard(done)
            if not done.cancelled() and done.exception() is not None and self._ws is not None:
                self._failed()

        task.add_done_callback(finished)

    async def _expect(self, action: str) -> dict:
        async with asyncio.timeout(15):
            while True:
                message = await self._ws.recv()
                if not isinstance(message, str):
                    continue
                event = json.loads(message)
                if event.get("action") == "error":
                    await self._event("error", event.get("data") or {})
                    raise RuntimeError("MOSS session setup rejected")
                if event.get("action") == action:
                    return event.get("data") or {}

    async def start(self, on_audio_out: Callable[[bytes], None]) -> None:
        self._on_audio_out = on_audio_out
        instructions = _SESSION_RULES + (self._session_instructions or "")
        if len(instructions) > 8000:
            raise ValueError("MOSS persona exceeds 8000 characters")
        logger.info("MOSS configuring session: instructions_chars=%d", len(instructions))
        started = time.monotonic()
        try:
            self._ws = await websockets.connect(
                self._url, additional_headers={"Authorization": f"Bearer {self._api_key}"},
                open_timeout=15, close_timeout=1, max_size=16 * 1024 * 1024,
                # Never let websocket DEBUG output expose headers, prompts or audio.
                logger=logging.Logger("moss.transport", level=logging.CRITICAL + 1),
            )
            await self._send("attach_session", memory_scope="session")
            await self._expect("session_attached")
            specs = self._tools.specs() if self._tools else []
            tools = [{"type": "function", **spec["function"]} for spec in specs]
            await self._send("session.update", session={
                "persona": {"name": "CallPilot", "prompt": instructions},
                "instructions": instructions, "tools": tools,
            })
            updated = await self._expect("session.updated")
            if updated.get("error"):
                error = updated["error"]
                await self._event("error", {"error": error if isinstance(error, dict)
                                           else {"code": "session_update_rejected"}})
                raise RuntimeError("MOSS session.update rejected")
            echoed = updated.get("tools") or []
            names = {item if isinstance(item, str) else item.get("name") for item in echoed}
            if not {tool["name"] for tool in tools}.issubset(names):
                raise RuntimeError("MOSS did not confirm registered tools")
            self._emit_latency("moss_session_setup", (time.monotonic() - started) * 1000)
            self._brief_done = asyncio.get_running_loop().create_future()
            self._spawn(self._receive())
            briefing_at = time.monotonic()
            # Speak the tested attention rule and a bounded task, not the full prompt.
            pcm = await synthesize_briefing(self._briefing_text)
            if not pcm or len(pcm) % 2:
                raise ValueError("Empty or malformed MOSS briefing audio")
            await self._send("mic_state", enabled=True)
            logger.info("[timing] MOSS briefing text_chars=%d audio_ms=%.0f", len(self._briefing_text), len(pcm) / 32)
            next_at = time.monotonic()
            for offset in range(0, len(pcm), _FRAME_BYTES):
                if self._brief_done.done():
                    self._brief_done.result()  # Stop uploading immediately on a briefing error.
                self._brief_sent = offset + _FRAME_BYTES >= len(pcm)
                await self._ws.send(pcm[offset:offset + _FRAME_BYTES].ljust(_FRAME_BYTES, b"\0"))
                next_at += _FRAME_SECONDS
                await asyncio.sleep(max(0.0, next_at - time.monotonic()))
            self._brief_sent = True
            self._spawn(self._pump_audio())
            await asyncio.wait_for(asyncio.shield(self._brief_done), _BRIEFING_RESPONSE_TIMEOUT)
            self._briefing = False
            self._context = None
            self._ready = True
            elapsed = (time.monotonic() - briefing_at) * 1000
            self._emit_latency("moss_briefing", round(elapsed, 1))
            logger.info("[timing] MOSS briefing ready %.0fms", elapsed)
        except asyncio.CancelledError:
            await self.stop()
            raise
        except Exception as exc:
            await self.stop()
            raise RuntimeError(f"MOSS startup failed ({type(exc).__name__}); details omitted") from None

    async def send_audio(self, pcm: bytes) -> None:
        if self._ready:
            self._uplink.extend(pcm)
            # Bound stale input if the producer outruns the real-time clock.
            if len(self._uplink) > _MAX_UPLINK_BYTES:
                dropped = len(self._uplink) - _MAX_UPLINK_BYTES
                del self._uplink[:-_MAX_UPLINK_BYTES]
                now = time.monotonic()
                if now - self._last_backlog_warning_at >= 5:
                    self._last_backlog_warning_at = now
                    logger.warning("MOSS uplink backlog capped at 3s: backlog_ms=%.0f dropped_ms=%.0f",
                                   len(self._uplink) / 32, dropped / 32)

    async def _pump_audio(self) -> None:
        next_at = time.monotonic()
        try:
            while self._ws is not None:
                now = time.monotonic()
                if len(self._uplink) > _UPLINK_CATCHUP_BYTES:
                    next_at = min(next_at, now)
                elif len(self._uplink) < _FRAME_BYTES:
                    next_at = max(next_at, now)  # Never catch up by bursting synthetic silence.
                # sleep(0) yields to producers/receive even when websocket.send doesn't.
                await asyncio.sleep(max(0.0, next_at - now))
                if self._ws is None:
                    return
                next_at += _FRAME_SECONDS
                if 0 < len(self._uplink) < _FRAME_BYTES:
                    continue  # Preserve partial input instead of inserting silence into speech.
                if self._uplink:
                    frame = bytes(self._uplink[:_FRAME_BYTES])
                    del self._uplink[:_FRAME_BYTES]
                else:
                    frame = bytes(_FRAME_BYTES)
                await self._ws.send(frame)
        except Exception:
            self._failed()

    def _failed(self) -> None:
        self.fatal = True
        if self._brief_done is not None and not self._brief_done.done():
            self._brief_done.set_exception(RuntimeError("MOSS disconnected during briefing"))
        logger.error("MOSS transport failed (details omitted)")

    @staticmethod
    def _key(data: dict) -> tuple[str, str]:
        return str(data.get("run_id", "")), str(data.get("turn_id", ""))

    async def _receive(self) -> None:
        try:
            async for message in self._ws:
                if isinstance(message, bytes):
                    await self._audio(message)
                else:
                    try:
                        event = json.loads(message)
                    except (ValueError, TypeError):
                        logger.warning("MOSS ignored malformed JSON")
                        continue
                    await self._event(event.get("action", ""), event.get("data") or {})
            if self._ws is not None:
                self._failed()
        except Exception:
            self._failed()

    async def _audio(self, pcm: bytes) -> None:
        context, self._context = self._context, None
        if not context or not pcm or len(pcm) % 2:
            logger.warning("MOSS ignored audio without context or valid PCM")
            return
        key = self._key(context)
        if self._briefing or key in self._brief_turns:
            self._brief_turns.add(key)
            await self._send("playback.chunk.completed", **context)
            return
        turn = self._turns.setdefault(key, _Playback({k: context[k] for k in ("run_id", "turn_id")}))
        chunk_id = str(context["chunk_id"])
        if turn.stopped or chunk_id in turn.chunks:
            return
        turn.chunks.add(chunk_id)
        turn.pending += 1
        if self._speech_stopped_at is not None:
            elapsed = (time.monotonic() - self._speech_stopped_at) * 1000
            self._speech_stopped_at = None
            self._emit_latency("moss_input_stop_to_audio", round(elapsed, 1))
            logger.info("[timing] MOSS provider stop -> audio packet %.0fms", elapsed)
        loop = asyncio.get_running_loop()
        remaining = max(1, len(pcm) // 12) * 2  # Same 48k -> 8k length as the bridge.

        async def acknowledge() -> None:
            if not turn.stopped:
                await self._send("playback.chunk.completed", **context)
                turn.pending -= 1
                await self._complete(turn)

        tagged = AgentPcm(pcm)

        def schedule_ack() -> None:
            if self._ws is not None:
                self._spawn(acknowledge())

        def on_played(count: int) -> None:
            nonlocal remaining
            with turn.lock:
                if turn.stopped or remaining <= 0:
                    return
                count = min(count, remaining)
                remaining -= count
                turn.played_bytes += count
                if remaining:
                    return
            try:
                loop.call_soon_threadsafe(schedule_ack)
            except RuntimeError:
                pass  # A final hardware notification can race event-loop shutdown.

        tagged.on_played = on_played
        if self._on_audio_out is not None:
            self._on_audio_out(tagged)

    async def _complete(self, turn: _Playback) -> None:
        if turn.done and not turn.pending and not turn.stopped and not turn.completed:
            turn.completed = True
            await self._send("playback.completed", **turn.ids)

    async def _event(self, action: str, data: dict) -> None:
        status = self._safe_log_token(data.get("status", "unknown"))
        if action == "response.audio.started":
            logger.info("MOSS response.audio.started briefing=%s", self._briefing)
        elif action in {"output.transcript.done", "response.interrupted"}:
            logger.info("MOSS %s text_chars=%d briefing=%s", action, len(str(data.get("text") or "")), self._briefing)
        elif action == "response.ended":
            logger.info("MOSS response.ended status=%s briefing=%s", status, self._briefing)
        key = self._key(data)
        if key == ("", "") and action in {
            "response.audio.stop", "response.audio.done", "response.ended",
            "output.transcript.done", "response.interrupted",
        }:
            key = self._active_key
        if action == "response.audio.context":
            if all(data.get(k) is not None for k in ("chunk_id", "run_id", "turn_id")):
                self._context = {k: data[k] for k in ("chunk_id", "run_id", "turn_id")}
                self._active_key = self._key(data)
        elif action == "response.audio.stop":
            if key == self._active_key:
                self._context = None
            turn = self._turns.setdefault(key, _Playback({"run_id": key[0], "turn_id": key[1]}))
            if not self._briefing and key == self._active_key:
                self._emit_late_cut()  # Clear queues without counting a local VAD barge-in.
            with turn.lock:
                turn.stopped = True
            await self._send("playback.stopped", **turn.ids, played_audio_ms=round(turn.played_bytes / 16, 1))
        elif action in {"response.audio.done", "response.ended"}:
            if self._briefing or key in self._brief_turns:
                if key not in self._brief_completed:
                    self._brief_completed.add(key)
                    await self._send("playback.completed", run_id=key[0], turn_id=key[1])
                if self._brief_sent and self._brief_done is not None and not self._brief_done.done():
                    if status in {"failed", "cancelled", "incomplete", "error"}:
                        self._brief_done.set_exception(RuntimeError("MOSS briefing response failed"))
                    else:
                        self._brief_done.set_result(None)
            else:
                finished_turn = self._turns.get(key)
                if finished_turn:
                    finished_turn.done = True
                    await self._complete(finished_turn)
                self._emit_output_complete()
        elif action == "error":
            error = data.get("error") or {}
            # Providers may echo submitted text/credentials in error.message.
            safe_code = self._safe_log_token(error.get("code", "unknown"))
            logger.warning("MOSS error code=%s message_chars=%d", safe_code, len(str(error.get("message", ""))))
            if self._briefing and self._brief_done is not None and not self._brief_done.done():
                self._brief_done.set_exception(RuntimeError("MOSS briefing rejected"))
        elif self._briefing or key in self._brief_turns:
            return
        elif action == "input.speech.started":
            logger.info("[timing] MOSS provider speech started")
        elif action == "input.speech.stopped":
            self._speech_stopped_at = time.monotonic()
            logger.info("[timing] MOSS provider speech stopped")
        elif action in {"input.transcript.done", "output.transcript.done", "response.interrupted"}:
            self._emit_transcript("user" if action.startswith("input.") else "agent", str(data.get("text") or ""))
        elif action == "response.function_call_arguments.done":
            self._spawn(self._tool(data))

    def _safe_log_token(self, value: Any) -> str:
        value = str(value)
        return value if re.fullmatch(r"[a-zA-Z0-9_.-]{1,80}", value) and self._api_key not in value else "redacted"

    async def _tool(self, data: dict) -> None:
        call_id, name = data.get("call_id"), data.get("name")
        if not call_id or call_id in self._handled_calls or not name:
            return
        self._handled_calls.add(call_id)
        log_name = self._safe_log_token(name)
        logger.info("MOSS tool requested name=%s", log_name)
        try:
            args = json.loads(data.get("arguments") or "{}")
            if not isinstance(args, dict):
                raise ValueError("Tool arguments must be an object")
            if self._tools:
                if name == "send_dtmf":
                    # The existing DTMF guard discards AI playback. Report that
                    # discard rather than leaving MOSS waiting for impossible ACKs.
                    await self.cancel_response()
                work = asyncio.create_task(asyncio.to_thread(self._tools.dispatch, name, args))
                try:
                    result = await asyncio.shield(work)
                finally:
                    await asyncio.gather(work, return_exceptions=True)
            else:
                result = {"success": False}
        except Exception:
            result = {"success": False, "message": "Tool execution or arguments failed"}
        logger.info("MOSS tool result name=%s success=%s", log_name,
                    isinstance(result, dict) and result.get("success") is True)
        try:
            await self._send("conversation.item.create", item={
                "type": "function_call_output", "call_id": call_id,
                "output": json.dumps(result, ensure_ascii=False),
            })
        except Exception:
            logger.warning("MOSS tool output could not be sent")

    async def cancel_response(self) -> bool:
        """Stop local playback and report it; no unverified response.cancel action."""
        turn = self._turns.get(self._active_key)
        if turn is None or turn.completed or turn.stopped:
            return False
        await self._event("response.audio.stop", turn.ids)
        return True

    async def stop(self) -> None:
        self._ready = False
        ws, self._ws = self._ws, None
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        if ws is not None:
            await ws.close()
        if self._brief_done is not None and self._brief_done.done() and not self._brief_done.cancelled():
            self._brief_done.exception()
        self._uplink.clear()
