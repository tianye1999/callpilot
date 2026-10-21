"""Offline regression coverage for issue #131's shared takeover gates."""

from __future__ import annotations

import asyncio
import threading
import time

import pytest
from fakes import FakeAgent, FakeAudioBridge, FakeModem

from agentcall.call_agent import CallAgentService
from agentcall.takeover_coordinator import TakeoverState
from agentcall.triage_judge import TriageVerdict

_EDGE_CREDENTIAL = "edge_abcdefghijkl." + "s" * 40


def _register(monkeypatch) -> None:
    monkeypatch.setattr("keyring.get_password", lambda *_args: _EDGE_CREDENTIAL)


class Events:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def log_event(self, event_type: str, **fields) -> None:
        self.events.append({"type": event_type, **fields})


def _service(monkeypatch) -> CallAgentService:
    monkeypatch.setenv("INBOUND_TAKEOVER_ENABLED", "true")
    monkeypatch.setenv("REMOTE_WEB_DIALER_ENABLED", "true")
    monkeypatch.setenv("REMOTE_CLOUD_ENABLED", "true")
    monkeypatch.setattr("keyring.get_password", lambda *_args: None)
    service = CallAgentService(
        modem_port="unused", audio_keyword="unused", provider="openai",
        modem=FakeModem(),  # type: ignore[arg-type]
    )
    session = service.session
    session._active = True
    session._session_generation = 4
    session._initialize_takeover_context("inbound")
    return service


@pytest.mark.parametrize("trigger", ["agent_tool", "triage_judge"])
@pytest.mark.parametrize("remote_enabled,cloud_enabled,credential,reason", [
    (True, True, None, "edge_not_registered"),
    (True, True, "invalid", "edge_not_registered"),
    (False, True, _EDGE_CREDENTIAL, "hosted_takeover_disabled"),
    (True, False, _EDGE_CREDENTIAL, "hosted_takeover_disabled"),
])
def test_no_endpoint_keeps_ai_audio_and_tools_active(
    monkeypatch, trigger, remote_enabled, cloud_enabled, credential, reason
) -> None:
    service = _service(monkeypatch)
    monkeypatch.setenv("REMOTE_WEB_DIALER_ENABLED", str(remote_enabled).lower())
    monkeypatch.setenv("REMOTE_CLOUD_ENABLED", str(cloud_enabled).lower())
    monkeypatch.setattr("keyring.get_password", lambda *_args: credential)
    session = service.session
    agent, bridge = FakeAgent(), FakeAudioBridge()
    agent._on_audio_out = session._make_agent_audio_handler(agent, bridge, None)
    session._triage_pending = True
    events = Events()
    session._record = events

    async def run() -> None:
        if trigger == "agent_tool":
            result = session._build_tools("inbound").dispatch("request_owner_takeover", {})
            assert result["code"] == "NO_TAKEOVER_ENDPOINT"
            await session._resume_ai_after_takeover(agent, bridge, 4)
        else:
            session._triage_mode = "enforce"
            session._triage_results.put_nowait(TriageVerdict(
                category="unknown", action="transfer", confidence=0.7,
                reason_code="unknown_intent", turn_id=4, call_generation=4,
            ))
            assert await session._consume_triage_results(agent, bridge, 4) is None

    asyncio.run(run())
    assert session.takeover_state is TakeoverState.AI_ACTIVE
    assert session._agent_effect_allowed(4)
    assert not session._triage_pending
    assert not session._triage_terminal
    assert service.next_inbound_takeover_offer() is None
    assert agent.said
    assert bridge.downlink
    assert service.modem.calls == []
    assert [event for event in events.events if event["type"] == "takeover_unavailable"] == [{
        "type": "takeover_unavailable", "reason": reason, "code": "NO_TAKEOVER_ENDPOINT", "trigger": trigger,
    }]


@pytest.mark.parametrize("has_endpoint", [False, True])
def test_main_loop_reclaims_timeout_and_resumes_both_audio_directions(monkeypatch, has_endpoint) -> None:
    service = _service(monkeypatch)
    session = service.session
    session._barge_in = True
    if has_endpoint:
        _register(monkeypatch)
        assert session.begin_owner_takeover()["success"]
        session._takeover_deadline = time.monotonic() - 0.01
    else:
        assert session._build_tools("inbound").dispatch("request_owner_takeover", {})["code"] == "NO_TAKEOVER_ENDPOINT"
    session._triage_terminal = True
    session._triage_pending = True

    class CallerAudioAgent(FakeAgent):
        async def send_audio(self, pcm: bytes) -> None:
            await super().send_audio(pcm)
            session._active = False

    agent, bridge = CallerAudioAgent(), FakeAudioBridge()
    bridge.feed_uplink(b"\x00\x10" * 160)
    agent._on_audio_out = session._make_agent_audio_handler(agent, bridge, None)
    asyncio.run(asyncio.wait_for(session._run_agent_loop(agent, bridge, None, []), timeout=1))
    assert agent.received_audio
    assert bridge.downlink
    assert len(agent.said) == 1
    assert session.takeover_state is TakeoverState.AI_ACTIVE
    assert service.modem.calls == []


