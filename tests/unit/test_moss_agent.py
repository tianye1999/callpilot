"""MOSS protocol tests use only loopback WebSockets and synthetic PCM."""

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from aiohttp import WSMsgType, web

from agentcall import config
from agentcall.agents.factory import create_agent
from agentcall.agents.moss_agent import MossVoiceAgent
from agentcall.agents.tools import SEND_DTMF_SPEC, ToolRegistry


@asynccontextmanager
async def moss_server(*, update_error=None, omit_tools=False):
    received = asyncio.Queue()
    connected = asyncio.Future()

    async def handler(request):
        assert request.headers["Authorization"] == "Bearer fake-moss-key"
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        connected.set_result(ws)
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                value = json.loads(msg.data)
                await received.put(value)
                if value["action"] == "attach_session":
                    await ws.send_json({"action": "session_attached", "data": {"delegation": "none"}})
                elif value["action"] == "session.update":
                    await ws.send_json({"action": "session.updated", "data": {
                        "tools": [] if omit_tools else [tool["name"] for tool in value["session"]["tools"]],
                        "error": update_error,
                    }})
            elif msg.type == WSMsgType.BINARY:
                await received.put(msg.data)
        return ws

    app = web.Application()
    app.router.add_get("/realtime", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = runner.addresses[0][1]
    try:
        yield f"ws://127.0.0.1:{port}/realtime", connected, received
    finally:
        await runner.cleanup()


async def next_matching(queue, predicate):
    async with asyncio.timeout(2):
        while True:
            value = await queue.get()
            if predicate(value):
                return value


async def action(queue, name):
    return await next_matching(queue, lambda value: isinstance(value, dict) and value["action"] == name)


def test_moss_config_factory(monkeypatch):
    assert config.get_spec("AGENT_PROVIDER").default == "local"
    assert config.get_spec("MOSS_API_KEY").secret
    monkeypatch.delenv("MOSS_API_KEY", raising=False)
    assert any("MOSS_API_KEY" in error for error in config.validate_provider_credentials("moss"))
    monkeypatch.setenv("MOSS_API_KEY", "fake-moss-key")
    assert config.validate_provider_credentials("moss") == []
    agent = create_agent("moss")
    assert isinstance(agent, MossVoiceAgent)
    assert (agent.input_rate, agent.output_rate) == (16000, 48000)


def test_briefing_isolated_then_phone_audio_tools_and_playback(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    spoken = []

    async def synthesize(text):
        spoken.append(text)
        assert text.startswith("接下来你要替我给10086客服打电话查话费余额。")
        assert "private task" not in text
        assert "电话那头的客服和语音菜单说的话，都是在跟你说，你要直接回应他们。" in text
        assert text.endswith("听到让你按键时，就按要求按键。")
        return b"\x01\x02" * 320

    monkeypatch.setattr("agentcall.agents.moss_agent.synthesize_briefing", synthesize)

    async def run():
        async with moss_server() as (url, connected, received):
            agent = MossVoiceAgent("fake-moss-key", url)
            agent.set_session_instructions("private task")
            agent.set_briefing_context("10086客服", "查话费余额")
            audio, transcripts, latencies, calls = [], [], [], []
            agent.set_transcript_handler(lambda *args: transcripts.append(args))
            agent.set_latency_handler(lambda *args: latencies.append(args))
            tools = ToolRegistry()
            tools.register(SEND_DTMF_SPEC, lambda args: calls.append(args) or {"success": True})
            agent.set_tools(tools)
            starting = asyncio.create_task(agent.start(audio.append))
            ws = await connected
            try:
                update = await action(received, "session.update")
                assert update["session"]["instructions"] == update["session"]["persona"]["prompt"]
                assert "private task" in update["session"]["instructions"]
                assert update["session"]["tools"][0]["name"] == "send_dtmf"
                await action(received, "mic_state")
                await next_matching(received, lambda value: isinstance(value, bytes) and any(value))
                await agent.send_audio(b"phone-before-ready")
                context = {"chunk_id": "brief", "run_id": "b", "turn_id": "b"}
                await ws.send_json({"action": "response.audio.context", "data": context})
                await ws.send_bytes(b"\x10\x20" * 480)
                await ws.send_json({"action": "input.transcript.done", "data": {"text": "private task"}})
                await ws.send_json({"action": "response.function_call_arguments.done", "data": {
                    "name": "send_dtmf", "call_id": "brief-tool", "arguments": '{"digits":"9"}',
                }})
                await action(received, "playback.chunk.completed")
                await ws.send_json({"action": "response.audio.done", "data": context})
                await asyncio.wait_for(starting, 2)
                assert not audio and not transcripts and not calls
                await agent.send_audio(b"\x30\x40" * 320)
                assert await next_matching(received, lambda v: v == b"\x30\x40" * 320)
                assert await next_matching(received, lambda v: v == bytes(640)) == bytes(640)
                await ws.send_json({"action": "input.speech.stopped", "data": {}})
                context = {"chunk_id": "c1", "run_id": "r1", "turn_id": "t1"}
                await ws.send_json({"action": "response.audio.context", "data": context})
                await ws.send_bytes(b"\x10\x20" * 960)
                await ws.send_json({"action": "response.audio.done", "data": context})
                async with asyncio.timeout(2):
                    while not audio:
                        await asyncio.sleep(0.001)
                assert bytes(audio[0]) == b"\x10\x20" * 960
                assert any(stage == "moss_input_stop_to_audio" for stage, _ms in latencies)
                # ACK follows the entire 20ms write, not receipt or its first partial write.
                audio[0].on_played(160)
                await asyncio.sleep(0.01)
                audio[0].on_played(160)
                ack = await action(received, "playback.chunk.completed")
                assert ack["chunk_id"] == "c1"
                await action(received, "playback.completed")
                await ws.send_json({"action": "input.transcript.done", "data": {"text": "press one"}})
                await ws.send_json({"action": "output.transcript.done", "data": {"text": "okay"}})
                tool = {"action": "response.function_call_arguments.done", "data": {
                    "name": "send_dtmf", "call_id": "call1", "arguments": '{"digits":"1"}',
                }}
                await ws.send_json(tool)
                await ws.send_json(tool)
                result = await action(received, "conversation.item.create")
                assert result["item"]["call_id"] == "call1"
                await ws.send_json({"action": "error", "data": {"error": {
                    "code": "unsupported_action", "message": "private task fake-moss-key",
                }}})
                await asyncio.sleep(0.01)
                assert calls == [{"digits": "1"}]
                assert transcripts == [("user", "press one"), ("agent", "okay")]
                assert not agent.fatal
            finally:
                starting.cancel()
                await asyncio.gather(starting, return_exceptions=True)
                await agent.stop()

    asyncio.run(run())
    assert "private task" not in caplog.text
    assert "fake-moss-key" not in caplog.text
    assert spoken[0] not in caplog.text
    assert f"MOSS briefing text_chars={len(spoken[0])} audio_ms=20" in caplog.text


@pytest.fixture
def fake_tts(monkeypatch):
    async def synthesize(_text):
        return b"\x01\x02" * 320
    monkeypatch.setattr("agentcall.agents.moss_agent.synthesize_briefing", synthesize)


async def finish_briefing(ws, received, starting):
    await next_matching(received, lambda value: isinstance(value, bytes) and any(value))
    await ws.send_json({"action": "response.audio.context", "data": {
        "chunk_id": "brief", "run_id": "brief", "turn_id": "brief",
    }})
    await ws.send_bytes(b"\x10\x20" * 480)
    await ws.send_json({"action": "response.ended", "data": {}})
    await asyncio.wait_for(starting, 2)
    await action(received, "playback.completed")


def test_stop_clears_queued_audio_counts_partial_writes_and_ignores_late_audio(fake_tts):
    async def run():
        async with moss_server() as (url, connected, received):
            agent = MossVoiceAgent("fake-moss-key", url)
            audio, cleared, transcripts = [], [], []
            agent.set_late_cut_handler(lambda: (audio.clear(), cleared.append(True), True)[-1])
            agent.set_transcript_handler(lambda *args: transcripts.append(args))
            starting = asyncio.create_task(agent.start(audio.append))
            ws = await connected
            try:
                await finish_briefing(ws, received, starting)
                context = {"chunk_id": "chunk", "run_id": "run", "turn_id": "turn"}
                await ws.send_json({"action": "response.audio.context", "data": context})
                await ws.send_bytes(b"\x10\x20" * 960)
                async with asyncio.timeout(2):
                    while not audio:
                        await asyncio.sleep(0.001)
                pcm = audio[0]
                pcm.on_played(160)
                await ws.send_json({"action": "response.audio.stop", "data": context})
                ack = await action(received, "playback.stopped")
                assert ack["played_audio_ms"] == 10
                assert cleared == [True] and not audio
                pcm.on_played(160)  # A stale callback cannot ACK a stopped chunk.
                await ws.send_json({"action": "response.audio.context", "data": {**context, "chunk_id": "late"}})
                await ws.send_bytes(b"\x10\x20" * 960)
                await ws.send_json({"action": "response.interrupted", "data": {**context, "text": "partial reply"}})
                await asyncio.sleep(0.02)
                assert not audio
                assert transcripts == [("agent", "partial reply")]
                assert not any(isinstance(item, dict) and item["action"] == "playback.chunk.completed"
                               for item in list(received._queue))
            finally:
                await agent.stop()
    asyncio.run(run())


@pytest.mark.parametrize("mode", [
    "reject", "tools", "disconnect", "timeout", "cancel", "response_failed", "brief_error",
])
def test_startup_failures_do_not_release_phone_audio_or_leak_tasks(fake_tts, monkeypatch, mode):
    monkeypatch.setattr("agentcall.agents.moss_agent._BRIEFING_RESPONSE_TIMEOUT", 0.05)

    async def run():
        async with moss_server(update_error="private rejection" if mode == "reject" else None,
                               omit_tools=mode == "tools") as (url, connected, received):
            agent = MossVoiceAgent("fake-moss-key", url)
            tools = ToolRegistry()
            tools.register(SEND_DTMF_SPEC, lambda _args: pytest.fail("Briefing must not execute tools"))
            agent.set_tools(tools)
            audio = []
            starting = asyncio.create_task(agent.start(audio.append))
            ws = await connected
            if mode in {"disconnect", "cancel"}:
                await action(received, "mic_state")
                if mode == "disconnect":
                    await ws.close()
                else:
                    starting.cancel()
            elif mode in {"response_failed", "brief_error"}:
                await next_matching(received, lambda value: isinstance(value, bytes) and any(value))
                if mode == "response_failed":
                    await ws.send_json({"action": "response.ended", "data": {"status": "failed"}})
                else:
                    await ws.send_json({"action": "error", "data": {"error": {"code": "briefing_rejected"}}})
            with pytest.raises(asyncio.CancelledError if mode == "cancel" else RuntimeError):
                await asyncio.wait_for(starting, 2)
            await agent.send_audio(b"not permitted")
            assert not audio and not agent._ready and not agent._uplink
            assert agent._ws is None and not agent._tasks
    asyncio.run(run())


def test_tool_only_reply_after_briefing_can_hang_up(fake_tts):
    from agentcall.agents.tools import HANGUP_SPEC

    async def run():
        async with moss_server() as (url, connected, received):
            agent = MossVoiceAgent("fake-moss-key", url)
            calls = []
            tools = ToolRegistry()
            tools.register(HANGUP_SPEC, lambda args: calls.append(args) or {"success": True})
            agent.set_tools(tools)
            starting = asyncio.create_task(agent.start(lambda _pcm: None))
            ws = await connected
            try:
                await finish_briefing(ws, received, starting)
                await ws.send_json({"action": "response.function_call_arguments.done", "data": {
                    "name": "hangup_call", "call_id": "hangup", "arguments": "{}",
                }})
                await action(received, "conversation.item.create")
                assert calls == [{}]
            finally:
                await agent.stop()
    asyncio.run(run())


def test_mac_briefing_uses_file_synthesis_and_16k_wav(monkeypatch):
    import wave

    from agentcall.agents import moss_agent

    monkeypatch.setattr(moss_agent.sys, "platform", "darwin")
    monkeypatch.setattr(moss_agent.shutil, "which", lambda name: f"/fake/{name}")
    commands = []
    pcm = b"\x00\x10" * 320

    async def command(*args, stdin=None):
        commands.append((args, stdin))
        if args[0] == "afconvert":
            with wave.open(args[-1], "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(16000)
                wav.writeframes(pcm)

    monkeypatch.setattr(moss_agent, "_command", command)
    assert asyncio.run(moss_agent.synthesize_briefing("private briefing")) == pcm
    assert commands[0][0][:3] == ("say", "-v", "Tingting")
    assert "-r" not in commands[0][0]
    assert commands[0][1] == b"private briefing"
    assert "private briefing" not in repr([args for args, _stdin in commands])
    assert commands[1][0][1:7] == ("-f", "WAVE", "-d", "LEI16@16000", "-c", "1")


def test_briefing_falls_back_to_existing_local_tts_and_resamples(monkeypatch):
    from agentcall.agents import local_agent, moss_agent

    class Models:
        sample_rate = 8000

        def synthesize(self, text):
            assert text == "private briefing"
            return b"\x00\x10" * 160

    monkeypatch.setattr(moss_agent.shutil, "which", lambda _name: None)
    monkeypatch.setattr(local_agent, "_get_shared_models", lambda: Models())
    assert asyncio.run(moss_agent.synthesize_briefing("private briefing")) == b"\x00\x10" * 320


def test_overlong_briefing_fails_before_opening_websocket():
    agent = MossVoiceAgent("fake-moss-key", "ws://unused")
    agent.set_session_instructions("x" * 8000)
    with pytest.raises(ValueError, match="8000"):
        asyncio.run(agent.start(lambda _pcm: None))
    assert agent._ws is None


def test_moss_dynamic_prompt_reuses_existing_text_backend(monkeypatch):
    from agentcall import prompt_gen

    prompt_gen.clear_cache()
    monkeypatch.setenv("PROMPT_GEN_ENABLED", "true")
    monkeypatch.setenv("LOCAL_LLM_PROVIDER", "dashscope")
    monkeypatch.setattr(prompt_gen, "_call_qwen", lambda *_args: ('{"scenario":"test scenario"}', None))
    try:
        result = prompt_gen.generate_prompt_scenario("10086", "test task", "zh", provider="moss")
        assert result["ok"] and result["scenario"] == "test scenario" and result["provider"] == "qwen"
    finally:
        prompt_gen.clear_cache()


@pytest.mark.parametrize("send_seconds", [0.005, 0.015])
def test_uplink_absolute_clock_has_no_long_term_backlog(monkeypatch, caplog, send_seconds):
    from agentcall.agents import moss_agent

    async def run():
        agent = MossVoiceAgent("fake-moss-key")
        agent._ready = True
        frame = b"\x01\x02" * 320
        now, produced, sent, peak = 0.0, 1, 0, 0
        await agent.send_audio(frame)

        async def advance(seconds):
            nonlocal now, produced, peak
            now += seconds
            while produced * 0.02 <= now + 1e-9:
                await agent.send_audio(frame)
                produced += 1
            peak = max(peak, len(agent._uplink))

        async def send(pcm):
            nonlocal sent
            assert pcm == frame
            sent += 1
            await advance(send_seconds)
            if sent == 6000:  # Two virtual minutes, with nonzero transport cost per frame.
                agent._ws = None

        monkeypatch.setattr(moss_agent, "time", SimpleNamespace(monotonic=lambda: now))
        monkeypatch.setattr(moss_agent, "asyncio", SimpleNamespace(sleep=advance))
        agent._ws = SimpleNamespace(send=send)
        await agent._pump_audio()
        assert not agent.fatal
        assert peak <= 1280
        assert produced - sent <= 1
        assert 119.9 < now < 120.1

    asyncio.run(run())
    assert "backlog capped" not in caplog.text


def test_uplink_catches_up_without_positive_sleep_above_100ms(monkeypatch):
    from agentcall.agents import moss_agent

    async def run():
        agent = MossVoiceAgent("fake-moss-key")
        frames = [bytes([i, 1]) * 320 for i in range(20)]
        agent._uplink.extend(b"".join(frames))
        now, sent, sleeps = [0.0], [], []

        async def sleep(seconds):
            sleeps.append((len(sent), seconds))
            now[0] += seconds

        async def send(pcm):
            sent.append(pcm)
            now[0] += 0.001
            if len(sent) == 20:
                agent._ws = None

        monkeypatch.setattr(moss_agent, "time", SimpleNamespace(monotonic=lambda: now[0]))
        monkeypatch.setattr(moss_agent, "asyncio", SimpleNamespace(sleep=sleep))
        agent._ws = SimpleNamespace(send=send)
        await agent._pump_audio()
        assert sent == frames
        assert all(delay == 0 for count, delay in sleeps if count < 15)
        assert now[0] < 0.15  # 400ms backlog caught up to the 100ms pacing threshold.

    asyncio.run(run())


def test_uplink_keeps_partial_frame_and_only_synthesizes_silence_when_empty(monkeypatch):
    from agentcall.agents import moss_agent

    async def run():
        agent = MossVoiceAgent("fake-moss-key")
        now, sent = [0.0], []

        async def sleep(seconds):
            now[0] += seconds
            if now[0] >= 0.04 and len(agent._uplink) == 320:
                agent._uplink.extend(b"\x03\x04" * 160)

        async def send(pcm):
            sent.append(pcm)
            if len(sent) == 1:
                agent._uplink.extend(b"\x01\x02" * 160)
            else:
                agent._ws = None

        monkeypatch.setattr(moss_agent, "time", SimpleNamespace(monotonic=lambda: now[0]))
        monkeypatch.setattr(moss_agent, "asyncio", SimpleNamespace(sleep=sleep))
        agent._ws = SimpleNamespace(send=send)
        await agent._pump_audio()
        assert sent == [bytes(640), b"\x01\x02" * 160 + b"\x03\x04" * 160]

    asyncio.run(run())


def test_uplink_overflow_is_bounded_and_warning_is_rate_limited(monkeypatch, caplog):
    from agentcall.agents import moss_agent

    now = [0.0]
    monkeypatch.setattr(moss_agent, "time", SimpleNamespace(monotonic=lambda: now[0]))
    agent = MossVoiceAgent("fake-moss-key")
    agent._ready = True

    async def run():
        await agent.send_audio(bytes(96000))
        for timestamp in (0.0, 0.02, 1.0, 4.99, 5.0, 5.02, 9.99, 10.0):
            now[0] = timestamp
            await agent.send_audio(b"\x01\x02" * 320)
            assert len(agent._uplink) == 96000

    asyncio.run(run())
    warnings = [record for record in caplog.records if "backlog capped" in record.message]
    assert len(warnings) == 3
    assert all("backlog_ms=3000" in record.message for record in warnings)


def test_briefing_contains_target_and_only_first_15_task_characters():
    from agentcall.agents.moss_agent import build_briefing

    task = "查询本机话费余额以及套餐内流量和超出套餐的费用"
    spoken = build_briefing("10086客服", task)
    assert spoken == (
        f"接下来你要替我给10086客服打电话{task[:15]}。"
        "电话那头的客服和语音菜单说的话，都是在跟你说，你要直接回应他们。"
        "听到让你按键时，就按要求按键。"
    )
    assert task not in spoken


def test_moss_observability_logs_events_and_tool_outcomes_without_payloads(caplog):
    caplog.set_level(logging.INFO)
    agent = MossVoiceAgent("fake-moss-key")
    tools = ToolRegistry()
    tools.register(SEND_DTMF_SPEC, lambda _args: {"success": True, "message": "private tool output"})
    agent.set_tools(tools)

    async def run():
        for briefing in (True, False):
            agent._briefing = briefing
            await agent._event("response.audio.started", {})
            await agent._event("output.transcript.done", {"text": "private transcript"})
            await agent._event("response.interrupted", {"text": "private partial"})
            await agent._event("response.ended", {"status": "completed"})
        await agent._event("error", {"error": {"code": "unsupported_action", "message": "fake-moss-key"}})
        await agent._tool({"name": "send_dtmf", "call_id": "ok", "arguments": '{"digits":"1"}'})
        await agent._tool({"name": "send_dtmf", "call_id": "bad", "arguments": "invalid-json"})

    asyncio.run(run())
    for message in ("MOSS response.audio.started", "MOSS output.transcript.done text_chars=18",
                    "MOSS response.interrupted text_chars=15", "MOSS response.ended status=completed",
                    "MOSS error code=unsupported_action", "MOSS tool requested name=send_dtmf",
                    "MOSS tool result name=send_dtmf success=True", "MOSS tool result name=send_dtmf success=False"):
        assert message in caplog.text
    for private in ("private transcript", "private partial", "private tool output", "fake-moss-key"):
        assert private not in caplog.text
