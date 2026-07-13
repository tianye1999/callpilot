"""Spoken DTMF fallback parsing and call-session orchestration."""

from __future__ import annotations

import json
import time

import pytest
from fakes import FakeAgent, FakeAudioBridge, FakeModem

from agentcall.call_agent import CallAgentService
from agentcall.dtmf_followup import extract_spoken_dtmf


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("我按1。", "1"),
        ("好的，我来按一", "1"),
        ("我帮您按幺零三#", "103#"),
        ("我帮你按103#进入下一步", "103#"),
        ("我按*9#", "*9#"),
        ("我按井号", "#"),
        ("我按星号", "*"),
    ],
)
def test_extract_spoken_dtmf_accepts_first_person_affirmative_statements(
    text, expected
):
    assert extract_spoken_dtmf(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "您按1就可以了",
        "请按1",
        "我按1吗？",
        "我按1吧",
        "我按1还是2",
        "我不按1",
        "我还没按1",
        "我按错了1",
        "如果需要，我按1",
        "系统提示请按1，我按1",
        "他说我按1就可以",
        "对方说‘我按1’",
        "我只是复述：我按1",
        "我按十",
        "我按A",
    ],
)
def test_extract_spoken_dtmf_rejects_questions_negation_conditions_and_quotes(text):
    assert extract_spoken_dtmf(text) is None


class _ExternalResultAgent(FakeAgent):
    def __init__(self, *, accept_external_result: bool = True) -> None:
        super().__init__()
        self.accept_external_result = accept_external_result
        self.external_results: list[tuple[str, dict, str]] = []

    async def external_tool_result(
        self, name: str, result: dict, *, source: str
    ) -> bool:
        self.external_results.append((name, result, source))
        return self.accept_external_result


class _SpyCallRecord:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def log_event(self, event_type: str, **fields) -> None:
        self.events.append((event_type, fields))

    def write_downlink(self, _pcm: bytes) -> None:
        pass

    def write_uplink(self, _pcm: bytes) -> None:
        pass

    def finish(self, **_kwargs) -> None:
        pass


