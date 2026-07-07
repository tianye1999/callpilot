"""批量外呼队列 + 白名单（roadmap P2-1 模块层）。

设计要点：
- ``DialQueue`` 只负责排队与调度，实际拨号委托给 ``dial_fn``
  （即 ``CallAgentService.dial`` 的签名：``dial_fn(number) -> tuple[bool, str | None]``）。
- 回调（``on_session_ended``）来自模组监听线程，enqueue 来自 web 线程，
  所有共享状态用一把锁保护；拨号动作在 ``threading.Timer`` 线程里执行，
  不阻塞任何调用方。
- 白名单为空 => 一律放行（产品决策：宽松，白名单只做可选过滤）；
  非空 => 精确匹配 或 前缀通配（如 ``"138*"``）。

环境变量（读取与默认值统一走 config 注册表，供 ``DialQueue.from_env`` 使用）：
- ``DIAL_WHITELIST``：逗号分隔的白名单，如 ``"13800000000,139*"``；默认空（放行全部）。
- ``DIAL_INTERVAL_SECONDS``：两通电话之间的间隔秒数；默认 5.0。
"""

from __future__ import annotations

import logging
import os
import threading
from collections import deque
from typing import Any, Callable

from . import config

logger = logging.getLogger(__name__)

# 环境变量名（与 config 注册表中的 key 一致）
ENV_WHITELIST = "DIAL_WHITELIST"
ENV_INTERVAL = "DIAL_INTERVAL_SECONDS"

DialFn = Callable[[str], "tuple[bool, str | None]"]


def number_allowed(number: str, whitelist: tuple[str, ...]) -> bool:
    """判断号码是否被白名单放行。

    - 白名单为空元组 => 一律放行；
    - 非空 => 任一条目精确匹配，或前缀通配条目（以 ``*`` 结尾，如 ``"138*"``）
      前缀匹配即放行；
    - 空白条目会被忽略（不参与匹配，但不改变「白名单非空」的判定）。
    """
    if not whitelist:
        return True
    number = (number or "").strip()
    for pattern in whitelist:
        pattern = (pattern or "").strip()
        if not pattern:
            continue
        if pattern.endswith("*"):
            if number.startswith(pattern[:-1]):
                return True
        elif number == pattern:
            return True
    return False


def whitelist_from_env() -> tuple[str, ...]:
    """从 ``DIAL_WHITELIST`` 读取白名单（逗号分隔），默认空元组。"""
    raw = config.get_str(ENV_WHITELIST)
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def interval_from_env() -> float:
    """从 ``DIAL_INTERVAL_SECONDS`` 读取拨号间隔；非法值由 config 回退默认，负值同样回退。"""
    value = config.get_float(ENV_INTERVAL)
    # 用 not (value >= 0) 而非 value < 0：float("nan") 能通过 config 校验，
    # 两种比较对 NaN 都为 False，前者才能把 NaN 也拦进回退分支
    # （NaN 传给 threading.Timer 会立即触发，拨号节流实质归零）。
    if not (value >= 0):
        default = float(config.get_spec(ENV_INTERVAL).default)
        logger.warning("%s=%s 非法（负数/NaN），使用默认 %s", ENV_INTERVAL, value, default)
        return default
    return value


