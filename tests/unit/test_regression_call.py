from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts import regression_call


def _event(type_: str, **fields: object) -> dict[str, object]:
    return {"type": type_, **fields}


def _base_events(*extra: dict[str, object]) -> list[dict[str, object]]:
    return [
        {"type": "call_started", "ts": 1.0},
        {"type": "prompt_gen", "source": "profile", "ts": 1.1},
        *extra,
        {"type": "ended", "status": "completed", "t_ms": 20_000, "ts": 21.0},
    ]


def _report(events: list[dict[str, object]]) -> regression_call.Report:
    transcripts = regression_call.extract_transcripts(events)
    return regression_call.run_assertions(events, transcripts)


def _write_events(call_dir, events: list[dict[str, object]]) -> None:
    call_dir.mkdir(parents=True)
    lines = [json.dumps(event, ensure_ascii=False) for event in events]
    (call_dir / "events.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _result(report: regression_call.Report, key: str) -> regression_call.CheckResult:
    matches = [result for result in report.results if result.key == key]
    assert len(matches) == 1
    return matches[0]


def test_all_good_events_pass():
    report = _report(
        _base_events(
            _event("transcript", role="agent", text="你好，我想查一下这个号码的流量使用情况。"),
            _event("transcript", role="user", text="您当前剩余 5GB。"),
            _event("transcript", role="agent", text="好的，确认当前剩余 5GB。"),
        )
    )

    assert not report.has_failures
    assert all(result.status == "PASS" for result in report.results)


def test_fabricated_gb_value_fails():
    report = _report(
        _base_events(
            _event("transcript", role="agent", text="你好，我想查一下这个号码的流量使用情况。"),
            _event("transcript", role="agent", text="您当前剩余 5GB。"),
        )
    )

    result = _result(report, "no_fabricated_values")
    assert result.status == "FAIL"
    assert "5GB" in result.detail


def test_impersonating_customer_service_fails():
    report = _report(
        _base_events(
            _event("transcript", role="agent", text="你好，我想查一下这个号码的流量使用情况。"),
            _event("transcript", role="agent", text="这里是电信客服，请问有什么可以帮您？"),
        )
    )

    result = _result(report, "no_institution_impersonation")
    assert result.status == "FAIL"
    assert "电信客服" in result.detail


def test_opening_digital_avatar_self_intro_fails():
    report = _report(
        _base_events(
            _event("transcript", role="agent", text="你好，我是数字分身，想查流量。"),
        )
    )

    result = _result(report, "opening_no_self_intro")
    assert result.status == "FAIL"
    assert "数字分身" in result.detail


def test_same_agent_sentence_repeated_three_times_fails():
    repeated = "正在查询，请稍后。"
    report = _report(
        _base_events(
            _event("transcript", role="agent", text="你好，我想查一下这个号码的流量使用情况。"),
            _event("transcript", role="agent", text=repeated),
            _event("transcript", role="agent", text=repeated),
            _event("transcript", role="agent", text=repeated),
        )
    )

    result = _result(report, "no_repeat_stuck")
    assert result.status == "FAIL"


def test_short_courtesy_token_repeats_do_not_fail():
    """≤4 字礼貌短语（"您好"）多次出现属正常应答，不算复读卡死（真机假阳性标定）。"""
    report = _report(
        _base_events(
            _event("transcript", role="agent", text="你好，我想查一下这个号码的流量使用情况。"),
            _event("transcript", role="agent", text="您好"),
            _event("transcript", role="agent", text="您好"),
            _event("transcript", role="agent", text="您好"),
            _event("transcript", role="agent", text="好的"),
            _event("transcript", role="agent", text="好的"),
            _event("transcript", role="agent", text="好的"),
        )
    )

    result = _result(report, "no_repeat_stuck")
    assert result.status == "PASS"


def test_says_i_press_without_dtmf_warns_but_does_not_fail():
    report = _report(
        _base_events(
            _event("transcript", role="agent", text="你好，我想查一下这个号码的流量使用情况。"),
            _event("transcript", role="agent", text="我按1进入流量查询。"),
        )
    )

    result = _result(report, "dtmf_audit")
    assert result.status == "WARN"
    assert not report.has_failures


def test_dtmf_waits_for_next_remote_transcript():
    events = _base_events(
        _event("transcript", role="agent", text="你好，我想查流量。", ts=2.0),
        _event("dtmf", mode="inband", result="success", ts=5.0),
        _event("transcript", role="user", text="正在为您连接。", ts=7.4),
    )

    result = _result(_report(events), "dtmf_followup")

    assert result.status == "PASS"
    assert "+2.4s" in result.detail
    assert "正在为您连接" in result.detail


def test_legacy_dtmf_without_result_is_still_observed():
    events = _base_events(
        _event("transcript", role="agent", text="你好，我想查流量。", ts=2.0),
        _event("dtmf", mode="inband", ts=5.0),
        _event("transcript", role="user", text="正在为您连接。", ts=9.1),
    )

    result = _result(_report(events), "dtmf_followup")

    assert result.status == "PASS"
    assert "+4.1s" in result.detail


def test_agent_speaking_after_dtmf_before_remote_transcript_fails():
    events = _base_events(
        _event("transcript", role="agent", text="你好，我想查流量。", ts=2.0),
        _event("dtmf", mode="inband", result="success", ts=5.0),
        _event("transcript", role="user", text="请按一。", ts=5.1),
        _event("transcript", role="agent", text="好的，我已经按一了。", ts=5.6),
        _event("transcript", role="user", text="正在为您连接。", ts=7.0),
    )

    result = _result(_report(events), "dtmf_followup")

    assert result.status == "FAIL"
    assert "0.6s" in result.detail
    assert "已经按一" in result.detail


def test_dtmf_without_remote_transcript_in_observation_window_warns():
    events = _base_events(
        _event("transcript", role="agent", text="你好，我想查流量。", ts=2.0),
        _event("dtmf", mode="inband", result="success", ts=5.0),
        _event("transcript", role="user", text="稍后才出现的播报。", ts=14.1),
    )

    result = _result(_report(events), "dtmf_followup")

    assert result.status == "WARN"
    assert "8s" in result.detail
    assert "9.1s" in result.detail


def test_dtmf_judge_shadow_reports_exact_match_without_exposing_digits():
    events = _base_events(
        _event(
            "dtmf_judge",
            decision_id="decision-a",
            action="press",
            confidence=0.9,
            reason_code="menu_matched",
            latency_ms=12.0,
            digits_len=1,
            window_mode="merged",
            ts=5.0,
        ),
        _event(
            "dtmf_action",
            action_id="action-a",
            source="realtime",
            digits_len=1,
            ts=5.5,
        ),
    )
    private = [
        {
            "kind": "decision",
            "decision_id": "decision-a",
            "action": "press",
            "digits": "7",
            "confidence": 0.9,
            "reason_code": "menu_matched",
            "reason": "明确菜单",
            "latency_ms": 12.0,
            "ts": 5.0,
            "window_mode": "merged",
        },
        {
            "kind": "action",
            "action_id": "action-a",
            "source": "realtime",
            "digits": "7",
            "digits_len": 1,
            "ts": 5.5,
            "t_ms": 500.0,
        },
    ]

    result = regression_call.check_dtmf_judge_shadow(events, private)

    assert result.status == "PASS"
    assert "exact=1" in result.detail
    assert "7" not in result.detail


def test_dtmf_judge_shadow_disagreement_is_warning_not_gate_failure():
    events = _base_events(
        _event(
            "dtmf_judge",
            decision_id="decision-a",
            action="press",
            confidence=0.9,
            reason_code="menu_matched",
            latency_ms=12.0,
            digits_len=1,
            window_mode="fragmented",
            ts=5.0,
        )
    )
    private = [
        {
            "kind": "decision",
            "decision_id": "decision-a",
            "action": "press",
            "digits": "7",
            "confidence": 0.9,
            "reason_code": "menu_matched",
            "reason": "明确菜单",
            "latency_ms": 12.0,
            "ts": 5.0,
            "window_mode": "fragmented",
        }
    ]

    result = regression_call.check_dtmf_judge_shadow(events, private)

    assert result.status == "WARN"
    assert "no_action=1" in result.detail
    assert "7" not in result.detail


def test_dtmf_judge_shadow_wait_followed_by_press_is_not_false_pass():
    events = _base_events(
        _event(
            "dtmf_judge",
            decision_id="decision-a",
            action="wait",
            confidence=0.8,
            reason_code="menu_incomplete",
            latency_ms=12.0,
            digits_len=0,
            window_mode="merged",
            ts=5.0,
        ),
        _event(
            "dtmf_action",
            action_id="action-a",
            source="realtime",
            digits_len=1,
            ts=5.5,
        ),
    )
    private = [
        {
            "kind": "decision",
            "decision_id": "decision-a",
            "action": "wait",
            "digits": None,
            "confidence": 0.8,
            "reason_code": "menu_incomplete",
            "reason": "菜单未完",
            "latency_ms": 12.0,
            "window_mode": "merged",
            "ts": 5.0,
        },
        {
            "kind": "action",
            "action_id": "action-a",
            "source": "realtime",
            "digits": "7",
            "digits_len": 1,
            "ts": 5.5,
            "t_ms": 500.0,
        },
    ]

    result = regression_call.check_dtmf_judge_shadow(events, private)

    assert result.status == "WARN"
    assert "unexpected_action=1" in result.detail
    assert "7" not in result.detail


@pytest.mark.parametrize(
    ("public_override", "private_override"),
    [
        ({"digits_len": 2}, {}),
        ({"action": "wait"}, {}),
        ({"window_mode": "banana"}, {}),
        ({}, {"window_mode": "banana"}),
    ],
)
def test_dtmf_judge_shadow_rejects_public_private_schema_mismatch(
    public_override, private_override
):
    public = {
        "type": "dtmf_judge",
        "decision_id": "decision-a",
        "action": "press",
        "confidence": 0.9,
        "reason_code": "menu_matched",
        "latency_ms": 12.0,
        "digits_len": 1,
        "window_mode": "merged",
        "ts": 5.0,
        **public_override,
    }
    private = {
        "kind": "decision",
        "decision_id": "decision-a",
        "action": "press",
        "digits": "7",
        "confidence": 0.9,
        "reason_code": "menu_matched",
        "reason": "明确菜单",
        "latency_ms": 12.0,
        "window_mode": "merged",
        "ts": 5.0,
        **private_override,
    }

    result = regression_call.check_dtmf_judge_shadow(
        _base_events(public), [private]
    )

    assert result.status == "WARN"
    assert "schema_errors=" in result.detail
    assert "7" not in result.detail


def test_dtmf_judge_shadow_errors_without_decisions_warn():
    events = _base_events(
        _event(
            "judge_error",
            code="timeout",
            latency_ms=3000.0,
            window_mode="fragmented",
            ts=5.0,
        )
    )

    result = regression_call.check_dtmf_judge_shadow(events, [])

    assert result.status == "WARN"
    assert "errors=1" in result.detail


def test_load_judge_shadow_is_optional_and_validates_jsonl(tmp_path):
    assert regression_call.load_judge_shadow(tmp_path) == []
    path = tmp_path / "judge_shadow.jsonl"
    path.write_text('{"kind":"decision","digits":"1"}\n', encoding="utf-8")
    assert regression_call.load_judge_shadow(tmp_path) == [
        {"kind": "decision", "digits": "1"}
    ]

    path.write_text("not-json\n", encoding="utf-8")
    with pytest.raises(regression_call.RegressionError):
        regression_call.load_judge_shadow(tmp_path)


def test_agent_repeats_user_supplied_value_passes_fabrication_check():
    report = _report(
        _base_events(
            _event("transcript", role="agent", text="你好，我想查一下这个号码的流量使用情况。"),
            _event("transcript", role="user", text="您当前剩余 5GB。"),
            _event("transcript", role="agent", text="好的，我确认剩余 5GB。"),
        )
    )

    assert _result(report, "no_fabricated_values").status == "PASS"
    assert not report.has_failures


def test_recording_arg_resolves_relative_path_and_reports_missing_dir(tmp_path, monkeypatch, capsys):
    call_dir = tmp_path / "recordings" / "call-1"
    _write_events(
        call_dir,
        _base_events(
            _event("transcript", role="agent", text="你好，我想查一下这个号码的流量使用情况。"),
        ),
    )
    monkeypatch.chdir(tmp_path)

    def fail_if_dialed(task: str) -> None:
        raise AssertionError(f"--recording should not dial, got task={task}")

    monkeypatch.setattr(regression_call, "dial_call", fail_if_dialed)

    assert regression_call.main(["--recording", "recordings/call-1"]) == 0
    assert str(call_dir.resolve()) in capsys.readouterr().out

    assert regression_call.main(["--recording", "recordings/missing"]) == 1
    missing_out = capsys.readouterr().out
    assert "录音目录不存在" in missing_out
    assert str((tmp_path / "recordings" / "missing").resolve()) in missing_out


def test_resolve_recordings_dir_priority(tmp_path, monkeypatch):
    """优先级：CLI > 服务 /api/meta > CALL_LOG_DIR > 打包目录 > 仓库目录（#15）。"""
    cli_dir = tmp_path / "cli"
    service_dir = tmp_path / "service"
    env_dir = tmp_path / "env"
    bundled = tmp_path / "bundled"

    monkeypatch.setattr(
        regression_call, "_service_recordings_dir", lambda api_base=None: service_dir
    )
    monkeypatch.setenv("CALL_LOG_DIR", str(env_dir))
    monkeypatch.setattr(regression_call, "BUNDLED_RECORDINGS_DIR", bundled)

    # CLI 显式最优先
    assert regression_call.resolve_recordings_dir(str(cli_dir)) == cli_dir
    # 服务 SSOT 次之
    assert regression_call.resolve_recordings_dir(None) == service_dir
    # 服务不可达 → env
    monkeypatch.setattr(
        regression_call, "_service_recordings_dir", lambda api_base=None: None
    )
    assert regression_call.resolve_recordings_dir(None) == env_dir
    # env 缺失 → 打包目录（存在时）
    monkeypatch.delenv("CALL_LOG_DIR")
    bundled.mkdir()
    assert regression_call.resolve_recordings_dir(None) == bundled
    # 打包目录不存在 → 仓库目录
    monkeypatch.setattr(regression_call, "BUNDLED_RECORDINGS_DIR", tmp_path / "absent")
    assert (
        regression_call.resolve_recordings_dir(None)
        == regression_call.REPO_RECORDINGS_DIR
    )


def test_wait_for_finished_recording_catches_call_finished_in_last_poll_gap(tmp_path, monkeypatch):
    """极短通话在最后一个 poll 间隙内结束时，超时兜底扫描仍应命中（#15）。"""
    recordings = tmp_path / "recordings"
    recordings.mkdir()
    call_dir = recordings / "20260710-000000-outbound-10000"

    ticks = iter([0.0, 100.0, 200.0])  # 首查即越过 deadline，直接走兜底扫描
    monkeypatch.setattr(regression_call.time, "monotonic", lambda: next(ticks, 300.0))
    monkeypatch.setattr(regression_call.time, "sleep", lambda _s: None)

    _write_events(
        call_dir,
        _base_events(
            _event("transcript", role="agent", text="你好。"),
        ),
    )

    result = regression_call.wait_for_finished_recording(recordings, set(), timeout_seconds=1)
    assert result.timed_out is False
    assert result.path == call_dir


def test_no_redundant_reask_warns_on_paraphrased_repeats():
    """#16 实测转写形态：同一诉求换措辞追问 ≥3 次 → WARN（非 FAIL）。"""
    report = _report(
        _base_events(
            _event("transcript", role="agent", text="你好，我想查一下这个号码的流量使用情况。"),
            _event("transcript", role="user", text="您当前号码尚未办理流量类套餐。"),
            _event("transcript", role="agent", text="您好，我想查一下这个号码的流量使用情况。"),
            _event("transcript", role="user", text="正在查询，请稍后。"),
            _event("transcript", role="agent", text="您好，我想查一下这个号码的流量使用情况。"),
            _event("transcript", role="user", text="您好。"),
            _event(
                "transcript",
                role="agent",
                text="您好，我想查一下这个号码的流量使用情况，麻烦您帮我看看。",
            ),
        )
    )
    result = _result(report, "no_redundant_reask")
    assert result.status == "WARN"
    assert "3 次" in result.detail
    assert not report.has_failures  # WARN 不算失败


def test_no_redundant_reask_passes_on_normal_dialog():
    report = _report(
        _base_events(
            _event("transcript", role="agent", text="你好，我想查一下这个号码的流量使用情况。"),
            _event("transcript", role="user", text="您当前号码尚未办理流量类套餐。"),
            _event("transcript", role="agent", text="好的，明白了，那就不打扰了，再见。"),
        )
    )
    assert _result(report, "no_redundant_reask").status == "PASS"
