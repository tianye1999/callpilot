"""Agent 抽象接口。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from .tools import ToolRegistry


class VoiceAgent(ABC):
    input_rate: int = 16000
    output_rate: int = 24000

    # 会话不可恢复标志：实现（如断线重连全败）置 True 后，
    # CallSession 主循环会结束整通电话，避免"电话活着但 AI 已死"。
    fatal: bool = False

    _on_transcript: "Callable[[str, str], None] | None" = None
    _on_repeat_stuck: "Callable[[str], None] | None" = None
    _on_status: "Callable[[str], None] | None" = None
    _tools: "ToolRegistry | None" = None
    _session_instructions: str | None = None

    def set_transcript_handler(
        self, handler: "Callable[[str, str], None] | None"
    ) -> None:
        """注册转写回调，参数为 (role, text)，role 为 'user' 或 'agent'。"""
        self._on_transcript = handler

    def set_tools(self, registry: "ToolRegistry | None") -> None:
        """注册可调用工具（function calling）；不支持的实现可忽略。"""
        self._tools = registry

    def set_session_instructions(self, instructions: str | None) -> None:
        """设置本通电话的系统提示词。"""
        self._session_instructions = instructions

    def set_repeat_stuck_handler(
        self, handler: "Callable[[str], None] | None"
    ) -> None:
        """注册复读抑制连续触发后的卡死回调。"""
        self._on_repeat_stuck = handler

    def set_status_handler(self, handler: "Callable[[str], None] | None") -> None:
        """注册面向用户的状态提示回调（如首启下载模型的进度）；多数实现无需用。"""
        self._on_status = handler

    def _emit_status(self, text: str) -> None:
        if self._on_status and text:
            try:
                self._on_status(text)
            except Exception:  # noqa: BLE001
                pass

    def _emit_transcript(self, role: str, text: str) -> None:
        if self._on_transcript and text:
            try:
                self._on_transcript(role, text)
            except Exception:  # noqa: BLE001
                pass

    def _emit_repeat_stuck(self, reason: str) -> None:
        if self._on_repeat_stuck:
            try:
                self._on_repeat_stuck(reason)
            except Exception:  # noqa: BLE001
                pass

    @abstractmethod
    async def start(self, on_audio_out: Callable[[bytes], None]) -> None:
        """启动会话，收到 AI 音频时回调 on_audio_out。"""

    @abstractmethod
    async def send_audio(self, pcm: bytes) -> None:
        """发送用户侧 PCM（input_rate, mono, int16）。"""

    async def say(self, instructions: str) -> None:
        """让 Agent 主动说一段话；不支持的实现可以忽略。"""

    async def external_tool_result(
        self,
        name: str,
        result: dict[str, Any],
        *,
        source: str,
    ) -> bool:
        """Record an externally executed tool fact without requesting a reply.

        Providers that cannot add a reliable standalone context item return
        ``False``. Callers must never fabricate a function-call id as fallback.
        """

        return False

    @abstractmethod
    async def stop(self) -> None:
        """结束会话。"""
