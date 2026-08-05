from __future__ import annotations

from fakes import FakeAgent


def test_agent_trace_uses_a_strict_safe_field_allowlist():
    agent = FakeAgent()
    events: list[dict] = []
    agent.set_trace_handler(events.append)

    agent._emit_trace(
        "model",
        "response_done",
        "ok",
        ms=321,
        chars=12,
        code="done",
        prompt="do not expose this",
        transcript="private conversation",
        api_key="sk-secret",
        arguments={"phone": "10000"},
    )

    assert events == [
        {
            "stage": "model",
            "event": "response_done",
            "status": "ok",
            "chars": 12,
            "code": "done",
            "ms": 321,
        }
    ]
