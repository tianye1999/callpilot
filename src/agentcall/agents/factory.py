"""Agent 工厂。"""

from __future__ import annotations

import os

from .base import VoiceAgent
from .doubao_agent import DoubaoVoiceAgent
from .qwen_agent import QwenVoiceAgent


def create_agent(provider: str | None = None) -> VoiceAgent:
    selected = (provider or os.getenv("AGENT_PROVIDER", "qwen")).lower()

    if selected == "qwen":
        return QwenVoiceAgent(
            api_key=os.environ["DASHSCOPE_API_KEY"],
            model=os.getenv("QWEN_REALTIME_MODEL", "qwen3.5-omni-flash-realtime"),
            model_display_name=os.getenv("AGENT_MODEL_NAME", "通义千问 Qwen3.5-Omni"),
            voice=os.getenv("QWEN_VOICE", "Raymond"),
            realtime_url=os.getenv("DASHSCOPE_REALTIME_URL"),
        )

    if selected == "doubao":
        return DoubaoVoiceAgent(
            app_id=os.getenv("DOUBAO_APP_ID", ""),
            access_key=os.getenv("DOUBAO_ACCESS_KEY", ""),
            resource_id=os.getenv("DOUBAO_RESOURCE_ID", "volc.speech.dialog"),
            app_key=os.getenv("DOUBAO_APP_KEY", "PlgvMymc7f3tQnJ6"),
            model_display_name=os.getenv(
                "AGENT_MODEL_NAME_DOUBAO", "豆包实时语音大模型"
            ),
        )

    raise ValueError(f"不支持的 AGENT_PROVIDER: {selected}，请使用 qwen 或 doubao")
