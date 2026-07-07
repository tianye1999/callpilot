"""语音 Agent 可调用的工具（function calling）注册与分发。

工具规格遵循千问 Omni Realtime 的 session.tools 格式（function 嵌套）：
{"type": "function", "function": {"name": ..., "description": ..., "parameters": {JSON Schema}}}
"""

from __future__ import annotations

import logging
from typing import Any, Callable

logger = logging.getLogger(__name__)

ToolHandler = Callable[[dict[str, Any]], dict[str, Any]]


# 发送短信工具规格（千问 Realtime 要求 function 嵌套格式）
SEND_SMS_SPEC: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "send_sms",
        "description": (
            "给指定手机号发送一条短信，支持中文。当用户在通话中要求发短信时调用本工具，"
            "例如“给我发一条短信”“给这个号码发条广告”。若用户说发给他本人/发到当前号码，"
            "可以把 to 留空，系统会自动使用当前通话对方的号码。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "to": {
                    "type": "string",
                    "description": "收件手机号码；若指当前通话对方本人则可留空。",
                },
                "content": {
                    "type": "string",
                    "description": "短信正文内容，可为中文。",
                },
            },
            "required": ["content"],
        },
    },
}


# 挂断电话工具规格
HANGUP_SPEC: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "hangup_call",
        "description": (
            "结束并挂断当前这通电话。当用户明确表示要挂断、结束通话、再见、不聊了时调用。"
            "调用前请先用一句话向对方道别，系统会在你说完后再挂断。"
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}


# 发送 DTMF 按键工具规格（IVR 电话菜单导航）
SEND_DTMF_SPEC: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "send_dtmf",
        "description": (
            "在通话中发送电话按键音（DTMF），用于电话菜单/IVR 导航。"
            "当对方是自动语音系统并提示“请按1”“查话费请按2”之类时，"
            "调用本工具发送对应按键。digits 可以是多位，如 \"1\" 或 \"103#\"。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "digits": {
                    "type": "string",
                    "description": "要发送的按键序列，仅允许 0-9、*、#。",
                },
            },
            "required": ["digits"],
        },
    },
}


# 查询最近短信验证码工具规格
QUERY_CODE_SPEC: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "query_verification_code",
        "description": (
            "查询最近收到的短信里的验证码。当用户问“我收到的验证码是多少”“帮我查下验证码”"
            "“刚发的验证码”之类问题时调用，返回最近一条含验证码短信中的数字验证码。"
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}


def _tool_name(spec: dict[str, Any]) -> str | None:
    """兼容扁平与 function 嵌套两种规格，取出工具名。"""
    if "function" in spec and isinstance(spec["function"], dict):
        return spec["function"].get("name")
    return spec.get("name")


class ToolRegistry:
    """工具注册表：保存规格与对应处理函数，供 Agent 调用。"""

    def __init__(self) -> None:
        self._tools: dict[str, tuple[dict[str, Any], ToolHandler]] = {}

    def register(self, spec: dict[str, Any], handler: ToolHandler) -> None:
        name = _tool_name(spec)
        if not name:
            raise ValueError("工具规格缺少 name")
        self._tools[name] = (spec, handler)

    def specs(self) -> list[dict[str, Any]]:
        return [spec for spec, _ in self._tools.values()]

    def has_tools(self) -> bool:
        return bool(self._tools)

    def dispatch(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        entry = self._tools.get(name)
        if entry is None:
            logger.warning("请求了未知工具: %s", name)
            return {"success": False, "message": f"未知工具: {name}"}
        _spec, handler = entry
        logger.info("执行工具 %s，参数=%s", name, args)
        try:
            result = handler(args)
        except Exception as exc:  # noqa: BLE001
            logger.exception("工具 %s 执行异常: %s", name, exc)
            return {"success": False, "message": f"工具执行异常: {exc}"}
        logger.info("工具 %s 执行结果=%s", name, result)
        return result
