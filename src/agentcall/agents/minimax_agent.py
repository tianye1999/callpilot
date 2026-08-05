"""MiniMax Realtime 实时语音 Agent（复用 OpenAIVoiceAgent 的连接/重连/工具骨架）。

MiniMax 的 realtime 端点（``wss://api.minimaxi.com/ws/v1/realtime``）说的是
**OpenAI Realtime beta 协议**：``session.created`` / ``session.update`` /
``conversation.item.create`` / ``input_audio_buffer.append`` /
``response.audio.delta`` / ``response.audio_transcript.done`` / ``response.done``
全部同名，音频双向 pcm16 @ 24kHz。父类 ``_handle_event`` 本就同时容忍 beta 与
GA 两代事件名，所以事件处理可直接复用。

以下差异全部来自 2026-08-04 的真机实测（key 实测于国内区），不是照文档推断：

1. **``session.tools`` 被静默丢弃** —— 扁平/嵌套两种写法、加不加 ``tool_choice``
   都一样，``session.updated`` 回显里没有 ``tools`` 字段，也从不产生
   ``response.function_call_arguments.done``。本实现因此采用混合模式：Realtime
   继续听说，文本侧 MiniMax-M3 审计 Realtime 的行动提案并执行注册工具。
2. **没有服务端 VAD** —— ``turn_detection`` 同样被丢弃；只推
   ``input_audio_buffer.append`` 而不显式 commit 时，20s 内零事件、全程沉默。
   因此断句必须由本端判定：见 :meth:`send_audio` 的能量 VAD。
3. **不发用户侧转写事件** —— ``input_audio_transcription`` 配置被接受，但
   ``conversation.item.input_audio_transcription.completed`` 从不到达。后果是
   转写只有 Agent 侧，通话摘要与 DTMF 判官会因缺少对方发言而降质。
4. **``response.create`` 不接受 ``response.instructions``** —— 直接发会报
   ``2013 no context items provided``。``say()`` 改为先写一条 ``role="system"``
   的上下文 item 再触发回复。
5. **``conversation.item.create`` 的 item 必须显式带 ``status="completed"``**，
   否则报 ``1000 invalid status: ''``；且 ``role="user"``/``"system"`` 的
   content type 必须是 ``input_text``（``"text"`` 会被拒）。
6. **``max_response_output_tokens`` 必须是字符串**，传数字报
   ``1000 json: cannot unmarshal number``。服务端默认只有 ``"256"``，
   电话场景会把回答截断，所以这里显式放大。

端点只提供 ``abab6.5s-chat``（``?model=`` 查询参数被忽略），拿不到 M3。
国际站 ``api.minimax.io`` 与国内区 key 不通用（实测回 401）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from collections import deque
from typing import Any

import numpy as np
import websockets

from .. import config
from .minimax_hybrid import (
    DEFAULT_TEXT_MODEL,
    DEFAULT_TEXT_URL,
    MiniMaxM3ToolRouter,
    might_request_tool,
)
from .openai_agent import OpenAIVoiceAgent, _response_id
from .tools import SILENT_AFTER_TOOLS, TERMINAL_TOOLS

logger = logging.getLogger(__name__)

# 国内区 realtime 端点；可经 MINIMAX_REALTIME_URL 覆盖。
DEFAULT_REALTIME_URL = "wss://api.minimaxi.com/ws/v1/realtime"

# 上行转写模型（session.input_audio_transcription）。配置被接受但不回事件，
# 仍照发：服务端将来补上事件时无需改代码。
TRANSCRIPTION_MODEL = "asr-01"

# 服务端默认 "256" 会把电话里的回答截断；协议要求本字段是字符串。
MAX_RESPONSE_OUTPUT_TOKENS = "1024"

# SIM7600 上行电平会随对端/网络变化几个数量级。固定 400 RMS 在 IVR 上正常，
# 但真人手机语音常只有 40~300 RMS。VAD 用噪底自适应阈值，并要求连续人声，
# 避免把单个脉冲当作一句话；送模型前另做峰值受限的安全自动增益。
_VAD_NOISE_WINDOW_FRAMES = 200
_VAD_NOISE_MULTIPLIER = 4.0
_VAD_MIN_SPEECH_MS = 100.0
_LOW_SIGNAL_DIAGNOSTIC_MS = 5000.0
_AUTO_GAIN_TARGET_PEAK = 12000.0

_HYBRID_INSTRUCTIONS = (
    "\n你具备由外部控制器执行电话工具的能力。需要操作时，先生成一句简短、"
    "参数明确的行动句：发短信必须说清正文，按电话菜单必须说清按键数字，"
    "查验证码或转机主必须明确说明，结束通话先自然道别。不要声称工具不可用。"
    "外部控制器会在播放前审计行动句；执行后以它返回的真实结果为准。"
)

_SEMANTIC_WAIT_MARKER = "[[WAIT]]"
_SEMANTIC_TURN_INSTRUCTIONS = (
    "\n对每次提交的对方语音，先根据完整语义判断对方此刻是否在等你回答。"
    "如果是明确提问、请求、确认、选择题或要求语音输入，必须直接、简短、及时回答，"
    "不要仅因不确定而沉默。如果只是系统播报、等待音乐、查询中提示、尚未说完的句子，"
    "或不需要你回应，请只输出精确标记 [[WAIT]]，不得添加任何其他文字。"
    "[[WAIT]] 是内部控制标记，不是要说给对方听的内容。不要用客套话填充停顿。"
)


class MiniMaxVoiceAgent(OpenAIVoiceAgent):
    """MiniMax Realtime。协议与能力差异见模块 docstring。"""

    # 实测输出为 pcm16 @ 24kHz（基频反推 226Hz 落在女声区，有效能量到 9.4kHz——
    # 16k 采样的 Nyquist 只有 8k，物理上不可能）；输入 16k/24k 均可被理解，
    # 取 24k 与输出对称，也与 beta 协议对 pcm16 的约定一致。
    input_rate = 24000
    output_rate = 24000

    provider_label = "MiniMax"
    reconnect_max_key = "MINIMAX_RECONNECT_MAX"
    # 说话 Vibe 是 OpenAI 专属，MiniMax 侧无对应能力。
    supports_vibe = False

    def __init__(
        self,
        *args: Any,
        hybrid_tools_enabled: bool = True,
        text_model: str = DEFAULT_TEXT_MODEL,
        text_url: str = DEFAULT_TEXT_URL,
        tool_timeout: float = 10.0,
        tool_router: MiniMaxM3ToolRouter | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        # ---- 客户端 VAD 状态（见 send_audio）----
        self._vad_lock = threading.Lock()
        self._vad_speech_seen = False
        self._vad_silence_ms = 0.0
        self._vad_utterance_ms = 0.0
        self._vad_response_in_flight = False
        self._vad_candidate_ms = 0.0
        self._vad_candidate_peak = 0.0
        self._vad_noise_samples: deque[float] = deque(
            maxlen=_VAD_NOISE_WINDOW_FRAMES
        )
        self._vad_unheard_ms = 0.0
        self._vad_unheard_peak = 0.0
        self._vad_low_signal_logged = False
        self._turn_end_silence_ms: int | None = None
        self._semantic_turns_enabled = False
        self._turn_deferred_audio: list[bytes] = []
        self._turn_deferred_ms = 0.0
        self._turn_stale_response = False
        self._turn_stale_logged = False
        # ---- MiniMax Realtime + M3 混合工具路由 ----
        self._hybrid_tools_enabled = hybrid_tools_enabled
        self._hybrid_tool_timeout = max(1.0, tool_timeout)
        self._hybrid_router = tool_router or MiniMaxM3ToolRouter(
            api_key=self.api_key,
            model=text_model,
            url=text_url,
            timeout=self._hybrid_tool_timeout,
        )
        self._hybrid_tasks: set[asyncio.Task[Any]] = set()
        # 语义轮次与 M3 工具路由共用同一层“先扣住语音、看完转写再裁决”的门。
        self._gated_processed: set[str] = set()
        self._gated_held: set[str] = set()
        self._hybrid_followup_pending = 0

    def _hybrid_active(self) -> bool:
        return bool(
            self._hybrid_tools_enabled
            and self._tools is not None
            and self._tools.has_tools()
        )

    def _spawn_hybrid_task(self, coroutine: Any) -> None:
        task = asyncio.get_running_loop().create_task(coroutine)
        self._hybrid_tasks.add(task)
        task.add_done_callback(self._hybrid_tasks.discard)

    # ---- 连接 ----

    def _build_url(self) -> str:
        """MiniMax 忽略 ``?model=``，所以不拼该参数，避免误导性的 URL。"""
        return (self.realtime_url or "").strip() or DEFAULT_REALTIME_URL

    async def _connect(self) -> None:
        """建连并发 beta 形态的 ``session.update``。

        不发 ``tools``：实测被静默丢弃（见模块 docstring 第 1 条），发了只会让
        日志看起来"已注册工具"而实际不可用，掩盖真实能力边界。
        """
        headers = {"Authorization": f"Bearer {self.api_key}"}
        ws = await websockets.connect(self._build_url(), additional_headers=headers)

        instructions = self._instructions
        if self._semantic_turns_enabled:
            instructions = (
                f"{(instructions or '').rstrip()}{_SEMANTIC_TURN_INSTRUCTIONS}"
            )
        if self._hybrid_active():
            instructions = f"{(instructions or '').rstrip()}{_HYBRID_INSTRUCTIONS}"
        session: dict = {
            "modalities": ["audio", "text"],
            "instructions": instructions,
            "voice": self.voice,
            "input_audio_format": "pcm16",
            "output_audio_format": "pcm16",
            "input_audio_transcription": {"model": TRANSCRIPTION_MODEL},
            # 必须字符串，传数字服务端 Go unmarshal 报错。
            "max_response_output_tokens": MAX_RESPONSE_OUTPUT_TOKENS,
        }
        await ws.send(json.dumps({"type": "session.update", "session": session}))
        self._ws = ws
        self._reset_vad()

        if self._hybrid_active():
            logger.info(
                "MiniMax 混合工具模式已启用: Realtime 听说 + %s 工具决策（%d 个工具）",
                self._hybrid_router.model,
                len(self._tools.specs()) if self._tools is not None else 0,
            )
            self._emit_trace(
                "tool_router", "hybrid_ready", "ok",
                capability="minimax_m3_tools",
            )
        elif self._tools is not None and self._tools.has_tools():
            logger.warning(
                "MiniMax 混合工具模式已关闭；Realtime 会丢弃 session.tools，"
                "已注册的 %d 个工具本通电话不会生效",
                len(self._tools.specs()),
            )
        if self._manual_response_enabled:
            logger.warning(
                "MANUAL_RESPONSE_CONTROL 对 MiniMax 无效（服务端丢弃 turn_detection，"
                "且不发用户转写事件）；断句一律由本端能量 VAD 负责"
            )
        logger.info("MiniMax Realtime 连接已建立: %s", self.model)
        self._emit_trace(
            "model",
            "capability_notice",
            "warning",
            capability="no_user_transcript",
        )
        if self._semantic_turns_enabled:
            self._emit_trace(
                "turn", "semantic_policy_ready", "ok",
                policy="model_meaning",
            )

    # ---- 客户端 VAD ----

    def _reset_vad(self) -> None:
        with self._vad_lock:
            self._vad_speech_seen = False
            self._vad_silence_ms = 0.0
            self._vad_utterance_ms = 0.0
            self._vad_response_in_flight = False
            self._vad_candidate_ms = 0.0
            self._vad_candidate_peak = 0.0
            self._vad_noise_samples.clear()
            self._vad_unheard_ms = 0.0
            self._vad_unheard_peak = 0.0
            self._vad_low_signal_logged = False
            self._turn_deferred_audio.clear()
            self._turn_deferred_ms = 0.0
            self._turn_stale_response = False
            self._turn_stale_logged = False

    def configure_turn_taking(
        self,
        *,
        silence_ms: int | None = None,
        semantic: bool = False,
    ) -> None:
        with self._vad_lock:
            self._turn_end_silence_ms = (
                max(0, int(silence_ms)) if silence_ms is not None else None
            )
            self._semantic_turns_enabled = bool(semantic)

    @staticmethod
    def _is_semantic_wait(transcript: str) -> bool:
        """Recognize only the private protocol marker, never business keywords."""
        normalized = "".join((transcript or "").split()).upper()
        return normalized.rstrip("。.!！") == _SEMANTIC_WAIT_MARKER

    def notify_remote_speech(self) -> None:
        resumed = False
        with self._vad_lock:
            if (
                self._turn_end_silence_ms is not None
                and self._vad_response_in_flight
                and not self._turn_stale_response
            ):
                self._turn_stale_response = True
                resumed = True
        if resumed:
            self._emit_trace(
                "turn", "remote_resumed", "running",
                reason="response_in_flight",
            )

    @staticmethod
    def _frame_rms(pcm: bytes) -> float:
        """帧能量（int16 RMS）。奇数字节丢尾字节，避免 frombuffer 抛错。"""
        if len(pcm) < 2:
            return 0.0
        samples = np.frombuffer(pcm[: len(pcm) // 2 * 2], dtype="<i2")
        if samples.size == 0:
            return 0.0
        return float(np.sqrt(np.mean(samples.astype(np.float64) ** 2)))

    def _noise_floor_locked(self) -> float:
        if not self._vad_noise_samples:
            return 0.0
        # 低分位数不容易被偶发按键声、线路爆点或很短的语音污染。
        return float(np.percentile(tuple(self._vad_noise_samples), 20))

    def _effective_vad_threshold_locked(self) -> tuple[float, float]:
        configured = max(1.0, float(config.get_int("MINIMAX_VAD_RMS_THRESHOLD")))
        minimum = max(1.0, float(config.get_int("MINIMAX_VAD_MIN_RMS")))
        noise_rms = self._noise_floor_locked()
        adaptive = max(minimum, noise_rms * _VAD_NOISE_MULTIPLIER)
        # 配置值是低噪线路的旧基准上限；噪底很高时仍允许阈值超过它，
        # 否则持续底噪会被误判为连续人声。
        threshold = max(noise_rms * 2.0, min(configured, adaptive))
        return threshold, noise_rms

    @staticmethod
    def _frame_peak(pcm: bytes) -> float:
        if len(pcm) < 2:
            return 0.0
        samples = np.frombuffer(pcm[: len(pcm) // 2 * 2], dtype="<i2")
        if samples.size == 0:
            return 0.0
        return float(np.max(np.abs(samples.astype(np.int32))))

    def _condition_input_audio(self, pcm: bytes) -> tuple[bytes, float]:
        """Peak-limited auto gain for MiniMax input; never clips valid int16."""
        if not config.get_bool("MINIMAX_INPUT_AUTO_GAIN"):
            return pcm, 1.0
        peak = self._frame_peak(pcm)
        if peak <= 0:
            return pcm, 1.0
        max_gain = max(1.0, config.get_float("MINIMAX_INPUT_MAX_GAIN"))
        gain = max(1.0, min(max_gain, _AUTO_GAIN_TARGET_PEAK / peak))
        if gain <= 1.01:
            return pcm, 1.0
        usable = pcm[: len(pcm) // 2 * 2]
        samples = np.frombuffer(usable, dtype="<i2").astype(np.float64)
        amplified = np.rint(samples * gain)
        conditioned = np.clip(amplified, -32768, 32767).astype("<i2").tobytes()
        return conditioned + pcm[len(usable) :], gain

    def _vad_should_commit(self, pcm: bytes, *, input_gain: float = 1.0) -> bool:
        """喂一帧上行音频，返回是否该断句（commit + 触发回复）。

        判据：见到过人声之后，静默累计超过 ``MANUAL_RESPONSE_SILENCE_MS``；
        或单次发言超过 ``MANUAL_RESPONSE_MAX_WAIT_MS`` 强制断句（对方一直不停
        时也得让 AI 有机会说话）。回复在飞时不再断句，否则会把同一段话切成
        多个 response 并发，模型侧交错、对方听到重叠语音。
        """
        silence_window = float(
            self._turn_end_silence_ms
            if self._turn_end_silence_ms is not None
            else config.get_int("MANUAL_RESPONSE_SILENCE_MS")
        )
        max_utterance = float(config.get_int("MANUAL_RESPONSE_MAX_WAIT_MS"))
        frame_ms = len(pcm) / 2 / self.input_rate * 1000.0
        rms = self._frame_rms(pcm)

        with self._vad_lock:
            if self._vad_response_in_flight:
                return False
            threshold, noise_rms = self._effective_vad_threshold_locked()
            if rms >= threshold:
                speech_started = False
                if self._vad_speech_seen:
                    self._vad_silence_ms = 0.0
                    self._vad_utterance_ms += frame_ms
                else:
                    self._vad_candidate_ms += frame_ms
                    self._vad_candidate_peak = max(
                        self._vad_candidate_peak, rms
                    )
                    if self._vad_candidate_ms >= _VAD_MIN_SPEECH_MS:
                        self._vad_speech_seen = True
                        self._vad_silence_ms = 0.0
                        self._vad_utterance_ms = self._vad_candidate_ms
                        speech_started = True
                if speech_started:
                    self._vad_unheard_ms = 0.0
                    self._vad_unheard_peak = 0.0
                    self._emit_trace(
                        "vad", "speech_started", "running",
                        rms=round(self._vad_candidate_peak, 1),
                        threshold=round(threshold, 1),
                        noise_rms=round(noise_rms, 1),
                        gain=round(input_gain, 2),
                    )
            elif self._vad_speech_seen:
                self._vad_silence_ms += frame_ms
                self._vad_utterance_ms += frame_ms
            else:
                self._vad_candidate_ms = 0.0
                self._vad_candidate_peak = 0.0
                # 只用当前判为非人声的帧学习噪底，避免阈值追着人声上涨。
                self._vad_noise_samples.append(rms)
            if not self._vad_speech_seen:
                self._vad_unheard_ms += frame_ms
                self._vad_unheard_peak = max(self._vad_unheard_peak, rms)
                if (
                    not self._vad_low_signal_logged
                    and self._vad_unheard_ms >= _LOW_SIGNAL_DIAGNOSTIC_MS
                    and 0 < self._vad_unheard_peak < threshold
                ):
                    self._vad_low_signal_logged = True
                    self._emit_trace(
                        "vad", "signal_below_threshold", "warning",
                        rms=round(self._vad_unheard_peak, 1),
                        threshold=round(threshold, 1),
                        noise_rms=round(noise_rms, 1),
                    )
                return False
            if (
                self._vad_silence_ms >= silence_window
                or self._vad_utterance_ms >= max_utterance
            ):
                reason = (
                    "silence"
                    if self._vad_silence_ms >= silence_window
                    else "max_utterance"
                )
                # 立刻置为在飞并清状态：commit 是 await，不能让下一帧重复触发。
                self._vad_response_in_flight = True
                self._vad_speech_seen = False
                self._vad_silence_ms = 0.0
                self._vad_utterance_ms = 0.0
                self._vad_candidate_ms = 0.0
                self._vad_candidate_peak = 0.0
                self._emit_trace(
                    "vad", "utterance_committed", "ok", reason=reason,
                )
                return True
            return False

    async def send_audio(self, pcm: bytes) -> None:
        """推一帧上行音频；本端判定断句后显式 commit 并触发回复。

        MiniMax 没有服务端 VAD（实测只 append 不 commit 时全程零事件），所以
        父类"推流即可、服务端自己断句"的假设在这里不成立。
        """
        ws = self._ws
        if not ws or not pcm:
            return
        frames = self._take_deferred_frames(pcm)
        for index, frame in enumerate(frames):
            if self._defer_frame_while_response_in_flight(frame):
                for remaining in frames[index + 1 :]:
                    self._defer_frame_while_response_in_flight(remaining)
                return
            conditioned, input_gain = self._condition_input_audio(frame)
            await super().send_audio(conditioned)
            if self._vad_should_commit(frame, input_gain=input_gain):
                await self._request_response(ws)

    def _take_deferred_frames(self, current: bytes) -> list[bytes]:
        with self._vad_lock:
            if self._vad_response_in_flight or not self._turn_deferred_audio:
                return [current]
            frames = [*self._turn_deferred_audio, current]
            self._turn_deferred_audio.clear()
            self._turn_deferred_ms = 0.0
            return frames

    def _defer_frame_while_response_in_flight(self, pcm: bytes) -> bool:
        resumed = False
        rms = self._frame_rms(pcm)
        frame_ms = len(pcm) / 2 / self.input_rate * 1000.0
        with self._vad_lock:
            if (
                self._turn_end_silence_ms is None
                or not self._vad_response_in_flight
            ):
                return False
            threshold, _noise_rms = self._effective_vad_threshold_locked()
            voiced = rms >= threshold
            if voiced and not self._turn_stale_response:
                self._turn_stale_response = True
                resumed = True
            # Drop leading silence.  Once remote speech starts, retain following
            # silence as part of the deferred turn so VAD can find its real end.
            if voiced or self._turn_deferred_audio:
                self._turn_deferred_audio.append(pcm)
                self._turn_deferred_ms += frame_ms
                # A broken endpoint must not grow memory without bound.  This is
                # longer than MANUAL_RESPONSE_MAX_WAIT_MS, so normal turns fit.
                while self._turn_deferred_ms > 15000 and self._turn_deferred_audio:
                    dropped = self._turn_deferred_audio.pop(0)
                    self._turn_deferred_ms -= (
                        len(dropped) / 2 / self.input_rate * 1000.0
                    )
        if resumed:
            self._emit_trace(
                "turn", "remote_resumed", "running",
                reason="response_in_flight",
            )
        return True

    async def _request_response(self, ws: Any) -> None:
        try:
            await ws.send(json.dumps({"type": "input_audio_buffer.commit"}))
            await ws.send(json.dumps({"type": "response.create"}))
            self._emit_trace("model", "response_requested", "running")
        except Exception as exc:  # noqa: BLE001
            # 断线窗口内失败不炸通话；重连由接收循环统一负责。状态要放开，
            # 否则重连后永远认为"回复在飞"而再也不断句。
            logger.warning("MiniMax 断句提交失败: %s", exc)
            self._emit_trace(
                "model", "response_request_failed", "error",
                error_type=type(exc).__name__,
            )
            with self._vad_lock:
                self._vad_response_in_flight = False

    def _on_response_created(self) -> None:
        with self._vad_lock:
            self._vad_response_in_flight = True
        super()._on_response_created()

    def _on_response_done(self) -> None:
        with self._vad_lock:
            self._vad_response_in_flight = False
            self._turn_stale_response = False
            self._turn_stale_logged = False
        super()._on_response_done()

    # ---- Realtime + M3 混合工具路由 ----

    def _finish_spoken_response(self, response_id: str | None, transcript: str) -> None:
        suppressed = self._audio_gate.complete_transcript(response_id, transcript)
        self._audio_gate.release_response(response_id)
        if response_id:
            self._gated_processed.add(response_id)
            self._gated_held.discard(response_id)
        if not suppressed:
            self._emit_transcript("agent", transcript)

    def _handle_event(self, event: dict) -> None:
        event_type = event.get("type", "")
        active = self._hybrid_active()
        gated = active or self._semantic_turns_enabled
        response_id = _response_id(event)

        with self._vad_lock:
            stale_response = self._turn_stale_response
            stale_logged = self._turn_stale_logged
            buffered_ms = round(self._turn_deferred_ms)
        if stale_response and event_type in (
            "response.audio.delta", "response.output_audio.delta",
        ):
            self._audio_gate.drop_response(response_id)
            return
        if stale_response and event_type in (
            "response.audio_transcript.done",
            "response.output_audio_transcript.done",
        ):
            self._audio_gate.drop_response(response_id)
            if response_id:
                self._gated_processed.add(response_id)
                self._gated_held.discard(response_id)
            if not stale_logged:
                with self._vad_lock:
                    self._turn_stale_logged = True
                self._emit_trace(
                    "turn", "stale_response_dropped", "ok",
                    reason="remote_resumed", buffered_ms=buffered_ms,
                )
            return
        if stale_response and event_type == "response.done":
            self._audio_gate.drop_response(response_id)
            if not stale_logged:
                with self._vad_lock:
                    self._turn_stale_logged = True
                self._emit_trace(
                    "turn", "stale_response_dropped", "ok",
                    reason="remote_resumed", buffered_ms=buffered_ms,
                )

        if gated and event_type in (
            "response.audio.delta", "response.output_audio.delta",
        ):
            self._audio_gate.hold_response(response_id)
            if response_id:
                self._gated_held.add(response_id)

        if gated and event_type in (
            "response.audio_transcript.done",
            "response.output_audio_transcript.done",
        ):
            transcript = (event.get("transcript") or "").strip()
            if not transcript:
                return
            if self._semantic_turns_enabled and self._is_semantic_wait(transcript):
                self._audio_gate.drop_response(response_id)
                if response_id:
                    self._gated_processed.add(response_id)
                    self._gated_held.discard(response_id)
                logger.info("[语义轮次] 对方当前无需回答，候选语音已丢弃")
                self._emit_trace(
                    "turn", "semantic_wait", "ok",
                    decision="no_reply_needed",
                )
                return
            logger.info("[下行·Agent] %s", transcript)
            self._emit_trace(
                "model", "output_transcript_ready", "ok",
                role="agent", chars=len(transcript),
            )
            if not active:
                self._finish_spoken_response(response_id, transcript)
            elif self._hybrid_followup_pending > 0:
                self._hybrid_followup_pending -= 1
                self._finish_spoken_response(response_id, transcript)
            elif not might_request_tool(transcript):
                self._finish_spoken_response(response_id, transcript)
            else:
                self._spawn_hybrid_task(
                    self._route_hybrid_tools(response_id, transcript, self._ws)
                )
            return

        super()._handle_event(event)

        if gated and event_type == "response.done":
            # 极少数异常轮次可能只有音频而无 transcript。不能永久扣住声音；
            # 比 M3 超时多留 1 秒，正常路由任务会先把 response 标为 processed。
            for held_id in tuple(self._gated_held):
                self._spawn_hybrid_task(
                    self._release_gated_timeout(held_id, hybrid=active)
                )

    async def _release_gated_timeout(
        self,
        response_id: str,
        *,
        hybrid: bool,
    ) -> None:
        delay = self._hybrid_tool_timeout + 1.0 if hybrid else 2.0
        await asyncio.sleep(delay)
        if response_id in self._gated_processed:
            return
        logger.warning("MiniMax 等待输出转写超时，原语音降级放行")
        self._emit_trace(
            "tool_router" if hybrid else "turn",
            "transcript_timeout",
            "warning",
            ms=round(delay * 1000),
        )
        self._audio_gate.release_response(response_id)
        self._gated_processed.add(response_id)
        self._gated_held.discard(response_id)

    async def _route_hybrid_tools(
        self,
        response_id: str | None,
        transcript: str,
        ws: Any,
    ) -> None:
        started = time.monotonic()
        self._emit_trace("tool_router", "decision_started", "running")
        try:
            specs = self._tools.specs() if self._tools is not None else []
            calls = await asyncio.wait_for(
                self._hybrid_router.decide(transcript, specs),
                timeout=self._hybrid_tool_timeout + 0.5,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "MiniMax M3 工具决策失败，原语音降级放行: error_type=%s",
                type(exc).__name__,
            )
            self._emit_trace(
                "tool_router", "decision_failed", "warning",
                error_type=type(exc).__name__,
                ms=round((time.monotonic() - started) * 1000),
            )
            self._finish_spoken_response(response_id, transcript)
            return

        valid_names = {
            str(spec.get("function", {}).get("name"))
            for spec in specs
            if isinstance(spec.get("function"), dict)
        }
        calls = [call for call in calls if call.name in valid_names]
        if not calls:
            self._emit_trace(
                "tool_router", "no_tool", "ok",
                ms=round((time.monotonic() - started) * 1000),
            )
            self._finish_spoken_response(response_id, transcript)
            return

        # 挂断前的自然道别要播放；其他行动句只是控制信号，执行后再根据真实
        # 结果生成确认，避免对端听到“已发送”但工具实际失败。
        has_terminal = any(call.name in TERMINAL_TOOLS for call in calls)
        if has_terminal:
            self._finish_spoken_response(response_id, transcript)
        else:
            self._audio_gate.drop_response(response_id)
            if response_id:
                self._gated_processed.add(response_id)
                self._gated_held.discard(response_id)

        results: list[tuple[str, dict[str, Any]]] = []
        for call in calls:
            if call.call_id in self._handled_tool_calls:
                continue
            self._handled_tool_calls.add(call.call_id)
            self._emit_trace("tool", "requested", "running", tool=call.name)
            try:
                if self._tools is None:
                    result = {"success": False, "message": "无可用工具"}
                else:
                    result = await asyncio.to_thread(
                        self._tools.dispatch, call.name, call.arguments
                    )
            except Exception as exc:  # noqa: BLE001
                logger.exception("MiniMax 混合工具 %s 执行异常", call.name)
                result = {"success": False, "message": f"工具执行异常: {exc}"}
            self._emit_trace(
                "tool", "completed",
                "ok" if result.get("success") is True else "error",
                tool=call.name,
            )
            results.append((call.name, result))

        self._emit_trace(
            "tool_router", "decision_completed", "ok",
            ms=round((time.monotonic() - started) * 1000),
        )
        needs_followup = any(
            name not in TERMINAL_TOOLS and name not in SILENT_AFTER_TOOLS
            for name, _result in results
        )
        if needs_followup and ws is self._ws:
            await self._request_hybrid_followup(results, ws)

    async def _request_hybrid_followup(
        self,
        results: list[tuple[str, dict[str, Any]]],
        ws: Any,
    ) -> None:
        safe_results = [
            {"tool": name, "result": result}
            for name, result in results
        ]
        instruction = (
            "[hybrid_tool_result] 以下是刚才工具的真实结果："
            f"{json.dumps(safe_results, ensure_ascii=False)}。"
            "请只根据真实结果用一句简短中文告诉对方，不要再次调用或提议同一工具。"
        )
        self._hybrid_followup_pending += 1
        try:
            await ws.send(json.dumps({
                "type": "conversation.item.create",
                "item": self._context_item(instruction, role="user"),
            }))
            await ws.send(json.dumps({"type": "response.create"}))
        except Exception as exc:  # noqa: BLE001
            self._hybrid_followup_pending = max(0, self._hybrid_followup_pending - 1)
            logger.warning(
                "MiniMax 混合工具结果回注失败: error_type=%s",
                type(exc).__name__,
            )
            self._emit_trace(
                "tool_router", "followup_failed", "warning",
                error_type=type(exc).__name__,
            )

    # ---- 上下文与主动说话 ----

    @staticmethod
    def _context_item(text: str, role: str = "system") -> dict:
        """MiniMax 认的上下文 item：必须带 status，content type 必须 input_text。"""
        return {
            "type": "message",
            "role": role,
            "status": "completed",
            "content": [{"type": "input_text", "text": text}],
        }

    async def say(self, instructions: str) -> None:
        """让 Agent 主动说一段话（外呼开场白 / 重连安抚语）。

        不能像 OpenAI 那样用 ``response.create`` 带 ``instructions``——MiniMax 会报
        ``2013 no context items provided``。必须先写一条上下文 item，而且**只能是
        ``role="user"``**：``role="system"`` 的 item 虽然能创建成功，却不被算作
        "chat content"，紧随的 ``response.create`` 会报
        ``2013 invalid params, chat content is empty``（真机实测）。代价是这条
        指令会以对方发言的身份进入上下文，这是该端点唯一可用的主动说话方式。
        """
        ws = self._ws
        if not ws:
            return
        try:
            await ws.send(json.dumps({
                "type": "conversation.item.create",
                "item": self._context_item(instructions, role="user"),
            }))
            await ws.send(json.dumps({"type": "response.create"}))
        except Exception as exc:  # noqa: BLE001
            logger.warning("发送说话指令失败: %s", exc)

    async def external_tool_result(
        self,
        name: str,
        result: dict[str, Any],
        *,
        source: str,
    ) -> bool:
        """写入外部已执行的工具事实（纯上下文，不请求回复）。

        与父类的差别只是 item 要带 ``status``；这条路径不依赖 function calling，
        所以在 MiniMax 上是可用的。
        """
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
            await ws.send(json.dumps({
                "type": "conversation.item.create",
                "item": self._context_item(text),
            }))
        except Exception as exc:  # noqa: BLE001
            logger.warning("写入外部工具结果失败: error_type=%s", type(exc).__name__)
            return False
        return True

    async def stop(self) -> None:
        tasks = list(self._hybrid_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._hybrid_tasks.clear()
        self._gated_processed.clear()
        self._gated_held.clear()
        self._hybrid_followup_pending = 0
        await super().stop()
