"""通义千问 Qwen-Omni 实时语音 Agent。"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import threading
from datetime import datetime
from queue import Empty, Queue
from typing import Callable

import dashscope
from dashscope.audio.qwen_omni import (
    AudioFormat,
    MultiModality,
    OmniRealtimeCallback,
    OmniRealtimeConversation,
)

from .base import VoiceAgent

logger = logging.getLogger(__name__)


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
        voice: str = "Chelsie",
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

    async def start(self, on_audio_out: Callable[[bytes], None]) -> None:
        self._on_audio_out = on_audio_out
        self._running = True

        weekdays = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
        now = datetime.now()
        now_str = f"{now:%Y年%m月%d日 %H:%M}（{weekdays[now.weekday()]}）"

        instructions = (
            f"你叫红茶语音助手，是接入电话的语音 Agent。接通后请先用中文简短自我介绍，"
            f"说明你是红茶语音助手。"
            "之后用口语化、简洁的方式回答对方问题，每次回答控制在两三句话以内。"
            f"当前真实日期时间是 {now_str}，这是准确信息；对方询问日期、时间、"
            "今天几号或星期几时，必须以此为准回答，不要凭记忆猜测年份。"
            "你可以调用工具帮用户完成实际操作：发送短信(send_sms，发给本人时号码留空)、"
            "挂断电话(hangup_call，挂断前先说一句告别语)、查询最近收到的短信验证码"
            "(query_verification_code)。需要时主动调用对应工具，操作完成后用一句话口头确认结果。"
        )

        conversation_kwargs = {
            "model": self.model,
            "callback": self._callback,
        }
        if self.realtime_url:
            conversation_kwargs["url"] = self.realtime_url

        self._conversation = OmniRealtimeConversation(**conversation_kwargs)
        self._callback._conversation = self._conversation  # noqa: SLF001
        self._conversation.connect()

        tool_kwargs: dict = {}
        if self._tools is not None and self._tools.has_tools():
            # 千问 Omni Realtime 不支持 tool_choice / parallel_tool_calls。
            tool_kwargs["tools"] = self._tools.specs()
            logger.info("已为会话注册 %d 个工具", len(self._tools.specs()))

        self._conversation.update_session(
            output_modalities=[MultiModality.AUDIO, MultiModality.TEXT],
            voice=self.voice,
            input_audio_format=AudioFormat.PCM_16000HZ_MONO_16BIT,
            output_audio_format=AudioFormat.PCM_24000HZ_MONO_16BIT,
            enable_input_audio_transcription=True,
            input_audio_transcription_model="qwen3-asr-flash-realtime",
            enable_turn_detection=True,
            turn_detection_type="server_vad",
            turn_detection_silence_duration_ms=600,
            instructions=instructions,
            **tool_kwargs,
        )

        self._pump_thread = threading.Thread(target=self._pump_audio_out, daemon=True)
        self._pump_thread.start()
        logger.info("千问 Agent 已启动: %s", self.model)

    async def send_audio(self, pcm: bytes) -> None:
        if not self._conversation or not pcm:
            return
        audio_b64 = base64.b64encode(pcm).decode("ascii")
        self._conversation.append_audio(audio_b64)

    async def say(self, instructions: str) -> None:
        if not self._conversation:
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
        self._running = False
        if self._conversation:
            try:
                self._conversation.close()
            except Exception as exc:  # noqa: BLE001
                logger.warning("关闭千问连接异常: %s", exc)
        self._audio_queue.put(None)
        if self._pump_thread:
            self._pump_thread.join(timeout=3)
        self._conversation = None

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
