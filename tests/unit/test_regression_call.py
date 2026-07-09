from __future__ import annotations

import json

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
    assert repeated in result.detail


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
