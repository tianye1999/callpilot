"""通话中 Agent 工具（function calling）的处理与注册。

从 call_agent.CallSession 拆出（code-review 2026-07 P1 #6）：
``CallTools`` 只做工具语义（参数校验、modem 调用、事件推送、审计日志），
不持有会话生命周期——延迟挂断的 Timer/世代号机制留在 CallSession，
这里通过 ``schedule_hangup`` 回调触发。
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from typing import TYPE_CHECKING, Callable

from . import config
from .agents.tools import (
    ASK_OWNER_SPEC,
    HANGUP_SPEC,
    QUERY_CODE_SPEC,
    SEND_DTMF_SPEC,
    SEND_SMS_SPEC,
    WAIT_SMS_SPEC,
    ToolRegistry,
)
from .rate_limit import acquire_sms_send_slot

if TYPE_CHECKING:
    from .call_log import CallRecord
    from .events import EventHub
    from .modem import SerialModem

DtmfSender = Callable[[str], tuple[bool, str]]

logger = logging.getLogger(__name__)

# 号码清洗与校验：与拨号侧同一形状规则（call_agent/number_profiles/remote_dialer
# 均为 \+?[0-9*#]{1,32}）。分隔符先剥掉——模型常写 "+1 (650) 555-0100" 这种人类格式。
_NUMBER_SEPARATORS = re.compile(r"[\s\-()]+")
_VALID_NUMBER = re.compile(r"^\+?[0-9*#]{1,32}$")
_NON_DIGITS = re.compile(r"\D+")
# owner 标记的宽松剥离：提示词里带引号展示（pass "owner"），模型可能原样带引号传。
_OWNER_TOKEN_TRIM = " \t\"'“”‘’「」`.。"


def _sanitize_number(value: str) -> str:
    return _NUMBER_SEPARATORS.sub("", value.strip())


def _digits(value: str) -> str:
    return _NON_DIGITS.sub("", value)


def _same_number(a: str, b: str) -> bool:
    """两串是否指同一号码：全等，或数字部分相同/一方是另一方的长尾。

    国家码差异（+8613800000000 vs 13800000000）是最常见的形变；尾串至少 8 位
    才算同号，避免短号误配。完整 E.164 归一化是 #75 的范围，这里只收敛
    「同一通话里两种写法」这一类。
    """
    if not a or not b:
        return False
    if a == b:
        return True
    da, db = _digits(a), _digits(b)
    if not da or not db:
        return False
    if da == db:
        return True
    longer, shorter = (da, db) if len(da) >= len(db) else (db, da)
    return len(shorter) >= 8 and longer.endswith(shorter)


def _content_has_number(content: str, number: str) -> bool:
    """正文里是否已含该号码：数字边界匹配，容忍空格/连字符/括号分隔。

    朴素的 ``number in content`` 两头都错：10086 会命中「余额 100863 元」
    （误判已含，漏掉回拨号码），+86 全格式又匹配不到正文里的裸号码。
    """
    d = _digits(number)
    if not d:
        return number in content
    candidates = {d}
    if len(d) > 11:
        candidates.add(d[-11:])
    if len(d) > 10:
        candidates.add(d[-10:])
    for cand in candidates:
        pattern = r"[\s\-()]*".join(re.escape(ch) for ch in cand)
        if re.search(rf"(?<!\d){pattern}(?!\d)", content):
            return True
    return False


class CallTools:
    """一通会话的工具集：构造时注入会话上下文，``register()`` 产出注册表。

    ``get_caller``/``get_record`` 用取值回调而非快照——通话过程中
    当前号码与通话记录都可能变化，工具执行时才取当下值。
    """

    def __init__(
        self,
        modem: "SerialModem",
        *,
        hub: "EventHub | None",
        get_caller: Callable[[], str | None],
        get_record: Callable[[], "CallRecord | None"],
        schedule_hangup: Callable[[], None],
        is_sms_target_allowed: Callable[[str], bool] | None = None,
        send_dtmf: DtmfSender | None = None,
        effect_guard: Callable[[], bool] | None = None,
        direction: str | None = None,
    ) -> None:
        self._modem = modem
        # 通话方向由 CallSession 显式注入（它本来就算好了传给 _build_tools），
        # 不从 CallRecord 上探——record 在 begin_call 失败时是 None，靠 getattr
        # 探方向会让来电转告静默丢掉来电号码行（review finding #3）。
        self._direction = direction
        self._hub = hub
        self._get_caller = get_caller
        self._get_record = get_record
        self._schedule_hangup = schedule_hangup
        # 发短信目标限制:只允许回复已联系过的号码(由 CallSession 注入)。
        # None = 不限制(直接构造 CallTools 的单测保持旧行为)。
        self._is_sms_target_allowed = is_sms_target_allowed
        self._send_dtmf_impl = send_dtmf or self._send_dtmf_via_modem
        self._dtmf_fallback_mode = "unknown" if send_dtmf else "qvts"
        self._effect_guard = effect_guard or (lambda: True)

    def register(self) -> ToolRegistry:
        registry = ToolRegistry()
        registry.register(SEND_SMS_SPEC, self._timed("send_sms", self._send_sms))
        registry.register(HANGUP_SPEC, self._timed("hangup_call", self._hangup))
        if config.get_bool("TOOL_QUERY_CODE_ENABLED"):
            registry.register(
                QUERY_CODE_SPEC,
                self._timed("query_verification_code", self._query_code),
            )
            registry.register(
                WAIT_SMS_SPEC, self._timed("wait_for_sms", self._wait_for_sms)
            )
        registry.register(SEND_DTMF_SPEC, self._timed("send_dtmf", self._send_dtmf))
        if self._direction == "outbound":
            # 机主确认环（WIL-120 二期）：外呼专用。inbound 的对应机制是
            # takeover（把电话整个交给机主），语义不同，不共用。
            registry.register(ASK_OWNER_SPEC, self._timed("ask_owner", self._ask_owner))
        return registry

    def _timed(
        self, tool: str, fn: Callable[[dict], dict]
    ) -> Callable[[dict], dict]:
        """工具耗时埋点（WIL-95 第二期）：latency 事件 stage=tool_call 带 tool 名。

        原 `tool_call` 审计事件无耗时字段；这里在注册处统一计时，覆盖含早退
        路径的完整耗时。埋点失败绝不影响工具本身（护栏同 call_agent 埋点）。
        """

        def wrapper(args: dict) -> dict:
            t0 = time.monotonic()
            try:
                return fn(args)
            finally:
                record = self._get_record()
                if record is not None:
                    try:
                        record.log_latency(
                            "tool_call",
                            round((time.monotonic() - t0) * 1000, 1),
                            tool=tool,
                        )
                    except Exception:  # noqa: BLE001
                        pass

        return wrapper

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

    def _stale_effect_result(
        self, tool: str, args: dict
    ) -> dict | None:
        if self._effect_guard():
            return None
        result = {
            "success": False,
            "code": "STALE_AGENT_GENERATION",
            "message": "会话媒体所有权已切换，忽略迟到的工具调用",
        }
        if tool == "send_dtmf":
            digits = args.get("digits")
            result.update(
                {
                    "count": len(digits.strip()) if isinstance(digits, str) else 0,
                    "mode": "unknown",
                }
            )
        logger.info("拒绝迟到的 Agent 工具调用: tool=%s", tool)
        return result

    def _with_caller_number(self, number: str, content: str, caller: str) -> str:
        """给机主的转告短信补一行来电号码，机主才知道该回给谁。

        真机 2026-08-14：来电者请 AI「给机主发个 message」，模型写出的正文只有
        「Caller needs to speak with you. Please reach out to them when available.」——
        机主既不知道是谁打来的，也没有号码可回。姓名只存在于对话里，只能交给模型
        （工具描述已要求写进正文）；但号码是系统确知的事实，不该指望模型转述。

        只在「来电 + 收件人是机主」时追加（调用方只在 owner 中继时进来）：
        给来电者本人回短信不加，第三方短信更不能加——那会把来电者的号码
        泄露给无关第三方（review finding #1）。追加行的语言跟正文的字符集走，
        不跟 AGENT_LANGUAGE：给纯 ASCII 正文追中文行会把整条短信从 GSM-7
        翻成 UCS2，单段上限从 160 字掉到 70 字（review finding #5）。
        """
        if not content or not caller or self._direction != "inbound":
            return content
        if _same_number(number, caller):
            return content
        if _content_has_number(content, caller):  # 模型已把号码写进正文，不重复
            return content
        line = (
            f"(Caller: {caller})"
            if content.isascii()
            else f"（来电号码：{caller}）"
        )
        return f"{content}\n{line}"

    def _send_sms(self, args: dict) -> dict:
        """工具处理：Agent 在通话中请求发送短信。"""
        if not getattr(self._modem, "supports_sms", True):
            return self._sms_unavailable("send_sms")
        stale = self._stale_effect_result("send_sms", args)
        if stale is not None:
            return stale
        caller = (self._get_caller() or "").strip()
        raw_to = (args.get("to") or "").strip()
        raw_content = (args.get("content") or "").strip()
        owner_phone = _sanitize_number(config.get_str("OWNER_PHONE"))
        # to="owner" 是工具契约里的固定标记：由系统解析成 OWNER_PHONE。
        # 号码不进提示词，模型既不用转述数字，也不该把它念给来电者（WIL-116）。
        # 宽松剥引号再比对——提示词里的写法是带引号的 "owner"，模型可能照抄。
        owner_token = raw_to.strip(_OWNER_TOKEN_TRIM).lower() == "owner"
        if owner_token:
            if not owner_phone:
                self._audit_tool(
                    "send_sms",
                    args={"to": "owner", "content_length": len(raw_content)},
                    result={"success": False},
                )
                return {
                    "success": False,
                    "message": "机主号码未配置（OWNER_PHONE），无法把短信发给机主",
                }
            if not _VALID_NUMBER.match(owner_phone):
                self._audit_tool(
                    "send_sms",
                    args={"to": "owner", "content_length": len(raw_content)},
                    result={"success": False},
                )
                return {
                    "success": False,
                    "message": "OWNER_PHONE 配置的机主号码格式无效，无法发送",
                }
            number = owner_phone
        else:
            number = _sanitize_number(raw_to) if raw_to else caller
            if raw_to and not _VALID_NUMBER.match(number):
                # 非号码也非 owner 标记：大概率是标记拼错（"机主"/"Owner phone"）。
                # 错误信息必须教会模型正确写法，否则它无从自纠（review finding #4）。
                self._audit_tool(
                    "send_sms",
                    args={"to": number, "content_length": len(raw_content)},
                    result={"success": False},
                )
                return {
                    "success": False,
                    "message": "收件号码无效；如果要发给机主本人，请把 to 填 owner",
                }
        # 只有发给机主的中继短信才补来电号码行；第三方短信补了就是把来电者
        # 的号码泄露给无关的人（review finding #1）。模型直接填机主号码（而非
        # owner 标记）时同样算中继。
        owner_relay = owner_token or (
            bool(owner_phone) and _same_number(number, owner_phone)
        )
        content = (
            self._with_caller_number(number, raw_content, caller)
            if owner_relay
            else raw_content
        )
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
            logger.warning("工具发送短信失败: error_type=%s", type(exc).__name__)
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
            # owner 标记发送时回传标记而不是真实号码：工具结果会回流进模型
            # 上下文，回传号码等于送到模型嘴边（review finding #2）。
            "to": "owner" if owner_token else number,
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
        stale = self._stale_effect_result("hangup_call", args)
        if stale is not None:
            return stale
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
        stale = self._stale_effect_result("send_dtmf", args)
        if stale is not None:
            return stale
        digits = (args.get("digits") or "").strip()
        if not digits:
            return {
                "success": False,
                "count": 0,
                "mode": "unknown",
                "message": "按键序列为空",
            }
        mode = self._dtmf_fallback_mode
        try:
            ok, mode = self._send_dtmf_impl(digits)
        except Exception as exc:  # noqa: BLE001
            logger.warning("工具发送 DTMF 失败: error_type=%s", type(exc).__name__)
            record = self._get_record()
            if record is not None:
                record.log_event(
                    "dtmf", count=len(digits), mode=mode, result="failure"
                )
            return {
                "success": False,
                "count": len(digits),
                "mode": mode,
                "message": "按键发送失败",
            }
        record = self._get_record()
        if record is not None:
            record.log_event(
                "dtmf",
                count=len(digits),
                mode=mode,
                result="success" if ok else "failure",
            )
        return {
            "success": ok,
            "count": len(digits),
            "mode": mode,
            "message": "按键发送成功" if ok else "按键发送失败",
        }

    def _send_dtmf_via_modem(self, digits: str) -> tuple[bool, str]:
        return self._modem.send_dtmf(digits), "qvts"

    def _ask_owner(self, args: dict) -> dict:
        """工具处理：把决定推给机主确认，阻塞等答复（WIL-120 二期 Path A）。

        运行在工具线程（openai: to_thread / qwen: worker 线程），阻塞等待
        不影响音频主循环；模型在等 function_call_output 期间不说话——所以
        提示词要求 AI **调用前**先对通话对方说「稍等，我确认一下」。
        超时=拒绝（fail-closed）：机主没看到 ≠ 同意。
        """
        stale = self._stale_effect_result("ask_owner", args)
        if stale is not None:
            return stale
        question = str(args.get("question") or "").strip()
        if not question:
            return {"success": False, "message": "question 不能为空"}
        question = question[:500]
        timeout = float(config.get_int("OWNER_CONFIRM_TIMEOUT_SECONDS"))
        confirm_id = uuid.uuid4().hex
        if self._hub is None:
            return {"success": False, "message": "确认通道不可用"}
        from .notification_events import publish_notification

        publish_notification("confirm_requested", confirm_id, question, timeout)
        self._hub.publish(
            {
                "type": "owner_confirm_request",
                "id": confirm_id,
                "question": question,
                "timeout_s": timeout,
            }
        )
        response = self._hub.wait_for_event(
            lambda e: (
                e.get("type") == "owner_confirm_response"
                and e.get("id") == confirm_id
            ),
            timeout=timeout,
        )
        if response is None:
            decision = "timeout"
        else:
            decision = (
                "approved"
                if response.get("choice") == "approve"
                else "declined"
            )
        # 关闭事件：无论怎么结束都发，UI 据此收卡（含另一个标签页里超时的卡）。
        publish_notification("confirm_closed", confirm_id)
        self._hub.publish(
            {
                "type": "owner_confirm_closed",
                "id": confirm_id,
                "decision": decision,
            }
        )
        # 审计不含 question 原文（可能带机主账户细节，同 WIL-95 §7 口径）。
        self._audit_tool(
            "ask_owner",
            args={"question_chars": len(question)},
            result={"success": True, "decision": decision},
        )
        messages = {
            "approved": "机主同意了",
            "declined": "机主不同意",
            "timeout": "机主暂时没回应，按不同意处理；请对方把方案记录在案",
        }
        return {
            "success": True,
            "decision": decision,
            "message": messages[decision],
        }

    def _query_code(self, args: dict) -> dict:
        """工具处理：从最近收到的短信里查验证码。"""
        if not getattr(self._modem, "supports_sms", True):
            return self._sms_unavailable("query_verification_code")
        stale = self._stale_effect_result("query_verification_code", args)
        if stale is not None:
            return stale
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

    def _wait_for_sms(self, args: dict) -> dict:
        """工具处理：阻塞等一条**新**短信（WIL-120 三期）。

        时间窗从调用时刻起算——不吃通话前的旧短信（query_verification_code
        的已知坑：会捞到历史验证码）。跑在工具线程，不阻塞音频主循环。
        """
        if not getattr(self._modem, "supports_sms", True):
            return self._sms_unavailable("wait_for_sms")
        stale = self._stale_effect_result("wait_for_sms", args)
        if stale is not None:
            return stale
        if self._hub is None:
            return {"success": False, "message": "短信通道不可用"}
        timeout = float(config.get_int("WAIT_SMS_TIMEOUT_SECONDS"))
        started = time.time()
        event = self._hub.wait_for_event(
            # 1s 余量：模组上报与工具调度之间的时钟毛边，不至于漏掉刚到的那条。
            lambda e: (
                e.get("type") == "sms_in"
                and isinstance(e.get("ts"), (int, float))
                and e["ts"] >= started - 1.0
            ),
            timeout=timeout,
        )
        if event is None:
            self._audit_tool(
                "wait_for_sms", args={}, result={"success": False, "hit": False}
            )
            return {
                "success": False,
                "message": f"等了 {timeout:g} 秒没有新短信到达",
            }
        text = str(event.get("text") or "")
        code_match = re.search(r"(?<!\d)(\d{4,8})(?!\d)", text)
        # 审计只记有无，不记短信内容/验证码值（WIL-95 §7 口径）。
        self._audit_tool(
            "wait_for_sms", args={}, result={"success": True, "hit": True}
        )
        return {
            "success": True,
            "sms_text": text,
            "sender": event.get("sender"),
            "code": code_match.group(1) if code_match else None,
            "message": "新短信已到达",
        }

    def _sms_unavailable(self, tool: str) -> dict:
        result = {"success": False, "code": "SMS_UNSUPPORTED",
                  "message": "蓝牙手机线路不支持短信或验证码读取；请在手机上查看，或通过 ask_owner 请求机主提供。"}
        self._audit_tool(tool, args={}, result=result)
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
