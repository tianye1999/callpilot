"""快速开场只准备音频，播放与工具仍由接通后的策略控制。"""

import asyncio
import threading

import pytest
from test_local_agent import FakePipeline, _wait_until

from agentcall.agents.local_agent import LocalPipelineAgent


@pytest.mark.parametrize("mode", ["say", "wait"])
def test_session_prepares_after_dial_before_answer_and_wait_profile_stays_silent(monkeypatch, mode):
    from fakes import FakeModem

    from agentcall.call_agent import CallSession

    monkeypatch.setenv("LOCAL_FAST_OPENING", "true")
    monkeypatch.setenv("LOCAL_LLM_PREWARM", "false")
    monkeypatch.setenv("PROMPT_GEN_ENABLED", "false")
    monkeypatch.setenv("NUMBER_PROFILES_ENABLED", "false")
    prepared = []
    agent = LocalPipelineAgent(pipeline_factory=FakePipeline,
                               llm_chat=lambda *a: prepared.append(True) or {"content": "您好。"})
    monkeypatch.setattr("agentcall.call_agent.LocalPipelineAgent", lambda: agent)
    session = CallSession(FakeModem(), "unused", "local", "nmea", "unused", 115200, 1.0)
    session._outbound_number = "+12025550100"
    session._prompt_gen_opening_mode = mode
    monkeypatch.setattr(session, "_build_agent_instructions", lambda direction: "任务")
    marks = []

    async def connected(**kwargs):
        assert "dialing" in marks and session._local_prepared_agent is agent
        await session._local_prepare_task
        assert bool(prepared) == (mode == "say")
        return True

    monkeypatch.setattr(session, "_wait_connected", connected)

    async def run():
        assert await session._connect_outbound(lambda stage, **kw: marks.append(stage))
        assert agent._on_audio_out is None
        await session._finish_local_preparation()

    asyncio.run(run())


def test_connection_warmup_refresh_loop_stops_without_api_request(monkeypatch):
    monkeypatch.setenv("LOCAL_LLM_PREWARM", "true")
    monkeypatch.setenv("LOCAL_LLM_PREWARM_IDLE", "0.01")
    agent = LocalPipelineAgent(pipeline_factory=FakePipeline, llm_chat=lambda *a: {})
    refreshed = threading.Event()
    calls = []

    class Client:
        def prewarm(self, timeout, *, idle_seconds):
            calls.append(idle_seconds)
            if len(calls) == 2:
                refreshed.set()

        def close(self):
            pass

    agent._deepseek = Client()
    agent.start_connection_warmup()
    worker = agent._warm_thread
    assert refreshed.wait(1)
    asyncio.run(agent.stop())
    assert not worker.is_alive()


def test_prepared_opening_is_silent_until_say_and_consumed_once(monkeypatch):
    monkeypatch.setenv("LOCAL_FAST_OPENING", "true")
    requests, audio = [], []

    def chat(messages, tools, timeout):
        assert tools is None
        requests.append(messages)
        return {"role": "assistant", "content": "您好，我想预约。"}

    agent = LocalPipelineAgent(pipeline_factory=FakePipeline, llm_chat=chat)
    agent.set_session_instructions("任务")
    agent.prepare_opening("开场")
    assert audio == [] and agent._messages == []
    asyncio.run(agent.start(audio.append))
    try:
        asyncio.run(agent.say("开场"))
        assert len(audio) == 1 and len(requests) == 1
        assert agent._messages[-1]["content"] == "您好，我想预约。"
        asyncio.run(agent.say("开场"))
        assert _wait_until(lambda: len(requests) == 2)
    finally:
        asyncio.run(agent.stop())


def test_opening_failure_and_changed_instructions_fall_back(monkeypatch):
    monkeypatch.setenv("LOCAL_FAST_OPENING", "true")
    requests, audio = [], []

    def chat(*args):
        requests.append(args)
        if len(requests) == 1:
            raise RuntimeError("offline")
        return {"role": "assistant", "content": "好的。"}

    agent = LocalPipelineAgent(pipeline_factory=FakePipeline, llm_chat=chat)
    agent.prepare_opening("开场")
    asyncio.run(agent.start(audio.append))
    try:
        asyncio.run(agent.say("更新的开场"))
        assert _wait_until(lambda: audio)
        assert len(requests) == 2 and not agent.fatal
    finally:
        asyncio.run(agent.stop())


def test_disabled_opening_never_prepares(monkeypatch):
    monkeypatch.setenv("LOCAL_FAST_OPENING", "false")
    agent = LocalPipelineAgent(pipeline_factory=lambda: (_ for _ in ()).throw(AssertionError()))
    agent.prepare_opening("开场")


@pytest.mark.parametrize("reply, spoken", [("（等待）", []), ("您好（停顿）请讲。", ["您好请讲。"])])
def test_prepared_opening_filters_stage_directions_but_keeps_original(monkeypatch, reply, spoken):
    monkeypatch.setenv("LOCAL_FAST_OPENING", "true")
    pipeline, requests, transcripts, audio = FakePipeline(), [], [], []
    agent = LocalPipelineAgent(pipeline_factory=lambda: pipeline,
                               llm_chat=lambda *a: requests.append(True) or {"content": reply})
    agent.set_transcript_handler(lambda r, t: transcripts.append((r, t)))
    agent.prepare_opening("开场")
    asyncio.run(agent.start(audio.append))
    try:
        asyncio.run(agent.say("开场"))
        assert pipeline.synthesized == spoken
        assert bool(audio) == bool(spoken)
        assert len(requests) == 1
        assert agent._messages[-1]["content"] == reply
        assert transcripts == [("agent", reply)]
    finally:
        asyncio.run(agent.stop())


def test_inbound_configured_phrase_avoids_llm(monkeypatch):
    monkeypatch.setenv("LOCAL_FAST_OPENING", "true")
    monkeypatch.setenv("LOCAL_TTS_CACHE_PHRASES", "您好。")
    agent = LocalPipelineAgent(pipeline_factory=FakePipeline, llm_chat=lambda *a: {})
    agent.prepare_opening("问候", phrase="您好。")
    audio = []
    asyncio.run(agent.start(audio.append))
    try:
        asyncio.run(agent.say("问候"))
        assert audio and agent._messages[-1]["content"] == "您好。"
    finally:
        asyncio.run(agent.stop())


def test_opening_metrics_and_pcm_write_callback_are_distinct(monkeypatch):
    monkeypatch.setenv("LOCAL_FAST_OPENING", "true")
    monkeypatch.setenv("LOCAL_LATENCY_METRICS", "true")
    agent = LocalPipelineAgent(pipeline_factory=FakePipeline, llm_chat=lambda *a: {})
    events, audio = [], []
    agent.set_turn_event_handler(events.append)
    agent.prepare_opening("问候", phrase="您好。")
    asyncio.run(agent.start(audio.append))
    try:
        asyncio.run(agent.say("问候"))
        assert events[0]["type"] == "local_opening" and events[0]["hit"] is True
        assert events[-1]["stage"] == "tts_first_audio"
        audio[0].on_written()
        audio[0].on_written()
        written = [e for e in events if e.get("stage") == "first_pcm_written"]
        assert len(written) == 1 and written[0]["turn_id"] == 1
    finally:
        asyncio.run(agent.stop())
