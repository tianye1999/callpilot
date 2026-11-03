"""Backstop judge: triage off, the AI speaks freely, a fenced transfer verdict still reaches the owner.

Verdicts travel the same control-loop commit point as enforce-mode triage. The backstop is
two-phase (endpoint probe off-loop, commit on a later tick under the judge lock), so these
tests tick the consumer the way _run_agent_loop does, with a judge whose worker never calls
a model.
"""

from __future__ import annotations

import asyncio
import time

from fakes import FakeAgent, FakeAudioBridge, FakeModem

from agentcall.call_agent import CallAgentService
from agentcall.prompts import agent_persona, build_instructions
from agentcall.takeover_coordinator import TakeoverState
from agentcall.triage_judge import InboundTriageJudge, TakeoverBackstopPolicy, TriageVerdict

PREFERENCE = "外卖、快递、熟人和急事转给我；推销自己应付。"


def _service(monkeypatch, **env: str) -> CallAgentService:
    monkeypatch.setenv("INBOUND_TAKEOVER_ENABLED", "true")
    monkeypatch.setenv("INBOUND_TAKEOVER_BACKSTOP_ENABLED", "true")
    monkeypatch.setenv("INBOUND_TAKEOVER_PREFERENCE", PREFERENCE)
    monkeypatch.setenv("INBOUND_TRIAGE_MODE", "off")
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    # The judge thread exits at once: turns are counted, no model is ever called.
    monkeypatch.setattr(InboundTriageJudge, "_run", lambda self, worker_generation: None)
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
    session._takeover_endpoint_rejection = lambda: None
    return service


def _verdict(
    *, action: str, turn_id: int, confidence: float = 0.9, generation: int = 4
) -> TriageVerdict:
    return TriageVerdict(
        category="personal",
        action=action,  # type: ignore[arg-type]
        confidence=confidence,
        reason_code="test_reason",
        turn_id=turn_id,
        call_generation=generation,
    )


async def _drive(session, generation: int, hook, ticks: int = 300):
    """Tick the consumer like the audio loop does; the backstop commits on a later tick."""
    outcome = None
    hooked = False
    for _ in range(ticks):
        outcome = await session._consume_triage_results(FakeAgent(), FakeAudioBridge(), generation)
        if hook is not None and not hooked and session._backstop_pending is not None:
            hooked = True
            hook()
        if outcome is not None or (
            session._backstop_pending is None and session._triage_results.empty()
        ):
            break
        await asyncio.sleep(0.01)
    return outcome


def _consume(session, generation: int = 4, hook=None):
    return asyncio.run(_drive(session, generation, hook))


def _started(service):
    session = service.session
    session._initialize_triage_context("inbound", None)
    judge = session._triage_judge
    assert judge is not None
    assert judge.submit_turn("user", "我是送外卖的") == 1
    return session, judge


def test_backstop_policy_fences_and_fires_once() -> None:
    policy = TakeoverBackstopPolicy()
    outcome = lambda v: policy.consume(v, current_generation=4).outcome  # noqa: E731
    assert outcome(_verdict(action="transfer", turn_id=1, generation=3)) == "ignored"
    assert outcome(_verdict(action="clarify", turn_id=1)) == "observe"
    assert outcome(_verdict(action="transfer", turn_id=1)) == "ignored"
    assert outcome(_verdict(action="reject", turn_id=2, confidence=0.99)) == "observe"
    assert outcome(_verdict(action="transfer", turn_id=3, confidence=0.69)) == "observe"
    assert outcome(_verdict(action="transfer", turn_id=4, confidence=0.7)) == "transfer"
    assert outcome(_verdict(action="transfer", turn_id=5)) == "ignored"
    policy.rollback_terminal()
    assert outcome(_verdict(action="transfer", turn_id=6)) == "transfer"


def test_backstop_judge_starts_when_triage_off_and_keeps_ai_free(monkeypatch) -> None:
    service = _service(monkeypatch)
    session, judge = _started(service)
    assert isinstance(session._triage_consumer, TakeoverBackstopPolicy)
    assert session._triage_mode == "off" and session._triage_pending is False
    assert judge.is_current(1) and not judge.is_current(0)
    # The conversational model keeps its own tool and preference; the judge is only a backstop.
    assert "owner_takeover_preference" in session._build_agent_instructions("inbound")
    names = [spec["function"]["name"] for spec in session._build_tools("inbound").specs()]
    assert "request_owner_takeover" in names


