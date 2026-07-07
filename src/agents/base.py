"""Agent 抽象接口。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from .tools import ToolRegistry


class VoiceAgent(ABC):
    input_rate: int = 16000
    output_rate: int = 24000

    _on_transcript: "Callable[[str, str], None] | None" = None
    _tools: "ToolRegistry | None" = None

    def set_transcript_handler(
        self, handler: "Callable[[str, str], None] | None"
    ) -> None:
        """注册转写回调，参数为 (role, text)，role 为 'user' 或 'agent'。"""
        self._on_transcript = handler

    def set_tools(self, registry: "ToolRegistry | None") -> None:
        """注册可调用工具（function calling）；不支持的实现可忽略。"""
        self._tools = registry

    def _emit_transcript(self, role: str, text: str) -> None:
        if self._on_transcript and text:
            try:
                self._on_transcript(role, text)
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

    @abstractmethod
    async def stop(self) -> None:
        """结束会话。"""
