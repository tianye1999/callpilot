"""通话中 Agent 工具（function calling）的处理与注册。

从 call_agent.CallSession 拆出（code-review 2026-07 P1 #6）：
``CallTools`` 只做工具语义（参数校验、modem 调用、事件推送、审计日志），
不持有会话生命周期——延迟挂断的 Timer/世代号机制留在 CallSession，
这里通过 ``schedule_hangup`` 回调触发。
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Callable

from . import config
from .agents.tools import (
    HANGUP_SPEC,
    QUERY_CODE_SPEC,
    SEND_DTMF_SPEC,
    SEND_SMS_SPEC,
    ToolRegistry,
)
from .rate_limit import acquire_sms_send_slot

if TYPE_CHECKING:
    from .call_log import CallRecord
    from .events import EventHub
    from .modem import Eg25Modem

logger = logging.getLogger(__name__)


class CallTools:
    """一通会话的工具集：构造时注入会话上下文，``register()`` 产出注册表。

    ``get_caller``/``get_record`` 用取值回调而非快照——通话过程中
    当前号码与通话记录都可能变化，工具执行时才取当下值。
    """

    def __init__(
        self,
        modem: "Eg25Modem",
        *,
        hub: "EventHub | None",
        get_caller: Callable[[], str | None],
        get_record: Callable[[], "CallRecord | None"],
        schedule_hangup: Callable[[], None],
        is_sms_target_allowed: Callable[[str], bool] | None = None,
    ) -> None:
        self._modem = modem
        self._hub = hub
        self._get_caller = get_caller
        self._get_record = get_record
        self._schedule_hangup = schedule_hangup
        # 发短信目标限制:只允许回复已联系过的号码(由 CallSession 注入)。
        # None = 不限制(直接构造 CallTools 的单测保持旧行为)。
        self._is_sms_target_allowed = is_sms_target_allowed

    def register(self) -> ToolRegistry:
        registry = ToolRegistry()
        registry.register(SEND_SMS_SPEC, self._send_sms)
        registry.register(HANGUP_SPEC, self._hangup)
        if config.get_bool("TOOL_QUERY_CODE_ENABLED"):
            registry.register(QUERY_CODE_SPEC, self._query_code)
        registry.register(SEND_DTMF_SPEC, self._send_dtmf)
        return registry

    def _publish(self, event: dict) -> None:
        if self._hub:
            self._hub.publish(event)

    def _audit_tool(
        self,
        tool: str,
        *,
        args: dict,
        result: dict,
    ) -> None:
        record = self._get_record()
        if record is not None:
            record.log_event("tool_call", tool=tool, args=args, result=result)

    def _send_sms(self, args: dict) -> dict:
        """工具处理：Agent 在通话中请求发送短信。"""
        number = (args.get("to") or "").strip() or (self._get_caller() or "").strip()
        content = (args.get("content") or "").strip()
        if not number:
            self._audit_tool(
                "send_sms",
                args={"to": "", "content_length": len(content)},
                result={"success": False},
            )
            return {"success": False, "message": "没有可用的收件号码"}
        if not content:
            self._audit_tool(
                "send_sms",
                args={"to": number, "content_length": 0},
                result={"success": False},
            )
            return {"success": False, "message": "短信内容为空"}
        if self._is_sms_target_allowed is not None and not self._is_sms_target_allowed(
            number
        ):
            logger.warning("发短信被拦截(非已联系号码): %s", number)
            result = {
                "success": False,
                "message": "只能给来过电或发过短信的号码回复短信",
            }
            self._audit_tool(
                "send_sms",
                args={"to": number, "content_length": len(content)},
                result={"success": False},
            )
            return result
        slot = acquire_sms_send_slot(config.get_int("SMS_RATE_LIMIT_PER_HOUR"))
        if not slot.allowed:
            logger.warning("发短信被频控拦截: to=%s retry_after=%.1fs", number, slot.retry_after)
            result = {
                "success": False,
                "message": "短信发送触发频控，请稍后再试",
                "retry_after": round(slot.retry_after, 1),
            }
            self._audit_tool(
                "send_sms",
                args={"to": number, "content_length": len(content)},
                result={"success": False},
            )
            return result
        try:
            ok = self._modem.send_sms(number, content)
        except Exception as exc:  # noqa: BLE001
            logger.warning("工具发送短信失败: %s", exc)
            self._audit_tool(
                "send_sms",
                args={"to": number, "content_length": len(content)},
                result={"success": False},
            )
            return {"success": False, "message": f"发送失败: {exc}"}
        if ok:
            self._publish(
                {
                    "type": "sms_out",
                    "number": number,
                    "text": content,
                    "status": "sent",
                }
            )
        result = {
            "success": ok,
            "to": number,
            "content": content,
            "message": "短信已发送" if ok else "短信发送失败",
        }
        self._audit_tool(
            "send_sms",
            args={"to": number, "content_length": len(content)},
            result={"success": bool(ok)},
        )
        return result

    def _hangup(self, args: dict) -> dict:
        """工具处理：Agent 请求挂断当前通话。

        实际是排定延迟挂断（CallSession 负责 Timer 与世代号），
        先让 Agent 把告别语播完，避免话没说完线路就断了。
        """
        self._schedule_hangup()
        result = {"success": True, "message": "好的，马上为您挂断电话"}
        self._audit_tool(
            "hangup_call",
            args={},
            result={"success": True},
        )
        return result

    def _send_dtmf(self, args: dict) -> dict:
        """工具处理：Agent 请求发送 DTMF 按键（IVR 导航）。"""
        digits = (args.get("digits") or "").strip()
        if not digits:
            return {"success": False, "message": "按键序列为空"}
        try:
            ok = self._modem.send_dtmf(digits)
        except Exception as exc:  # noqa: BLE001
            logger.warning("工具发送 DTMF 失败: %s", exc)
            return {"success": False, "message": f"按键发送失败: {exc}"}
        record = self._get_record()
        if ok and record is not None:
            record.log_event("dtmf", digits=digits)
        return {
            "success": ok,
            "digits": digits,
            "message": f"已按 {digits}" if ok else "按键发送失败",
        }

    def _query_code(self, args: dict) -> dict:
        """工具处理：从最近收到的短信里查验证码。"""
        code, text, sender = self._find_latest_code()
        if code:
            result = {
                "success": True,
                "code": code,
                "sender": sender,
                "sms_text": text,
                "message": f"最近收到的验证码是 {code}",
            }
            self._audit_tool(
                "query_verification_code",
                args={},
                result={"success": True, "hit": True},
            )
            return result
        result = {"success": False, "message": "最近没有收到含验证码的短信"}
        self._audit_tool(
            "query_verification_code",
            args={},
            result={"success": False, "hit": False},
        )
        return result

    def _find_latest_code(self) -> tuple[str | None, str | None, str | None]:
        """在已收到的短信中查找最近的数字验证码。

        优先匹配含“验证码/校验码/code”等关键词的短信，找不到再退回任意含
        4-8 位数字的短信。返回 (验证码, 短信全文, 发件号码)。
        """
        if not self._hub:
            return None, None, None
        sms_events = [e for e in self._hub.history() if e.get("type") == "sms_in"]
        code_re = re.compile(r"(?<!\d)(\d{4,8})(?!\d)")
        keyword_re = re.compile(r"验证码|校验码|动态码|verification|code|otp", re.I)

        def scan(prefer_keyword: bool) -> tuple[str | None, str | None, str | None]:
            for event in reversed(sms_events):
                text = event.get("text") or ""
                if prefer_keyword and not keyword_re.search(text):
                    continue
                match = code_re.search(text)
                if match:
                    return match.group(1), text, event.get("sender")
            return None, None, None

        result = scan(prefer_keyword=True)
        if result[0]:
            return result
        return scan(prefer_keyword=False)