def test_recovery_drops_old_response_and_bridge_backlog_before_new_scene(monkeypatch) -> None:
    service = _service(monkeypatch)
    session = service.session
    assert session.begin_owner_takeover()["code"] == "NO_TAKEOVER_ENDPOINT"

    class CancellableAgent(FakeAgent):
        async def cancel_response(self) -> bool:
            self._on_audio_out(b"\x00\x20" * 240)
            return True

    agent, bridge = CancellableAgent(), FakeAudioBridge()
    bridge.pending_bytes = 1600
    agent._on_audio_out = session._make_agent_audio_handler(agent, bridge, None)
    asyncio.run(session._resume_ai_after_takeover(agent, bridge, 4))
    assert bridge.discarded_bytes == 1600
    assert len(bridge.downlink) == 1
    assert len(agent.said) == 1


def test_blocked_hold_obeys_overall_deadline_and_recovers(monkeypatch) -> None:
    service = _service(monkeypatch)
    session = service.session
    _register(monkeypatch)
    assert session.begin_owner_takeover()["success"]
    session._takeover_deadline = time.monotonic() + 0.02

    class BlockedHoldAgent(FakeAgent):
        async def say(self, instructions: str) -> None:
            await asyncio.Event().wait()

    async def run() -> None:
        await asyncio.wait_for(session._speak_takeover_hold_if_needed(BlockedHoldAgent(), FakeAudioBridge(), 4), 0.2)
        # Production polls _expire_takeover every loop turn; this single call must
        # not depend on clock granularity (~15.6ms on Windows/Python 3.12).
        deadline = session._takeover_deadline
        assert deadline is not None
        session._expire_takeover(generation=4, now=deadline + 0.001)
        await session._resume_ai_after_takeover(FakeAgent(), FakeAudioBridge(), 4)

    asyncio.run(run())
    assert session._agent_effect_allowed(4)
    assert session.takeover_state is TakeoverState.AI_ACTIVE


def test_delayed_hold_audio_is_played_before_ai_gate_closes(monkeypatch) -> None:
    service = _service(monkeypatch)
    session = service.session
    _register(monkeypatch)
    assert session.begin_owner_takeover()["success"]

    class DelayedAgent(FakeAgent):
        async def say(self, instructions: str) -> None:
            asyncio.get_running_loop().call_later(0.01, lambda: self._on_audio_out(self.reply_pcm))

    agent, bridge = DelayedAgent(), FakeAudioBridge()
    agent._on_audio_out = session._make_agent_audio_handler(agent, bridge, None)

    async def run() -> None:
        await session._speak_takeover_hold_if_needed(agent, bridge, 4)
        await asyncio.sleep(0.02)

    asyncio.run(run())
    assert bridge.downlink


@pytest.mark.parametrize("trigger", ["agent_tool", "triage_judge"])
def test_timeout_reclaims_once_and_blocks_another_offer(monkeypatch, trigger) -> None:
    service = _service(monkeypatch)
    session = service.session
    _register(monkeypatch)
    agent, bridge = FakeAgent(), FakeAudioBridge()
    agent._on_audio_out = session._make_agent_audio_handler(agent, bridge, None)
    events = Events()
    session._record = events

    async def run() -> None:
        if trigger == "triage_judge":
            session._triage_mode = "enforce"
            session._triage_pending = True
            session._triage_results.put_nowait(TriageVerdict(
                category="unknown", action="transfer", confidence=0.7,
                reason_code="unknown_intent", turn_id=4, call_generation=4,
            ))
            assert await session._consume_triage_results(agent, bridge, 4) == "transfer"
        else:
            assert session._build_tools("inbound").dispatch("request_owner_takeover", {})["success"]
        request = service.next_inbound_takeover_offer()
        assert request is not None
        assert request.expires_at - request.created_at == 20.0
        started = session._takeover_started_at
        assert started is not None
        session._expire_takeover(generation=4, now=started + 19.99)
        assert session.takeover_state is TakeoverState.TAKEOVER_PREPARING
        session._expire_takeover(generation=4, now=started + 20)
        await session._resume_ai_after_takeover(agent, bridge, 4)
        assert session._agent_effect_allowed(4)
        count = len(agent.said)
        session._expire_takeover(generation=4, now=started + 40)
        await session._resume_ai_after_takeover(agent, bridge, 4)
        assert len(agent.said) == count == 1
        reclaimed = [event for event in events.events if event["type"] == "takeover_timeout_reclaimed"]
        assert len(reclaimed) == 1
        assert reclaimed[0]["elapsed_ms"] == 20_000.0
        revoke = service.next_inbound_takeover_revoke()
        assert revoke is not None and revoke.reason == "OWNER_TIMEOUT"
        assert service.next_inbound_takeover_revoke() is None
        repeated = session.begin_owner_takeover()
        assert repeated["code"] == "TAKEOVER_ATTEMPT_EXHAUSTED"
        assert service.next_inbound_takeover_offer() is None

    asyncio.run(run())
    assert not session._triage_terminal
    assert not session._triage_pending
    assert bridge.downlink
    assert service.modem.calls == []


