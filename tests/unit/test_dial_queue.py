"""DialQueue / number_allowed 单测（fake dial_fn 驱动，无硬件）。"""

from __future__ import annotations

import os
import time

from agentcall.dial_queue import DialQueue, number_allowed

# 快间隔，让顺序调度测试不拖慢整体用例。
FAST = 0.02


def wait_until(cond, timeout: float = 2.0) -> bool:
    """轮询等待条件成立（拨号在 Timer 线程里异步发生）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.005)
    return cond()


class FakeDial:
    """记录调用顺序与调用时 env 的 dial_fn 替身。"""

    def __init__(self, results: dict[str, tuple[bool, str | None]] | None = None):
        self.calls: list[str] = []
        self.env_at_call: list[str | None] = []
        self.results = dict(results or {})

    def __call__(self, number: str) -> tuple[bool, str | None]:
        self.calls.append(number)
        self.env_at_call.append(os.environ.get("AGENT_OUTBOUND_TASK"))
        return self.results.get(number, (True, None))


# ---- 白名单 ----

def test_empty_whitelist_allows_all():
    assert number_allowed("13800000000", ())
    assert number_allowed("10086", ())


def test_whitelist_exact_match():
    wl = ("13800000000", "10086")
    assert number_allowed("10086", wl)
    assert not number_allowed("13900000000", wl)


def test_whitelist_prefix_wildcard():
    wl = ("138*",)
    assert number_allowed("13800000000", wl)
    assert number_allowed("13811112222", wl)
    assert not number_allowed("13900000000", wl)


def test_enqueue_rejects_by_whitelist():
    dial = FakeDial()
    queue = DialQueue(dial, whitelist=("138*", "10086"), interval_seconds=FAST)
    result = queue.enqueue(["13800000000", "13900000000", "10086"])
    assert result["accepted"] == ["13800000000", "10086"]
    assert result["rejected"] == ["13900000000"]
    # 被拒号码不应被拨打
    assert wait_until(lambda: len(dial.calls) >= 1)
    assert "13900000000" not in queue.status()["pending"]


# ---- 顺序调度 ----

def test_sequential_dialing_order():
    dial = FakeDial()
    queue = DialQueue(dial, interval_seconds=FAST)

    result = queue.enqueue(["111", "222", "333"])
    assert result["accepted"] == ["111", "222", "333"]

    # 队列空闲 → 立即拨第一个
    assert wait_until(lambda: dial.calls == ["111"])
    status = queue.status()
    assert status["current"] == "111"
    assert status["pending"] == ["222", "333"]
    assert status["active"]

    # 通话结束 → interval 后拨下一个
    queue.on_session_ended()
    assert wait_until(lambda: dial.calls == ["111", "222"])
    queue.on_session_ended()
    assert wait_until(lambda: dial.calls == ["111", "222", "333"])
    queue.on_session_ended()

    assert wait_until(lambda: not queue.status()["active"])
    status = queue.status()
    assert status["pending"] == []
    assert status["current"] is None
    assert [d["number"] for d in status["done"]] == ["111", "222", "333"]
    assert all(d["ok"] for d in status["done"])


def test_enqueue_does_not_block_caller():
    started = time.monotonic()
    slow_dial = FakeDial()

    def slow(number: str) -> tuple[bool, str | None]:
        time.sleep(0.3)
        return slow_dial(number)

    queue = DialQueue(slow, interval_seconds=FAST)
    queue.enqueue(["111"])
    elapsed = time.monotonic() - started
    assert elapsed < 0.2  # 拨号在后台线程，enqueue 立即返回
    assert wait_until(lambda: slow_dial.calls == ["111"])


# ---- 失败继续 ----

def test_dial_failure_records_and_continues():
    dial = FakeDial(results={"222": (False, "占线")})
    queue = DialQueue(dial, interval_seconds=FAST)
    queue.enqueue(["111", "222", "333"])

    assert wait_until(lambda: dial.calls == ["111"])
    queue.on_session_ended()
    # 222 失败 → 不等 on_session_ended，立即拨 333
    assert wait_until(lambda: dial.calls == ["111", "222", "333"])

    status = queue.status()
    assert status["current"] == "333"
    failed = [d for d in status["done"] if d["number"] == "222"]
    assert failed == [{"number": "222", "ok": False, "error": "占线"}]


def test_dial_exception_treated_as_failure():
    dial = FakeDial()

    def flaky(number: str) -> tuple[bool, str | None]:
        if number == "111":
            raise RuntimeError("模组串口断开")
        return dial(number)

    queue = DialQueue(flaky, interval_seconds=FAST)
    queue.enqueue(["111", "222"])
    assert wait_until(lambda: dial.calls == ["222"])
    done = queue.status()["done"]
    assert done[0]["number"] == "111"
    assert done[0]["ok"] is False
    assert "模组串口断开" in done[0]["error"]


# ---- cancel ----

def test_cancel_clears_pending_and_stops_scheduling():
    dial = FakeDial()
    queue = DialQueue(dial, interval_seconds=FAST)
    queue.enqueue(["111", "222", "333"])
    assert wait_until(lambda: dial.calls == ["111"])

    removed = queue.cancel()
    assert removed == 2
    assert queue.status()["pending"] == []
    # 当前通话不受影响，结束后也不再拨新号码
    assert queue.status()["current"] == "111"
    queue.on_session_ended()
    time.sleep(FAST * 5)
    assert dial.calls == ["111"]
    assert not queue.status()["active"]


def test_cancel_on_empty_queue_returns_zero():
    queue = DialQueue(FakeDial(), interval_seconds=FAST)
    assert queue.cancel() == 0


# ---- enqueue 去重 / 空号 ----

def test_enqueue_dedup_and_empty_numbers():
    dial = FakeDial(results={"111": (False, "无人接听")})  # 让队列迅速空闲，避免占用 current
    queue = DialQueue(dial, interval_seconds=FAST)
    result = queue.enqueue(["111", "111", "", "   ", " 222 "])
    assert result["accepted"] == ["111", "222"]  # 去重 + strip
    assert result["rejected"] == ["111", "", "   "]

    # 与已在队列中的号码重复也拒绝
    dial2 = FakeDial()
    queue2 = DialQueue(dial2, interval_seconds=FAST)
    queue2.enqueue(["111", "222"])
    assert wait_until(lambda: dial2.calls == ["111"])
    again = queue2.enqueue(["111", "222", "333"])  # 111 是 current，222 在 pending
    assert again["accepted"] == ["333"]
    assert again["rejected"] == ["111", "222"]


# ---- task 环境变量 ----

def test_task_sets_env_before_each_dial(monkeypatch):
    monkeypatch.delenv("AGENT_OUTBOUND_TASK", raising=False)
    dial = FakeDial()
    queue = DialQueue(dial, interval_seconds=FAST)
    queue.enqueue(["111", "222"], task="提醒客户明天复诊")

    assert wait_until(lambda: dial.calls == ["111"])
    queue.on_session_ended()
    assert wait_until(lambda: dial.calls == ["111", "222"])
    assert dial.env_at_call == ["提醒客户明天复诊", "提醒客户明天复诊"]
    # 清理，避免污染其他测试
    monkeypatch.delenv("AGENT_OUTBOUND_TASK", raising=False)


def test_no_task_leaves_env_untouched(monkeypatch):
    monkeypatch.delenv("AGENT_OUTBOUND_TASK", raising=False)
    dial = FakeDial()
    queue = DialQueue(dial, interval_seconds=FAST)
    queue.enqueue(["111"])
    assert wait_until(lambda: dial.calls == ["111"])
    assert dial.env_at_call == [None]


# ---- from_env ----

def test_from_env_reads_whitelist_and_interval(monkeypatch):
    monkeypatch.setenv("DIAL_WHITELIST", "138*, 10086 ,")
    monkeypatch.setenv("DIAL_INTERVAL_SECONDS", "0.5")
    queue = DialQueue.from_env(FakeDial())
    assert queue._whitelist == ("138*", "10086")
    assert queue._interval == 0.5


def test_from_env_defaults(monkeypatch):
    monkeypatch.delenv("DIAL_WHITELIST", raising=False)
    monkeypatch.delenv("DIAL_INTERVAL_SECONDS", raising=False)
    queue = DialQueue.from_env(FakeDial())
    assert queue._whitelist == ()
    assert queue._interval == 5.0


def test_from_env_bad_interval_falls_back(monkeypatch):
    # 非法值由 config.get_float 回退注册表默认值。
    monkeypatch.setenv("DIAL_INTERVAL_SECONDS", "not-a-number")
    queue = DialQueue.from_env(FakeDial())
    assert queue._interval == 5.0


def test_from_env_negative_interval_falls_back(monkeypatch):
    monkeypatch.setenv("DIAL_INTERVAL_SECONDS", "-3")
    queue = DialQueue.from_env(FakeDial())
    assert queue._interval == 5.0


def test_from_env_nan_interval_falls_back(monkeypatch):
    """NaN 能通过 config 的 float() 校验，必须同负值一样回退默认。"""
    monkeypatch.setenv("DIAL_INTERVAL_SECONDS", "nan")
    queue = DialQueue.from_env(FakeDial())
    assert queue._interval == 5.0


# ---- 竞态回归：会话在 dial_fn 返回前极速结束（codex review P1） ----


def test_session_ended_during_dial_does_not_stall_queue():
    """dial_fn 返回 True 前 on_session_ended 已到达：队列必须继续拨下一个。"""
    import threading as _threading

    dialed: list[str] = []
    queue_ref: list = []
    second_dialed = _threading.Event()

    def dial_fn(number: str):
        dialed.append(number)
        if number == "111":
            # 模拟会话极速失败：dial 尚未返回，结束回调先到。
            queue_ref[0].on_session_ended()
        if number == "222":
            second_dialed.set()
        return True, None

    from agentcall.dial_queue import DialQueue

    q = DialQueue(dial_fn, interval_seconds=0.0)
    queue_ref.append(q)
    q.enqueue(["111", "222"])

    assert second_dialed.wait(timeout=5), f"队列卡死，只拨了 {dialed}"
    assert dialed == ["111", "222"]
