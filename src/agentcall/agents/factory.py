"""Agent 工厂。"""

from __future__ import annotations

import logging
import os

from .. import config
from .base import VoiceAgent
from .doubao_agent import DoubaoVoiceAgent
from .qwen_agent import QwenVoiceAgent

logger = logging.getLogger(__name__)


def create_agent(provider: str | None = None) -> VoiceAgent:
    selected = (provider or config.get_str("AGENT_PROVIDER")).lower()

    if selected == "qwen":
        return QwenVoiceAgent(
            # API Key 属凭证不走注册表默认值：缺失即 KeyError fail-fast。
            api_key=os.environ["DASHSCOPE_API_KEY"],
            model=config.get_str("QWEN_REALTIME_MODEL"),
            model_display_name=config.get_str("AGENT_MODEL_NAME"),
            voice=config.get_str("QWEN_VOICE"),
            realtime_url=config.get_str("DASHSCOPE_REALTIME_URL") or None,
        )

    if selected == "doubao":
        # 豆包 realtime 二进制协议中未确认与 qwen create_response 等价的
        # 文本指令注入消息格式，say() 保持 base 默认 no-op（详见 roadmap P3-5）。
        logger.warning(
            "豆包 provider 暂不支持外呼开场白（say 未实现），外呼请用 qwen"
        )
        return DoubaoVoiceAgent(
            # APP_ID/ACCESS_KEY 属凭证，不进注册表（见 PROVIDER_REQUIRED_KEYS）。
            app_id=os.getenv("DOUBAO_APP_ID", ""),
            access_key=os.getenv("DOUBAO_ACCESS_KEY", ""),
            resource_id=config.get_str("DOUBAO_RESOURCE_ID"),
            app_key=config.get_str("DOUBAO_APP_KEY"),
            model_display_name=config.get_str("AGENT_MODEL_NAME_DOUBAO"),
        )

    raise ValueError(f"不支持的 AGENT_PROVIDER: {selected}，请使用 qwen 或 doubao")