@pytest.mark.parametrize("configured,expected", [("7", 7.0), ("0", 20.0), ("-1", 20.0), ("nan", 20.0)])
def test_timeout_config_is_positive_and_platform_neutral(monkeypatch, configured, expected) -> None:
    service = _service(monkeypatch)
    monkeypatch.setenv("INBOUND_TAKEOVER_TIMEOUT_SECONDS", configured)
    _register(monkeypatch)
    assert service.session.begin_owner_takeover()["success"]
    request = service.next_inbound_takeover_offer()
    assert request is not None
    assert request.expires_at - request.created_at == expected


def test_registration_after_preflight_rejection_does_not_consume_an_attempt(monkeypatch) -> None:
    service = _service(monkeypatch)
    assert service.session.begin_owner_takeover()["code"] == "NO_TAKEOVER_ENDPOINT"
    _register(monkeypatch)
    assert service.session.begin_owner_takeover()["success"]
    agent = FakeAgent()
    asyncio.run(service.session._resume_ai_after_takeover(agent, FakeAudioBridge(), 4))
    assert not agent.said
    assert not service.session._takeover_ai_fallback


def test_unreadable_local_registration_fails_closed(monkeypatch) -> None:
    service = _service(monkeypatch)

    def unavailable_keyring(*_args):
        raise RuntimeError("credential store unavailable")

    monkeypatch.setattr("keyring.get_password", unavailable_keyring)
    assert service.session.begin_owner_takeover()["code"] == "NO_TAKEOVER_ENDPOINT"
    assert service.session.takeover_state is TakeoverState.AI_ACTIVE
    assert service.next_inbound_takeover_offer() is None


def test_local_registration_is_rechecked_at_request_time(monkeypatch) -> None:
    service = _service(monkeypatch)
    _register(monkeypatch)
    tools = service.session._build_tools("inbound")
    monkeypatch.setattr("keyring.get_password", lambda *_args: None)
    assert tools.dispatch("request_owner_takeover", {})["code"] == "NO_TAKEOVER_ENDPOINT"
    assert service.next_inbound_takeover_offer() is None


def test_slow_tool_preflight_does_not_lock_audio_or_queue_a_stale_offer(monkeypatch) -> None:
    service = _service(monkeypatch)
    session = service.session
    entered, release = threading.Event(), threading.Event()
    results: list[dict] = []

    def slow_read(*_args):
        entered.set()
        release.wait(2)
        return _EDGE_CREDENTIAL

    monkeypatch.setattr("keyring.get_password", slow_read)
    tools = session._build_tools("inbound")
    worker = threading.Thread(target=lambda: results.append(tools.dispatch("request_owner_takeover", {})), daemon=True)
    worker.start()
    try:
        assert entered.wait(1)
        assert session._agent_effect_allowed(4)
        session._session_generation = 5
    finally:
        release.set()
        worker.join(1)
    assert not worker.is_alive()
    assert results[0]["code"] == "STALE_AGENT_GENERATION"
    assert service.next_inbound_takeover_offer() is None


