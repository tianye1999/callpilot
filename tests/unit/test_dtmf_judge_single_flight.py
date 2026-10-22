"""判官的 single-flight 必须按实例隔离，不能跨通话互相顶掉。

回归 #72：`_MODEL_SINGLE_FLIGHT_THREAD` 是**模块全局**。两通并发通话时，
第二通的判官在每次重叠判定上直接拿到伪 `"timeout"` —— 它根本没调用模型，
却被记成超时。

这会污染 shadow 采集数据，而那正是 #45 按键统计的来源：一个用来测量
「按键有没有生效」的通道，自己先失真了。
"""

from __future__ import annotations

import threading

import pytest

from agentcall.dtmf_judge import DtmfActionLedger, DtmfJudge

pytestmark = pytest.mark.usefixtures("dtmf_judge_lifecycle")
BLOCKED_MODEL_SECONDS = 10.0


class SpyRecord:
    def log_event(self, event_type: str, **fields) -> None:
        pass


def make_judge(model_call) -> DtmfJudge:
    return DtmfJudge(
        record=SpyRecord(),  # type: ignore[arg-type]
        task_goal="查询话费",
        ledger=DtmfActionLedger(),
        model="stub-model",
        window_mode="remote_only",
        model_call=model_call,
        timeout_seconds=BLOCKED_MODEL_SECONDS,
    )


def test_two_concurrent_calls_do_not_time_each_other_out():
    """核心：两个判官实例并发判定，两边都必须真正调用到模型。

    旧实现下慢的那一个占住模块全局，另一个直接返回 ("timeout")——
    本测试断言 **没有任何一方拿到 timeout**。
    """
    started = threading.Event()
    release = threading.Event()

    def slow_call(messages, model, timeout):
        started.set()
        release.wait(BLOCKED_MODEL_SECONDS)
        return '{"action":"wait","reason_code":"ok"}', None

    def fast_call(messages, model, timeout):
        return '{"action":"wait","reason_code":"ok"}', None

    slow_judge = make_judge(slow_call)
    fast_judge = make_judge(fast_call)

    results: dict[str, tuple] = {}
    slow_thread = threading.Thread(
        target=lambda: results.__setitem__(
            "slow", slow_judge._invoke_model([{"role": "user", "content": "x"}])
        )
    )
    try:
        slow_thread.start()
        assert started.wait(5.0), "慢判官应已进入模型调用"

        # 第二通在第一通仍在飞行中时判定 —— 旧实现在这里给伪 timeout。
        results["fast"] = fast_judge._invoke_model(
            [{"role": "user", "content": "y"}]
        )
    finally:
        release.set()
        if slow_thread.ident is not None:
            slow_thread.join(5.0)
            assert not slow_thread.is_alive()

    assert results["fast"][1] != "timeout", (
        "并发通话被模块级 single-flight 顶成了伪 timeout"
    )
    assert results["fast"][0], "第二通的判官必须真的拿到模型输出"


def test_single_flight_still_applies_within_one_judge():
    """隔离不等于取消：同一个判官实例内仍要 single-flight，避免堆积模型调用。"""
    started = threading.Event()
    release = threading.Event()
    calls = []

    def slow_call(messages, model, timeout):
        calls.append(1)
        started.set()
        release.wait(BLOCKED_MODEL_SECONDS)
        return '{"action":"wait","reason_code":"ok"}', None

    judge = make_judge(slow_call)
    thread = threading.Thread(
        target=lambda: judge._invoke_model([{"role": "user", "content": "x"}])
    )
    try:
        thread.start()
        assert started.wait(5.0)

        # 同一实例的第二次判定必须被 single-flight 挡住。
        _raw, err = judge._invoke_model([{"role": "user", "content": "y"}])
        assert err == "timeout", "同实例内的重入应仍被 single-flight 拦下"
        assert len(calls) == 1, "被拦下的那次不应真的发起模型调用"
    finally:
        release.set()
        if thread.ident is not None:
            thread.join(5.0)
            assert not thread.is_alive()


def test_slot_is_released_so_the_next_turn_can_judge():
    """飞行结束后槽位要还回去，否则一通电话只判得了一次。"""
    judge = make_judge(lambda m, model, t: ('{"action":"wait","reason_code":"ok"}', None))

    for _ in range(3):
        raw, err = judge._invoke_model([{"role": "user", "content": "x"}])
        assert err != "timeout", "槽位没释放，后续判定全被顶掉"
        assert raw

    # Join actual cleanup instead of assuming a fixed sleep was long enough.
    model_thread = judge._single_flight_thread
    if model_thread is not None:
        model_thread.join(5.0)
    assert judge._single_flight_thread is None or not judge._single_flight_thread.is_alive()


def test_slot_exhaustion_is_not_labelled_as_a_model_timeout():
    """Codex P1：准入被拒 ≠ 模型太慢，两者必须分开记。

    槽位耗尽如果沿用 "timeout"，shadow 统计里就分不出「模型真的慢」与
    「我们自己拒绝了这次调用」—— 那正是 #72 的数据污染，只是阈值更高。
    """
    from agentcall import dtmf_judge as module

    release = threading.Event()
    entered = threading.Semaphore(0)

    def blocking(messages, model, timeout):
        entered.release()
        release.wait(BLOCKED_MODEL_SECONDS)
        return '{"action":"wait","reason_code":"ok"}', None

    # 占满所有全局槽位（每个判官实例内部各自 single-flight，故要多个实例）。
    judges = [make_judge(blocking) for _ in range(module._MAX_CONCURRENT_MODEL_CALLS)]
    threads = [
        threading.Thread(target=j._invoke_model, args=([{"role": "user", "content": "x"}],))
        for j in judges
    ]
    try:
        for thread in threads:
            thread.start()
        for _ in judges:
            assert entered.acquire(timeout=5.0), "所有槽位应已占满"

        overflow = make_judge(lambda m, model, t: ("unused", None))
        _raw, err = overflow._invoke_model([{"role": "user", "content": "y"}])
    finally:
        release.set()
        for thread in threads:
            if thread.ident is not None:
                thread.join(5.0)
                assert not thread.is_alive()

    assert err == "model_saturated", f"槽位耗尽应报 model_saturated，实际 {err!r}"
    assert err != "timeout"


def test_saturation_code_survives_sanitisation():
    """新错误码必须在白名单里，否则会被降级成 model_error 而丢失语义。"""
    from agentcall.dtmf_judge import _sanitize_error_code

    assert _sanitize_error_code("model_saturated") == "model_saturated"
