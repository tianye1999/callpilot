"""Agent 工厂。"""

from __future__ import annotations

import logging
import os

from .base import VoiceAgent
from .doubao_agent import DoubaoVoiceAgent
from .qwen_agent import QwenVoiceAgent

logger = logging.getLogger(__name__)


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
        # 豆包 realtime 二进制协议中未确认与 qwen create_response 等价的
        # 文本指令注入消息格式，say() 保持 base 默认 no-op（详见 roadmap P3-5）。
        logger.warning(
            "豆包 provider 暂不支持外呼开场白（say 未实现），外呼请用 qwen"
        )
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
