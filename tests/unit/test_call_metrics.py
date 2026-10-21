"""指标契约（WIL-95 第一期）：build_call_metrics 纯函数 + finish 落盘 + 汇总 CLI。

锁住的契约点：schema_version 常驻；测不了记 null + 原因、绝不记 0；隐私
（不含转写文本/按键值）；p90 最近秩；跨版本拒绝合并。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

from agentcall.call_log import CallLogger
from agentcall.call_metrics import SCHEMA_VERSION, build_call_metrics


def _load_summary_module():
    path = (
        Path(__file__).resolve().parent.parent.parent
        / "scripts"
        / "metrics_summary.py"
    )
    spec = importlib.util.spec_from_file_location("metrics_summary", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["metrics_summary"] = module
    spec.loader.exec_module(module)
    return module


def _snapshot(**overrides):
    event = {
        "type": "config_snapshot",
        "ts": 1.0,
        "provider": "openai",
        "model": "gpt-realtime-mini",
        "turn_detection": "semantic_vad",
        "vad_eagerness": "auto",
        "vad_silence_ms": 300,
        "barge_in_enabled": False,
        "hangover_seconds": 0.5,
        "dtmf_mode": "qvts",
        "recording_enabled": True,
        "audio_mode": "nmea",
        "modem_vendor": "simcom",
    }
    event.update(overrides)
    return event


def _build(events, **kwargs):
    defaults = dict(
        call_id="20260814-120000-outbound-611",
        direction="outbound",
        duration_s=42.0,
        answered=True,
        status="completed",
    )
    defaults.update(kwargs)
    return build_call_metrics(events, **defaults)


# ---- 契约基本面 ----


def test_schema_version_and_meta_always_present():
    metrics = _build([])
    assert metrics["schema_version"] == SCHEMA_VERSION
    assert metrics["direction"] == "outbound"
    assert metrics["answered"] is True
    assert metrics["status"] == "completed"


def test_latency_stats_use_nearest_rank_p90():
    events = [
        {"type": "latency", "ts": 1.0, "stage": "local_response", "ms": float(v)}
        for v in range(100, 1100, 100)  # 100..1000，10 个样本
    ]
    metrics = _build(events)
    stats = metrics["latency"]["local_response_latency_ms"]
    assert stats["n"] == 10
    assert stats["median"] == 550.0
    # 最近秩 p90 = 第 ceil(0.9*10)=9 个 = 900（int 截断会错取 1000）。
    assert stats["p90"] == 900.0
    assert stats["max"] == 1000.0
    assert stats["values"][0] == 100.0 and stats["values"][-1] == 1000.0


def test_missing_metrics_are_null_with_reason_never_zero():
    metrics = _build([_snapshot()])
    unavailable = metrics["unavailable"]
    # 三个时延 stage 无样本 → 各自记原因；latency 表里不出现（更不会是 0）。
    assert unavailable["local_response_latency_ms"] == "no_samples"
    assert metrics["latency"] == {}
    assert metrics["first_audio_ms"] is None
    assert unavailable["first_audio_ms"] == "no_samples"
    # 先天测不了/未埋点的常驻原因（WIL-95 2.1 / 分期）。
    assert unavailable["e2e_latency_ms"] == "carrier_legs_unobservable"
    # v3 起 takeover 时延可测：无接管的通记「没发生」而非「未埋点」。
    assert unavailable["takeover_latency_ms"] == "no_takeover_request"


def test_parallel_startup_playout_is_not_pooled_with_legacy_arrival_metrics():
    events = [
        {"type": "answered", "t_ms": 100},
        {"type": "agent_connect_started", "t_ms": 110},
        {"type": "agent_connected", "t_ms": 300},
        {"type": "greeting_requested", "t_ms": 310},
        {"type": "agent_audio_received", "t_ms": 400},
        {"type": "greeting_sent", "t_ms": 320},
        {"type": "pcm_ready", "t_ms": 600},
        {"type": "bridge_started", "t_ms": 650},
        {"type": "first_audio", "t_ms": 682, "ms": 372, "boundary": "pcm_written"},
    ]
    metrics = _build(events)
    assert metrics["first_playout_ms"] == 372
    assert metrics["answered_to_first_playout_ms"] == 582
    assert metrics["first_playout_boundary"] == "pcm_written"
    assert metrics["startup_marks_ms"]["agent_audio_received"] == 400
    assert metrics["startup_marks_ms"]["pcm_ready"] == 600
    assert metrics["first_audio_ms"] is None
    assert metrics["answered_to_first_audio_ms"] is None


def test_interruption_reason_forks_on_barge_in_flag():
    half_duplex = _build([_snapshot(barge_in_enabled=False)])
    assert half_duplex["unavailable"]["interruption_latency_ms"] == "half_duplex_mode"
    barge_in = _build([_snapshot(barge_in_enabled=True)])
    assert (
        barge_in["unavailable"]["interruption_latency_ms"] == "not_instrumented_yet"
    )


def test_module_greeting_metrics_are_separate_from_ai_first_audio_and_private():
    metrics = _build([
        {"type": "answered", "t_ms": 100},
        {"type": "module_greeting", "t_ms": 2200, "ok": True, "duration_ms": 1600,
         "started_after_answer_ms": 500, "text": "private-text"},
    ])
    assert metrics["module_greeting"] == {
        "ok": True, "duration_ms": 1600, "started_after_answer_ms": 500, "t_ms": 2200,
    }
    assert metrics["startup_marks_ms"]["module_greeting"] == 2200
    assert metrics["first_playout_ms"] is None and metrics["first_audio_ms"] is None
    assert "private-text" not in json.dumps(metrics)
    missing = _build([])
    assert missing["module_greeting"] is None
    assert missing["unavailable"]["module_greeting"] == "no_module_greeting_event"


def test_audio_derived_reason_forks_on_recording_flag():
    off = _build([_snapshot(recording_enabled=False)])
    assert off["unavailable"]["audio_derived_metrics"] == "recording_disabled"
    on = _build([_snapshot(recording_enabled=True)])
    assert on["unavailable"]["audio_derived_metrics"] == "offline_audit_wil89"


def test_missing_config_snapshot_is_flagged():
    metrics = _build([])
    assert metrics["config"] is None
    assert metrics["unavailable"]["config"] == "config_snapshot_event_missing"


def test_transcript_counts_but_text_never_leaks():
    events = [
        _snapshot(),
        {"type": "transcript", "ts": 2.0, "role": "agent", "text": "您好我是客服"},
        {"type": "transcript", "ts": 3.0, "role": "user", "text": "帮我查话费"},
        {"type": "transcript", "ts": 4.0, "role": "agent", "text": "好的"},
    ]
    metrics = _build(events)
    assert metrics["agent_turn_chars"]["n"] == 2
    assert metrics["peer_turns"] == 1
    # 隐私（WIL-95 §7）：metrics.json 里不允许出现任何转写原文。
    dumped = json.dumps(metrics, ensure_ascii=False)
    assert "您好我是客服" not in dumped
    assert "帮我查话费" not in dumped


def test_tool_dtmf_and_fallback_rollups():
    events = [
        {"type": "tool_call", "ts": 1.0, "tool": "send_sms", "args": {}, "result": {}},
        {"type": "tool_call", "ts": 2.0, "tool": "send_sms", "args": {}, "result": {}},
        {"type": "dtmf_action", "ts": 3.0, "action_id": "a1", "digits_len": 1},
        {"type": "dtmf_outcome", "ts": 4.0, "status": "observed"},
        {"type": "dtmf_outcome", "ts": 5.0, "status": "unobserved"},
        {"type": "barge_in_fallback", "ts": 6.0, "strikes": 3},
    ]
    metrics = _build(events)
    assert metrics["tool_calls"] == {"send_sms": 2}
    assert metrics["dtmf"] == {
        "actions": 1,
        "outcomes": {"observed": 1, "unobserved": 1},
    }
    assert metrics["barge_in_fallback"] is True


# ---- v2：终止归类 / 挂断时延 / 工具耗时 / 通话上下文 ----


def test_termination_defaults_to_peer_hangup():
    """没有任何本机主动信号且正常结束 → 排除法归为对端先挂。"""
    metrics = _build([_snapshot()])
    assert metrics["termination"] == {
        "kind": "peer_hangup",
        "reason": "inferred_no_local_signal",
    }


def test_termination_prefers_local_signals():
    hang = _build([
        {"type": "tool_call", "ts": 1.0, "tool": "hangup_call",
         "args": {}, "result": {"success": True}},
    ])
    assert hang["termination"] == {"kind": "agent_hangup", "reason": "hangup_tool"}

    wind = _build([{"type": "winddown", "ts": 2.0, "reason": "wrap_up_judge"}])
    assert wind["termination"] == {"kind": "agent_hangup", "reason": "wrap_up_judge"}

    took = _build([{"type": "takeover_committed", "ts": 3.0, "generation": 1}])
    assert took["termination"]["kind"] == "takeover"
    assert took["takeover"] == {"committed": 1}

    dead = _build([{"type": "dead_media_detected", "ts": 4.0,
                    "silent_seconds": 8.0, "hangup": True}])
    assert dead["termination"]["kind"] == "dead_media"

    err = _build([], status="failed")
    assert err["termination"] == {"kind": "error", "reason": "failed"}

    missed = _build([], status="not_connected")
    assert missed["termination"]["kind"] == "not_connected"


def test_hangup_latency_from_last_dialogue_activity():
    events = [
        {"type": "transcript", "ts": 100.0, "role": "user", "text": "好，再见"},
        {"type": "call_finished", "ts": 101.5, "status": "completed"},
    ]
    metrics = _build(events)
    assert metrics["hangup_latency_ms"] == 1500.0

    empty = _build([])
    assert empty["hangup_latency_ms"] is None
    assert empty["unavailable"]["hangup_latency_ms"] == "no_dialogue_activity"


def test_tool_latency_context_and_goal_rollup():
    events = [
        {"type": "latency", "ts": 1.0, "stage": "tool_call", "ms": 42.0,
         "tool": "send_sms"},
        {"type": "call_context", "ts": 2.0, "contact_known": True},
        {"type": "task_goal", "ts": 3.0, "goal": "确认周六 19:00 有无空位"},
    ]
    metrics = _build(events)
    assert metrics["latency"]["tool_call_latency_ms"]["n"] == 1
    assert metrics["contact_known"] is True
    assert metrics["has_task_goal"] is True
    # 任务目标原文不得进 metrics（隐私：目标可能含机主个人信息）。
    assert "确认周六" not in json.dumps(metrics, ensure_ascii=False)


# ---- v3 新增：接起→首字 / 接管时延 / 异常断线 / pcm 指纹 ----


def test_answered_to_first_audio_derivation():
    """接起→首字(到达) = (greeting_sent - answered 的 t_ms 差) + first_audio_ms。"""
    events = [
        {"type": "answered", "ts": 10.0, "t_ms": 1000.0},
        {"type": "greeting_sent", "ts": 10.4, "t_ms": 1400.0},
        {"type": "first_audio", "ts": 11.0, "ms": 600},
    ]
    metrics = _build(events)
    assert metrics["answered_to_first_audio_ms"] == 1000.0
    assert "answered_to_first_audio_ms" not in metrics["unavailable"]


def test_answered_to_first_audio_unavailable_reasons():
    # 无 answered 标记（远程链路的 answered 事件不带 t_ms）。
    remote = _build([
        {"type": "answered", "ts": 10.0, "source": "remote"},
        {"type": "first_audio", "ts": 11.0, "ms": 600},
    ])
    assert remote["answered_to_first_audio_ms"] is None
    assert remote["unavailable"]["answered_to_first_audio_ms"] == "no_answered_mark"
    # opening_mode=wait：不发开场白，无 greeting_sent。
    wait = _build([
        {"type": "answered", "ts": 10.0, "t_ms": 1000.0},
        {"type": "first_audio", "ts": 11.0, "ms": 600},
    ])
    assert wait["unavailable"]["answered_to_first_audio_ms"] == "greeting_not_sent"
    # 有开场白但整通没出过声。
    silent = _build([
        {"type": "answered", "ts": 10.0, "t_ms": 1000.0},
        {"type": "greeting_sent", "ts": 10.4, "t_ms": 1400.0},
    ])
    assert silent["unavailable"]["answered_to_first_audio_ms"] == "no_first_audio"


def test_takeover_latency_from_requested_to_committed():
    events = [
        {"type": "takeover_requested", "ts": 100.0, "trigger": "agent_tool"},
        {"type": "takeover_committed", "ts": 103.5, "generation": 1},
    ]
    metrics = _build(events)
    assert metrics["takeover_latency_ms"] == 3500.0
    assert metrics["termination"]["kind"] == "takeover"


def test_takeover_latency_unavailable_reasons():
    none = _build([])
    assert none["takeover_latency_ms"] is None
    assert none["unavailable"]["takeover_latency_ms"] == "no_takeover_request"
    # 请求了但没接通（超时/回滚）——绝不把「没发生」记成 0。
    pending = _build([
        {"type": "takeover_requested", "ts": 100.0},
        {"type": "takeover_rollback", "ts": 105.0, "reason": "offer_expired"},
    ])
    assert pending["takeover_latency_ms"] is None
    assert pending["unavailable"]["takeover_latency_ms"] == "takeover_not_committed"


def test_abnormal_drop_flags_dead_media_and_error():
    dead = _build([{"type": "dead_media_detected", "ts": 5.0, "hangup": True}])
    assert dead["abnormal_drop"] is True
    errored = _build([], status="failed")
    assert errored["abnormal_drop"] is True
    normal = _build([])
    assert normal["abnormal_drop"] is False


def test_pcm_enable_event_rollup():
    """CPCMREG 启用行为是无声通/劣化通的通级指纹（文档 D 组，蜂窝形态）。"""
    failed = _build([
        {"type": "pcm_enable", "ts": 5.0, "ok": False, "attempts": 6,
         "degraded": True},
    ])
    assert failed["pcm_enable_failed"] is True
    assert failed["pcm_degraded"] is True
    assert failed["pcm_enable_attempts"] == 6
    assert "pcm_enable" not in failed["unavailable"]

    healthy = _build([
        {"type": "pcm_enable", "ts": 5.0, "ok": True, "attempts": 1,
         "degraded": False},
    ])
    assert healthy["pcm_enable_failed"] is False
    assert healthy["pcm_degraded"] is False

    # 事件缺失（Quectel 路径/历史通话）：三态 None + 原因，不猜 False。
    absent = _build([])
    assert absent["pcm_enable_failed"] is None
    assert absent["unavailable"]["pcm_enable"] == "no_pcm_enable_event"


# ---- v4 新增：排队等待（hold） ----


def test_hold_seconds_aggregated_from_events():
    events = [
        {"type": "call_limits", "ts": 1.0, "outbound_max_seconds": 3600.0,
         "wrap_up_judge": True},
        {"type": "hold_started", "ts": 10.0},
        {"type": "hold_ended", "ts": 130.0, "seconds": 120.0},
        {"type": "hold_started", "ts": 200.0},
        {"type": "hold_ended", "ts": 230.0, "seconds": 30.0},
    ]
    metrics = _build(events)
    assert metrics["hold_seconds"] == 150.0
    assert metrics["hold_periods"] == 2
    assert "hold_seconds" not in metrics["unavailable"]


def test_hold_open_interval_closes_at_call_finished():
    """挂在排队里被硬时限收尾：未闭合区间按 call_finished 收口，不能丢。"""
    events = [
        {"type": "call_limits", "ts": 1.0, "wrap_up_judge": True},
        {"type": "hold_started", "ts": 100.0},
        {"type": "call_finished", "ts": 400.0, "status": "completed"},
    ]
    metrics = _build(events)
    assert metrics["hold_seconds"] == 300.0
    assert metrics["hold_periods"] == 1


def test_hold_zero_is_valid_only_when_detection_ran():
    # 裁判开着、没排过队：0 是有效读数。
    ran = _build([
        {"type": "call_limits", "ts": 1.0, "wrap_up_judge": True},
    ])
    assert ran["hold_seconds"] == 0.0
    # 裁判被 profile 关掉：检测不在场，记 null + 原因，绝不记 0。
    off = _build([
        {"type": "call_limits", "ts": 1.0, "wrap_up_judge": False},
    ])
    assert off["hold_seconds"] is None
    assert off["unavailable"]["hold_seconds"] == "hold_detection_disabled"
    # 老事件流（无 call_limits）：无从判断，同样 null。
    legacy = _build([])
    assert legacy["hold_seconds"] is None
    assert legacy["unavailable"]["hold_seconds"] == "no_call_limits_event"
    # 来电：hold 检测是外呼专属。
    inbound = _build([], direction="inbound")
    assert inbound["unavailable"]["hold_seconds"] == "outbound_only"


# ---- finish 落盘 ----


def test_finish_writes_metrics_json(tmp_path):
    clog = CallLogger(tmp_path, recording_enabled=False)
    record = clog.begin_call("outbound", "611")
    record.log_event(**{k: v for k, v in _snapshot().items() if k != "ts"})
    record.log_latency("local_response", 820.5)
    record.log_latency("playout_backlog", 0.0)
    record.log_event("first_audio", ms=1200)
    record.finish("completed")

    metrics = json.loads((record.path / "metrics.json").read_text(encoding="utf-8"))
    assert metrics["schema_version"] == SCHEMA_VERSION
    assert metrics["latency"]["local_response_latency_ms"]["n"] == 1
    # 0 是 playout_backlog 的有效读数（无积压），必须保留而不是当缺失。
    assert metrics["latency"]["playout_backlog_ms"]["median"] == 0.0
    assert metrics["first_audio_ms"] == 1200.0
    assert metrics["config"]["provider"] == "openai"


# ---- 汇总 CLI ----


def _write_call(root: Path, name: str, metrics: dict) -> None:
    call_dir = root / name
    call_dir.mkdir(parents=True)
    (call_dir / "metrics.json").write_text(
        json.dumps(metrics, ensure_ascii=False), encoding="utf-8"
    )


def test_summary_pools_values_within_version(tmp_path):
    summary = _load_summary_module()
    base = {
        "schema_version": SCHEMA_VERSION,
        "direction": "outbound",
        "config": {"provider": "openai", "turn_detection": "server_vad",
                   "barge_in_enabled": True},
        "first_audio_ms": 1000.0,
        "barge_in_fallback": False,
    }
    _write_call(tmp_path, "call-a", {
        **base,
        "latency": {"local_response_latency_ms": {
            "n": 2, "median": 200.0, "p90": 300.0, "max": 300.0,
            "values": [200.0, 300.0]}},
    })
    _write_call(tmp_path, "call-b", {
        **base,
        "latency": {"local_response_latency_ms": {
            "n": 2, "median": 500.0, "p90": 600.0, "max": 600.0,
            "values": [500.0, 600.0]}},
    })
    metrics, skipped = summary.collect(tmp_path)
    report = summary.summarize(metrics, skipped)

    stats = report["schema_versions"][str(SCHEMA_VERSION)]["directions"][
        "outbound"]["latency"]["local_response_latency_ms"]
    # 精确合并原始值 [200,300,500,600]，不是中位数的中位数。
    assert stats["n"] == 4
    assert stats["p50"] == 300.0
    assert stats["max"] == 600.0


def test_summary_refuses_cross_version_merge(tmp_path):
    summary = _load_summary_module()
    common = {"direction": "outbound", "config": {}, "first_audio_ms": None,
              "barge_in_fallback": False,
              "latency": {"local_response_latency_ms": {
                  "n": 1, "median": 100.0, "p90": 100.0, "max": 100.0,
                  "values": [100.0]}}}
    _write_call(tmp_path, "call-v1", {"schema_version": 1, **common})
    _write_call(tmp_path, "call-v2", {"schema_version": 2, **common})
    metrics, skipped = summary.collect(tmp_path)
    report = summary.summarize(metrics, skipped)

    versions = report["schema_versions"]
    assert set(versions) == {"1", "2"}, "不同版本必须分组，绝不合并"
    for vdata in versions.values():
        assert vdata["calls"] == 1


def test_summary_reports_skipped_calls(tmp_path):
    summary = _load_summary_module()
    (tmp_path / "call-without-metrics").mkdir()
    metrics, skipped = summary.collect(tmp_path)
    report = summary.summarize(metrics, skipped)
    assert report["calls_skipped"] == 1
    assert report["skipped_ids"] == ["call-without-metrics"]
