"""通义千问 Qwen-Omni 实时语音 Agent。"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import socket
import ssl
import threading
import time
from datetime import datetime
from queue import Empty, Queue
from typing import Callable
from urllib.parse import urlparse

import dashscope
from dashscope.audio.qwen_omni import (
    AudioFormat,
    MultiModality,
    OmniRealtimeCallback,
    OmniRealtimeConversation,
)

from .. import config
from .base import VoiceAgent

logger = logging.getLogger(__name__)

# 千问 Realtime 连接重试次数（SDK connect 硬超时 5s，冷连接抖动会踩线）。
QWEN_CONNECT_MAX_ATTEMPTS = 3

# 重连成功后让 Agent 主动安抚对方的提示词。
RECONNECT_NOTICE = "请直接用中文说：抱歉刚才信号不太好，请继续。"

# Realtime 端点默认 host/port（与 dashscope SDK 内置的 wss 地址一致）。
DEFAULT_PREWARM_HOST = "dashscope.aliyuncs.com"
DEFAULT_PREWARM_PORT = 443


def _reconnect_max() -> int:
    """读取运行中断线的最大重连次数（注册表 QWEN_RECONNECT_MAX，默认 2）。"""
    return config.get_int("QWEN_RECONNECT_MAX")


def _resolve_prewarm_target() -> tuple[str, int]:
    """解析预热目标 host/port。

    优先解析 DASHSCOPE_REALTIME_URL 的 host（兼容缺 scheme 的裸 host 写法），
    否则回落到 dashscope 官方 Realtime 端点。
    """
    url = config.get_str("DASHSCOPE_REALTIME_URL").strip()
    if url:
        if "://" not in url:
            url = "//" + url
        parsed = urlparse(url)
        if parsed.hostname:
            return parsed.hostname, parsed.port or DEFAULT_PREWARM_PORT
        logger.warning("DASHSCOPE_REALTIME_URL 无法解析 host: %s", url)
    return DEFAULT_PREWARM_HOST, DEFAULT_PREWARM_PORT


def prewarm_connection(timeout: float | None = None) -> float | None:
    """对 Realtime 端点做一次 TCP+TLS 握手后立即关闭，预热 DNS/TLS 缓存。

    返回握手耗时（秒）；失败时记 warning 并返回 None，不抛异常。
    """
    if timeout is None:
        timeout = config.get_float("QWEN_PREWARM_TIMEOUT")
    host, port = _resolve_prewarm_target()
    started = time.monotonic()
    try:
        context = ssl.create_default_context()
        with socket.create_connection((host, port), timeout=timeout) as raw_sock:
            with context.wrap_socket(raw_sock, server_hostname=host):
                pass
    except Exception as exc:  # noqa: BLE001
        logger.warning("千问 Realtime 连接预热失败(%s:%d): %s", host, port, exc)
        return None
    elapsed = time.monotonic() - started
    logger.debug("千问 Realtime 连接预热完成(%s:%d): %.3fs", host, port, elapsed)
    return elapsed


def start_prewarm_keepalive(
    interval_seconds: float = 240.0,
    stop_event: threading.Event | None = None,
) -> threading.Thread:
    """启动 daemon 线程周期性预热 Realtime 连接，返回线程对象便于测试/管理。

    interval_seconds 可被 env QWEN_PREWARM_INTERVAL 覆盖；传入 stop_event
    可随时停止循环（线程也挂在返回对象的 stop_event 属性上）。
    """
    event = stop_event if stop_event is not None else threading.Event()
    # env 覆盖优先于入参（保持原有语义）；未设置时沿用调用方传入的间隔。
    interval = (
        config.get_float("QWEN_PREWARM_INTERVAL")
        if "QWEN_PREWARM_INTERVAL" in os.environ
        else interval_seconds
    )

    def _worker() -> None:
        while not event.is_set():
            prewarm_connection()
            if event.wait(interval):
                break

    thread = threading.Thread(target=_worker, daemon=True, name="qwen-prewarm")
    thread.stop_event = event  # type: ignore[attr-defined]
    thread.start()
    return thread


class _QwenCallback(OmniRealtimeCallback):
    def __init__(
        self,
        audio_queue: Queue[bytes | None],
        agent: "QwenVoiceAgent | None" = None,
    ) -> None:
        self._audio_queue = audio_queue
        self._agent = agent

    def on_open(self) -> None:
        logger.info("千问 Realtime 连接已建立")

    def on_close(self, close_status_code, close_msg) -> None:
        logger.info("千问 Realtime 连接关闭: %s %s", close_status_code, close_msg)
        agent = self._agent
        if agent is not None and agent._running:  # noqa: SLF001
            # 通话进行中被动断线：先标记，等下一次 send_audio 触发重连；
            # 不投 None，保持下行泵线程存活以便重连后继续放音。
            agent._mark_disconnected()  # noqa: SLF001
        else:
            self._audio_queue.put(None)

    def on_event(self, response: dict) -> None:
        event_type = response.get("type", "")
        if event_type == "response.audio.delta":
            delta = response.get("delta", "")
            if delta:
                self._audio_queue.put(base64.b64decode(delta))
        elif event_type == "conversation.item.input_audio_transcription.completed":
            transcript = (response.get("transcript") or "").strip()
            if transcript:
                logger.info("[上行·用户] %s", transcript)
                if self._agent:
                    self._agent._emit_transcript("user", transcript)  # noqa: SLF001
        elif event_type == "conversation.item.input_audio_transcription.delta":
            delta = (response.get("delta") or "").strip()
            if delta:
                logger.debug("[上行·用户·增量] %s", delta)
        elif event_type == "response.audio_transcript.done":
            transcript = (response.get("transcript") or "").strip()
            if transcript:
                logger.info("[下行·Agent] %s", transcript)
                if self._agent:
                    self._agent._emit_transcript("agent", transcript)  # noqa: SLF001
        elif event_type == "response.function_call_arguments.done":
            name = response.get("name")
            call_id = response.get("call_id")
            arguments = response.get("arguments") or ""
            logger.info("千问请求调用工具 %s (call_id=%s)", name, call_id)
            if self._agent and name and call_id:
                self._agent._dispatch_tool_call(  # noqa: SLF001
                    name, call_id, arguments
                )
        elif event_type == "response.done":
            logger.debug("千问回复轮次完成")
        elif event_type == "error":
            logger.error("千问 Realtime 错误: %s", response)


class QwenVoiceAgent(VoiceAgent):
    input_rate = 16000
    output_rate = 24000

    def __init__(
        self,
        api_key: str,
        model: str,
        model_display_name: str,
        voice: str = "Raymond",
        realtime_url: str | None = None,
    ) -> None:
        dashscope.api_key = api_key
        self.model = model
        self.model_display_name = model_display_name
        self.voice = voice
        self.realtime_url = realtime_url
        self._conversation: OmniRealtimeConversation | None = None
        self._audio_queue: Queue[bytes | None] = Queue()
        self._callback = _QwenCallback(self._audio_queue, agent=self)
        self._on_audio_out: Callable[[bytes], None] | None = None
        self._pump_thread: threading.Thread | None = None
        self._running = False
        self._handled_tool_calls: set[str] = set()
        self._instructions: str | None = None
        # 断线重连状态：_disconnected 由回调线程置位、send_audio 消费；
        # _reconnecting/_reconnect_attempts 由 _reconnect_lock 保护。
        self._disconnected = threading.Event()
        self._reconnect_lock = threading.Lock()
        self._reconnecting = False
        self._reconnect_attempts = 0
        self._reconnect_thread: threading.Thread | None = None

    async def start(self, on_audio_out: Callable[[bytes], None]) -> None:
        self._on_audio_out = on_audio_out
        self._running = True
        self._disconnected.clear()
        with self._reconnect_lock:
            self._reconnecting = False
            self._reconnect_attempts = 0

        weekdays = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
        now = datetime.now()
        now_str = f"{now:%Y年%m月%d日 %H:%M}（{weekdays[now.weekday()]}）"

        self._instructions = self._session_instructions or (
            "你是接入电话的语音 Agent。接通后请先用中文简短自我介绍。"
            "之后用口语化、简洁的方式回答对方问题，每次回答控制在两三句话以内。"
            f"当前真实日期时间是 {now_str}，这是准确信息；对方询问日期、时间、"
            "今天几号或星期几时，必须以此为准回答，不要凭记忆猜测年份。"
            "你可以调用工具帮用户完成实际操作：发送短信(send_sms，发给本人时号码留空)、"
            "挂断电话(hangup_call，挂断前先说一句告别语)、查询最近收到的短信验证码"
            "(query_verification_code)。需要时主动调用对应工具，操作完成后用一句话口头确认结果。"
        )

        self._connect_session()

        self._pump_thread = threading.Thread(target=self._pump_audio_out, daemon=True)
        self._pump_thread.start()
        logger.info("千问 Agent 已启动: %s", self.model)

    def _connect_session(self) -> None:
        """新建 conversation 并完成 connect + update_session（含连接重试）。

        start() 与断线重连共用；全部成功后才把新 conversation 挂到
        self._conversation 上，失败则抛出最后一次异常。
        """
        conversation_kwargs = {
            "model": self.model,
            "callback": self._callback,
        }
        if self.realtime_url:
            conversation_kwargs["url"] = self.realtime_url

        # dashscope 的 connect 硬超时 5s，冷连接(TLS 冷启)遇网络抖动会踩线失败。
        # 重试若干次：第二次起复用热 DNS/TLS，通常 <1s 连上，避免整通电话因一次
        # 瞬时超时而失败。
        conversation: OmniRealtimeConversation | None = None
        last_exc: Exception | None = None
        for attempt in range(1, QWEN_CONNECT_MAX_ATTEMPTS + 1):
            conversation = OmniRealtimeConversation(**conversation_kwargs)
            self._callback._conversation = conversation  # noqa: SLF001
            try:
                conversation.connect()
                last_exc = None
                break
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                logger.warning(
                    "千问 Realtime 连接失败(第 %d/%d 次): %s",
                    attempt, QWEN_CONNECT_MAX_ATTEMPTS, exc,
                )
        if last_exc is not None or conversation is None:
            raise last_exc if last_exc is not None else RuntimeError("连接失败")

        tool_kwargs: dict = {}
        if self._tools is not None and self._tools.has_tools():
            # 千问 Omni Realtime 不支持 tool_choice / parallel_tool_calls。
            tool_kwargs["tools"] = self._tools.specs()
            logger.info("已为会话注册 %d 个工具", len(self._tools.specs()))

        conversation.update_session(
            output_modalities=[MultiModality.AUDIO, MultiModality.TEXT],
            voice=self.voice,
            input_audio_format=AudioFormat.PCM_16000HZ_MONO_16BIT,
            output_audio_format=AudioFormat.PCM_24000HZ_MONO_16BIT,
            enable_input_audio_transcription=True,
            input_audio_transcription_model="qwen3-asr-flash-realtime",
            enable_turn_detection=True,
            turn_detection_type="server_vad",
            turn_detection_silence_duration_ms=600,
            instructions=self._instructions,
            **tool_kwargs,
        )

        self._conversation = conversation

    def _mark_disconnected(self) -> None:
        """回调线程通知：运行中连接被动关闭，等待 send_audio 触发重连。"""
        if not self._disconnected.is_set():
            logger.warning("千问 Realtime 运行中断线，等待下一帧音频触发重连")
        self._disconnected.set()

    def _maybe_start_reconnect(self) -> None:
        """若当前无重连在进行且未超限，启动后台重连线程（幂等）。"""
        with self._reconnect_lock:
            if not self._running or self._reconnecting:
                return
            if self._reconnect_attempts >= _reconnect_max():
                return
            self._reconnecting = True
        self._reconnect_thread = threading.Thread(
            target=self._reconnect_worker, daemon=True, name="qwen-reconnect"
        )
        self._reconnect_thread.start()

    def _reconnect_worker(self) -> None:
        max_attempts = _reconnect_max()
        try:
            while self._running:
                with self._reconnect_lock:
                    if self._reconnect_attempts >= max_attempts:
                        break
                    self._reconnect_attempts += 1
                    attempt = self._reconnect_attempts
                logger.warning(
                    "千问 Realtime 尝试重连(第 %d/%d 次)", attempt, max_attempts
                )
                try:
                    self._connect_session()
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "千问 Realtime 重连失败(第 %d/%d 次): %s",
                        attempt, max_attempts, exc,
                    )
                    continue
                # stop() 可能在 _connect_session 期间执行完毕：在锁内复查
                # _running，若会话已停止则立刻关闭刚建立的新连接，防止
                # 连接泄漏 / 死连接挂回 _conversation（codex review P0）。
                with self._reconnect_lock:
                    if not self._running:
                        stale = self._conversation
                        self._conversation = None
                        if stale is not None:
                            try:
                                stale.close()
                            except Exception:  # noqa: BLE001
                                pass
                        logger.info("重连完成时会话已停止，已关闭新建连接")
                        return
                    self._disconnected.clear()
                logger.info("千问 Realtime 重连成功(第 %d/%d 次)", attempt, max_attempts)
                try:
                    asyncio.run(self.say(RECONNECT_NOTICE))
                except Exception as exc:  # noqa: BLE001
                    logger.warning("重连后安抚语发送失败: %s", exc)
                return
            # 重连超限或会话已停止：置 fatal 让 CallSession 主循环感知并
            # 收尾整通电话（否则电话"活着但 AI 已死"，对方只听到沉默）。
            logger.error("千问 Realtime 重连全部失败，标记会话不可恢复")
            self.fatal = True
            self._audio_queue.put(None)
        finally:
            with self._reconnect_lock:
                self._reconnecting = False

    async def send_audio(self, pcm: bytes) -> None:
        if not pcm:
            return
        if self._disconnected.is_set():
            # 断线中：静默丢弃本帧，同时（幂等地）拉起后台重连。
            self._maybe_start_reconnect()
            return
        conversation = self._conversation
        if not conversation:
            return
        audio_b64 = base64.b64encode(pcm).decode("ascii")
        try:
            conversation.append_audio(audio_b64)
        except Exception as exc:  # noqa: BLE001
            # 连接可能刚死但 on_close 未及时触发：标记断线，下一帧走重连。
            logger.warning("发送音频失败，标记断线: %s", exc)
            self._disconnected.set()

    async def say(self, instructions: str) -> None:
        if not self._conversation or self._disconnected.is_set():
            return
        self._conversation.create_response(
            instructions=instructions,
            output_modalities=[MultiModality.AUDIO, MultiModality.TEXT],
        )

    def _dispatch_tool_call(self, name: str, call_id: str, arguments: str) -> None:
        if call_id in self._handled_tool_calls:
            return
        self._handled_tool_calls.add(call_id)

        def worker() -> None:
            try:
                args = json.loads(arguments) if arguments.strip() else {}
            except json.JSONDecodeError:
                logger.warning("工具参数解析失败: %s", arguments)
                args = {}
            result = (
                self._tools.dispatch(name, args)
                if self._tools is not None
                else {"success": False, "message": "无可用工具"}
            )
            conversation = self._conversation
            if conversation is None:
                return
            try:
                conversation.create_item(
                    {
                        "type": "function_call_output",
                        "call_id": call_id,
                        "output": json.dumps(result, ensure_ascii=False),
                    }
                )
                conversation.create_response(
                    output_modalities=[MultiModality.AUDIO, MultiModality.TEXT]
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("回传工具结果失败: %s", exc)

        threading.Thread(target=worker, daemon=True).start()

    async def stop(self) -> None:
        # 与重连线程的"连接成功挂载"在同一把锁下串行：先置 _running=False
        # 再摘取连接，保证重连线程随后要么看到已停止（自行关闭新连接），
        # 要么其新连接在此处被关闭。
        with self._reconnect_lock:
            self._running = False
            conversation = self._conversation
            self._conversation = None
        if conversation:
            try:
                conversation.close()
            except Exception as exc:  # noqa: BLE001
                logger.warning("关闭千问连接异常: %s", exc)
        self._audio_queue.put(None)
        if self._pump_thread:
            self._pump_thread.join(timeout=3)

    def _pump_audio_out(self) -> None:
        while self._running:
            try:
                chunk = self._audio_queue.get(timeout=0.2)
            except Empty:
                continue
            if chunk is None:
                break
            if self._on_audio_out:
                self._on_audio_out(chunk)