def _wait_until(condition, *, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert condition()


def _start_profile_call(
    monkeypatch,
    tmp_path,
    *,
    followup: bool,
    accept_external_result: bool = True,
    delay: float = 0.05,
):
    profile_file = tmp_path / "number_profiles.json"
    profile_file.write_text(
        json.dumps(
            {
                "profiles": [
                    {
                        "number": "10086",
                        "scenario": "IVR hotline",
                        "opening_mode": "wait",
                        "dtmf_spoken_followup": followup,
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("NUMBER_PROFILES_ENABLED", "true")
    monkeypatch.setenv("NUMBER_PROFILES_FILE", str(profile_file))
    monkeypatch.setenv("PROMPT_GEN_ENABLED", "false")
    monkeypatch.setattr(
        "agentcall.call_agent.DTMF_SPOKEN_FOLLOWUP_DELAY_SECONDS", delay
    )

    modem = FakeModem()
    bridge = FakeAudioBridge()
    agent = _ExternalResultAgent(accept_external_result=accept_external_result)
    record = _SpyCallRecord()
    monkeypatch.setattr("agentcall.call_agent.create_audio_bridge", lambda **_kw: bridge)
    monkeypatch.setattr("agentcall.call_agent.create_agent", lambda _provider: agent)
    monkeypatch.setattr(
        "agentcall.call_agent.CallSession._begin_record",
        lambda _self, _direction, _number: record,
    )

    service = CallAgentService(
        modem_port="unused",
        audio_keyword="unused",
        provider="qwen",
        audio_mode="nmea",
        modem=modem,  # type: ignore[arg-type]
    )
    ok, error = service.dial("10086")
    assert ok, error
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and ("dial", ("10086",)) not in modem.calls:
        time.sleep(0.01)
    assert ("dial", ("10086",)) in modem.calls
    modem.trigger_call_connected("10086")
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and not agent.started:
        time.sleep(0.01)
    assert agent.started
    return service, modem, agent, record


def _stop_profile_call(service: CallAgentService) -> None:
    service.session.stop()
    assert service.session._thread is not None
    service.session._thread.join(timeout=3)


def _dtmf_calls(modem: FakeModem) -> list[tuple[str, tuple]]:
    return [call for call in modem.calls if call[0] == "send_dtmf"]


def test_spoken_followup_dispatches_multi_digit_once_and_injects_result(
    monkeypatch, tmp_path
):
    service, modem, agent, record = _start_profile_call(
        monkeypatch, tmp_path, followup=True
    )
    try:
        agent._emit_transcript("agent", "好的，我帮您按幺零三#")
        _wait_until(
            lambda: len(agent.external_results) == 1
            and any(kind == "dtmf_auto_followup" for kind, _fields in record.events)
        )

        assert _dtmf_calls(modem) == [("send_dtmf", ("103#",))]
        assert len(agent.external_results) == 1
        name, result, source = agent.external_results[0]
        assert name == "send_dtmf"
        assert result["success"] is True
        assert result["count"] == 4
        assert source == "spoken_followup"

        events = [fields for kind, fields in record.events if kind == "dtmf_auto_followup"]
        assert len(events) == 1
        assert events[0]["count"] == 4
        assert events[0]["mode"] == "qvts"
        assert events[0]["result"] == "success"
        assert events[0]["source"] == "agent_transcript"
        assert "digit" not in events[0] and "digits" not in events[0]
    finally:
        _stop_profile_call(service)


def test_spoken_followup_is_disabled_without_profile_opt_in(monkeypatch, tmp_path):
    service, modem, agent, _record = _start_profile_call(
        monkeypatch, tmp_path, followup=False
    )
    try:
        agent._emit_transcript("agent", "我按1")
        time.sleep(0.15)
        assert _dtmf_calls(modem) == []
        assert agent.external_results == []
    finally:
        _stop_profile_call(service)


def test_real_tool_before_timer_cancels_spoken_followup(monkeypatch, tmp_path):
    service, modem, agent, _record = _start_profile_call(
        monkeypatch, tmp_path, followup=True, delay=0.15
    )
    try:
        agent._emit_transcript("agent", "我按1")
        assert agent._tools is not None
        result = agent._tools.dispatch("send_dtmf", {"digits": "1"})
        assert result["success"] is True
        time.sleep(0.25)
        assert _dtmf_calls(modem) == [("send_dtmf", ("1",))]
        assert agent.external_results == []
    finally:
        _stop_profile_call(service)


def test_real_tool_before_transcript_prevents_spoken_followup(monkeypatch, tmp_path):
    service, modem, agent, _record = _start_profile_call(
        monkeypatch, tmp_path, followup=True, delay=0.1
    )
    try:
        assert agent._tools is not None
        result = agent._tools.dispatch("send_dtmf", {"digits": "1"})
        assert result["success"] is True
        agent._emit_transcript("agent", "我按1")
        time.sleep(0.2)
        assert _dtmf_calls(modem) == [("send_dtmf", ("1",))]
        assert agent.external_results == []
    finally:
        _stop_profile_call(service)


def test_real_tool_after_auto_followup_is_deduplicated(monkeypatch, tmp_path):
    service, modem, agent, _record = _start_profile_call(
        monkeypatch, tmp_path, followup=True
    )
    try:
        agent._emit_transcript("agent", "我按1")
        _wait_until(lambda: len(_dtmf_calls(modem)) == 1)
        assert agent._tools is not None
        result = agent._tools.dispatch("send_dtmf", {"digits": "1"})
        assert result["success"] is True
        assert _dtmf_calls(modem) == [("send_dtmf", ("1",))]
    finally:
        _stop_profile_call(service)


def test_two_genuine_tool_calls_are_not_deduplicated(monkeypatch, tmp_path):
    service, modem, agent, _record = _start_profile_call(
        monkeypatch, tmp_path, followup=True
    )
    try:
        assert agent._tools is not None
        first = agent._tools.dispatch("send_dtmf", {"digits": "1"})
        second = agent._tools.dispatch("send_dtmf", {"digits": "1"})
        assert first["success"] is True and second["success"] is True
        assert _dtmf_calls(modem) == [
            ("send_dtmf", ("1",)),
            ("send_dtmf", ("1",)),
        ]
    finally:
        _stop_profile_call(service)


def test_external_result_rejection_does_not_repeat_or_kill_call(monkeypatch, tmp_path):
    service, modem, agent, _record = _start_profile_call(
        monkeypatch,
        tmp_path,
        followup=True,
        accept_external_result=False,
    )
    try:
        agent._emit_transcript("agent", "我按1")
        _wait_until(lambda: len(agent.external_results) == 1)
        assert _dtmf_calls(modem) == [("send_dtmf", ("1",))]
        assert service.session.is_active
        assert len(agent.external_results) == 1
    finally:
        _stop_profile_call(service)


def test_pending_spoken_followup_is_cancelled_when_call_ends(monkeypatch, tmp_path):
    service, modem, agent, _record = _start_profile_call(
        monkeypatch, tmp_path, followup=True, delay=0.2
    )
    agent._emit_transcript("agent", "我按1")
    _stop_profile_call(service)
    time.sleep(0.3)
    assert _dtmf_calls(modem) == []
