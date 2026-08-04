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
   ``response.function_call_arguments.done``；模型会明说"无法执行挂断这类操作"。
   后果：本 provider 下 AI **无法自行挂断电话 / 发短信 / 查验证码 / 发 DTMF**，
   收尾只能靠 ``OUTBOUND_MAX_SECONDS`` / ``INBOUND_MAX_SECONDS`` 硬时限兜底。
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

import json
import logging
import threading
from typing import Any

import numpy as np
import websockets

from .. import config
from .openai_agent import OpenAIVoiceAgent

logger = logging.getLogger(__name__)

# 国内区 realtime 端点；可经 MINIMAX_REALTIME_URL 覆盖。
DEFAULT_REALTIME_URL = "wss://api.minimaxi.com/ws/v1/realtime"

# 上行转写模型（session.input_audio_transcription）。配置被接受但不回事件，
# 仍照发：服务端将来补上事件时无需改代码。
TRANSCRIPTION_MODEL = "asr-01"

# 服务端默认 "256" 会把电话里的回答截断；协议要求本字段是字符串。
MAX_RESPONSE_OUTPUT_TOKENS = "1024"


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

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # ---- 客户端 VAD 状态（见 send_audio）----
        self._vad_lock = threading.Lock()
        self._vad_speech_seen = False
        self._vad_silence_ms = 0.0
        self._vad_utterance_ms = 0.0
        self._vad_response_in_flight = False

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

        session: dict = {
            "modalities": ["audio", "text"],
            "instructions": self._instructions,
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

        if self._tools is not None and self._tools.has_tools():
            logger.warning(
                "MiniMax realtime 不支持工具调用（服务端丢弃 session.tools），"
                "已注册的 %d 个工具本通电话不会生效：AI 无法自行挂断/发短信/发 DTMF，"
                "收尾依赖 OUTBOUND_MAX_SECONDS / INBOUND_MAX_SECONDS 硬时限",
                len(self._tools.specs()),
            )
        if self._manual_response_enabled:
            logger.warning(
                "MANUAL_RESPONSE_CONTROL 对 MiniMax 无效（服务端丢弃 turn_detection，"
                "且不发用户转写事件）；断句一律由本端能量 VAD 负责"
            )
        logger.info("MiniMax Realtime 连接已建立: %s", self.model)

    # ---- 客户端 VAD ----

    def _reset_vad(self) -> None:
        with self._vad_lock:
            self._vad_speech_seen = False
            self._vad_silence_ms = 0.0
            self._vad_utterance_ms = 0.0
            self._vad_response_in_flight = False

    @staticmethod
    def _frame_rms(pcm: bytes) -> float:
        """帧能量（int16 RMS）。奇数字节丢尾字节，避免 frombuffer 抛错。"""
        if len(pcm) < 2:
            return 0.0
        samples = np.frombuffer(pcm[: len(pcm) // 2 * 2], dtype="<i2")
        if samples.size == 0:
            return 0.0
        return float(np.sqrt(np.mean(samples.astype(np.float64) ** 2)))

    def _vad_should_commit(self, pcm: bytes) -> bool:
        """喂一帧上行音频，返回是否该断句（commit + 触发回复）。

        判据：见到过人声之后，静默累计超过 ``MANUAL_RESPONSE_SILENCE_MS``；
        或单次发言超过 ``MANUAL_RESPONSE_MAX_WAIT_MS`` 强制断句（对方一直不停
        时也得让 AI 有机会说话）。回复在飞时不再断句，否则会把同一段话切成
        多个 response 并发，模型侧交错、对方听到重叠语音。
        """
        threshold = float(config.get_int("MINIMAX_VAD_RMS_THRESHOLD"))
        silence_window = float(config.get_int("MANUAL_RESPONSE_SILENCE_MS"))
        max_utterance = float(config.get_int("MANUAL_RESPONSE_MAX_WAIT_MS"))
        frame_ms = len(pcm) / 2 / self.input_rate * 1000.0
        rms = self._frame_rms(pcm)

        with self._vad_lock:
            if self._vad_response_in_flight:
                return False
            if rms >= threshold:
                self._vad_speech_seen = True
                self._vad_silence_ms = 0.0
            elif self._vad_speech_seen:
                self._vad_silence_ms += frame_ms
            if not self._vad_speech_seen:
                return False
            self._vad_utterance_ms += frame_ms
            if (
                self._vad_silence_ms >= silence_window
                or self._vad_utterance_ms >= max_utterance
            ):
                # 立刻置为在飞并清状态：commit 是 await，不能让下一帧重复触发。
                self._vad_response_in_flight = True
                self._vad_speech_seen = False
                self._vad_silence_ms = 0.0
                self._vad_utterance_ms = 0.0
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
        await super().send_audio(pcm)
        if not self._vad_should_commit(pcm):
            return
        try:
            await ws.send(json.dumps({"type": "input_audio_buffer.commit"}))
            await ws.send(json.dumps({"type": "response.create"}))
        except Exception as exc:  # noqa: BLE001
            # 断线窗口内失败不炸通话；重连由接收循环统一负责。状态要放开，
            # 否则重连后永远认为"回复在飞"而再也不断句。
            logger.warning("MiniMax 断句提交失败: %s", exc)
            with self._vad_lock:
                self._vad_response_in_flight = False

    def _on_response_created(self) -> None:
        with self._vad_lock:
            self._vad_response_in_flight = True
        super()._on_response_created()

    def _on_response_done(self) -> None:
        with self._vad_lock:
            self._vad_response_in_flight = False
        super()._on_response_done()

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
