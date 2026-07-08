"""通话后结构化总结：把整通转写交给 dashscope 文本模型，产出结构化摘要。

调用方通常在通话结束后的后台线程里执行，因此本模块保证 ``summarize_call``
**绝不抛出异常**——任何失败（无有效转写、API 报错、超时、JSON 解析失败）
都以 ``ok=False + error`` 的形式返回。
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from typing import Any

import dashscope

from . import config
from .prompts import agent_language, owner_name

logger = logging.getLogger(__name__)

# urgency 合法取值（按语言）；模型输出不在集合内时回落到默认值。
_VALID_URGENCY = {"zh": ("高", "中", "低"), "en": ("high", "medium", "low")}
_DEFAULT_URGENCY = {"zh": "中", "en": "medium"}
_UNKNOWN = {"zh": "未知", "en": "unknown"}

_ROLE_LABELS = {
    "zh": {"user": "对方", "agent": "分身"},
    "en": {"user": "Caller", "agent": "AI"},
}
_DIRECTION_LABELS = {
    "zh": {
        "inbound": "来电（对方打给机主）",
        "outbound": "去电（机主的分身打给对方）",
    },
    "en": {
        "inbound": "inbound (the other party called the owner)",
        "outbound": "outbound (the owner's AI called the other party)",
    },
}
_META_LABELS = {
    "zh": {"direction": "通话方向", "number": "对方号码", "transcript": "通话转写"},
    "en": {"direction": "Call direction", "number": "Other party's number",
           "transcript": "Transcript"},
}

_SYSTEM_PROMPT = {
    "zh": (
        "你是通话记录分析助手。通话中的 AI 是机主{owner}的电话助理，"
        "代替{owner}接打电话。下面给你一通电话的完整转写，请从{owner}的视角分析并总结。\n"
        "必须只输出一个严格合法的 JSON 对象（不要输出任何解释、markdown 围栏或多余文字），"
        "字段如下：\n"
        '- "caller_identity": 字符串，对方是谁（如"快递员""银行客服""朋友张三"），'
        '判断不出写"未知"\n'
        '- "intent": 字符串，对方来意/通话目的，一句话\n'
        '- "urgency": 字符串，紧急程度，只能是"高"、"中"、"低"之一\n'
        '- "callback_needed": 布尔值，机主本人是否需要回电或跟进\n'
        '- "summary": 字符串，2~3 句话的通话摘要，写清结论和待办\n'
    ),
    "en": (
        "You are a call-log analysis assistant. The AI on the call is {owner}'s "
        "phone assistant, taking and making calls on {owner}'s behalf. Below is the "
        "full transcript of one call; analyze and summarize it from {owner}'s "
        "perspective.\n"
        "Output ONLY a single strictly-valid JSON object (no explanation, no "
        "markdown fences, no extra text), with these fields:\n"
        '- "caller_identity": string, who the other party is (e.g. "courier", '
        '"bank agent", "friend Alex"); write "unknown" if unclear\n'
        '- "intent": string, the other party\'s purpose, one sentence\n'
        '- "urgency": string, one of "high", "medium", "low"\n'
        '- "callback_needed": boolean, whether the owner personally needs to call '
        "back or follow up\n"
        '- "summary": string, a 2-3 sentence summary with the conclusion and any '
        "to-dos\n"
    ),
}


def _default_result(lang: str = "zh") -> dict[str, Any]:
    """返回一份全默认字段的结果骨架。"""
    return {
        "ok": False,
        "caller_identity": _UNKNOWN[lang],
        "intent": _UNKNOWN[lang],
        "urgency": _DEFAULT_URGENCY[lang],
        "callback_needed": False,
        "summary": "",
        "error": None,
    }


def _fail(error: str, lang: str = "zh") -> dict[str, Any]:
    result = _default_result(lang)
    result["error"] = error
    return result


def _build_messages(
    transcripts: list[tuple[str, str]], direction: str, number: str | None, lang: str = "zh"
) -> list[dict[str, str]]:
    roles = _ROLE_LABELS[lang]
    meta = _META_LABELS[lang]
    lines = []
    for role, text in transcripts:
        text = (text or "").strip()
        if not text:
            continue
        lines.append(f"{roles.get(role, role)}: {text}")
    user_content = (
        f"{meta['direction']}：{_DIRECTION_LABELS[lang].get(direction, direction)}\n"
        f"{meta['number']}：{number or _UNKNOWN[lang]}\n"
        f"{meta['transcript']}：\n" + "\n".join(lines)
    )
    return [
        {"role": "system", "content": _SYSTEM_PROMPT[lang].format(owner=owner_name(lang))},
        {"role": "user", "content": user_content},
    ]


def _extract_text(response: Any) -> str | None:
    """从 GenerationResponse（result_format='message'）里取出正文文本。"""
    try:
        content = response.output.choices[0].message.content
    except (AttributeError, IndexError, KeyError, TypeError):
        return None
    # 多模态模型可能返回 [{"text": "..."}] 形式，做一层兼容。
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        content = "".join(parts)
    return content if isinstance(content, str) else None


def _parse_json_payload(text: str) -> dict[str, Any] | None:
    """解析模型输出的 JSON，容忍 markdown 围栏和前后杂讯。"""
    text = text.strip()
    # 剥掉 ```json ... ``` / ``` ... ``` 围栏。
    fence = re.match(r"^```[a-zA-Z]*\s*(.*?)\s*```\s*$", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # 兜底：截取首个 "{" 到最后一个 "}" 之间的子串再试一次。
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            data = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None
    return data if isinstance(data, dict) else None


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"true", "yes", "1", "是", "需要"}
    if isinstance(value, (int, float)):
        return bool(value)
    return False


def _normalize(data: dict[str, Any], lang: str = "zh") -> dict[str, Any]:
    """把模型输出的 JSON 规整为契约字段，缺省/非法值补默认。"""
    result = _default_result(lang)
    result["ok"] = True

    identity = data.get("caller_identity")
    if isinstance(identity, str) and identity.strip():
        result["caller_identity"] = identity.strip()

    intent = data.get("intent")
    if isinstance(intent, str) and intent.strip():
        result["intent"] = intent.strip()

    urgency = data.get("urgency")
    if isinstance(urgency, str) and urgency.strip().lower() in _VALID_URGENCY[lang]:
        result["urgency"] = urgency.strip().lower() if lang == "en" else urgency.strip()

    result["callback_needed"] = _coerce_bool(data.get("callback_needed"))

    summary = data.get("summary")
    if isinstance(summary, str):
        result["summary"] = summary.strip()

    return result


def _call_with_timeout(
    messages: list[dict[str, str]], model: str, timeout: float
) -> tuple[Any, str | None]:
    """在守护线程里调 dashscope，超时不阻塞调用方。

    返回 ``(response, error)``；超时或异常时 response 为 None。
    """
    box: dict[str, Any] = {}

    def _worker() -> None:
        try:
            box["response"] = dashscope.Generation.call(
                model=model,
                messages=messages,
                result_format="message",
                api_key=os.environ.get("DASHSCOPE_API_KEY"),
            )
        except Exception as exc:  # noqa: BLE001 —— 后台线程里不允许异常外逸
            box["error"] = f"{type(exc).__name__}: {exc}"

    thread = threading.Thread(target=_worker, name="call-summarizer", daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        return None, f"总结请求超时（>{timeout:g}s）"
    if "error" in box:
        return None, box["error"]
    return box.get("response"), None


def summarize_call(
    transcripts: list[tuple[str, str]],
    direction: str,
    number: str | None,
    *,
    timeout: float | None = None,
) -> dict:
    """对一通电话的转写做结构化总结。

    :param transcripts: ``[(role, text)]``，role 为 ``"user"`` 或 ``"agent"``。
    :param direction: 通话方向（``"inbound"``/``"outbound"``，其余值原样带入 prompt）。
    :param number: 对方号码，未知可传 None。
    :param timeout: API 调用超时秒数；缺省读注册表 ``SUMMARY_TIMEOUT``（默认 30，
        真机实测 15s 对长转写不够用）。
    :returns: ``{"ok", "caller_identity", "intent", "urgency",
        "callback_needed", "summary", "error"}``；失败时 ``ok=False`` 且
        ``error`` 描述原因。本函数保证不抛出异常。
    """
    lang = agent_language()
    try:
        has_user_speech = any(
            role == "user" and (text or "").strip() for role, text in transcripts or []
        )
        if not has_user_speech:
            return _fail("转写为空或无用户发言，跳过总结", lang)

        if timeout is None:
            timeout = config.get_float("SUMMARY_TIMEOUT")

        model = config.get_str("SUMMARY_MODEL")
        messages = _build_messages(transcripts, direction, number, lang)

        response, error = _call_with_timeout(messages, model, timeout)
        if error is not None:
            logger.warning("通话总结失败: %s", error)
            return _fail(error, lang)

        status = getattr(response, "status_code", None)
        if status is not None and status != 200:
            error = (
                f"dashscope 返回 {status}: "
                f"{getattr(response, 'message', '') or getattr(response, 'code', '')}"
            )
            logger.warning("通话总结失败: %s", error)
            return _fail(error, lang)

        text = _extract_text(response)
        if not text:
            return _fail("dashscope 响应中没有文本内容", lang)

        data = _parse_json_payload(text)
        if data is None:
            logger.warning("通话总结 JSON 解析失败，原文: %.200s", text)
            return _fail("模型输出不是合法 JSON", lang)

        return _normalize(data, lang)
    except Exception as exc:  # noqa: BLE001 —— 契约：绝不抛出
        logger.exception("通话总结出现未预期异常")
        return _fail(f"{type(exc).__name__}: {exc}", lang)