def test_blocked_triage_preflight_times_out_without_a_late_offer(monkeypatch) -> None:
    service = _service(monkeypatch)
    session = service.session
    session._triage_mode = "enforce"
    entered, release = threading.Event(), threading.Event()
    events = Events()
    session._record = events

    def blocked_read(*_args):
        entered.set()
        release.wait(2)
        return _EDGE_CREDENTIAL

    monkeypatch.setattr("keyring.get_password", blocked_read)
    session._triage_results.put_nowait(TriageVerdict(
        category="unknown", action="transfer", confidence=0.9,
        reason_code="unknown_intent", turn_id=1, call_generation=4,
    ))
    agent = FakeAgent()

    async def run() -> None:
        task = asyncio.create_task(session._consume_triage_results(agent, FakeAudioBridge(), 4))
        try:
            await asyncio.sleep(0.02)
            assert entered.is_set()
            assert session._agent_effect_allowed(4) and not session._triage_terminal
            assert await asyncio.wait_for(task, 1.3) is None
        finally:
            release.set()

    asyncio.run(run())
    assert len(agent.said) == 1
    assert service.next_inbound_takeover_offer() is None
    assert session.takeover_state is TakeoverState.AI_ACTIVE
    rejected = [event for event in events.events if event["type"] == "takeover_unavailable"]
    assert rejected[0]["reason"] == "local_registration_timeout"


def test_blocked_tool_preflight_returns_and_recovers_without_a_late_offer(monkeypatch) -> None:
    service = _service(monkeypatch)
    session = service.session
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()

    def blocked_read(*_args):
        entered.set()
        release.wait(3)
        finished.set()
        return _EDGE_CREDENTIAL

    monkeypatch.setattr("keyring.get_password", blocked_read)
    try:
        started = time.monotonic()
        result = session._build_tools("inbound").dispatch("request_owner_takeover", {})
        assert time.monotonic() - started < 1.5
        assert entered.is_set() and not finished.is_set()
        assert result["code"] == "NO_TAKEOVER_ENDPOINT"
        agent, bridge = FakeAgent(), FakeAudioBridge()
        agent._on_audio_out = session._make_agent_audio_handler(agent, bridge, None)
        asyncio.run(session._resume_ai_after_takeover(agent, bridge, 4))
        assert len(agent.said) == 1 and bridge.downlink
    finally:
        release.set()
        assert finished.wait(1)
    assert service.next_inbound_takeover_offer() is None
    assert session.takeover_state is TakeoverState.AI_ACTIVE


def test_ended_and_stale_generation_cannot_recover_or_speak(monkeypatch) -> None:
    service = _service(monkeypatch)
    session = service.session
    _register(monkeypatch)
    assert session.begin_owner_takeover()["success"]
    deadline = session._takeover_deadline
    session._expire_takeover(generation=3, now=deadline + 1)
    assert session.takeover_state is TakeoverState.TAKEOVER_PREPARING
    session._end_takeover_context("CALL_ENDED")
    session._active = False
    session._expire_takeover(generation=4, now=deadline + 1)
    agent = FakeAgent()
    asyncio.run(session._resume_ai_after_takeover(agent, FakeAudioBridge(), 4))
    assert session.takeover_state is TakeoverState.ENDED
    assert not agent.said


def test_queued_triage_rejections_cannot_override_continue_ai_fallback(monkeypatch) -> None:
    service = _service(monkeypatch)
    session = service.session
    session._triage_mode = "enforce"
    for turn_id, action in [(1, "transfer"), (2, "reject"), (3, "reject")]:
        session._triage_results.put_nowait(TriageVerdict(
            category="unknown", action=action, confidence=0.9,
            reason_code="unknown_intent", turn_id=turn_id, call_generation=4,
        ))
    agent = FakeAgent()
    asyncio.run(session._consume_triage_results(agent, FakeAudioBridge(), 4))
    asyncio.run(session._consume_triage_results(agent, FakeAudioBridge(), 4))
    assert session._agent_effect_allowed(4)
    assert not session._triage_terminal
    assert session._triage_reject_deadline is None
    assert len(agent.said) == 1


def test_cancel_failure_does_not_skip_releasing_provider_instructions(monkeypatch) -> None:
    service = _service(monkeypatch)
    session = service.session
    session._triage_mode = "enforce"
    session._triage_pending = True
    assert session.begin_owner_takeover()["code"] == "NO_TAKEOVER_ENDPOINT"

    class FailedCancelAgent(FakeAgent):
        updated: str | None = None

        async def cancel_response(self) -> bool:
            raise RuntimeError("provider unavailable")

        async def update_session_instructions(self, instructions: str) -> bool:
            self.updated = instructions
            return True

    agent = FailedCancelAgent()
    asyncio.run(session._resume_ai_after_takeover(agent, FakeAudioBridge(), 4))
    assert agent.updated is not None and "分诊等待态" not in agent.updated
    assert len(agent.said) == 1
    assert session._agent_effect_allowed(4)
