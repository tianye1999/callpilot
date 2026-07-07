"""FakeAudioBridge：内存环回音频桥，接口与 ModemAudioBridge 对齐。"""

from __future__ import annotations

from collections import deque
from typing import Iterable

from agentcall.audio_bridge import resample_pcm


class FakeAudioBridge:
    def __init__(self) -> None:
        self.started = False
        self.stopped = False
        self.uplink: deque[bytes] = deque()  # 测试注入的"模组上行"音频
        self.downlink: list[bytes] = []  # 会话写给模组的下行音频

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True

    def feed_uplink(self, pcm: bytes) -> None:
        """测试注入一块 8kHz 上行 PCM（模拟对方说话）。"""
        self.uplink.append(pcm)

    def read_modem_chunk(self) -> bytes:
        return self.uplink.popleft() if self.uplink else b""

    def pending_output_bytes(self) -> int:
        return 0

    def write_modem_chunks(self, chunks: Iterable[bytes]) -> None:
        self.downlink.extend(chunk for chunk in chunks if chunk)

    @staticmethod
    def modem_to_agent(pcm_8k: bytes, agent_rate: int) -> bytes:
        return resample_pcm(pcm_8k, 8000, agent_rate)

    @staticmethod
    def agent_to_modem(pcm_agent: bytes, agent_rate: int) -> bytes:
        return resample_pcm(pcm_agent, agent_rate, 8000)
