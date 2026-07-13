"""CallSession wiring for the provider-independent DTMF shadow judge."""

from __future__ import annotations

from pathlib import Path

import pytest
from fakes import FakeAgent, FakeModem

from agentcall.call_agent import CallSession


class SpyRecord:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.mkdir(parents=True)
        self.events: list[tuple[str, dict]] = []

    def log_event(self, event_type: str, **fields) -> None:
        self.events.append((event_type, fields))


class FakeJudge:
    def __init__(self) -> None:
        self.submitted: list[tuple[str, float]] = []
        self.stopped = False
        self.actions = []

    def submit_remote_transcript(self, text: str, *, t_ms: float) -> None:
        self.submitted.append((text, t_ms))

    def stop(self, *, join_timeout: float = 0.2) -> None:
        self.stopped = True

    def record_action(self, entry) -> None:
        self.actions.append(entry)


def make_session(*, provider: str = "qwen") -> CallSession:
    return CallSession(
        modem=FakeModem(),  # type: ignore[arg-type]
        audio_keyword="unused",
        provider=provider,
        audio_mode="uac",
        pcm_port=None,
        pcm_baudrate=921600,
        tx_gain=1.0,
    )


def test_judge_off_creates_no_worker(tmp_path, monkeypatch):
    monkeypatch.setenv("DTMF_JUDGE_MODE", "off")
    session = make_session()
    record = SpyRecord(tmp_path / "call")

    session._start_dtmf_judge(record, session_t0=10.0)

    assert session._dtmf_judge is None


def test_shadow_start_failure_does_not_fail_call_session(tmp_path, monkeypatch):
    class BrokenJudge:
        def __init__(self, **kwargs) -> None:
            raise RuntimeError("model setup leaked menu text")

    monkeypatch.setenv("DTMF_JUDGE_MODE", "shadow")
    monkeypatch.setattr("agentcall.call_agent.DtmfJudge", BrokenJudge)
    session = make_session()
    record = SpyRecord(tmp_path / "call")

    session._start_dtmf_judge(record, session_t0=10.0)

    assert session._dtmf_judge is None
    assert record.events == [
        (
            "judge_error",
            {"code": "startup_error", "latency_ms": 0.0, "window_mode": "fragmented"},
        )
    ]
    assert "model setup leaked menu text" not in repr(record.events)


def test_shadow_uses_model_fallback_and_mrc_window_mode(tmp_path, monkeypatch):
    captured: dict[str, object] = {}

    class CapturingJudge:
        def __init__(self, **kwargs) -> None:
            captured.update(kwargs)

        def start(self) -> None:
            captured["started"] = True

        def stop(self, *, join_timeout: float = 0.2) -> None:
            captured["stopped"] = True

    monkeypatch.setenv("DTMF_JUDGE_MODE", "shadow")
    monkeypatch.setenv("DTMF_JUDGE_MODEL", "")
    monkeypatch.setenv("PROMPT_GEN_MODEL", "qwen-custom")
    monkeypatch.setenv("MANUAL_RESPONSE_CONTROL", "true")
    monkeypatch.setattr("agentcall.call_agent.DtmfJudge", CapturingJudge)
    session = make_session(provider="openai")
    session._outbound_number = "10000"
    session._outbound_task_value = "查询套餐余量"
    record = SpyRecord(tmp_path / "call")

    session._start_dtmf_judge(record, session_t0=10.0)

    assert captured["record"] is record
    assert captured["task_goal"] == "查询套餐余量"
    assert captured["model"] == "qwen-custom"
    assert captured["window_mode"] == "merged"
    assert captured["ledger"] is session._dtmf_ledger
    assert captured["started"] is True


@pytest.mark.parametrize("provider", ["qwen", "openai", "local"])
def test_remote_transcripts_feed_same_shadow_hook_for_all_providers(
    provider, tmp_path
):
    session = make_session(provider=provider)
    session._active = True
    session._session_generation = 7
    session._dtmf_judge_started_at = 100.0
    judge = FakeJudge()
    session._dtmf_judge = judge  # type: ignore[assignment]
    record = SpyRecord(tmp_path / provider)
    transcripts: list[tuple[str, str]] = []
    handler = session._make_transcript_handler(record, transcripts, FakeAgent())

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr("agentcall.call_agent.time.monotonic", lambda: 100.25)
        handler("user", "请按一查询")
        handler("agent", "我先听菜单")

    assert judge.submitted == [("请按一查询", 250.0)]


def test_session_stop_invalidates_judge_worker():
    session = make_session()
    judge = FakeJudge()
    session._dtmf_judge = judge  # type: ignore[assignment]
    session._active = True

    session.stop()

    assert judge.stopped is True
    assert session._dtmf_judge is None


def test_sent_dtmf_records_redacted_action_ledger_for_three_sources(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("DTMF_MODE", "qvts")
    session = make_session()
    session._record = SpyRecord(tmp_path / "call")  # type: ignore[assignment]

    for digits, source in (
        ("1", "agent_tool"),
        ("2", "spoken_followup"),
        ("3", "judge"),
    ):
        ok, _mode = session._send_dtmf_raw(digits, source=source)
        assert ok

    entries = session._dtmf_ledger.recent(3)
    assert [entry.source for entry in entries] == ["realtime", "guard", "judge"]
    events = [fields for kind, fields in session._record.events if kind == "dtmf_action"]
    assert [event["source"] for event in events] == ["realtime", "guard", "judge"]
    assert [event["digits_len"] for event in events] == [1, 1, 1]
    assert all("digits" not in event and "hash" not in repr(event).lower() for event in events)
    assert "1" not in {event.get("digits") for event in events}


def test_sent_dtmf_notifies_shadow_private_ledger(tmp_path, monkeypatch):
    monkeypatch.setenv("DTMF_MODE", "qvts")
    session = make_session()
    session._record = SpyRecord(tmp_path / "call")  # type: ignore[assignment]
    judge = FakeJudge()
    session._dtmf_judge = judge  # type: ignore[assignment]

    ok, _mode = session._send_dtmf_raw("1", source="agent_tool")

    assert ok
    assert len(judge.actions) == 1
    assert judge.actions[0].source == "realtime"
    assert judge.actions[0].digits == "1"


def test_shadow_judge_has_no_dispatch_dependency(tmp_path):
    """The Phase-S component cannot press a key because no tools enter its API."""
    from agentcall.dtmf_judge import DtmfActionLedger, DtmfJudge

    record = SpyRecord(tmp_path / "call")
    judge = DtmfJudge(
        record=record,
        task_goal="查询套餐余量",
        ledger=DtmfActionLedger(),
        model="qwen-plus",
        window_mode="fragmented",
        model_call=lambda messages, model, timeout: (
            '{"action":"press","digits":"1","confidence":1.0,'
            '"reason_code":"menu_matched","reason":"明确菜单"}',
            None,
        ),
        throttle_seconds=0.01,
    )

    assert not hasattr(judge, "tools")
    assert not hasattr(judge, "dispatch")
