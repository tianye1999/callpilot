"""发短信目标限制:只允许回复「已联系过的号码」。

安全护栏:防止 AI 被话术注入诱导给任意陌生号码群发短信,也防止无鉴权的
Web 接口被 CSRF 利用发信。允许的目标 = 收到过短信的号码 ∪ 所有来电方,
外加当前通话对端(通话中可回短信,此时对方可能还没进落盘记录)。

数据源都取自「落盘、重启存活」的记录:
- 收到过短信的号码:``EventHub.history()`` 里 ``type == "sms_in"`` 的 ``sender``。
  EventHub 启动时会从 ``messages.json`` 把历史短信载回 history,故跨重启存活。
- 所有来电方:``CallLogger.list_calls()`` 里 ``direction == "inbound"`` 的 ``number``,
  按通话目录落盘,跨重启存活。

号码只做 strip 比对,不改写国家码——宁可漏放行(可退回不发)也不误放行。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from .call_log import CallLogger
    from .events import EventHub

logger = logging.getLogger(__name__)

# 查询来电历史的上限:够覆盖实际联系人,又不至于每次发信都扫太多通话目录。
CALL_HISTORY_LIMIT = 1000


def _norm(number: str | None) -> str:
    return (number or "").strip()


def known_contact_numbers(
    hub: "EventHub | None",
    call_logger: "CallLogger | None",
) -> set[str]:
    """返回已联系过的号码集合:收到过短信的发件方 ∪ 所有来电方(inbound)。

    任一数据源读取失败只告警并跳过(宁可少放行),不影响另一源与整体判定。
    """
    numbers: set[str] = set()

    history: Callable[[], list] | None = getattr(hub, "history", None)
    if callable(history):
        try:
            for event in history():
                if isinstance(event, dict) and event.get("type") == "sms_in":
                    sender = _norm(event.get("sender"))
                    if sender:
                        numbers.add(sender)
        except Exception as exc:  # noqa: BLE001
            logger.warning("读取短信历史用于联系人判定失败: %s", exc)

    list_calls: Callable[..., list] | None = getattr(call_logger, "list_calls", None)
    if callable(list_calls):
        try:
            for entry in list_calls(limit=CALL_HISTORY_LIMIT):
                if isinstance(entry, dict) and entry.get("direction") == "inbound":
                    number = _norm(entry.get("number"))
                    if number:
                        numbers.add(number)
        except Exception as exc:  # noqa: BLE001
            logger.warning("读取来电历史用于联系人判定失败: %s", exc)

    return numbers


def is_reply_target_allowed(
    number: str,
    hub: "EventHub | None",
    call_logger: "CallLogger | None",
    *,
    extra_allowed: str | None = None,
) -> bool:
    """判断能否给 ``number`` 发短信。

    放行条件(满足其一):
    - 等于 ``extra_allowed``(当前通话对端,通话中可直接回短信);
    - 是收到过短信的号码,或任一来电方。

    空号码一律拒绝。
    """
    target = _norm(number)
    if not target:
        return False
    if extra_allowed and _norm(extra_allowed) == target:
        return True
    return target in known_contact_numbers(hub, call_logger)
