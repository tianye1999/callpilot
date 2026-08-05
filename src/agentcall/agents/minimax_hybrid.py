"""MiniMax M3 text-side tool router for the Realtime voice connection.

The Realtime beta endpoint understands and speaks audio but drops session.tools.
M3's text chat-completion endpoint does support function calling.  This module
bridges the two without pretending M3 can hear raw call audio: it only audits a
short action proposal already produced by Realtime and returns zero or more
registered tool calls.
"""

from __future__ import annotations

import asyncio
import json
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable

DEFAULT_TEXT_URL = "https://api.minimaxi.com/v1/text/chatcompletion_v2"
DEFAULT_TEXT_MODEL = "MiniMax-M3"


@dataclass(frozen=True)
class HybridToolCall:
    call_id: str
    name: str
    arguments: dict[str, Any]


_ROUTER_PROMPT = """你是电话 Agent 的工具安全路由器。
输入是语音模型刚生成、尚未播放给对方的一句行动提案，不是用户原话。
只在提案明确表示现在就要执行某个已提供工具，并且必需参数都明确时调用工具；
普通对话、解释能力、询问确认、假设或信息不足时不要调用。
send_dtmf 的 digits 必须在提案中明确；不得猜按键。
send_sms 的 content 必须明确；不得补写正文。一次最多选择两个工具。不要输出解释。"""


class MiniMaxM3ToolRouter:
    """Call MiniMax's non-streaming text endpoint and parse function calls."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str = DEFAULT_TEXT_MODEL,
        url: str = DEFAULT_TEXT_URL,
        timeout: float = 10.0,
        post_json: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.url = url
        self.timeout = timeout
        self._post_json_override = post_json

    async def decide(
        self,
        proposal: str,
        tools: list[dict[str, Any]],
    ) -> list[HybridToolCall]:
        if not proposal.strip() or not tools:
            return []
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": _ROUTER_PROMPT},
                {"role": "user", "content": proposal},
            ],
            "tools": tools,
            "tool_choice": "auto",
        }
        if self._post_json_override is not None:
            data = await asyncio.to_thread(self._post_json_override, payload)
        else:
            data = await asyncio.to_thread(self._post_json, payload)
        return self._parse_calls(data)

    def _post_json(self, payload: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self.url,
            data=body,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            raw = response.read()
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("MiniMax M3 returned a non-object response")
        return data

    @staticmethod
    def _parse_calls(data: dict[str, Any]) -> list[HybridToolCall]:
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            return []
        choice = choices[0] if isinstance(choices[0], dict) else {}
        message = choice.get("message")
        message_data = message if isinstance(message, dict) else {}
        raw_calls = message_data.get("tool_calls")
        if not isinstance(raw_calls, list):
            return []

        calls: list[HybridToolCall] = []
        for index, raw_call in enumerate(raw_calls[:2]):
            if not isinstance(raw_call, dict):
                continue
            function = raw_call.get("function")
            if not isinstance(function, dict):
                continue
            name = function.get("name")
            if not isinstance(name, str) or not name:
                continue
            raw_arguments = function.get("arguments")
            if isinstance(raw_arguments, dict):
                arguments = raw_arguments
            elif isinstance(raw_arguments, str) and raw_arguments.strip():
                try:
                    decoded = json.loads(raw_arguments)
                except json.JSONDecodeError:
                    decoded = {}
                arguments = decoded if isinstance(decoded, dict) else {}
            else:
                arguments = {}
            call_id = raw_call.get("id")
            calls.append(
                HybridToolCall(
                    call_id=str(call_id or f"m3-{index}"),
                    name=name,
                    arguments=arguments,
                )
            )
        return calls


def might_request_tool(text: str) -> bool:
    """Cheap conservative prefilter so ordinary dialogue keeps Realtime latency."""
    lowered = text.casefold()
    cues = (
        "短信", "验证码", "按键", "请按", "准备按", "挂断", "结束通话",
        "再见", "转接", "本人接听", "sms", "text message",
        "verification code", "press ", "dtmf", "hang up", "goodbye",
        "transfer", "owner",
    )
    return any(cue in lowered for cue in cues)