def test_backstop_transfer_goes_through_control_loop_and_tool_acknowledges_once(monkeypatch) -> None:
    service = _service(monkeypatch)
    session, _judge = _started(service)
    session._triage_results.put_nowait(_verdict(action="transfer", turn_id=1))
    assert _consume(session) == "transfer"
    assert session.takeover_state is TakeoverState.TAKEOVER_PREPARING
    assert session._triage_terminal is True
    assert service.next_inbound_takeover_offer() is not None
    # The model's own tool call arriving after the backstop reads as "already sent"...
    result = session._request_owner_takeover(4, trigger="agent_tool")
    assert result["success"] is True and result["code"] == "TAKEOVER_REQUESTED"
    # ...but only once; a genuine repeat stays double-gated.
    assert session._request_owner_takeover(4, trigger="agent_tool")["success"] is False
    # Once fired, later verdicts are ignored.
    session._triage_results.put_nowait(_verdict(action="transfer", turn_id=2))
    assert _consume(session) is None
    assert service.next_inbound_takeover_offer() is None


def test_backstop_request_ended_before_model_asks_is_never_confirmed(monkeypatch) -> None:
    service = _service(monkeypatch)
    session, _judge = _started(service)
    session._triage_results.put_nowait(_verdict(action="transfer", turn_id=1))
    assert _consume(session) == "transfer"
    assert service.next_inbound_takeover_offer() is not None
    session._end_takeover_context("CALL_ENDED")
    # Backstop-origin request, not yet acknowledged, context ended: the late tool call must fail.
    assert session._request_owner_takeover(4, trigger="agent_tool")["success"] is False


def test_backstop_ignores_non_transfer_low_confidence_and_stale_generation(monkeypatch) -> None:
    service = _service(monkeypatch)
    session, _judge = _started(service)
    for verdict in (
        _verdict(action="reject", turn_id=1, confidence=0.99),
        _verdict(action="clarify", turn_id=1, confidence=0.99),
        _verdict(action="transfer", turn_id=1, confidence=0.5),
        _verdict(action="transfer", turn_id=1, generation=3),
    ):
        session._triage_results.put_nowait(verdict)
        assert _consume(session) is None
    assert session.takeover_state is TakeoverState.AI_ACTIVE
    assert session._triage_pending is False and session._triage_terminal is False


def test_backstop_verdict_from_an_older_call_cannot_transfer_the_next_one(monkeypatch) -> None:
    service = _service(monkeypatch)
    session, _judge = _started(service)
    session._triage_results.put_nowait(_verdict(action="transfer", turn_id=1, generation=4))
    session._session_generation = 5
    assert _consume(session, generation=5) is None
    assert session.takeover_state is TakeoverState.AI_ACTIVE


def test_backstop_new_caller_turn_during_endpoint_probe_supersedes_the_request(monkeypatch) -> None:
    service = _service(monkeypatch)
    session, judge = _started(service)

    def endpoint_check_with_new_turn():
        judge.submit_turn("user", "不用找他了")
        return None

    session._takeover_endpoint_rejection = endpoint_check_with_new_turn
    session._triage_results.put_nowait(_verdict(action="transfer", turn_id=1))
    assert _consume(session) is None
    assert session.takeover_state is TakeoverState.AI_ACTIVE
    assert session._triage_terminal is False
    # The policy re-opened: the next live verdict can still transfer.
    session._takeover_endpoint_rejection = lambda: None
    session._triage_results.put_nowait(_verdict(action="transfer", turn_id=2))
    assert _consume(session) == "transfer"
    assert session.takeover_state is TakeoverState.TAKEOVER_PREPARING


def test_backstop_commit_is_atomic_with_judge_currency(monkeypatch) -> None:
    service = _service(monkeypatch)
    session, judge = _started(service)
    # A newer caller turn landed after every earlier check: the commit itself refuses.
    assert judge.submit_turn("user", "不用找他了") == 2
    stale = session._request_owner_takeover(
        4, trigger="takeover_backstop", commit_guard=lambda: judge.hold_if_current(1)
    )
    assert stale["success"] is False and stale["code"] == "TAKEOVER_SUPERSEDED"
    assert session.takeover_state is TakeoverState.AI_ACTIVE
    live = session._request_owner_takeover(
        4, trigger="takeover_backstop", commit_guard=lambda: judge.hold_if_current(2)
    )
    assert live["success"] is True
    assert session.takeover_state is TakeoverState.TAKEOVER_PREPARING


