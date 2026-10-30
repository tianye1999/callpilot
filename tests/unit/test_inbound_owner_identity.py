"""来电只向通话模型注入中性机主称呼；外呼仍可使用配置姓名。"""

import asyncio

import pytest
from fakes import FakeAgent, FakeAudioBridge, FakeModem

from agentcall import call_agent, config, prompts


@pytest.mark.parametrize("lang", ["zh", "en"])
@pytest.mark.parametrize("pending", [False, True])
def test_inbound_owner_name_never_enters_spoken_prompts(monkeypatch, lang, pending):
    monkeypatch.setenv("OWNER_NAME", "李明")
    monkeypatch.setenv("AGENT_LANGUAGE", lang)
    monkeypatch.setenv("AGENT_PERSONA", "")
    session = call_agent.CallAgentService(
        modem_port="unused", audio_keyword="unused", provider="openai", modem=FakeModem(),
    ).session
    session._triage_pending = pending
    instructions = session._build_agent_instructions("inbound")
    opening = session._opening_instructions("inbound")
    module_opening = session._opening_instructions("inbound", module_greeting="Hello")
    spoken = [instructions, opening, module_opening,
              call_agent._INBOUND_TRIAGE_CLARIFY_TEXT[lang],
              call_agent._INBOUND_TRIAGE_REJECT_TEXT[lang],
              call_agent._INBOUND_TAKEOVER_HOLD_INSTRUCTIONS[lang],
              config.get_spec("LOCAL_INBOUND_GREETING").default,
              config.get_spec("MODEM_ANSWER_GREETING").default]
    assert all("李明" not in text for text in spoken)
    assert prompts._OWNER_FALLBACK[lang] in instructions
    assert "李明" in session._build_agent_instructions("outbound")
    assert "李明" in session._opening_instructions("outbound")


@pytest.mark.parametrize("lang", ["zh", "en"])
def test_inbound_takeover_failure_and_resumed_prompt_do_not_name_owner(monkeypatch, lang):
    monkeypatch.setenv("OWNER_NAME", "李明")
    monkeypatch.setenv("AGENT_LANGUAGE", lang)
    monkeypatch.setenv("INBOUND_TAKEOVER_ENABLED", "true")
    session = call_agent.CallAgentService(
        modem_port="unused", audio_keyword="unused", provider="openai", modem=FakeModem(),
    ).session
    session._active = True
    session._initialize_takeover_context("inbound")
    result = session._request_owner_takeover(
        session._session_generation, endpoint_check=lambda: "unavailable",
    )
    assert not result["success"] and result["code"] == "NO_TAKEOVER_ENDPOINT"
    assert "李明" not in result["message"]
    agent = FakeAgent()
    asyncio.run(session._resume_ai_after_takeover(agent, FakeAudioBridge(), session._session_generation))
    assert agent.said and all("李明" not in text for text in agent.said)
    assert "李明" not in agent._session_instructions
    assert prompts._OWNER_FALLBACK[lang].casefold() in agent.said[-1].casefold()