class DialQueue:
    """线程安全的批量外呼队列。

    生命周期：``enqueue`` 入队（空闲则立即拨第一个）→ 拨号成功后等待
    ``on_session_ended`` 回调 → ``interval_seconds`` 秒后拨下一个；
    拨号失败（``dial_fn`` 返回 False 或抛异常）记录结果后立即拨下一个。

    ``cancel`` 只清空未拨的号码，不挂断进行中的通话（挂断由服务层负责）。
    """

    def __init__(
        self,
        dial_fn: DialFn,
        *,
        whitelist: tuple[str, ...] = (),
        interval_seconds: float = 5.0,
    ) -> None:
        self._dial_fn = dial_fn
        self._whitelist = whitelist
        self._interval = interval_seconds
        self._lock = threading.RLock()
        self._pending: deque[str] = deque()
        self._current: str | None = None
        self._done: list[dict[str, Any]] = []
        self._task: str | None = None
        self._timer: threading.Timer | None = None
        self._dialing = False  # 拨号工作线程正在运行（含 dial_fn 调用中）
        # 拨号在途时会话已结束的标记：会话可能在 dial_fn 返回前就极速失败
        # 并触发 on_session_ended，若不标记会把已结束号码写回 _current 卡死队列。
        self._ended_during_dial = False

    @classmethod
    def from_env(cls, dial_fn: DialFn) -> "DialQueue":
        """按环境变量构造：``DIAL_WHITELIST`` + ``DIAL_INTERVAL_SECONDS``。"""
        return cls(
            dial_fn,
            whitelist=whitelist_from_env(),
            interval_seconds=interval_from_env(),
        )

    # ---- 对外接口 ----

    def enqueue(self, numbers: list[str], task: str | None = None) -> dict:
        """批量入队。

        返回 ``{"accepted": [...], "rejected": [...]}``，二者合起来覆盖全部输入：
        - accepted：本次真正入队的号码（已 strip）；
        - rejected：空号/空白、白名单不放行、与队列中或本批次重复的号码（原样返回）。

        ``task`` 非空时记为本队列的外呼任务，每次拨号前写入
        ``AGENT_OUTBOUND_TASK`` 环境变量（现有 prompt 层从该 env 读取）。
        队列空闲时立即（异步）拨打第一个号码，不阻塞调用方。
        """
        accepted: list[str] = []
        rejected: list[str] = []
        with self._lock:
            if task:
                self._task = task
            seen: set[str] = set(self._pending)
            if self._current is not None:
                seen.add(self._current)
            for raw in numbers:
                number = (raw or "").strip()
                if not number:
                    rejected.append(raw)
                    continue
                if number in seen:
                    rejected.append(raw)
                    continue
                if not number_allowed(number, self._whitelist):
                    rejected.append(raw)
                    continue
                seen.add(number)
                accepted.append(number)
                self._pending.append(number)
            if accepted and self._idle_locked():
                self._schedule_next_locked(0.0)
        if rejected:
            logger.info("外呼入队：接受 %d 个，拒绝 %d 个", len(accepted), len(rejected))
        return {"accepted": accepted, "rejected": rejected}

    def on_session_ended(self) -> None:
        """通话结束回调（任意线程）：清除当前号码，interval 秒后拨下一个。

        与本队列无关的会话结束（队列为空时）是无害的 no-op。
        """
        with self._lock:
            if self._dialing:
                # 拨号在途（dial_fn 尚未返回）：只做标记并清预占，
                # 后续调度由 _dial_next 消费标记后负责，避免双重调度。
                self._ended_during_dial = True
                self._current = None
                return
            self._current = None
            if self._pending and self._idle_locked():
                self._schedule_next_locked(self._interval)

    def status(self) -> dict:
        """队列快照：``{"pending", "current", "done", "active"}``。

        - done：每次拨号尝试的结果 ``{"number", "ok", "error"}``（含成功的）；
        - active：有通话进行中、有待拨号码、或调度/拨号在途。
        """
        with self._lock:
            return {
                "pending": list(self._pending),
                "current": self._current,
                "done": [dict(entry) for entry in self._done],
                "active": (
                    self._current is not None
                    or bool(self._pending)
                    or self._timer is not None
                    or self._dialing
                ),
            }

    def cancel(self) -> int:
        """清空待拨队列并取消已排定的下一次拨号，返回移除的号码数。

        不影响进行中的通话；已进入 ``dial_fn`` 的那一次拨号无法追回。
        """
        with self._lock:
            removed = len(self._pending)
            self._pending.clear()
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
            if removed:
                logger.info("外呼队列已取消，移除 %d 个待拨号码", removed)
            return removed

    # ---- 内部实现 ----

    def _idle_locked(self) -> bool:
        """是否空闲（无通话、无排定的 Timer、无拨号线程在跑）。需持锁调用。"""
        return self._current is None and self._timer is None and not self._dialing

    def _schedule_next_locked(self, delay: float) -> None:
        """排定 ``delay`` 秒后拨下一个号码。需持锁调用。"""
        timer = threading.Timer(delay, self._dial_next)
        timer.daemon = True
        timer.name = "dial-queue-timer"
        self._timer = timer
        timer.start()

    def _dial_next(self) -> None:
        """Timer 线程入口：依次拨号，失败立即继续，成功则等 on_session_ended。"""
        with self._lock:
            self._timer = None
            self._dialing = True
        try:
            while True:
                with self._lock:
                    if not self._pending:
                        return
                    number = self._pending.popleft()
                    task = self._task
                    # 预占 current：dial_fn 返回前会话就可能极速结束并触发
                    # on_session_ended，预占让该回调有东西可清、标记可留。
                    self._current = number
                    self._ended_during_dial = False
                if task:
                    os.environ["AGENT_OUTBOUND_TASK"] = task
                try:
                    ok, error = self._dial_fn(number)
                except Exception as exc:  # noqa: BLE001
                    logger.exception("拨号 %s 时 dial_fn 抛出异常", number)
                    ok, error = False, f"dial 异常: {exc}"
                with self._lock:
                    self._done.append({"number": number, "ok": bool(ok), "error": error})
                    if ok and not self._ended_during_dial:
                        logger.info("外呼已发起: %s", number)
                        return
                    # 拨号失败，或会话在拨号返回前已结束：清预占继续下一个。
                    self._current = None
                if ok:
                    logger.info("外呼 %s 会话已极速结束，继续下一个", number)
                else:
                    logger.warning("外呼 %s 失败（%s），继续下一个", number, error)
        finally:
            with self._lock:
                self._dialing = False
                # 竞态兜底：拨号线程收尾期间有新号码入队且无人调度时补一枪。
                if self._pending and self._idle_locked():
                    self._schedule_next_locked(0.0)
