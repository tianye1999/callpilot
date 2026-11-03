"""F2: the AI is told what happened on the owner's phone before it answered (background, not a rule)."""

from __future__ import annotations

from fakes import FakeModem

from agentcall.call_agent import _PHONE_FIRST_OUTCOMES, CallAgentService
from agentcall.prompts import agent_persona, build_instructions

PREFERENCE = "外卖、快递、熟人和急事转给我；推销自己应付。"


def _session(monkeypatch):
    monkeypatch.setenv("INBOUND_TAKEOVER_ENABLED", "true")
    monkeypatch.setenv("INBOUND_TAKEOVER_PREFERENCE", PREFERENCE)
    monkeypatch.setenv("INBOUND_TRIAGE_MODE", "off")
    monkeypatch.setenv("INBOUND_PHONE_FIRST_RING_SECONDS", "12")
    service = CallAgentService(
        modem_port="unused", audio_keyword="unused", provider="openai",
        modem=FakeModem(),  # type: ignore[arg-type]
    )
    session = service.session
    session._active = True
    session._outbound_number = None
    session._session_generation = 4
    session._initialize_takeover_context("inbound")
    return session


def test_inbound_context_is_a_background_line_only_for_inbound() -> None:
    for lang, marker in (("zh", "本通背景："), ("en", "Context for this call:")):
        inbound = build_instructions("inbound", "机主", agent_persona(lang), None, lang,
                                     inbound_context="刚才响过手机", takeover_preference=PREFERENCE)
        line = inbound.split("\n")[1]
        assert marker in line and "刚才响过手机" in line, lang
        outbound = build_instructions("outbound", "机主", agent_persona(lang), "查话费", lang,
                                      inbound_context="刚才响过手机")
        assert "刚才响过手机" not in outbound, lang
        plain = build_instructions("inbound", "机主", agent_persona(lang), None, lang,
                                   inbound_context="  ", takeover_preference=PREFERENCE)
        assert marker not in plain, lang


def test_phone_first_outcomes_map_only_the_states_the_caller_can_feel() -> None:
    assert _PHONE_FIRST_OUTCOMES == {
        "OWNER_TIMEOUT": "missed",
        "OWNER_DECLINED": "declined",
        "NO_PHONE_AUDIO": "media_failed",
        "MEDIA_PREPARE_FAILED": "media_failed",
    }


def test_missed_ring_keeps_transfer_and_tells_the_ai_the_caller_waited(monkeypatch) -> None:
    session = _session(monkeypatch)
    assert session._build_agent_instructions("inbound").count("本通背景") == 0
    session._phone_first_outcome = "missed"
    text = session._build_agent_instructions("inbound")
    assert "本通背景：本通先响了机主的手机约 12 秒没人接" in text
    assert "owner_takeover_preference" in text
    # The background states facts only; whether to transfer stays with the capability branch,
    # so a later failed AI transfer (no capability) never contradicts it.
    session._takeover_ai_fallback = True
    fallback = session._build_agent_instructions("inbound")
    assert "本通先响了机主的手机约 12 秒没人接" in fallback
    assert "owner_takeover_preference" not in fallback and "照常转接" not in fallback


def test_late_decline_after_ring_timeout_switches_the_background(monkeypatch) -> None:
    monkeypatch.setenv("INBOUND_OWNER_DECLINE_ENABLED", "true")
    session = _session(monkeypatch)
    session._phone_first_outcome = "missed"
    session._owner_offer_fences["offer_test_000000000001"] = ("call_test_000000000001", 4, "nonce-1")
    accepted = session.owner_declined({
        "offerId": "offer_test_000000000001", "callId": "call_test_000000000001",
        "generation": 4, "nonce": "nonce-1",
    })
    assert accepted is True and session._owner_declined is True
    assert session._takeover_resume_pending is True  # the resume path re-pushes instructions
    text = session._build_agent_instructions("inbound")
    assert "本通背景：机主看到了这通来电，选择由你全权处理" in text
    assert "没人接" not in text and "owner_takeover_preference" not in text


def test_declined_call_tells_the_ai_not_to_mention_the_owner_and_has_no_transfer(monkeypatch) -> None:
    session = _session(monkeypatch)
    session._owner_declined = True
    session._phone_first_outcome = "declined"
    text = session._build_agent_instructions("inbound")
    assert "本通背景：机主看到了这通来电，选择由你全权处理" in text
    assert "owner_takeover_preference" not in text


def test_context_note_is_reset_per_call_and_localized(monkeypatch) -> None:
    session = _session(monkeypatch)
    session._phone_first_outcome = "media_failed"
    assert "线路没接上" in (session._phone_first_context_note("zh") or "")
    assert "did not connect" in (session._phone_first_context_note("en") or "")
    session._initialize_takeover_context("inbound")
    assert session._phone_first_context_note("zh") is None
