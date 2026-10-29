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
from typing import Any, Callable
from urllib.parse import urlparse

import dashscope
from dashscope.audio.qwen_omni import (
    AudioFormat,
    MultiModality,
    OmniRealtimeCallback,
    OmniRealtimeConversation,
)

from .. import config
from ..prompts import agent_language, repeat_nudge_instructions
from ..repeat_suppression import ResponseAudioGate
from .base import VoiceAgent
from .tools import SILENT_AFTER_TOOLS, TERMINAL_TOOLS

logger = logging.getLogger(__name__)

# 千问 Realtime 连接重试次数（SDK connect 硬超时 5s，冷连接抖动会踩线）。
QWEN_CONNECT_MAX_ATTEMPTS = 3

# 同步 ws send 的超时熔断（秒）：正常一帧 <50ms，超过即视为链路劣化按断线处理，
# 保住音频主循环的心跳（硬时限/裁判判定都在主循环里）。
_SEND_TIMEOUT_SECONDS = 2.0
_SAY_TIMEOUT_SECONDS = 5.0

# create_response 发出后等待 wire response.created 的独立超时。
_MANUAL_RESPONSE_CREATED_TIMEOUT_SECONDS = 5.0

# 重连成功后让 Agent 主动安抚对方的提示词。
RECONNECT_NOTICE = "请直接用中文说：抱歉刚才信号不太好，请继续。"

# Realtime 端点默认 host/port（与 dashscope SDK 内置的 wss 地址一致）。
DEFAULT_PREWARM_HOST = "dashscope.aliyuncs.com"
DEFAULT_PREWARM_PORT = 443


