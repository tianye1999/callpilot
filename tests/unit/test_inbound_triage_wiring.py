from __future__ import annotations

import asyncio

from fakes import FakeAgent, FakeAudioBridge, FakeModem

from agentcall.call_agent import CallAgentService
from agentcall.takeover_coordinator import TakeoverState
from agentcall.triage_judge import TriageVerdict


def _service() -> CallAgentService:
    service = CallAgentService(
        modem_port="unused",
        audio_keyword="unused",
        provider="openai",
        modem=FakeModem(),  # type: ignore[arg-type]
    )
    session = service.session
    session._active = True
    session._outbound_number = None
    session._session_generation = 4
    session._initialize_takeover_context("inbound")
    session._triage_mode = "enforce"
    return service


def _verdict(*, action: str, turn_id: int, confidence: float, category: str):
    return TriageVerdict(
        category=category,  # type: ignore[arg-type]
        action=action,  # type: ignore[arg-type]
        confidence=confidence,
        reason_code="test_reason",
        turn_id=turn_id,
        call_generation=4,
    )


def test_transfer_verdict_calls_orchestrator_not_realtime_tool(monkeypatch) -> None:
    monkeypatch.setenv("INBOUND_TAKEOVER_ENABLED", "true")
    service = _service()
    session = service.session
    session._triage_results.put_nowait(
        _verdict(
            action="transfer", turn_id=1, confidence=0.7, category="personal"
        )
    )

    outcome = asyncio.run(
        session._consume_triage_results(FakeAgent(), FakeAudioBridge(), 4)
    )

    assert outcome == "transfer"
    assert session.takeover_state is TakeoverState.TAKEOVER_PREPARING
    assert service.next_inbound_takeover_offer() is not None
    assert session._triage_terminal is True


def test_reject_needs_two_turns_then_uses_fixed_line_and_bounded_timer() -> None:
    service = _service()
    session = service.session
    agent = FakeAgent()
    bridge = FakeAudioBridge()
    session._triage_results.put_nowait(
        _verdict(
            action="reject", turn_id=1, confidence=0.9, category="marketing"
        )
    )
    assert asyncio.run(session._consume_triage_results(agent, bridge, 4)) is None
    assert "具体事情找本人" in agent.said[-1]
    assert session._triage_terminal is False

    session._triage_results.put_nowait(
        _verdict(
            action="reject", turn_id=2, confidence=0.9, category="marketing"
        )
    )
    outcome = asyncio.run(session._consume_triage_results(agent, bridge, 4))

    assert outcome == "reject"
    assert "目前不需要这项服务" in agent.said[-1]
    assert session._triage_terminal is True
    assert session._triage_reject_deadline is not None


def test_stale_or_low_confidence_verdict_has_no_irreversible_effect() -> None:
    service = _service()
    session = service.session
    session._triage_results.put_nowait(
        TriageVerdict(
            category="personal",
            action="transfer",
            confidence=0.99,
            reason_code="owner_requested",
            turn_id=1,
            call_generation=3,
        )
    )
    session._triage_results.put_nowait(
        _verdict(
            action="reject", turn_id=2, confidence=0.84, category="marketing"
        )
    )

    outcome = asyncio.run(
        session._consume_triage_results(FakeAgent(), FakeAudioBridge(), 4)
    )
    assert outcome is None
    assert session.takeover_state is TakeoverState.AI_ACTIVE
    assert session._triage_terminal is False


def test_transfer_precommit_failure_reopens_policy_lane(monkeypatch) -> None:
    monkeypatch.setenv("INBOUND_TAKEOVER_ENABLED", "false")
    service = _service()
    session = service.session
    session._triage_results.put_nowait(
        _verdict(
            action="transfer", turn_id=1, confidence=0.9, category="personal"
        )
    )
    assert (
        asyncio.run(
            session._consume_triage_results(FakeAgent(), FakeAudioBridge(), 4)
        )
        is None
    )
    assert session._triage_terminal is False

    monkeypatch.setenv("INBOUND_TAKEOVER_ENABLED", "true")
    session._triage_results.put_nowait(
        _verdict(
            action="transfer", turn_id=2, confidence=0.9, category="personal"
        )
    )
    assert (
        asyncio.run(
            session._consume_triage_results(FakeAgent(), FakeAudioBridge(), 4)
        )
        == "transfer"
    )


def test_owner_preference_is_reserved_for_judge_not_realtime(monkeypatch) -> None:
    service = _service()
    service.session._triage_mode = "enforce"
    monkeypatch.setenv("INBOUND_TAKEOVER_ENABLED", "true")
    monkeypatch.setenv(
        "INBOUND_TAKEOVER_PREFERENCE",
        "PRIVATE_OWNER_POLICY_SENTINEL",
    )

    instructions = service.session._build_agent_instructions("inbound")

    assert "PRIVATE_OWNER_POLICY_SENTINEL" not in instructions
    assert "分诊等待态" in instructions