def test_backstop_stopped_judge_leaves_queued_verdict_inert(monkeypatch) -> None:
    service = _service(monkeypatch)
    session, _judge = _started(service)
    session._triage_results.put_nowait(_verdict(action="transfer", turn_id=1))
    session._stop_triage_judge()
    assert _consume(session) is None
    assert session.takeover_state is TakeoverState.AI_ACTIVE


def test_backstop_judge_stopped_during_endpoint_probe_does_not_commit(monkeypatch) -> None:
    service = _service(monkeypatch)
    session, _judge = _started(service)
    session._takeover_endpoint_rejection = lambda: time.sleep(0.05)
    session._triage_results.put_nowait(_verdict(action="transfer", turn_id=1))
    assert _consume(session, hook=session._stop_triage_judge) is None
    assert session.takeover_state is TakeoverState.AI_ACTIVE
    assert session._triage_terminal is False


def test_backstop_defers_when_model_requests_first_or_during_probe(monkeypatch) -> None:
    service = _service(monkeypatch)
    session, _judge = _started(service)
    session._takeover_endpoint_rejection = lambda: time.sleep(0.05)
    session._triage_results.put_nowait(_verdict(action="transfer", turn_id=1))
    model_first = lambda: session._request_owner_takeover(4, trigger="agent_tool")  # noqa: E731
    assert _consume(session, hook=model_first) is None
    assert session.takeover_state is TakeoverState.TAKEOVER_PREPARING
    assert service.next_inbound_takeover_offer() is not None
    assert service.next_inbound_takeover_offer() is None  # exactly one offer
    # A repeat after the model's own success stays gated.
    assert session._request_owner_takeover(4, trigger="agent_tool")["success"] is False


def test_backstop_endpoint_probe_never_suspends_the_audio_loop(monkeypatch) -> None:
    service = _service(monkeypatch)
    session, _judge = _started(service)
    session._takeover_endpoint_rejection = lambda: time.sleep(0.3)
    session._triage_results.put_nowait(_verdict(action="transfer", turn_id=1))

    async def ticks_stay_short():
        longest = 0.0
        outcome = None
        for _ in range(200):
            started = time.monotonic()
            outcome = await session._consume_triage_results(FakeAgent(), FakeAudioBridge(), 4)
            longest = max(longest, time.monotonic() - started)
            if outcome is not None:
                break
            await asyncio.sleep(0.005)
        return outcome, longest

    outcome, longest = asyncio.run(ticks_stay_short())
    assert outcome == "transfer"
    assert longest < 0.1, longest  # the 300 ms probe ran off the loop


def test_backstop_keeps_a_newer_verdict_queued_while_the_probe_runs(monkeypatch) -> None:
    service = _service(monkeypatch)
    session, judge = _started(service)
    session._takeover_endpoint_rejection = lambda: time.sleep(0.05)
    session._triage_results.put_nowait(_verdict(action="transfer", turn_id=1))

    def caller_speaks_again_and_judge_agrees():
        assert judge.submit_turn("user", "我到门口了") == 2
        session._triage_results.put_nowait(_verdict(action="transfer", turn_id=2))

    # Turn 1 is superseded by turn 2 during its probe; turn 2's verdict must survive and transfer.
    assert _consume(session, hook=caller_speaks_again_and_judge_agrees) == "transfer"
    assert session.takeover_state is TakeoverState.TAKEOVER_PREPARING
    assert service.next_inbound_takeover_offer() is not None


def test_backstop_verdict_landing_in_the_same_tick_as_the_probe_start_survives(monkeypatch) -> None:
    service = _service(monkeypatch)
    session, judge = _started(service)
    session._takeover_endpoint_rejection = lambda: time.sleep(0.05)
    original_begin = session._begin_backstop_transfer

    def begin_then_caller_speaks_again(verdict):
        original_begin(verdict)
        if verdict.turn_id == 1:
            # Same consumer tick, right after the probe was created.
            assert judge.submit_turn("user", "我到门口了") == 2
            session._triage_results.put_nowait(_verdict(action="transfer", turn_id=2))

    session._begin_backstop_transfer = begin_then_caller_speaks_again
    session._triage_results.put_nowait(_verdict(action="transfer", turn_id=1))
    assert _consume(session) == "transfer"
    assert session.takeover_state is TakeoverState.TAKEOVER_PREPARING
    assert service.next_inbound_takeover_offer() is not None


