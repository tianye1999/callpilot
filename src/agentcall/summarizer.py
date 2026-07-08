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

logger = logging.getLogger(__name__)

# urgency 合法取值；模型输出不在此集合内时回落到默认值。
_VALID_URGENCY = ("高", "中", "低")
_DEFAULT_URGENCY = "中"

_ROLE_LABELS = {"user": "对方", "agent": "分身"}
_DIRECTION_LABELS = {
    "inbound": "来电（对方打给机主）",
    "outbound": "去电（机主的分身打给对方）",
}

_SYSTEM_PROMPT = (
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
)


def _default_result() -> dict[str, Any]:
    """返回一份全默认字段的结果骨架。"""
    return {
        "ok": False,
        "caller_identity": "未知",
        "intent": "未知",
        "urgency": _DEFAULT_URGENCY,
        "callback_needed": False,
        "summary": "",
        "error": None,
    }


def _fail(error: str) -> dict[str, Any]:
    result = _default_result()
    result["error"] = error
    return result


def _build_messages(
    transcripts: list[tuple[str, str]], direction: str, number: str | None
) -> list[dict[str, str]]:
    lines = []
    for role, text in transcripts:
        text = (text or "").strip()
        if not text:
            continue
        lines.append(f"{_ROLE_LABELS.get(role, role)}: {text}")
    user_content = (
        f"通话方向：{_DIRECTION_LABELS.get(direction, direction)}\n"
        f"对方号码：{number or '未知'}\n"
        f"通话转写：\n" + "\n".join(lines)
    )
    owner = config.get_str("OWNER_NAME").strip() or "机主"
    return [
        {"role": "system", "content": _SYSTEM_PROMPT.format(owner=owner)},
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


def _normalize(data: dict[str, Any]) -> dict[str, Any]:
    """把模型输出的 JSON 规整为契约字段，缺省/非法值补默认。"""
    result = _default_result()
    result["ok"] = True

    identity = data.get("caller_identity")
    if isinstance(identity, str) and identity.strip():
        result["caller_identity"] = identity.strip()

    intent = data.get("intent")
    if isinstance(intent, str) and intent.strip():
        result["intent"] = intent.strip()

    urgency = data.get("urgency")
    if isinstance(urgency, str) and urgency.strip() in _VALID_URGENCY:
        result["urgency"] = urgency.strip()

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
    try:
        has_user_speech = any(
            role == "user" and (text or "").strip() for role, text in transcripts or []
        )
        if not has_user_speech:
            return _fail("转写为空或无用户发言，跳过总结")

        if timeout is None:
            timeout = config.get_float("SUMMARY_TIMEOUT")

        model = config.get_str("SUMMARY_MODEL")
        messages = _build_messages(transcripts, direction, number)

        response, error = _call_with_timeout(messages, model, timeout)
        if error is not None:
            logger.warning("通话总结失败: %s", error)
            return _fail(error)

        status = getattr(response, "status_code", None)
        if status is not None and status != 200:
            error = (
                f"dashscope 返回 {status}: "
                f"{getattr(response, 'message', '') or getattr(response, 'code', '')}"
            )
            logger.warning("通话总结失败: %s", error)
            return _fail(error)

        text = _extract_text(response)
        if not text:
            return _fail("dashscope 响应中没有文本内容")

        data = _parse_json_payload(text)
        if data is None:
            logger.warning("通话总结 JSON 解析失败，原文: %.200s", text)
            return _fail("模型输出不是合法 JSON")

        return _normalize(data)
    except Exception as exc:  # noqa: BLE001 —— 契约：绝不抛出
        logger.exception("通话总结出现未预期异常")
        return _fail(f"{type(exc).__name__}: {exc}")