def _response_id(event: dict[str, Any]) -> str | None:
    raw_response = event.get("response")
    response: dict[str, Any] = raw_response if isinstance(raw_response, dict) else {}
    raw = (
        event.get("response_id")
        or event.get("item_id")
        or event.get("id")
        or response.get("id")
    )
    return str(raw) if raw else None


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
        # 显式钉死下限。create_default_context() 本身已经是安全默认（当前 Python
        # 不会协商 TLS 1.0/1.1），写出来是为了不随运行环境的 openssl 策略漂移，
        # 同时让 CodeQL 的 py/insecure-protocol 有据可依（WIL-87）。
        context.minimum_version = ssl.TLSVersion.TLSv1_2
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
        *,
        fenced: bool = False,
    ) -> None:
        self._audio_queue = audio_queue
        self._agent = agent
        self._fenced = fenced
        self._invalidated = False
        # 由 QwenVoiceAgent._connect 在建连后回填；此前是动态挂属性，
        # dashscope 1.26.5 的类型信息补全后被 mypy 判为 attr-defined。
        self._conversation: OmniRealtimeConversation | None = None

    def on_open(self) -> None:
        if self._stale():
            return
        logger.info("千问 Realtime 连接已建立")

    def _stale(self) -> bool:
        return self._fenced and (
            self._invalidated or (self._agent is not None and self._agent._callback is not self)
        )

    def on_close(self, close_status_code, close_msg) -> None:
        if self._stale():
            return
        logger.info("千问 Realtime 连接关闭: %s %s", close_status_code, close_msg)
        agent = self._agent
        if agent is not None and agent._running:  # noqa: SLF001
            # 通话进行中被动断线：先标记，等下一次 send_audio 触发重连；
            # 不投 None，保持下行泵线程存活以便重连后继续放音。
            agent._mark_disconnected()  # noqa: SLF001
        else:
            self._audio_queue.put(None)

    def on_event(self, response: Any) -> None:
        if self._stale():
            return
        # SDK 基类把该参数注解成 str，但运行时传的是事件 dict（下面 .get 即证）。
        # 注解成 Any 以兼容这份不准确的父类签名，运行时行为不变。
        event_type = response.get("type", "")
        if event_type == "response.audio.delta":
            delta = response.get("delta", "")
            if delta:
                chunk = base64.b64decode(delta)
                if self._agent:
                    self._agent._audio_gate.push_audio(_response_id(response), chunk)  # noqa: SLF001
                else:
                    self._audio_queue.put(chunk)
        elif event_type == "conversation.item.input_audio_transcription.completed":
            transcript = (response.get("transcript") or "").strip()
            if transcript:
                logger.info("[上行·用户] %s", transcript)
                if self._agent:
                    self._agent._emit_transcript("user", transcript)  # noqa: SLF001
                    self._agent._on_user_transcription_completed()  # noqa: SLF001
        elif event_type == "conversation.item.input_audio_transcription.delta":
            delta = (response.get("delta") or "").strip()
            if delta:
                logger.debug("[上行·用户·增量] %s", delta)
        elif event_type == "response.audio_transcript.done":
            transcript = (response.get("transcript") or "").strip()
            if transcript:
                logger.info("[下行·Agent] %s", transcript)
                if self._agent:
                    suppressed = self._agent._audio_gate.complete_transcript(  # noqa: SLF001
                        _response_id(response), transcript
                    )
                    if not suppressed:
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
        elif event_type == "response.created":
            if self._agent:
                self._agent._on_response_created()  # noqa: SLF001
        elif event_type == "response.done":
            if self._agent:
                self._agent._audio_gate.complete_response(_response_id(response))  # noqa: SLF001
                self._agent._on_response_done()  # noqa: SLF001
                if self._agent._on_output_complete is not None:  # noqa: SLF001
                    status = (response.get("response") or {}).get("status")
                    if status in {"failed", "incomplete", "cancelled"}:
                        self._agent.output_failed = True
                    # 标记排在本轮 PCM 后面，由同一个输出线程消费。
                    self._audio_queue.put(b"")
            logger.debug("千问回复轮次完成")
        elif event_type == "session.updated":
            session = response.get("session")
            session_data = session if isinstance(session, dict) else {}
            turn_detection = session_data.get("turn_detection")
            turn_data = turn_detection if isinstance(turn_detection, dict) else {}
            create_response = turn_data.get("create_response", "unknown")
            requested = bool(
                self._agent is not None
                and self._agent._manual_response_enabled  # noqa: SLF001
            )
            logger.info(
                "千问 Realtime session.updated: "
                "manual_response_requested=%s create_response=%s",
                requested,
                create_response,
            )
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
        self._audio_gate = ResponseAudioGate(
            "qwen",
            lambda chunk: self._audio_queue.put(chunk),
            on_suppressed=self._nudge_after_repeat_suppressed,
            on_stuck=self._repeat_suppression_stuck,
            # 复读已开播才被判定时清设备侧积压（专用晚截回调，未注册则无害；
            # 不走打断回调，以免被计进 barge-in 自激笔数——见 base）。
            on_late_cut=self._emit_late_cut,
        )
        self._callback = _QwenCallback(self._audio_queue, agent=self)
        self._on_audio_out: Callable[[bytes], None] | None = None
        self._pump_thread: threading.Thread | None = None
        # _running 的无锁读取只做快速短路；CPython 下 bool 引用读写原子，
        # 资源摘取/状态转移仍由各自锁保护，避免把回调热路径全串行化。
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
        self._manual_response_enabled = False
        self._manual_response_lock = threading.Lock()
        self._manual_response_timer: threading.Timer | None = None
        self._manual_response_generation = 0
        self._manual_response_watchdog_timer: threading.Timer | None = None
        self._manual_response_watchdog_generation = 0
        self._manual_response_first_at: float | None = None
        self._manual_response_request_pending = False
        self._manual_response_outstanding = 0
        self._manual_response_pending_after_flight = False

    async def start(self, on_audio_out: Callable[[bytes], None]) -> None:
        self._on_audio_out = on_audio_out
        self._running = True
        self._manual_response_enabled = config.get_bool("MANUAL_RESPONSE_CONTROL")
        self._reset_manual_response_state()
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
            "你可以调用工具帮用户完成实际操作：发送短信(send_sms，发给通话对方时号码留空、"
            "发给机主时 to 填 owner)、"
            "挂断电话(hangup_call，挂断前先说一句告别语)、查询最近收到的短信验证码"
            "(query_verification_code)。需要时主动调用对应工具，操作完成后用一句话口头确认结果。"
        )

        if config.get_bool("PARALLEL_AGENT_CONNECT"):
            connecting = asyncio.create_task(asyncio.to_thread(self._connect_session))
            try:
                await asyncio.shield(connecting)
            except asyncio.CancelledError:
                # The SDK is synchronous: join before the owner calls stop(), or a
                # late successful connect could publish a socket after teardown.
                self._running = False
                await asyncio.gather(connecting, return_exceptions=True)
                raise
        else:
            self._connect_session()

        self._pump_thread = threading.Thread(target=self._pump_audio_out, daemon=True)
        self._pump_thread.start()
        logger.info("千问 Agent 已启动: %s", self.model)

    def _connect_session(self) -> None:
        """新建 conversation 并完成 connect + update_session（含连接重试）。

        start() 与断线重连共用；全部成功后才把新 conversation 挂到
        self._conversation 上，失败则抛出最后一次异常。
        """
        # 显式标注 Any：值是异构的（str 与 callback 对象），推断出的
        # dict[str, object] 在 ** 解包时与 SDK 形参类型冲突。
        conversation_kwargs: dict[str, Any] = {
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
            if config.get_bool("PARALLEL_AGENT_CONNECT") and not self._running:
                raise RuntimeError("千问建连已取消")
            if config.get_bool("PARALLEL_AGENT_CONNECT"):
                # A failed/old candidate may close late. Its callback must not
                # change the new connection's state or deliver stale audio/tools.
                self._callback = _QwenCallback(self._audio_queue, self, fenced=True)
                conversation_kwargs["callback"] = self._callback
            conversation = OmniRealtimeConversation(**conversation_kwargs)
            self._callback._conversation = conversation  # noqa: SLF001
            try:
                conversation.connect()
                last_exc = None
                break
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                if config.get_bool("PARALLEL_AGENT_CONNECT"):
                    self._close_unpublished_session(conversation)
                logger.warning(
                    "千问 Realtime 连接失败(第 %d/%d 次): %s",
                    attempt, QWEN_CONNECT_MAX_ATTEMPTS, exc,
                )
        if last_exc is not None or conversation is None:
            raise last_exc if last_exc is not None else RuntimeError("连接失败")
        if config.get_bool("PARALLEL_AGENT_CONNECT") and not self._running:
            self._close_unpublished_session(conversation)
            raise RuntimeError("千问建连已取消")

        tool_kwargs: dict = {}
        if self._tools is not None and self._tools.has_tools():
            # 千问 Omni Realtime 不支持 tool_choice / parallel_tool_calls。
            tool_kwargs["tools"] = self._tools.specs()
            logger.info("已为会话注册 %d 个工具", len(self._tools.specs()))

        session_kwargs = self._session_kwargs(tool_kwargs)
        try:
            conversation.update_session(**session_kwargs)
        except BaseException:
            if config.get_bool("PARALLEL_AGENT_CONNECT"):
                self._close_unpublished_session(conversation)
            raise

        self._conversation = conversation
        if config.get_bool("PARALLEL_AGENT_CONNECT"):
            self._disconnected.clear()

    def _close_unpublished_session(self, conversation) -> None:
        if self._callback._conversation is conversation:
            # close() can synchronously drain SDK callbacks, including tools.
            # Invalidate before closing, not only when installing the next retry.
            self._callback._invalidated = True
        try:
            conversation.close()
        except Exception:  # noqa: BLE001
            logger.warning("关闭未完成初始化的千问连接失败", exc_info=True)

    def _session_kwargs(self, tool_kwargs: dict) -> dict:
        session_kwargs = {
            "output_modalities": [MultiModality.AUDIO, MultiModality.TEXT],
            "voice": self.voice,
            "input_audio_format": AudioFormat.PCM_16000HZ_MONO_16BIT,
            "output_audio_format": AudioFormat.PCM_24000HZ_MONO_16BIT,
            "enable_input_audio_transcription": True,
            "input_audio_transcription_model": "qwen3-asr-flash-realtime",
            "enable_turn_detection": True,
            "turn_detection_type": "server_vad",
            "turn_detection_silence_duration_ms": 600,
            "instructions": self._instructions,
            **tool_kwargs,
        }
        if self._manual_response_enabled:
            session_kwargs["turn_detection_param"] = {"create_response": False}
        return session_kwargs

    async def update_session_instructions(self, instructions: str) -> bool:
        """中途下发新的系统提示词。

        千问 SDK 的 update_session 是整份覆盖，不是增量合并——只传 instructions
        会把音色/采样率/转写/工具一并抹掉。所以这里重建完整 session_kwargs。
        """
        self._session_instructions = instructions
        self._instructions = instructions
        conversation = self._conversation
        if conversation is None:
            # 还没连上：start() 会带着新的 instructions 建会话，等价生效。
            return False
        tool_kwargs: dict = {}
        if self._tools is not None and self._tools.has_tools():
            tool_kwargs["tools"] = self._tools.specs()
        try:
            # 与 append_audio 同样的熔断：dashscope 的 ws send 是同步阻塞调用，
            # 死链时可挂几十秒。直接在通话循环里调用会把音频主循环一起拖停
            # （2026-07-10 真机两通 180s+ 卡死的同一形态）。
            await asyncio.wait_for(
                asyncio.to_thread(
                    conversation.update_session, **self._session_kwargs(tool_kwargs)
                ),
                timeout=_SEND_TIMEOUT_SECONDS,
            )
        except (TimeoutError, asyncio.TimeoutError):
            logger.warning("中途更新会话提示词超时，AI 仍受限于旧提示词")
            return False
        except Exception as exc:  # noqa: BLE001
            logger.warning("中途更新会话提示词失败: error_type=%s", type(exc).__name__)
            return False
        logger.info("会话提示词已中途更新")
        return True

    async def cancel_response(self) -> bool:
        """打断本轮生成（dashscope ``cancel_response``）。

        掐的是**生成**不是**播放**：已经排进下行队列的音频仍会播完，所以对端
        听到的是一段完整播出的语音，只是不再继续变长。
        """
        conversation = self._conversation
        if conversation is None:
            return False
        try:
            # 与 update_session_instructions 同样的熔断理由：dashscope 的 ws send
            # 是同步阻塞调用，死链时能把音频主循环一起拖停。
            await asyncio.wait_for(
                asyncio.to_thread(conversation.cancel_response),
                timeout=_SEND_TIMEOUT_SECONDS,
            )
        except (TimeoutError, asyncio.TimeoutError):
            logger.warning("打断本轮回复超时")
            return False
        except Exception as exc:  # noqa: BLE001
            logger.warning("打断本轮回复失败: error_type=%s", type(exc).__name__)
            return False
        return True

    def _mark_disconnected(self) -> None:
        """回调线程通知：运行中连接被动关闭，等待 send_audio 触发重连。"""
        if not self._disconnected.is_set():
            logger.warning("千问 Realtime 运行中断线，等待下一帧音频触发重连")
        self._cancel_manual_response_control()
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
            # dashscope SDK 的 ws send 是同步阻塞调用：网络劣化/死链时一次 send
            # 可挂几十秒，音频主循环（含 150s 硬时限判定）随之停摆——2026-07-10
            # 真机两通 180s+ 卡死实证。线程池 + 2s 超时熔断：超时按断线处理
            # 丢帧走重连，主循环最多被拖一帧的超时时长。
            await asyncio.wait_for(
                asyncio.to_thread(conversation.append_audio, audio_b64),
                timeout=_SEND_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            logger.warning("发送音频超过 %.0fs 未完成，标记断线", _SEND_TIMEOUT_SECONDS)
            self._mark_disconnected()
        except Exception as exc:  # noqa: BLE001
            # 连接可能刚死但 on_close 未及时触发：标记断线，下一帧走重连。
            logger.warning("发送音频失败，标记断线: %s", exc)
            self._mark_disconnected()

    async def say(self, instructions: str) -> None:
        conversation = self._conversation
        if not conversation or self._disconnected.is_set():
            return
        try:
            # 同 send_audio：同步 ws send 放线程池 + 超时熔断，防拖垮主循环。
            await asyncio.wait_for(
                asyncio.to_thread(
                    lambda: conversation.create_response(
                        instructions=instructions,
                        output_modalities=[MultiModality.AUDIO, MultiModality.TEXT],
                    )
                ),
                timeout=_SAY_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            logger.warning("发送说话指令超过 %.0fs 未完成，标记断线", _SAY_TIMEOUT_SECONDS)
            self._mark_disconnected()
        except Exception as exc:  # noqa: BLE001
            logger.warning("发送说话指令失败，标记断线: %s", exc)
            self._mark_disconnected()

    def _reset_manual_response_state(self) -> None:
        with self._manual_response_lock:
            self._cancel_manual_response_timer_locked()
            self._cancel_manual_response_watchdog_locked()
            self._manual_response_first_at = None
            self._manual_response_request_pending = False
            self._manual_response_outstanding = 0
            self._manual_response_pending_after_flight = False

    def _cancel_manual_response_control(self) -> None:
        with self._manual_response_lock:
            self._cancel_manual_response_timer_locked()
            self._cancel_manual_response_watchdog_locked()
            self._manual_response_first_at = None
            self._manual_response_request_pending = False
            self._manual_response_outstanding = 0
            self._manual_response_pending_after_flight = False

    def _cancel_manual_response_timer_locked(self) -> None:
        timer = self._manual_response_timer
        self._manual_response_timer = None
        self._manual_response_generation += 1
        if timer is not None:
            timer.cancel()

    def _cancel_manual_response_watchdog_locked(self) -> None:
        timer = self._manual_response_watchdog_timer
        self._manual_response_watchdog_timer = None
        self._manual_response_watchdog_generation += 1
        if timer is not None:
            timer.cancel()

    def _on_user_transcription_completed(self) -> None:
        if not self._manual_response_enabled:
            return
        self._schedule_manual_response_timer()

    def _schedule_manual_response_timer(self) -> None:
        now = time.monotonic()
        with self._manual_response_lock:
            if not self._running or self._disconnected.is_set():
                return
            if self._manual_response_request_pending:
                self._manual_response_pending_after_flight = True
                self._manual_response_first_at = None
                return
            if self._manual_response_first_at is None:
                self._manual_response_first_at = now
            timer = self._manual_response_timer
            if timer is not None:
                timer.cancel()
            self._manual_response_generation += 1
            generation = self._manual_response_generation
            delay = self._manual_response_delay_locked(now)
            next_timer = threading.Timer(
                delay, self._fire_manual_response_timer, args=(generation,)
            )
            next_timer.daemon = True
            self._manual_response_timer = next_timer
            next_timer.start()

    def _manual_response_delay_locked(self, now: float) -> float:
        silence_seconds = max(0, config.get_int("MANUAL_RESPONSE_SILENCE_MS")) / 1000
        max_wait_seconds = max(0, config.get_int("MANUAL_RESPONSE_MAX_WAIT_MS")) / 1000
        if max_wait_seconds <= 0 or self._manual_response_first_at is None:
            return silence_seconds
        elapsed = now - self._manual_response_first_at
        return min(silence_seconds, max(0.0, max_wait_seconds - elapsed))

    def _fire_manual_response_timer(self, generation: int) -> None:
        conversation: OmniRealtimeConversation | None
        with self._manual_response_lock:
            if (
                generation != self._manual_response_generation
                or not self._running
                or self._disconnected.is_set()
            ):
                return
            self._manual_response_timer = None
            if self._manual_response_outstanding > 0:
                self._manual_response_pending_after_flight = True
                self._manual_response_first_at = None
                return
            conversation = self._conversation
            if conversation is None:
                self._manual_response_first_at = None
                return
            self._manual_response_first_at = None
            # This only gates the request-to-created wire gap. In-flight state
            # remains driven exclusively by response.created/response.done.
            self._manual_response_request_pending = True
            self._start_manual_response_watchdog_locked(
                _MANUAL_RESPONSE_CREATED_TIMEOUT_SECONDS
            )

        try:
            conversation.create_response(
                output_modalities=[MultiModality.AUDIO, MultiModality.TEXT],
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("手动触发千问回复失败: %s", exc)
            should_schedule = False
            with self._manual_response_lock:
                self._manual_response_request_pending = False
                if self._manual_response_outstanding == 0:
                    self._cancel_manual_response_watchdog_locked()
                if self._manual_response_pending_after_flight and self._running:
                    self._manual_response_pending_after_flight = False
                    should_schedule = True
            if should_schedule:
                self._schedule_manual_response_timer()

    def _on_response_created(self) -> None:
        if not self._manual_response_enabled:
            return
        with self._manual_response_lock:
            if not self._running:
                return
            self._manual_response_request_pending = False
            self._manual_response_outstanding += 1
            if self._manual_response_outstanding == 1:
                self._start_manual_response_watchdog_locked()

    def _start_manual_response_watchdog_locked(
        self, timeout_seconds: float | None = None
    ) -> None:
        timer = self._manual_response_watchdog_timer
        if timer is not None:
            timer.cancel()
        self._manual_response_watchdog_generation += 1
        generation = self._manual_response_watchdog_generation
        if timeout_seconds is None:
            timeout_seconds = (
                max(0, config.get_int("MANUAL_RESPONSE_MAX_WAIT_MS")) / 1000
            )
        if timeout_seconds <= 0:
            return
        next_timer = threading.Timer(
            timeout_seconds, self._fire_manual_response_watchdog, args=(generation,)
        )
        next_timer.daemon = True
        self._manual_response_watchdog_timer = next_timer
        next_timer.start()

    def _fire_manual_response_watchdog(self, generation: int) -> None:
        should_schedule = False
        with self._manual_response_lock:
            if (
                generation != self._manual_response_watchdog_generation
                or not self._running
                or self._disconnected.is_set()
                or (
                    not self._manual_response_request_pending
                    and self._manual_response_outstanding <= 0
                )
            ):
                return
            logger.warning("手动应答控制等待 response.created/done 超时，清理状态")
            self._manual_response_watchdog_timer = None
            self._manual_response_request_pending = False
            self._manual_response_outstanding = 0
            if self._manual_response_pending_after_flight:
                self._manual_response_pending_after_flight = False
                should_schedule = True
        if should_schedule:
            self._schedule_manual_response_timer()

    def _on_response_done(self) -> None:
        if not self._manual_response_enabled:
            return
        should_schedule = False
        with self._manual_response_lock:
            if self._manual_response_outstanding > 0:
                self._manual_response_outstanding -= 1
            if (
                self._manual_response_outstanding == 0
                and not self._manual_response_request_pending
            ):
                self._cancel_manual_response_watchdog_locked()
            if (
                self._manual_response_outstanding == 0
                and self._manual_response_pending_after_flight
                and self._running
            ):
                self._manual_response_pending_after_flight = False
                should_schedule = True
        if should_schedule:
            self._schedule_manual_response_timer()

    def _nudge_after_repeat_suppressed(self, _transcript: str) -> None:
        instructions = repeat_nudge_instructions(agent_language())
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            try:
                asyncio.run(self.say(instructions))
            except Exception as exc:  # noqa: BLE001
                logger.warning("发送复读换说法提示失败: %s", exc)
            return
        loop.create_task(self.say(instructions))

    def _repeat_suppression_stuck(self, count: int, _transcript: str) -> None:
        self._emit_repeat_stuck(f"复读抑制连续触发 {count} 次，判定模型卡死")

    def _dispatch_tool_call(self, name: str, call_id: str, arguments: str) -> None:
        if call_id in self._handled_tool_calls:
            return
        self._handled_tool_calls.add(call_id)

        def worker() -> None:
            try:
                args = json.loads(arguments) if arguments.strip() else {}
            except json.JSONDecodeError as exc:
                logger.warning(
                    "工具 %s 参数解析失败: arguments_length=%d, error_type=%s",
                    name,
                    len(arguments),
                    type(exc).__name__,
                )
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
                # hangup 后不再回复；DTMF 后等待 IVR 的下一段音频自然触发回复。
                if name not in TERMINAL_TOOLS and name not in SILENT_AFTER_TOOLS:
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
        with self._manual_response_lock:
            self._running = False
            self._cancel_manual_response_timer_locked()
            self._cancel_manual_response_watchdog_locked()
            self._manual_response_first_at = None
            self._manual_response_request_pending = False
            self._manual_response_pending_after_flight = False
            self._manual_response_outstanding = 0
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
            if chunk == b"" and self._on_output_complete is not None:
                self._emit_output_complete()
                continue
            if self._on_audio_out:
                self._on_audio_out(chunk)
