"""OpenAI Realtime 实时语音 Agent（raw websockets，不依赖 openai SDK）。

采用 GA（正式版）会话结构（session.type="realtime" + 嵌套 audio.input/output），
真机验证连通（2026-07）。事件命名同时容忍新旧两代以增强兼容：
- 下行音频：``response.output_audio.delta``（GA）与 ``response.audio.delta``；
- Agent 转写：``response.output_audio_transcript.done`` 与 ``response.audio_transcript.done``。

OPENAI_REALTIME_URL 可选：留空即直连 api.openai.com；仅在用反代 / Azure OpenAI，
或所在网络无法直连 OpenAI 时才需填。
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import threading
import time
from array import array
from datetime import datetime
from typing import Any, Callable, Coroutine

import websockets

from .. import config
from ..prompts import agent_language, openai_vibe_line, repeat_nudge_instructions
from ..repeat_suppression import ResponseAudioGate
from .base import VoiceAgent
from .tools import SILENT_AFTER_TOOLS, SMS_TOOL_NAMES, TERMINAL_TOOLS

logger = logging.getLogger(__name__)

# OpenAI Realtime 官方 wss 端点（可经 OPENAI_REALTIME_URL 覆盖 base）。
DEFAULT_REALTIME_URL = "wss://api.openai.com/v1/realtime"

# 输入音频转写模型（session.input_audio_transcription）。
TRANSCRIPTION_MODEL = "gpt-4o-mini-transcribe"
_MANUAL_RESPONSE_CREATED_TIMEOUT_SECONDS = 5.0
_MANUAL_RESPONSE_SEND_TIMEOUT_SECONDS = 5.0
_PARALLEL_CLOSE_TIMEOUT_SECONDS = 1.0

# 重连成功后让 Agent 主动安抚对方的提示词（与 qwen_agent 语义一致）。
RECONNECT_NOTICE = "请直接用中文说：抱歉刚才信号不太好，请继续。"

# provider 失聪看门狗（WIL-122）：真机 2026-08-17 出现「ws 活着、上行有声帧
# 一直在发、但服务端整整 60s 零事件」——机主接通只听到沉默，且现有 fatal
# 只覆盖重连全败，覆盖不了这种无声失聪。判定条件是「近窗口内仍在发**有声**
# 上行、但已连续 N 秒没收到任何服务端事件」；纯静默期（无人说话）不计，
# 避免把安静等待误判成失聪。
# 与 call_agent._VOICED_PEAK_THRESHOLD 同值：判「这帧有声」的 int16 峰值阈。
_DEAF_VOICED_PEAK_THRESHOLD = 350
# 有声帧多久内算「仍在发」；比看门狗轮询周期(5s)略宽即可。
_DEAF_VOICED_WINDOW_SECONDS = 6.0
# 连续无事件达到该时长先 WARNING + 落 provider_deaf 事件（可观测）。
_DEAF_WARN_SECONDS = 20.0
# 达到该时长主动断开 ws，借道既有断线重连路径自愈；重连全败走既有 fatal。
# 留意：若服务端对排队音乐类声音本就长期不发事件，这里会形成周期性重连——
# 代价是重连后的安抚语可能打破等待静默，但比整通「AI 已聋」可接受。
_DEAF_RECONNECT_SECONDS = 45.0


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
    """读取运行中断线的最大重连次数（注册表 OPENAI_RECONNECT_MAX，默认 2）。"""
    return config.get_int("OPENAI_RECONNECT_MAX")


def _vad_silence_ms() -> int:
    """server_vad 静默判停窗（注册表 OPENAI_VAD_SILENCE_MS，≤0 表示用服务端默认）。"""
    return config.get_int("OPENAI_VAD_SILENCE_MS")


def _vad_threshold() -> float:
    """server_vad 能量阈值（注册表 OPENAI_VAD_THRESHOLD，≤0 表示用服务端默认）。"""
    return config.get_float("OPENAI_VAD_THRESHOLD")


def _noise_reduction() -> str:
    """输入降噪模式（注册表 OPENAI_NOISE_REDUCTION，空=不启用）。"""
    return config.get_str("OPENAI_NOISE_REDUCTION").strip()


def _turn_detection_type() -> str:
    """判停方式（注册表 OPENAI_TURN_DETECTION）。非法值回落 server_vad。"""
    value = config.get_str("OPENAI_TURN_DETECTION").strip()
    return value if value in ("server_vad", "semantic_vad") else "server_vad"


def _vad_eagerness() -> str:
    """semantic_vad 接话积极度（注册表 OPENAI_VAD_EAGERNESS）。非法值回落 auto。"""
    value = config.get_str("OPENAI_VAD_EAGERNESS").strip()
    return value if value in ("low", "medium", "high", "auto") else "auto"


def _default_instructions() -> str:
    """无外部指令时的默认系统提示词（与 qwen_agent 的默认语义对齐）。"""
    weekdays = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
    now = datetime.now()
    now_str = f"{now:%Y年%m月%d日 %H:%M}（{weekdays[now.weekday()]}）"
    return (
        "你是接入电话的语音 Agent。接通后请先用中文简短自我介绍。"
        "之后用口语化、简洁的方式回答对方问题，每次回答控制在两三句话以内。"
        f"当前真实日期时间是 {now_str}，这是准确信息；对方询问日期、时间、"
        "今天几号或星期几时，必须以此为准回答，不要凭记忆猜测年份。"
        "你可以调用工具帮用户完成实际操作：发送短信(send_sms，发给通话对方时号码留空、"
        "发给机主时 to 填 owner)、"
        "挂断电话(hangup_call，挂断前先说一句告别语)、查询最近收到的短信验证码"
        "(query_verification_code)。需要时主动调用对应工具，操作完成后用一句话口头确认结果。"
    )


class OpenAIVoiceAgent(VoiceAgent):
    # OpenAI Realtime pcm16 固定 24kHz mono；桥自动做 8k↔24k 重采样。
    input_rate = 24000
    output_rate = 24000

    def __init__(
        self,
        api_key: str,
        model: str,
        model_display_name: str,
        voice: str = "alloy",
        realtime_url: str | None = None,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.model_display_name = model_display_name
        self.voice = voice
        self.realtime_url = realtime_url
        self._ws: Any = None
        self._recv_task: asyncio.Task | None = None
        self._on_audio_out: Callable[[bytes], None] | None = None
        self._audio_gate = ResponseAudioGate(
            "openai",
            self._emit_audio_out,
            on_suppressed=self._nudge_after_repeat_suppressed,
            on_stuck=self._repeat_suppression_stuck,
            # 复读已开播才被判定时清设备侧积压。与 barge-in 是同一个清积压
            # 动作、不同语义——晚截是自己复读被掐，不是对端插话，走专用回调
            # 以免被计进自激笔数（见 base.set_late_cut_handler）。
            on_late_cut=self._emit_late_cut,
        )
        self._running = False
        self._handled_tool_calls: set[str] = set()
        # 当前是否有回复轮次在生成（response.created→True / response.done→False）。
        # barge-in 用：对端开口时仅在生成中才发 response.cancel，避免无回复时
        # cancel 引来一条无谓的 error 事件。
        self._response_active = False
        # 最近一次 VAD 判停时刻（speech_stopped），用于逐轮响应延迟度量；
        # 首个音频增量到达时消费并置回 None。
        self._speech_stopped_at: float | None = None
        # 失聪看门狗状态（WIL-122）：最近一次收到任何服务端事件 / 最近一次
        # 发出「有声」上行帧的时刻；warned 防止同一段失聪期重复告警。
        self._last_event_at: float | None = None
        self._last_voiced_sent_at: float | None = None
        self._deaf_warned = False
        self._deaf_task: asyncio.Task | None = None
        self._instructions: str | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
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
        self._parallel_mode = config.get_bool("PARALLEL_AGENT_CONNECT")
        self._response_lock = asyncio.Lock()
        self._response_idle = asyncio.Event()
        self._response_idle.set()
        self._response_requested = False
        self._active_response_id: str | None = None
        self._background_tasks: set[asyncio.Task] = set()
        self._vad_response_task: asyncio.Task | None = None
        self._response_ack_task: asyncio.Task | None = None
        self._response_request_generation = 0
        self._response_request_started_at = 0.0
        self._response_recovering = False
        self._completed_response_ids: set[str] = set()
        self._manual_response_task: asyncio.Task | None = None
        self._manual_response_due = False
        self._user_speaking = False

    # ---- 连接管理 ----

    def _build_url(self) -> str:
        """拼接 wss 地址：base（默认官方端点，可覆盖）+ ?model=<model>。"""
        base = (self.realtime_url or "").strip() or DEFAULT_REALTIME_URL
        if "model=" in base:
            # 覆盖 URL 已自带 model 参数（如 Azure 兼容端点）则原样使用。
            return base
        separator = "&" if "?" in base else "?"
        return f"{base}{separator}model={self.model}"

    def _tool_specs(self) -> list[dict]:
        """把 ToolRegistry 的千问嵌套格式摊平成 OpenAI Realtime 的扁平格式。

        千问: {"type": "function", "function": {"name": ..., ...}}
        OpenAI: {"type": "function", "name": ..., ...}
        """
        specs: list[dict] = []
        if self._tools is None:
            return specs
        for spec in self._tools.specs():
            function = spec.get("function")
            if isinstance(function, dict):
                specs.append({"type": spec.get("type", "function"), **function})
            else:
                specs.append(spec)
        return specs

    async def _connect(self) -> None:
        """建立 websocket 连接并发送 session.update；成功后挂到 self._ws。

        采用 GA（正式版）会话结构：``session.type="realtime"`` + 嵌套的
        ``audio.input/output``（含 pcm 采样率、server_vad、转写、音色）。
        不发 ``OpenAI-Beta`` 头——beta 形态已在多数账号停用
        （否则报 beta_api_shape_disabled）。
        """
        headers = {"Authorization": f"Bearer {self.api_key}"}
        connect_options: dict[str, Any] = {"close_timeout": 0.5} if self._parallel_mode else {}
        ws = await websockets.connect(self._build_url(), additional_headers=headers, **connect_options)
        if self._parallel_mode:
            # The call owner may cancel startup while session.update is pending.
            # Publish ownership now so stop() can close this already-open socket.
            self._ws = ws
            self._response_active = False
            self._user_speaking = False
            self._response_requested = False
            self._active_response_id = None
            self._response_recovering = False
            self._completed_response_ids.clear()
            self._manual_response_due = False
            self._response_idle.set()
            self._response_request_generation += 1

        session: dict = {
            "type": "realtime",
            "instructions": self._instructions,
            "output_modalities": ["audio"],
            "audio": {
                "input": {
                    "format": {"type": "audio/pcm", "rate": self.input_rate},
                    # 打断事件的消费见 _handle_event 的 speech_started 分支
                    # （barge-in 模式），半双工模式下仍由 call_agent 统一管理。
                    "turn_detection": {
                        "type": _turn_detection_type(),
                        # semantic_vad：按语义判断对方说完没有——「OK…」稍顿不抢，
                        # 完整问句立刻接（server_vad 只看静音时长，两头顾不上）。
                        **(
                            {"eagerness": _vad_eagerness()}
                            if _turn_detection_type() == "semantic_vad"
                            else {
                                # 电话场景收窄静默判停窗（OpenAI 默认 500ms），
                                # 缩短「对方说完→AI 开口」；0/负值=服务端默认。
                                **(
                                    {"silence_duration_ms": _vad_silence_ms()}
                                    if _vad_silence_ms() > 0
                                    else {}
                                ),
                                # 能量阈值；0/负值=服务端默认。
                                **(
                                    {"threshold": _vad_threshold()}
                                    if _vad_threshold() > 0
                                    else {}
                                ),
                            }
                        ),
                        **(
                            {"create_response": False}
                            if self._manual_response_enabled or self._parallel_mode
                            else {}
                        ),
                    },
                    # 喂给 VAD 与模型前先降噪；far_field 适合电话/远场拾音，
                    # 与抬阈值同攻「判停拖尾」。空=不启用。
                    **(
                        {"noise_reduction": {"type": _noise_reduction()}}
                        if _noise_reduction()
                        else {}
                    ),
                    "transcription": {"model": TRANSCRIPTION_MODEL},
                },
                "output": {
                    "format": {"type": "audio/pcm", "rate": self.output_rate},
                    "voice": self.voice,
                },
            },
        }
        max_tokens = config.get_int("OPENAI_MAX_RESPONSE_TOKENS")
        if max_tokens > 0:
            # 从源头限长，比本地丢下行更干净：转写与对端听到的一致，不会出现
            # 「日志里说了 4 句、对端只听到 2 句」（WIL-112）。
            # 字段名 max_output_tokens 是**实测**出来的：GA 会话结构下
            # max_response_output_tokens / max_tokens / audio.output.max_output_tokens
            # 都被服务端拒为 unknown_parameter（2026-08-12 连线逐个试过）。
            session["max_output_tokens"] = max_tokens

        tool_specs = self._tool_specs()
        if tool_specs:
            session["tools"] = tool_specs
            logger.info("已为会话注册 %d 个工具", len(tool_specs))

        try:
            await ws.send(json.dumps({"type": "session.update", "session": session}))
        except BaseException:
            if self._parallel_mode:
                if self._ws is ws:
                    self._ws = None
                await self._close_parallel_socket(ws)
            raise
        self._ws = ws
        logger.info("OpenAI Realtime 连接已建立: %s", self.model)

    def _compose_instructions(self, base: str | None) -> str:
        text = base or _default_instructions()
        # OpenAI-only 说话 Vibe：追加在 VOICE_STYLE（已并入 instructions）之后。
        vibe_line = openai_vibe_line()
        if vibe_line:
            text = f"{text.rstrip()}\n{vibe_line}"
        return text

    async def update_session_instructions(self, instructions: str) -> bool:
        """中途下发新的系统提示词（Realtime session.update 是增量合并）。"""
        self._session_instructions = instructions
        self._instructions = self._compose_instructions(instructions)
        ws = self._ws
        if ws is None:
            # 还没连上：start() 会带着新的 instructions 建会话，等价生效。
            return False
        try:
            await ws.send(
                json.dumps(
                    {
                        "type": "session.update",
                        "session": {
                            "type": "realtime",
                            "instructions": self._instructions,
                        },
                    }
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("中途更新会话提示词失败: error_type=%s", type(exc).__name__)
            return False
        logger.info("会话提示词已中途更新")
        return True

    async def cancel_response(self) -> bool:
        """打断本轮生成（Realtime ``response.cancel``）。

        注意它掐的是**生成**，不是**播放**：模型把整轮音频以突发写入，远快于
        实时播放（见 ``call_log._build_stereo_mix`` 的注释），所以 cancel 之后
        已经排进 ``_outgoing_audio`` 的那部分仍会照常播完。这正是我们要的语义
        ——对端最多听到闸门允许的那么长，而不是被硬生生切掉半个字。
        """
        ws = self._ws
        if ws is None:
            return False
        if self._parallel_mode:
            return await self._cancel_parallel_response(ws, self._active_response_id)
        try:
            await ws.send(json.dumps({"type": "response.cancel"}))
        except Exception as exc:  # noqa: BLE001
            logger.warning("打断本轮回复失败: error_type=%s", type(exc).__name__)
            return False
        return True

    async def _cancel_parallel_response(self, ws: Any, response_id: str | None) -> bool:
        if ws is not self._ws or response_id is None or self._response_recovering:
            return False
        try:
            await ws.send(json.dumps({"type": "response.cancel", "response_id": response_id}))
        except Exception as exc:  # noqa: BLE001
            logger.warning("打断本轮回复失败: error_type=%s", type(exc).__name__)
            return False
        return True

    async def start(self, on_audio_out: Callable[[bytes], None]) -> None:
        self._loop = asyncio.get_running_loop()
        self._on_audio_out = on_audio_out
        self._running = True
        self._manual_response_enabled = config.get_bool("MANUAL_RESPONSE_CONTROL")
        self._reset_manual_response_state()
        self._instructions = self._compose_instructions(self._session_instructions)
        await self._connect()
        self._last_event_at = time.monotonic()
        self._recv_task = asyncio.create_task(self._recv_loop())
        self._deaf_task = asyncio.create_task(self._deaf_watchdog())

    async def _reconnect(self, *, announce: bool = True) -> bool:
        """断线重连（参照 qwen_agent 语义）；全部失败返回 False。"""
        max_attempts = _reconnect_max()
        for attempt in range(1, max_attempts + 1):
            if not self._running:
                return True  # 已主动 stop：不算失败，也不再重连
            logger.warning(
                "OpenAI Realtime 尝试重连(第 %d/%d 次)", attempt, max_attempts
            )
            try:
                await self._connect()
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "OpenAI Realtime 重连失败(第 %d/%d 次): %s",
                    attempt, max_attempts, exc,
                )
                continue
            if not self._running:
                # stop() 在重连期间完成：关闭刚建立的连接，避免泄漏。
                stale, self._ws = self._ws, None
                if stale is not None:
                    try:
                        await stale.close()
                    except Exception:  # noqa: BLE001
                        pass
                return True
            logger.info("OpenAI Realtime 重连成功(第 %d/%d 次)", attempt, max_attempts)
            if announce:
                try:
                    await self.say(RECONNECT_NOTICE)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("重连后安抚语发送失败: %s", exc)
            return True
        return False

    async def _deaf_watchdog(self) -> None:
        """provider 失聪看门狗（WIL-122）：有声上行仍在发、服务端却持续零事件时
        先告警落证据，超限主动断开 ws 借道既有重连路径自愈。

        只在「_ws 非 None」时判定：重连窗口本身有专门日志，不重复掺和。
        断开动作只 close 不重连——recv 循环收到 ConnectionClosed 后走统一的
        重连/fatal 路径，避免两处重连逻辑竞态。
        """
        while self._running:
            await asyncio.sleep(5.0)
            if self._running:
                await self._deaf_tick(time.monotonic())

    async def _deaf_tick(self, now: float) -> None:
        """看门狗单拍判定；从循环拆出便于单测（不含睡眠与运行态判断）。"""
        if self._ws is None:
            return
        last_event = self._last_event_at
        last_voiced = self._last_voiced_sent_at
        if last_event is None or last_voiced is None:
            return
        if (now - last_voiced) >= _DEAF_VOICED_WINDOW_SECONDS:
            return  # 近窗口没有声上行：安静等待，不算失聪
        silent_for = now - last_event
        if silent_for < _DEAF_WARN_SECONDS:
            return
        if not self._deaf_warned:
            self._deaf_warned = True
            logger.warning(
                "provider 疑似失聪：有声上行仍在发，但已连续 %.0fs "
                "未收到任何服务端事件",
                silent_for,
            )
            # 借 latency 事件通道落盘（{stage, ms} 形状吻合），让
            # events.jsonl 里留下失聪证据（WIL-95 归因惯例）。
            self._emit_latency("provider_deaf", round(silent_for * 1000, 1))
        if silent_for >= _DEAF_RECONNECT_SECONDS:
            logger.error(
                "provider 失聪 %.0fs，主动断开连接触发重连自愈", silent_for
            )
            self._emit_latency(
                "provider_deaf_reconnect", round(silent_for * 1000, 1)
            )
            # 重置计时：close 到 recv 循环真正察觉有窗口，别重复 close。
            self._last_event_at = now
            ws = self._ws
            if ws is not None:
                try:
                    await ws.close()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("失聪看门狗关闭连接异常: %s", exc)

    # ---- 收发 ----

    def _emit_audio_out(self, pcm: bytes) -> None:
        if self._on_audio_out:
            self._on_audio_out(pcm)

    async def send_audio(self, pcm: bytes) -> None:
        ws = self._ws
        if not ws or not pcm:
            return
        payload = json.dumps({
            "type": "input_audio_buffer.append",
            "audio": base64.b64encode(pcm).decode("ascii"),
        })
        try:
            await ws.send(payload)
        except Exception as exc:  # noqa: BLE001
            # 连接刚死：丢弃本帧，重连由接收循环统一负责。
            logger.warning("发送音频失败，丢弃本帧: %s", exc)
            return
        # 失聪看门狗的「有声上行」时钟：只记有声帧，纯静默不喂时钟，
        # 否则安静等待期会被误判成失聪。峰值扫描每帧几百样本，开销可忽略。
        usable = len(pcm) - (len(pcm) % 2)
        if usable:
            samples = array("h", pcm[:usable])
            if max(max(samples), -min(samples)) >= _DEAF_VOICED_PEAK_THRESHOLD:
                self._last_voiced_sent_at = time.monotonic()

    async def say(self, instructions: str) -> None:
        ws = self._ws
        if not ws:
            return
        try:
            message = {
                "type": "response.create",
                "response": {"instructions": instructions},
            }
            if self._parallel_mode:
                await self._request_response(ws, message, source="say")
            else:
                await ws.send(json.dumps(message))
        except Exception as exc:  # noqa: BLE001
            # 断线窗口内 say 失败不应炸掉整通电话（开场白路径直接 await）；
            # 重连由接收循环统一负责。
            logger.warning("发送说话指令失败: %s", exc)

    def _spawn_response_task(self, operation: Coroutine[Any, Any, Any]) -> asyncio.Task:
        task = asyncio.create_task(operation)
        if self._parallel_mode:
            self._background_tasks.add(task)
            task.add_done_callback(self._response_task_done)
        return task

    def _response_task_done(self, task: asyncio.Task) -> None:
        self._background_tasks.discard(task)
        if not task.cancelled():
            error = task.exception()
            if error is not None:
                logger.warning("OpenAI 后台任务失败: error_type=%s", type(error).__name__)

    async def _request_response(self, ws: Any, message: dict, *, source: str) -> None:
        task = asyncio.current_task()
        was_owned = task in self._background_tasks
        if task is not None:
            self._background_tasks.add(task)
        try:
            async with self._response_lock:
                while self._response_active or self._response_requested:
                    await self._response_idle.wait()
                    if not self._running or self.fatal or ws is not self._ws:
                        return
                if not self._running or self.fatal or ws is not self._ws:
                    return
                if self._response_recovering:
                    return
                if source in ("vad", "manual") and self._user_speaking:
                    if source == "manual":
                        self._manual_response_due = True
                    return  # A queued old stop must not answer across a newer speech_started.
                # Reserve and arm BEFORE send yields: even created/done may
                # arrive while the websocket is draining its outgoing buffer.
                self._response_requested = True
                self._response_idle.clear()
                self._response_request_generation += 1
                generation = self._response_request_generation
                self._response_request_started_at = time.monotonic()
                if source == "manual":
                    self._manual_response_due = False
                message = {**message, "response": {
                    **message.get("response", {}),
                    "metadata": {"agentcall_request": str(self._response_request_generation)},
                }}
                self._response_ack_task = self._spawn_response_task(self._watch_response_ack(
                    ws, self._response_request_generation,
                ))
            # Admission is reserved above; do not hold the lock across a socket
            # drain, which may itself be stalled when recovery replaces the socket.
            try:
                await ws.send(json.dumps(message))
            except BaseException:
                # Closing a fenced socket may fail an in-flight send. That is
                # recovery, not a fatal error on the replacement connection.
                if (self._running and ws is self._ws and not self._response_recovering
                        and generation == self._response_request_generation):
                    self.fatal = True
                raise
            logger.info("[timing] OpenAI response.create source=%s request=%d", source, generation)
        finally:
            if task is not None and not was_owned:
                self._background_tasks.discard(task)

    async def _watch_response_ack(self, ws: Any, generation: int) -> None:
        await asyncio.sleep(_MANUAL_RESPONSE_CREATED_TIMEOUT_SECONDS)
        if (self._running and ws is self._ws and self._response_requested
                and generation == self._response_request_generation
                and not self._response_recovering):
            elapsed = time.monotonic() - self._response_request_started_at
            logger.warning("OpenAI response.created 等待超时 request=%d elapsed=%.3fs；"
                           "隔离旧连接并重连，不结束通话", generation, elapsed)
            self._emit_latency("response_ack_timeout", round(elapsed * 1000, 1))
            # Missing ACK is not evidence of idleness. Fence this socket until
            # recv_loop reconnects; never release admission on the uncertain session.
            self._response_recovering = True
            await self._close_parallel_socket(ws)

    def _accept_parallel_response_event(self, event: dict) -> bool:
        response = event.get("response") or {}
        request = (response.get("metadata") or {}).get("agentcall_request")
        response_id = _response_id(event)
        stale = (
            (request is not None and request != str(self._response_request_generation))
            or response_id in self._completed_response_ids
            or (self._active_response_id is not None and response_id != self._active_response_id)
        )
        if stale:
            logger.info("OpenAI 忽略迟到回复事件 type=%s response=%s request=%s current=%d",
                        event.get("type"), response_id, request, self._response_request_generation)
            return False
        # A done without a created may acknowledge only its own tagged request,
        # not an unidentified old cancellation on a new pending request.
        if event.get("type") == "response.done" and self._active_response_id is None:
            return self._response_requested and request is not None
        return self._response_requested or self._response_active

    async def external_tool_result(
        self,
        name: str,
        result: dict[str, Any],
        *,
        source: str,
    ) -> bool:
        """Append a supported system item; do not fabricate a function call."""

        ws = self._ws
        if ws is None:
            return False
        success = result.get("success") is True
        count = result.get("count")
        safe_count = count if isinstance(count, int) and count >= 0 else 0
        mode = result.get("mode")
        safe_mode = mode if isinstance(mode, str) else "unknown"
        text = (
            f"[external_tool_result] {name} was executed by {source}; "
            f"success={str(success).lower()}, count={safe_count}, mode={safe_mode}. "
            "This is context only; do not speak merely to acknowledge it."
        )
        try:
            await ws.send(
                json.dumps(
                    {
                        "type": "conversation.item.create",
                        "item": {
                            "type": "message",
                            "role": "system",
                            "content": [{"type": "input_text", "text": text}],
                        },
                    }
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("写入外部工具结果失败: error_type=%s", type(exc).__name__)
            return False
        return True

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
        if self._manual_response_enabled:
            self._schedule_manual_response_timer()

    def _schedule_manual_response_timer(self) -> None:
        now = time.monotonic()
        with self._manual_response_lock:
            if not self._running or self._ws is None:
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
        if self._parallel_mode:
            # The timer owns debounce only. All response state lives on the event loop.
            with self._manual_response_lock:
                if generation != self._manual_response_generation or not self._running:
                    return
                self._manual_response_timer = None
                self._manual_response_first_at = None
                ws, loop = self._ws, self._loop
            if ws is not None and loop is not None and loop.is_running():
                loop.call_soon_threadsafe(self._queue_parallel_manual_response, ws)
            return
        with self._manual_response_lock:
            if generation != self._manual_response_generation or not self._running:
                return
            self._manual_response_timer = None
            if self._manual_response_outstanding > 0:
                self._manual_response_pending_after_flight = True
                self._manual_response_first_at = None
                return
            ws = self._ws
            loop = self._loop
            if ws is None or loop is None or not loop.is_running():
                self._manual_response_first_at = None
                return
            self._manual_response_first_at = None
            self._manual_response_request_pending = True
            self._start_manual_response_watchdog_locked(
                _MANUAL_RESPONSE_CREATED_TIMEOUT_SECONDS
            )

        try:
            future = asyncio.run_coroutine_threadsafe(
                self._send_manual_response(ws), loop
            )
            future.result(timeout=_MANUAL_RESPONSE_SEND_TIMEOUT_SECONDS)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "手动触发 OpenAI 回复失败: error_type=%s", type(exc).__name__
            )
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

    def _queue_parallel_manual_response(self, ws: Any) -> None:
        if (ws is None or not self._running or self.fatal
                or ws is not self._ws or self._response_recovering):
            return
        self._manual_response_due = True
        if self._manual_response_task is None or self._manual_response_task.done():
            self._manual_response_task = self._spawn_response_task(self._send_manual_response(ws))
            self._manual_response_task.add_done_callback(self._parallel_manual_response_sent)

    def _parallel_manual_response_sent(self, task: asyncio.Task) -> None:
        # created/done can precede send completion; retain any debounce that
        # arrived during that drain instead of losing the next turn.
        if (not task.cancelled() and task.exception() is None
                and self._manual_response_due and not self._user_speaking):
            self._queue_parallel_manual_response(self._ws)

    async def _send_manual_response(self, ws: Any) -> None:
        if ws is not self._ws or not self._running:
            raise ConnectionError("Realtime connection changed")
        if self._parallel_mode:
            await self._request_response(ws, {"type": "response.create"}, source="manual")
        else:
            await ws.send(json.dumps({"type": "response.create"}))

    def _on_response_created(self) -> None:
        if self._parallel_mode or not self._manual_response_enabled:
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
            timeout_seconds,
            self._fire_manual_response_watchdog,
            args=(generation,),
        )
        next_timer.daemon = True
        self._manual_response_watchdog_timer = next_timer
        next_timer.start()

    def _fire_manual_response_watchdog(self, generation: int) -> None:
        if self._parallel_mode:
            return
        should_schedule = False
        with self._manual_response_lock:
            if (
                generation != self._manual_response_watchdog_generation
                or not self._running
                or (
                    not self._manual_response_request_pending
                    and self._manual_response_outstanding <= 0
                )
            ):
                return
            logger.warning(
                "手动应答控制等待 OpenAI response.created/done 超时，清理状态"
            )
            self._manual_response_watchdog_timer = None
            self._manual_response_request_pending = False
            self._manual_response_outstanding = 0
            if self._manual_response_pending_after_flight:
                self._manual_response_pending_after_flight = False
                should_schedule = True
        if should_schedule:
            self._schedule_manual_response_timer()

    def _on_response_done(self) -> None:
        if self._parallel_mode:
            if self._manual_response_enabled and self._manual_response_due:
                self._queue_parallel_manual_response(self._ws)
            return
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
        try:
            asyncio.get_running_loop()
            self._spawn_response_task(
                self.say(repeat_nudge_instructions(agent_language()))
            )
        except RuntimeError:
            logger.warning("发送复读换说法提示失败: event loop 不可用")

    def _repeat_suppression_stuck(self, count: int, _transcript: str) -> None:
        self._emit_repeat_stuck(f"复读抑制连续触发 {count} 次，判定模型卡死")

    async def stop(self) -> None:
        with self._manual_response_lock:
            self._running = False
            self._cancel_manual_response_timer_locked()
            self._cancel_manual_response_watchdog_locked()
            self._manual_response_first_at = None
            self._manual_response_request_pending = False
            self._manual_response_outstanding = 0
            self._manual_response_pending_after_flight = False
        ws, self._ws = self._ws, None
        if self._parallel_mode:
            await self._stop_parallel_transport(ws)
            return
        if ws:
            try:
                await ws.close()
            except Exception as exc:  # noqa: BLE001
                logger.warning("关闭 OpenAI 连接异常: %s", exc)
        if self._deaf_task:
            # 看门狗最长睡 5s 且醒来即见 _running=False 退出；直接取消最省事。
            self._deaf_task.cancel()
            await asyncio.gather(self._deaf_task, return_exceptions=True)
            self._deaf_task = None
        if self._recv_task:
            # 有界等待：接收任务可能正阻塞在重连的 websockets.connect
            # （open_timeout × 重试次数可达 20s+），而 CallSession 收尾对
            # stop() 无超时——超时就取消，别拖延整通电话的落盘。
            try:
                await asyncio.wait_for(asyncio.shield(self._recv_task), timeout=3.0)
            except asyncio.TimeoutError:
                self._recv_task.cancel()
            except Exception:  # noqa: BLE001
                pass  # 任务自身异常不影响收尾
            await asyncio.gather(self._recv_task, return_exceptions=True)
            self._recv_task = None

    async def _stop_parallel_transport(self, ws: Any) -> None:
        # Cancel recv/reconnect BEFORE closing: it cannot publish a replacement
        # socket during the closing handshake. Release response admission waiters.
        self._response_idle.set()
        tasks = set(self._background_tasks)
        tasks.update(task for task in (self._recv_task, self._deaf_task) if task is not None)
        tasks.discard(asyncio.current_task())
        for task in tasks:
            task.cancel()
        started = time.monotonic()
        try:
            if ws is not None:
                await self._close_parallel_socket(ws)
        finally:
            await asyncio.gather(*tasks, return_exceptions=True)
            self._background_tasks.clear()
            self._recv_task = None
            self._deaf_task = None
            logger.info("[timing] OpenAI 关闭完成 %.3fs", time.monotonic() - started)

    async def _close_parallel_socket(self, ws: Any) -> None:
        closing = asyncio.create_task(ws.close())
        try:
            done, _pending = await asyncio.wait({closing}, timeout=_PARALLEL_CLOSE_TIMEOUT_SECONDS)
            if not done:
                logger.warning("OpenAI websocket 关闭超时，强制终止传输")
        finally:
            failed = closing.done() and (closing.cancelled() or closing.exception() is not None)
            if not closing.done() or failed:
                transport = getattr(ws, "transport", None)
                if transport is not None:
                    transport.abort()
            if not closing.done():
                closing.cancel()
            await asyncio.gather(closing, return_exceptions=True)

    async def _recv_loop(self) -> None:
        """接收循环：连接存活期间分发事件；断线走重连，全败置 fatal。"""
        while self._running:
            ws = self._ws
            if ws is None:
                break
            try:
                async for message in ws:
                    if isinstance(message, (bytes, bytearray)):
                        continue
                    try:
                        event = json.loads(message)
                    except json.JSONDecodeError:
                        logger.warning("收到非 JSON 消息，已忽略")
                        continue
                    self._handle_event(event)
                    if not self._running:
                        break
            except websockets.ConnectionClosed:
                logger.info("OpenAI Realtime 连接已关闭")
            except Exception as exc:  # noqa: BLE001
                logger.error("OpenAI 接收循环异常: %s", exc)
            if not self._running:
                break
            self._cancel_manual_response_control()
            recovering_response = self._parallel_mode and self._response_recovering
            # 立即摘掉死连接：重连期间 send_audio/say 看到 _ws 为 None 会
            # 静默丢帧（对齐 qwen 语义），否则每 10ms 一帧的上行都对死连接
            # send 并各刷一条 warning（重连数秒内可积数百条）。
            self._ws = None
            # 通话进行中断线：尝试重连；全部失败则会话不可恢复，置 fatal
            # 让 CallSession 主循环结束整通电话（避免"电话活着但 AI 已死"）。
            logger.warning("OpenAI Realtime 运行中断线，尝试重连")
            # ACK recovery must not automatically create another reply: a silent
            # provider would otherwise create a reconnect -> notice -> timeout loop.
            reconnected = (await self._reconnect(announce=False) if recovering_response
                           else await self._reconnect())
            if not reconnected:
                logger.error("OpenAI Realtime 重连全部失败，标记会话不可恢复")
                self.fatal = True
                return
            if recovering_response:
                self._emit_latency("response_ack_reconnected", 0.0)

    def _handle_event(self, event: dict) -> None:
        event_type = event.get("type", "")
        if self._parallel_mode and self._response_recovering:
            return  # Drain late old-session events, including audio, while closing.
        # 任何服务端事件都喂失聪看门狗的时钟（含未识别类型）。
        self._last_event_at = time.monotonic()
        self._deaf_warned = False
        if event_type in ("response.audio.delta", "response.output_audio.delta"):
            # beta 与 GA 两代事件名的下行音频增量
            delta = event.get("delta", "")
            if delta:
                # 逐轮响应延迟（WIL-104 度量）：VAD 判停（speech_stopped，已含
                # 静默窗）→ 本轮首个音频增量到达。到对端耳朵还要加播出+蜂窝
                # 链路 ~0.5s（固定，不在此计）。
                if self._speech_stopped_at is not None:
                    latency_ms = (time.monotonic() - self._speech_stopped_at) * 1000
                    self._speech_stopped_at = None
                    logger.info("轮次响应延迟(判停→首音频): %.0fms", latency_ms)
                    # first_audio_delta_ms（2026-08-14 定义，WIL-95 2.2：语音到
                    # 语音架构下 TTFT/TTFB 的唯一可观测合并量）→ 经回调落盘。
                    self._emit_latency("first_audio_delta", round(latency_ms, 1))
                self._audio_gate.push_audio(
                    _response_id(event), base64.b64decode(delta)
                )
        elif event_type == "input_audio_buffer.speech_stopped":
            self._user_speaking = False
            if self._parallel_mode and self._manual_response_enabled and self._manual_response_due:
                self._queue_parallel_manual_response(self._ws)
            self._speech_stopped_at = time.monotonic()
            # audio_end_ms 是 provider 认定的语音终点（其输入缓冲时间轴）。
            # 与 speech_started 的 audio_start_ms 相减 = 它听到的语音时长，
            # 拿来与本地录音对账，定位判停拖尾在哪一侧（WIL-112）。
            logger.info(
                "[timing] provider 判停 (speech_stopped, audio_end_ms=%s)",
                event.get("audio_end_ms"),
            )
            if self._parallel_mode and not self._manual_response_enabled:
                if self._vad_response_task is None or self._vad_response_task.done():
                    self._vad_response_task = self._spawn_response_task(self._request_response(
                        self._ws, {"type": "response.create"}, source="vad",
                    ))
        elif event_type == "conversation.item.input_audio_transcription.completed":
            transcript = (event.get("transcript") or "").strip()
            if transcript:
                logger.info("[上行·用户] %s", transcript)
                self._emit_transcript("user", transcript)
                self._on_user_transcription_completed()
        elif event_type in (
            "response.audio_transcript.done",
            "response.output_audio_transcript.done",
        ):
            transcript = (event.get("transcript") or "").strip()
            if transcript:
                logger.info("[下行·Agent] %s", transcript)
                suppressed = self._audio_gate.complete_transcript(
                    _response_id(event), transcript
                )
                if not suppressed:
                    self._emit_transcript("agent", transcript)
        elif event_type == "response.function_call_arguments.done":
            name = event.get("name")
            call_id = event.get("call_id")
            arguments = event.get("arguments") or ""
            logger.info("OpenAI 请求调用工具 %s (call_id=%s)", name, call_id)
            if name and call_id:
                # 独立任务执行，避免工具耗时阻塞接收循环；捕获当前连接，
                # 防止工具执行期间断线重连后把旧 call_id 的结果发进新会话。
                self._spawn_response_task(
                    self._dispatch_tool_call(name, call_id, arguments, self._ws)
                )
        elif event_type == "response.created":
            if self._parallel_mode and not self._accept_parallel_response_event(event):
                return
            self._response_active = True
            if self._parallel_mode:
                self._response_requested = False
                self._active_response_id = _response_id(event)
                self._response_idle.clear()
                if self._response_ack_task is not None:
                    self._response_ack_task.cancel()
                elapsed_ms = (time.monotonic() - self._response_request_started_at) * 1000
                logger.info("[timing] OpenAI response.created response=%s request=%d elapsed=%.3fs",
                            self._active_response_id, self._response_request_generation, elapsed_ms / 1000)
                self._emit_latency("response_created", round(elapsed_ms, 1))
            self._on_response_created()
        elif event_type == "input_audio_buffer.speech_started":
            self._user_speaking = True
            # 与 call_agent 的「本地检测到语音起点」对表用（WIL-112 暗区定位）。
            # audio_start_ms 是 provider 认定的语音起点在其输入缓冲时间轴上的
            # 位置——与 speech_stopped 的 audio_end_ms 相减即它听到的语音时长。
            logger.info(
                "[timing] provider 判定开口 (speech_started, audio_start_ms=%s)",
                event.get("audio_start_ms"),
            )
            # barge-in：对端在 AI 说话期间开口 → 先问 call_agent 接不接受这次
            # 打断（DTMF 护窗期让位、自激兜底退回半双工时会拒绝，WIL-94 坑 1/4），
            # 接受才掐当前生成；本地未播积压由回调自己清。未注册回调
            # （半双工模式，BARGE_IN_ENABLED=false）时维持原行为：事件忽略。
            if self._on_user_interrupt is not None:
                if self._emit_user_interrupt() and self._response_active:
                    if self._parallel_mode:
                        # Capture now, not when the coroutine eventually runs: an
                        # old barge-in must never cancel a subsequent response.
                        self._spawn_response_task(self._cancel_parallel_response(
                            self._ws, self._active_response_id,
                        ))
                    else:
                        self._spawn_response_task(self.cancel_response())
        elif event_type == "response.done":
            if self._parallel_mode and not self._accept_parallel_response_event(event):
                return
            self._response_active = False
            if self._parallel_mode:
                response_id = _response_id(event)
                if response_id is not None:
                    self._completed_response_ids.add(response_id)
                logger.info("[timing] OpenAI response.done response=%s request=%d",
                            response_id, self._response_request_generation)
                self._response_requested = False
                self._active_response_id = None
                self._response_idle.set()
                if self._response_ack_task is not None:
                    self._response_ack_task.cancel()
            status = (event.get("response") or {}).get("status")
            if status in ("failed", "incomplete"):
                # 轮次异常结束（内容审核/额度/服务端错误）：连接还活着，
                # 记 error 便于排查"通着但沉默"。
                logger.error("OpenAI 回复轮次异常结束: %s", event.get("response"))
            else:
                self._audio_gate.complete_response(_response_id(event))
                # INFO 而非 debug（WIL-122）：真机排查「通着但沉默」时，响应
                # 轮次是否正常收尾是关键证据，debug 级在现场日志里看不见。
                logger.info("OpenAI 回复轮次完成 (status=%s)", status)
            self._on_response_done()
            self._emit_output_complete(success=status not in {"failed", "incomplete", "cancelled"})
        elif event_type == "error":
            code = (event.get("error") or {}).get("code")
            if code == "response_cancel_not_active":
                # barge-in 的 cancel 与 response.done 的固有竞态：response.created
                # 后我们置 _response_active，但服务端可能在 cancel 送达前已生成完。
                # 无副作用，降级为 info 免得像故障。
                logger.info("response.cancel 晚到（响应已自然结束），忽略")
            else:
                logger.error("OpenAI Realtime 错误: %s", event)
        else:
            # 未识别的事件类型不再完全静默（WIL-122）：若服务端引入新形态的
            # 会话级错误/状态事件，至少 debug 级留痕可查。
            logger.debug("未处理的 OpenAI 事件类型: %s", event_type)

    async def _dispatch_tool_call(
        self, name: str, call_id: str, arguments: str, ws: Any
    ) -> None:
        if call_id in self._handled_tool_calls:
            return
        self._handled_tool_calls.add(call_id)
        # 任何一步失败都要给模型回一个错误形状的结果，否则模型会
        # 永远等 function_call_output（对方只听到沉默）。
        try:
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
            if self._tools is not None:
                # dispatch 可能有阻塞 IO（发短信/AT 指令），放线程池执行。
                result = await asyncio.to_thread(self._tools.dispatch, name, args)
            else:
                result = {"success": False, "message": "无可用工具"}
        except Exception as exc:  # noqa: BLE001
            if name in SMS_TOOL_NAMES:
                logger.error("工具 %s 执行异常: error_type=%s", name, type(exc).__name__)
            else:
                logger.exception("工具 %s 执行异常: %s", name, exc)
            result = {"success": False, "message": f"工具执行异常: {exc}"}

        if ws is None or ws is not self._ws:
            # 工具执行期间连接已更换/关闭：旧 call_id 对新会话无效，丢弃结果。
            logger.warning(
                "工具 %s 结果因连接已更换而丢弃 (call_id=%s)", name, call_id
            )
            return
        try:
            output = json.dumps(result, ensure_ascii=False)
        except (TypeError, ValueError) as exc:
            if name in SMS_TOOL_NAMES:
                logger.warning("工具 %s 结果无法序列化: error_type=%s", name, type(exc).__name__)
            else:
                logger.warning("工具 %s 结果无法序列化: %r", name, result)
            output = json.dumps(
                {"success": False, "message": "工具结果无法序列化"},
                ensure_ascii=False,
            )
        try:
            await ws.send(json.dumps({
                "type": "conversation.item.create",
                "item": {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": output,
                },
            }))
            # hangup 后不再回复；DTMF 后等待 IVR 的下一段音频自然触发回复。
            if name not in TERMINAL_TOOLS and name not in SILENT_AFTER_TOOLS:
                if self._parallel_mode:
                    await self._request_response(ws, {"type": "response.create"}, source="tool")
                else:
                    await ws.send(json.dumps({"type": "response.create"}))
        except Exception as exc:  # noqa: BLE001
            logger.warning("回传工具结果失败: %s", exc)
