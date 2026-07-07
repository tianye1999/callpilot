"""FakeAgent：脚本化 VoiceAgent，实现 start/send_audio/say/stop。"""

from __future__ import annotations

from typing import Callable

from agentcall.agents.base import VoiceAgent


class FakeAgent(VoiceAgent):
    """say() 时按脚本推一段假 PCM 到 on_audio_out 并产生 agent 转写。"""

    def __init__(self, reply_pcm: bytes = b"\x01\x00" * 240) -> None:
        self.reply_pcm = reply_pcm
        self.started = False
        self.stopped = False
        self.received_audio: list[bytes] = []
        self.said: list[str] = []
        self._on_audio_out: Callable[[bytes], None] | None = None

    async def start(self, on_audio_out: Callable[[bytes], None]) -> None:
        self.started = True
        self._on_audio_out = on_audio_out

    async def send_audio(self, pcm: bytes) -> None:
        self.received_audio.append(pcm)

    async def say(self, instructions: str) -> None:
        self.said.append(instructions)
        self._emit_transcript("agent", instructions)
        if self._on_audio_out:
            self._on_audio_out(self.reply_pcm)

    async def stop(self) -> None:
        self.stopped = True