def test_backstop_probe_is_settled_before_the_call_loop_closes(monkeypatch) -> None:
    service = _service(monkeypatch)
    session, _judge = _started(service)
    session._takeover_endpoint_rejection = lambda: time.sleep(0.3)
    session._triage_results.put_nowait(_verdict(action="transfer", turn_id=1))
    loop = asyncio.new_event_loop()
    try:
        assert loop.run_until_complete(
            session._consume_triage_results(FakeAgent(), FakeAudioBridge(), 4)
        ) is None
        assert session._backstop_pending is not None
        # Production order: stop the judge, drain the probe, then close the call's loop.
        session._stop_triage_judge()
        loop.run_until_complete(session._drain_backstop_pending())
        assert session._backstop_pending is None
    finally:
        loop.close()
    # The next call initializes on a fresh loop without touching the old one.
    session._session_generation = 5
    session._initialize_takeover_context("inbound")
    session._initialize_triage_context("inbound", None)
    assert session._triage_judge is not None
    assert session.takeover_state is TakeoverState.AI_ACTIVE


def test_backstop_probe_left_on_a_closed_loop_does_not_break_the_next_call(monkeypatch) -> None:
    service = _service(monkeypatch)
    session, _judge = _started(service)
    session._takeover_endpoint_rejection = lambda: time.sleep(0.3)
    session._triage_results.put_nowait(_verdict(action="transfer", turn_id=1))
    loop = asyncio.new_event_loop()
    assert loop.run_until_complete(
        session._consume_triage_results(FakeAgent(), FakeAudioBridge(), 4)
    ) is None
    # Abnormal teardown: the probe was never drained. Silence asyncio's GC report for the
    # deliberately orphaned task; the assertion below is what matters.
    loop.set_exception_handler(lambda _loop, _context: None)
    loop.close()
    session._session_generation = 5
    session._initialize_takeover_context("inbound")
    session._initialize_triage_context("inbound", None)  # must not raise "Event loop is closed"
    assert session._backstop_pending is None and session._triage_judge is not None


def test_backstop_does_not_touch_an_exhausted_attempt(monkeypatch) -> None:
    service = _service(monkeypatch)
    session, _judge = _started(service)
    session._takeover_attempted = True  # the AI's one attempt (#131) already happened
    session._triage_results.put_nowait(_verdict(action="transfer", turn_id=1))
    assert _consume(session) is None
    assert session.takeover_state is TakeoverState.AI_ACTIVE
    assert session._takeover_ai_fallback is False


def test_backstop_not_started_when_disabled_declined_or_unconfigured(monkeypatch) -> None:
    cases = (
        ({"INBOUND_TAKEOVER_BACKSTOP_ENABLED": "false"}, False),
        ({"INBOUND_TAKEOVER_PREFERENCE": "  "}, False),
        ({"INBOUND_TAKEOVER_ENABLED": "false"}, False),
        ({}, True),
    )
    for env, declined in cases:
        service = _service(monkeypatch, **env)
        session = service.session
        session._owner_declined = declined
        session._initialize_triage_context("inbound", None)
        assert session._triage_judge is None, (env, declined)
        assert not isinstance(session._triage_consumer, TakeoverBackstopPolicy), (env, declined)


def test_inbound_prompt_task_prefers_tool_over_relay_only_with_capability() -> None:
    for lang, relay in (("zh", "只说会转告"), ("en", "only say")):
        with_tool = build_instructions(
            "inbound", "机主", agent_persona(lang), None, lang,
            scenario=None, takeover_preference=PREFERENCE, triage_pending=False,
        )
        without = build_instructions(
            "inbound", "机主", agent_persona(lang), None, lang,
            scenario=None, takeover_preference=None, triage_pending=False,
        )
        assert "request_owner_takeover" in with_tool.split("\n")[1], lang
        assert relay not in with_tool and relay in without, lang
        # Relaying a message by SMS stays a valid capability; only live pickup is tool-only.
        assert "send_sms" in with_tool
