"""通话后结构化总结：按当前 Agent provider 调文本模型产出摘要。

调用方通常在通话结束后的后台线程里执行，因此本模块保证 ``summarize_call``
**绝不抛出异常**——任何失败（无有效转写、API 报错、超时、JSON 解析失败）
都以 ``ok=False + error`` 的形式返回。
"""

from __future__ import annotations

import logging
from typing import Any

from . import config
from .prompt_gen import (
    call_text_model,
    parse_json_payload,
    select_text_model,
    text_backend_for_agent,
)
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


# 收尾裁判：让文本模型「理解对话」判断该继续还是收尾，替代关键词枚举。
_JUDGE_TIMEOUT = 8.0

_JUDGE_SYSTEM = {
    "zh": (
        "你在监督机主{owner}的电话助理正在进行的一通外呼。本通目标：{goal}。\n"
        "判断依据是「有没有真正拿到实质结果」，而不是「对话听起来是否结束了」。\n"
        "- wrap_up（收尾）：所要的信息/结果已被对方真正给出、或事情确实办成；或已明显"
        "在原地打转、多次尝试仍无实质进展；或对方明确要结束。\n"
        "  特别注意：对方给出的**明确否定式/空结果答复也算拿到了实质结果**——比如"
        "「未办理该业务」「名下没有这项套餐」「查不到相关记录」「不支持办理」。"
        "这类答复本身就是最终答案，不是「还没查到」；此时应 wrap_up，"
        "而不是换个说法再问一遍同一件事。\n"
        "- continue（继续）：对方还没针对所问的事给出明确答复（在查询中、答非所问、"
        "被打断、只是客套），实质结果还没真正到手。\n"
        "拿不准时倾向继续：只有确信已拿到结果（含明确的否定答复）、或确信卡死，"
        "才判 wrap_up。\n"
        "只输出严格合法的 JSON，无任何多余文字："
        '{{"decision": "continue" 或 "wrap_up", "reason": "一句话理由"}}'
    ),
    "en": (
        "You are supervising an ongoing OUTBOUND call by {owner}'s phone assistant. "
        "Goal of this call: {goal}.\n"
        "Judge on whether the substance was actually obtained, not on whether the "
        "conversation sounds finished.\n"
        "- wrap_up: the requested info/result has actually been provided, or the task "
        "is genuinely done; or it's clearly going in circles with no real progress; "
        "or the other party wants to end.\n"
        "  Important: a clear NEGATIVE or empty-result answer from the other side "
        "ALSO counts as the substantive result — e.g. \"no such plan on this "
        "account\", \"this service is not activated\", \"no matching record\". "
        "Such an answer IS the final answer, not \"not found yet\"; wrap_up instead "
        "of re-asking the same question in different words.\n"
        "- continue: the other side has not yet given a definite answer to the "
        "question (still looking it up, answered something else, got cut off, or "
        "was merely being polite) — the substance isn't in hand yet.\n"
        "When in doubt, lean continue: only wrap_up once you truly have the result "
        "(including a definite negative answer) or it's clearly stuck.\n"
        'Output only strict JSON, no extra text: {{"decision": "continue" or '
        '"wrap_up", "reason": "one short sentence"}}'
    ),
}


def judge_wrap_up(
    transcripts: list[tuple[str, str]],
    goal: str,
    *,
    timeout: float | None = None,
) -> dict:
    """判断进行中的外呼该继续还是收尾（用文本模型理解对话，非关键词枚举）。

    契约同 summarize_call：**绝不抛异常**；任何失败一律返回 continue（保守，
    交给外呼硬时限 OUTBOUND_MAX_SECONDS 兜底，避免误判导致过早挂断）。
    """
    lang = agent_language()
    try:
        lines = [
            (r, (t or "").strip())
            for r, t in (transcripts or [])
            if (t or "").strip()
        ]
        if len(lines) < 3:
            return {"ok": True, "decision": "continue", "reason": "对话刚开始"}
        roles = _ROLE_LABELS[lang]
        convo = "\n".join(f"{roles.get(r, r)}: {t}" for r, t in lines[-16:])
        goal_text = (goal or "").strip() or _UNKNOWN[lang]
        messages = [
            {
                "role": "system",
                "content": _JUDGE_SYSTEM[lang].format(
                    owner=owner_name(lang), goal=goal_text
                ),
            },
            {"role": "user", "content": convo},
        ]
        provider = text_backend_for_agent()
        model = select_text_model(provider, config.get_str("SUMMARY_MODEL"))
        text, error = call_text_model(
            messages,
            provider=provider,
            model=model,
            timeout=timeout or _JUDGE_TIMEOUT,
        )
        if error is not None:
            logger.debug("收尾裁判失败（默认继续）: %s", error)
            return {"ok": False, "decision": "continue", "reason": error}
        data = parse_json_payload(text) if text else None
        reason = str((data or {}).get("reason", ""))[:120]
        if (data or {}).get("decision") == "wrap_up":
            return {"ok": True, "decision": "wrap_up", "reason": reason}
        return {"ok": True, "decision": "continue", "reason": reason}
    except Exception as exc:  # noqa: BLE001 —— 契约：绝不抛出
        logger.debug("收尾裁判异常（默认继续）: %s", exc)
        return {"ok": False, "decision": "continue", "reason": f"{type(exc).__name__}: {exc}"}


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

        provider = text_backend_for_agent()
        model = select_text_model(provider, config.get_str("SUMMARY_MODEL"))
        messages = _build_messages(transcripts, direction, number, lang)

        text, error = call_text_model(
            messages,
            provider=provider,
            model=model,
            timeout=timeout,
            max_tokens=600,
        )
        if error is not None:
            logger.warning("通话总结失败: %s", error)
            return _fail(error, lang)
        if not text:
            return _fail("文本模型响应中没有文本内容", lang)

        data = parse_json_payload(text)
        if data is None:
            logger.warning("通话总结 JSON 解析失败，原文: %.200s", text)
            return _fail("模型输出不是合法 JSON", lang)

        return _normalize(data, lang)
    except Exception as exc:  # noqa: BLE001 —— 契约：绝不抛出
        logger.exception("通话总结出现未预期异常")
        return _fail(f"{type(exc).__name__}: {exc}", lang)
