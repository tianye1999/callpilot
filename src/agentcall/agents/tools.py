"""语音 Agent 可调用的工具（function calling）注册与分发。

工具规格遵循千问 Omni Realtime 的 session.tools 格式（function 嵌套）：
{"type": "function", "function": {"name": ..., "description": ..., "parameters": {JSON Schema}}}
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from .. import config

logger = logging.getLogger(__name__)

ToolHandler = Callable[[dict[str, Any]], dict[str, Any]]
SMS_TOOL_NAMES = frozenset({"send_sms", "query_verification_code", "wait_for_sms"})


# 发送短信工具规格（千问 Realtime 要求 function 嵌套格式）
SEND_SMS_SPEC: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "send_sms",
        "description": (
            "给指定手机号发送一条短信，支持中文。当用户在通话中要求发短信时调用本工具，"
            "例如“给我发一条短信”“给这个号码发条广告”。若用户说发给他本人/发到当前号码，"
            "可以把 to 留空，系统会自动使用当前通话对方的号码。"
            "若这条短信是把通话里的事转达给不在通话中的人（例如来电者请你给机主留言），"
            "正文要写成收信人只看这条短信就够用：先写清对方是谁（通话里说到的姓名、"
            "单位或身份；对方没说就如实写没留姓名），再写要转达什么事。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "to": {
                    "type": "string",
                    "description": (
                        "收件手机号码；若指当前通话对方本人则可留空；"
                        "要发给机主本人时填 owner，系统会用机主配置的号码。"
                    ),
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
            "直接调用本工具发送对应按键，不要口头宣布按键动作；发送后保持沉默，"
            "等待自动语音系统的下一段提示。digits 可以是多位，如 \"1\" 或 \"103#\"。"
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


WAIT_SMS_SPEC: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "wait_for_sms",
        "description": (
            "等待一条**新**短信到达并返回内容（最多等约一分钟）。适用于刚请求"
            "对方发送验证码/确认短信、短信还没到的场景——与 query_verification_code"
            "（查已收到的历史短信）互补。返回短信全文、发件号码，以及其中的数字"
            "验证码（若能识别）。等待期间你不能说话，所以调用前先对对方说一句"
            "「稍等，我看一下短信」。"
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}


ASK_OWNER_SPEC: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "ask_owner",
        "description": (
            "把一个需要机主拿主意的决定推送给机主确认，等机主答复（约一分钟）。"
            "仅在对方给出的方案/报价超出你被授权的范围、或事情确实必须机主本人"
            "决定时调用；调用前先对通话对方说明「稍等，我跟机主确认一下」。"
            "question 里把要决定的事说完整（方案内容、价格、期限等关键数字），"
            "机主看到的就是这段文字。返回机主的决定：approved（同意）、"
            "declined（不同意）或 timeout（没回应——按不同意处理，并请对方"
            "把方案记录在案以便后续跟进）。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "要机主决定的事项，含关键数字与条款，一段话说完整。",
                }
            },
            "required": ["question"],
        },
    },
}


REQUEST_OWNER_TAKEOVER_SPEC: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "request_owner_takeover",
        "description": (
            "请求把当前来电转给机主手机真人接听。仅当来电者明确要求找机主本人，"
            "或对话符合系统提示中的机主接管偏好时调用一次；不要用参数复述偏好、"
            "来电内容或模型推理。系统会负责垫话、等待和媒体切换。"
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}


# 终结性工具：调用后会话即将结束，不应再让模型生成后续回复。
# hangup_call 已要求「先说完告别再调用」，此时告别语已播出；若回传结果后再
# 触发 response.create，模型会在挂断延迟里多说一句（如“电话已经挂断…”），
# 对端听到多余的话。故对这类工具只回传 function_call_output、不再要新回复。
TERMINAL_TOOLS: frozenset[str] = frozenset({"hangup_call"})
# 这类工具执行后会话仍继续，但应等待对端下一段音频，而不是立即让模型说话。
# DTMF 后立刻口播会覆盖 IVR 的确认/下一层菜单，并触发半双工上行屏蔽。
SILENT_AFTER_TOOLS: frozenset[str] = frozenset(
    {"send_dtmf", "request_owner_takeover"}
)
# ask_owner 不进 SILENT_AFTER_TOOLS：拿到机主决定后模型要立刻把结果转达给
# 通话对方（接受报价/婉拒），沉默反而失礼。
_DTMF_LOG_MODES: frozenset[str] = frozenset(
    {"inband", "qvts", "both", "unknown"}
)


def _tool_name(spec: dict[str, Any]) -> str | None:
    """兼容扁平与 function 嵌套两种规格，取出工具名。"""
    if "function" in spec and isinstance(spec["function"], dict):
        return spec["function"].get("name")
    return spec.get("name")


def _dtmf_count(args: dict[str, Any]) -> int:
    digits = args.get("digits")
    return len(digits.strip()) if isinstance(digits, str) else 0


def _dtmf_log_mode(result: dict[str, Any]) -> str:
    mode = result.get("mode")
    return mode if isinstance(mode, str) and mode in _DTMF_LOG_MODES else "unknown"


# ---- 工具描述本地化 ----
# 规格里的中文描述会**随 session 一起发给模型**；英文场景下模型看到满屏中文
# 会跟着说中文（真机 2026-08-12：机主配置 AGENT_LANGUAGE=en、对端全程说英文，
# AI 却用中文开口）。这里在注册时按语言替换，不改动既有规格结构。
_EN_TEXT: dict[tuple[str, str | None], str] = {
    ("send_sms", None): (
        "Send an SMS on the owner's behalf. Use it when the caller asks for "
        "something in writing, or to pass a message along. When the message "
        "relays something from this call to someone who is not on it (e.g. the "
        "caller asks you to leave a message for the owner), write the body so "
        "the recipient needs nothing else: start with who is calling (the name, "
        "company, or role given during the call — say plainly that they gave no "
        "name if they didn't), then what they want passed on."
    ),
    ("send_sms", "to"): (
        "Recipient phone number; leave empty to text the current caller; "
        "pass \"owner\" to text the owner (the system fills in the owner's "
        "configured number)."
    ),
    ("send_sms", "content"): "Message body.",
    ("hangup_call", None): (
        "End the call. Say a short goodbye line before calling this."
    ),
    ("send_dtmf", None): (
        "Send DTMF keypad tones, for navigating phone menus."
    ),
    ("send_dtmf", "digits"): "Key sequence to send; only 0-9, * and # are allowed.",
    ("query_verification_code", None): (
        "Look up the most recently received SMS verification code."
    ),
    ("request_owner_takeover", None): (
        "Hand the live call over to the owner's phone."
    ),
    ("ask_owner", None): (
        "Push a decision to the owner and wait (about a minute) for their "
        "answer. Call it only when an offer or decision exceeds what you are "
        "authorized to accept, or truly needs the owner personally; tell the "
        "other party \"one moment, let me check with the account holder\" "
        "BEFORE calling. Put the full decision in `question` (plan, price, "
        "term — the key numbers); that text is exactly what the owner sees. "
        "Returns the owner's decision: approved, declined, or timeout (treat "
        "timeout as declined and ask the rep to note the offer on the account "
        "for follow-up)."
    ),
    ("ask_owner", "question"): (
        "The decision for the owner, complete in one passage with the key "
        "numbers and terms."
    ),
    ("wait_for_sms", None): (
        "Wait for a NEW incoming SMS and return it (up to about a minute). Use "
        "when a verification/confirmation text was just requested and has not "
        "arrived yet — complements query_verification_code, which searches "
        "already-received messages. Returns the full text, the sender, and the "
        "numeric code if one can be recognized. You cannot speak while "
        "waiting, so tell the other party \"one moment, let me check the "
        "text\" before calling."
    ),
}


def localize_spec(spec: dict[str, Any]) -> dict[str, Any]:
    """按 AGENT_LANGUAGE 本地化工具描述；非英文原样返回（不复制、零开销）。"""
    if config.get_str("AGENT_LANGUAGE").strip().lower() != "en":
        return spec
    name = _tool_name(spec)
    if not name:
        return spec
    fn = dict(spec.get("function", {}))
    top = _EN_TEXT.get((name, None))
    if top:
        fn["description"] = top
    params = fn.get("parameters") or {}
    props = params.get("properties") or {}
    if props:
        new_props = {}
        for key, schema in props.items():
            text = _EN_TEXT.get((name, key))
            new_props[key] = {**schema, "description": text} if text else schema
        fn["parameters"] = {**params, "properties": new_props}
    return {**spec, "function": fn}


class ToolRegistry:
    """工具注册表：保存规格与对应处理函数，供 Agent 调用。"""

    def __init__(self) -> None:
        self._tools: dict[str, tuple[dict[str, Any], ToolHandler]] = {}

    def register(self, spec: dict[str, Any], handler: ToolHandler) -> None:
        name = _tool_name(spec)
        if not name:
            raise ValueError("工具规格缺少 name")
        self._tools[name] = (localize_spec(spec), handler)

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
        dtmf_count = _dtmf_count(args) if name == "send_dtmf" else None
        sms_tool = name in SMS_TOOL_NAMES
        if sms_tool:
            logger.info("执行工具 %s（短信载荷不记录）", name)
        elif dtmf_count is None:
            logger.info("执行工具 %s，参数=%s", name, args)
        else:
            logger.info("执行工具 %s: count=%d", name, dtmf_count)
        try:
            result = handler(args)
        except Exception as exc:  # noqa: BLE001
            if dtmf_count is not None:
                logger.error(
                    "工具 %s 执行异常: count=%d, mode=unknown, "
                    "result=failure, error_type=%s",
                    name,
                    dtmf_count,
                    type(exc).__name__,
                )
            elif sms_tool:
                logger.error("工具 %s 执行异常: error_type=%s", name, type(exc).__name__)
            else:
                logger.exception("工具 %s 执行异常: %s", name, exc)
            if dtmf_count is not None:
                return {
                    "success": False,
                    "count": dtmf_count,
                    "mode": "unknown",
                    "message": "按键发送失败",
                }
            return {"success": False, "message": f"工具执行异常: {exc}"}
        if sms_tool:
            logger.info(
                "工具 %s 执行结果: success=%s", name,
                isinstance(result, dict) and result.get("success") is True,
            )
        elif dtmf_count is None:
            logger.info("工具 %s 执行结果=%s", name, result)
        else:
            logger.info(
                "工具 %s 执行结果: count=%d, mode=%s, result=%s",
                name,
                dtmf_count,
                _dtmf_log_mode(result),
                "success" if result.get("success") is True else "failure",
            )
        return result
