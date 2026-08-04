"""Agent 工厂。"""

from __future__ import annotations

import logging
import os

from .. import config
from .base import VoiceAgent
from .doubao_agent import DoubaoVoiceAgent
from .minimax_agent import MiniMaxVoiceAgent
from .openai_agent import OpenAIVoiceAgent
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

    if selected == "local":
        # 三段式：本地 VAD/STT/TTS + 文本 LLM。sherpa-onnx/模型缺失时在
        # agent.start() 阶段报清晰错误并置 fatal（不在这里提前 import）。
        from .local_agent import LocalPipelineAgent

        return LocalPipelineAgent()

    if selected == "openai":
        return OpenAIVoiceAgent(
            # API Key 属凭证不走注册表默认值：缺失即 KeyError fail-fast。
            api_key=os.environ["OPENAI_API_KEY"],
            model=config.get_str("OPENAI_REALTIME_MODEL"),
            model_display_name=config.get_str("AGENT_MODEL_NAME_OPENAI"),
            voice=config.get_str("OPENAI_VOICE"),
            realtime_url=config.get_str("OPENAI_REALTIME_URL") or None,
        )

    if selected == "minimax":
        # 实测能力边界（见 minimax_agent 模块 docstring）：realtime 端点静默丢弃
        # session.tools，AI 无法自行挂断/发短信/发 DTMF；且无服务端 VAD，断句由
        # agent 内的能量 VAD 负责。选它之前要清楚这两条。
        logger.warning(
            "MiniMax provider 不支持工具调用：AI 无法自行挂断电话或发短信，"
            "通话收尾依赖 OUTBOUND_MAX_SECONDS / INBOUND_MAX_SECONDS 硬时限；"
            "需要完整工具能力请用 qwen 或 openai"
        )
        return MiniMaxVoiceAgent(
            # API Key 属凭证不走注册表默认值：缺失即 KeyError fail-fast。
            api_key=os.environ["MINIMAX_API_KEY"],
            model=config.get_str("MINIMAX_REALTIME_MODEL"),
            model_display_name=config.get_str("AGENT_MODEL_NAME_MINIMAX"),
            voice=config.get_str("MINIMAX_VOICE"),
            realtime_url=config.get_str("MINIMAX_REALTIME_URL") or None,
        )

    raise ValueError(
        f"不支持的 AGENT_PROVIDER: {selected}，"
        "请使用 qwen、doubao、openai、minimax 或 local"
    )
