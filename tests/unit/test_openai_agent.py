"""openai_agent 单测：session 配置、音频收发、转写、工具往返、say 与断线 fatal。

无真实 OPENAI_API_KEY，全部走 fake websocket；协议实现基于官方文档，
待真实 key 验证。
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging

import pytest

from agentcall.agents import factory, openai_agent
from agentcall.agents.openai_agent import OpenAIVoiceAgent
from agentcall.agents.tools import SEND_DTMF_SPEC, ToolRegistry

# ---------------------------------------------------------------------------
# 夹具：伪造 websocket 与 websockets.connect
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _serial_agent_by_default(monkeypatch):
    monkeypatch.setenv("PARALLEL_AGENT_CONNECT", "false")


class _FakeWs:
    """记录 send 的 JSON；服务端事件经 feed 注入，finish 结束迭代。"""

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.closed = False
        self._queue: asyncio.Queue = asyncio.Queue()

    async def send(self, data: str) -> None:
        if self.closed:
            raise ConnectionError("connection closed")
        self.sent.append(json.loads(data))

    def feed(self, event: dict) -> None:
        self._queue.put_nowait(json.dumps(event))

    def finish(self) -> None:
        """模拟服务端关闭：迭代结束。"""
        self._queue.put_nowait(None)

    def __aiter__(self):
        return self

    async def __anext__(self):
        item = await self._queue.get()
        if item is None:
            raise StopAsyncIteration
        return item

    async def close(self) -> None:
        self.closed = True
        self._queue.put_nowait(None)

    def sent_types(self) -> list[str]:
        return [msg.get("type") for msg in self.sent]


def _patch_connect(monkeypatch, fail_times: int = 0):
    """替换 websockets.connect；返回 (连接实例列表, 调用参数列表)。"""
    instances: list[_FakeWs] = []
    calls: list[tuple[str, dict | None]] = []
    state = {"fail_remaining": fail_times}

    async def fake_connect(url, additional_headers=None, **kwargs):
        calls.append((url, additional_headers))
        if state["fail_remaining"] > 0:
            state["fail_remaining"] -= 1
            raise OSError("模拟连接失败")
        ws = _FakeWs()
        instances.append(ws)
        return ws

    monkeypatch.setattr(openai_agent.websockets, "connect", fake_connect)
    return instances, calls


def _make_agent(**kwargs) -> OpenAIVoiceAgent:
    defaults = dict(
        api_key="sk-test",
        model="gpt-realtime-mini",
        model_display_name="OpenAI 测试",
        voice="alloy",
    )
    defaults.update(kwargs)
    return OpenAIVoiceAgent(**defaults)


async def _drain() -> None:
    """让接收任务把已注入的事件处理完。"""
    for _ in range(20):
        await asyncio.sleep(0)


def test_parallel_response_owner_reserves_until_done_and_coalesces_vad(monkeypatch):
    monkeypatch.setenv("PARALLEL_AGENT_CONNECT", "true")
    monkeypatch.setenv("MANUAL_RESPONSE_CONTROL", "false")
    instances, _calls = _patch_connect(monkeypatch)

    async def run():
        agent = _make_agent()
        await agent.start(lambda _pcm: None)
        ws = instances[0]
        assert ws.sent[0]["session"]["audio"]["input"]["turn_detection"]["create_response"] is False
        await agent.say("opening")
        second = asyncio.create_task(agent.say("follow-up"))
        await _drain()
        assert ws.sent_types().count("response.create") == 1
        ws.feed({"type": "response.created", "response": {"id": "opening"}})
        await _drain()
        assert not second.done()
        ws.feed({"type": "response.done", "response": {"id": "opening", "status": "completed"}})
        await _drain()
        await asyncio.wait_for(second, 1)
        ws.feed({"type": "response.created", "response": {"id": "second"}})
        ws.feed({"type": "input_audio_buffer.speech_stopped"})
        ws.feed({"type": "input_audio_buffer.speech_stopped"})
        await _drain()
        assert ws.sent_types().count("response.create") == 2
        ws.feed({"type": "response.done", "response": {"id": "old-response", "status": "completed"}})
        await _drain()
        assert ws.sent_types().count("response.create") == 2
        ws.feed({"type": "response.done", "response": {"id": "second", "status": "completed"}})
        await _drain()
        assert ws.sent_types().count("response.create") == 3
        await agent.stop()

    asyncio.run(run())


def test_parallel_manual_watchdog_does_not_relabel_active_response_idle(monkeypatch):
    monkeypatch.setenv("PARALLEL_AGENT_CONNECT", "true")
    monkeypatch.setenv("MANUAL_RESPONSE_CONTROL", "true")
    instances, _calls = _patch_connect(monkeypatch)

    async def run():
        agent = _make_agent()
        await agent.start(lambda _pcm: None)
        ws = instances[0]
        await agent.say("opening")
        agent._handle_event({"type": "response.created", "response": {"id": "active"}})
        agent._fire_manual_response_watchdog(agent._manual_response_watchdog_generation)
        assert agent._manual_response_outstanding == 1
        pending = asyncio.create_task(agent._send_manual_response(ws))
        await _drain()
        assert ws.sent_types().count("response.create") == 1
        agent._handle_event({"type": "response.done", "response": {"id": "active", "status": "completed"}})
        await asyncio.wait_for(pending, 1)
        assert ws.sent_types().count("response.create") == 2
        await agent.stop()

    asyncio.run(run())


@pytest.mark.parametrize("close_failure", ["stall", "error"])
def test_parallel_stop_aborts_stalled_close_and_collects_response_tasks(monkeypatch, close_failure):
    monkeypatch.setenv("PARALLEL_AGENT_CONNECT", "true")
    monkeypatch.setattr(openai_agent, "_PARALLEL_CLOSE_TIMEOUT_SECONDS", 0.05, raising=False)
    instances, _calls = _patch_connect(monkeypatch)

    async def run():
        agent = _make_agent()
        await agent.start(lambda _pcm: None)
        ws = instances[0]
        aborted = []

        class Transport:
            def abort(self):
                aborted.append(True)
                ws.finish()

        ws.transport = Transport()

        async def stalled_close():
            if close_failure == "error":
                raise ConnectionError("fake close failure")
            await asyncio.Event().wait()

        ws.close = stalled_close
        await agent.say("opening")
        agent._handle_event({"type": "input_audio_buffer.speech_stopped"})
        await _drain()
        owned = list(agent._background_tasks) + [agent._recv_task, agent._deaf_task]
        await asyncio.wait_for(agent.stop(), timeout=0.4)
        assert aborted
        assert all(task.done() for task in owned)
        assert agent._ws is None and agent._recv_task is None and agent._deaf_task is None
        assert not agent._background_tasks

    asyncio.run(run())


def test_parallel_ack_during_send_cancels_the_correct_watchdog(monkeypatch):
    monkeypatch.setenv("PARALLEL_AGENT_CONNECT", "true")
    instances, _calls = _patch_connect(monkeypatch)

    async def run():
        agent = _make_agent()
        await agent.start(lambda _pcm: None)
        ws = instances[0]
        original_send = ws.send

        async def send(data):
            await original_send(data)
            agent._handle_event({"type": "response.created", "response": {"id": "during-send"}})
            await asyncio.sleep(0)

        ws.send = send
        await agent.say("opening")
        await _drain()
        assert agent._response_ack_task.cancelled()
        pending = asyncio.create_task(agent._send_manual_response(ws))
        await _drain()
        assert pending in agent._background_tasks
        await agent.stop()
        assert pending.done()

    asyncio.run(run())


def test_parallel_ack_timeout_releases_admission_waiters(monkeypatch):
    monkeypatch.setenv("PARALLEL_AGENT_CONNECT", "true")
    instances, _calls = _patch_connect(monkeypatch)

    async def run():
        agent = _make_agent()
        await agent.start(lambda _pcm: None)
        ws = instances[0]
        await agent.say("opening")
        pending = asyncio.create_task(agent.say("clarification"))
        await _drain()
        monkeypatch.setattr(openai_agent, "_MANUAL_RESPONSE_CREATED_TIMEOUT_SECONDS", 0)
        await agent._watch_response_ack(ws, agent._response_request_generation)
        await asyncio.wait_for(pending, 0.3)
        assert agent.fatal
        assert ws.sent_types().count("response.create") == 1
        await agent.stop()

    asyncio.run(run())


def test_parallel_queued_vad_response_waits_for_new_speech_to_stop(monkeypatch):
    monkeypatch.setenv("PARALLEL_AGENT_CONNECT", "true")
    monkeypatch.setenv("MANUAL_RESPONSE_CONTROL", "false")
    instances, _calls = _patch_connect(monkeypatch)

    async def run():
        agent = _make_agent()
        await agent.start(lambda _pcm: None)
        ws = instances[0]
        await agent.say("opening")
        agent._handle_event({"type": "response.created", "response": {"id": "opening"}})
        agent._handle_event({"type": "input_audio_buffer.speech_stopped"})
        await _drain()
        agent._handle_event({"type": "input_audio_buffer.speech_started"})
        agent._handle_event({"type": "response.done", "response": {"id": "opening", "status": "cancelled"}})
        await _drain()
        assert ws.sent_types().count("response.create") == 1
        agent._handle_event({"type": "input_audio_buffer.speech_stopped"})
        await _drain()
        assert ws.sent_types().count("response.create") == 2
        await agent.stop()

    asyncio.run(run())


def test_parallel_failed_session_update_closes_candidate_before_retry(monkeypatch):
    monkeypatch.setenv("PARALLEL_AGENT_CONNECT", "true")
    sockets = [_FakeWs(), _FakeWs()]
    calls = []

    async def failed_send(_data):
        raise ConnectionError("fake session update failure")

    sockets[0].send = failed_send

    async def connect(*_args, **_kwargs):
        socket = sockets[len(calls)]
        calls.append(socket)
        return socket

    monkeypatch.setattr(openai_agent.websockets, "connect", connect)

    async def run():
        agent = _make_agent()
        agent._running = True
        assert await agent._reconnect()
        assert sockets[0].closed
        await agent.stop()
        assert sockets[1].closed

    asyncio.run(run())


def test_parallel_tool_continuation_and_nudge_share_response_admission(monkeypatch):
    monkeypatch.setenv("PARALLEL_AGENT_CONNECT", "true")
    instances, _calls = _patch_connect(monkeypatch)

    async def run():
        agent = _make_agent()
        await agent.start(lambda _pcm: None)
        ws = instances[0]
        await agent.say("opening")
        agent._handle_event({"type": "response.created", "response": {"id": "opening"}})
        continuation = asyncio.create_task(agent._dispatch_tool_call("fake-tool", "call-1", "{}", ws))
        await _drain()
        assert ws.sent_types().count("response.create") == 1
        agent._handle_event({"type": "response.done", "response": {"id": "opening", "status": "completed"}})
        await asyncio.wait_for(continuation, 1)
        assert ws.sent_types().count("response.create") == 2
        agent._handle_event({"type": "response.created", "response": {"id": "tool"}})
        agent._nudge_after_repeat_suppressed("not logged")
        await _drain()
        assert ws.sent_types().count("response.create") == 2
        agent._handle_event({"type": "response.done", "response": {"id": "tool", "status": "completed"}})
        await _drain()
        assert ws.sent_types().count("response.create") == 3
        await agent.stop()

    asyncio.run(run())


def test_parallel_background_send_failure_is_observed_without_logging_content(monkeypatch, caplog):
    monkeypatch.setenv("PARALLEL_AGENT_CONNECT", "true")
    instances, _calls = _patch_connect(monkeypatch)

    async def run():
        agent = _make_agent()
        await agent.start(lambda _pcm: None)

        async def fail(_data):
            raise ConnectionError("private-content")

        instances[0].send = fail
        agent._handle_event({"type": "input_audio_buffer.speech_stopped"})
        await _drain()
        assert agent.fatal
        await agent.stop()

    with caplog.at_level(logging.WARNING):
        asyncio.run(run())
    assert "后台任务失败" in caplog.text
    assert "private-content" not in caplog.text


async def _wait_for_sent_count(
    ws: _FakeWs,
    event_type: str,
    expected: int,
    *,
    timeout: float = 2.0,
) -> None:
    """Wait for timer-thread work without assuming runner scheduling speed."""

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while ws.sent_types().count(event_type) < expected and loop.time() < deadline:
        await asyncio.sleep(0.01)
    assert ws.sent_types().count(event_type) == expected


# ---------------------------------------------------------------------------
# 连接与 session.update
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("cancel", [False, True])
def test_parallel_failed_session_update_closes_acquired_socket(monkeypatch, cancel):
    monkeypatch.setenv("PARALLEL_AGENT_CONNECT", "true")

    async def run():
        ws = _FakeWs()
        entered = asyncio.Event()

        async def send(_data):
            entered.set()
            if cancel:
                await asyncio.Event().wait()
            raise RuntimeError("session.update failed")

        async def connect(*_args, **_kwargs):
            return ws

        monkeypatch.setattr(ws, "send", send)
        monkeypatch.setattr(openai_agent.websockets, "connect", connect)
        agent = _make_agent()
        task = asyncio.create_task(agent.start(lambda _pcm: None))
        await entered.wait()
        if cancel:
            task.cancel()
        with pytest.raises(asyncio.CancelledError if cancel else RuntimeError):
            await task
        await agent.stop()
        assert ws.closed

    asyncio.run(run())


def test_start_sends_session_update_with_expected_fields(monkeypatch):
    instances, calls = _patch_connect(monkeypatch)
    agent = _make_agent()
    registry = ToolRegistry()
    registry.register(
        {
            "type": "function",
            "function": {
                "name": "send_sms",
                "description": "发短信",
                "parameters": {"type": "object", "properties": {}},
            },
        },
        lambda args: {"success": True},
    )
    agent.set_tools(registry)

    async def scenario():
        await agent.start(lambda pcm: None)
        try:
            # URL 与鉴权 header（GA 形态不发 OpenAI-Beta 头）
            url, headers = calls[0]
            assert url == "wss://api.openai.com/v1/realtime?model=gpt-realtime-mini"
            assert headers["Authorization"] == "Bearer sk-test"
            assert "OpenAI-Beta" not in headers

            # 首条消息即 session.update（GA 嵌套 audio 结构）
            ws = instances[0]
            assert ws.sent_types()[0] == "session.update"
            session = ws.sent[0]["session"]
            assert session["type"] == "realtime"
            assert session["output_modalities"] == ["audio"]
            audio = session["audio"]
            assert audio["output"]["voice"] == "alloy"
            # 默认下发注册表里的静默判停窗（OPENAI_VAD_SILENCE_MS=300，电话场景提速）。
            assert audio["input"]["turn_detection"] == {
                "type": "server_vad",
                "silence_duration_ms": 300,
            }
            assert audio["input"]["format"] == {"type": "audio/pcm", "rate": 24000}
            assert audio["output"]["format"] == {"type": "audio/pcm", "rate": 24000}
            assert audio["input"]["transcription"] == {
                "model": openai_agent.TRANSCRIPTION_MODEL
            }
            assert session["instructions"]  # 默认系统提示词非空
            # 工具规格摊平成 OpenAI 扁平格式（name 提到顶层）
            assert session["tools"] == [
                {
                    "type": "function",
                    "name": "send_sms",
                    "description": "发短信",
                    "parameters": {"type": "object", "properties": {}},
                }
            ]
        finally:
            await agent.stop()

    asyncio.run(scenario())


def test_session_instructions_override_default(monkeypatch):
    instances, _calls = _patch_connect(monkeypatch)
    agent = _make_agent()
    agent.set_session_instructions("外呼任务：确认预约")

    async def scenario():
        await agent.start(lambda pcm: None)
        try:
            session = instances[0].sent[0]["session"]
            assert session["instructions"] == "外呼任务：确认预约"
            assert "tools" not in session  # 未注册工具时不带 tools 字段
        finally:
            await agent.stop()

    asyncio.run(scenario())


def test_openai_vibe_appends_to_session_instructions(monkeypatch):
    """OPENAI_VIBE 非空时，其风格补充追加在会话 instructions 末尾（VOICE_STYLE 之后）。"""
    monkeypatch.setenv("AGENT_LANGUAGE", "zh")
    monkeypatch.setenv("OPENAI_VIBE", "cheerful")
    instances, _calls = _patch_connect(monkeypatch)
    agent = _make_agent()
    agent.set_session_instructions("外呼任务：确认预约")

    async def scenario():
        await agent.start(lambda pcm: None)
        try:
            session = instances[0].sent[0]["session"]
            assert session["instructions"] == (
                "外呼任务：确认预约\n机主希望的说话风格补充：cheerful。"
            )
        finally:
            await agent.stop()

    asyncio.run(scenario())


def test_openai_vibe_empty_leaves_instructions_unchanged(monkeypatch):
    """OPENAI_VIBE 为空(默认)时不追加，instructions 与传入保持一致。"""
    monkeypatch.setenv("OPENAI_VIBE", "")
    instances, _calls = _patch_connect(monkeypatch)
    agent = _make_agent()
    agent.set_session_instructions("外呼任务：确认预约")

    async def scenario():
        await agent.start(lambda pcm: None)
        try:
            session = instances[0].sent[0]["session"]
            assert session["instructions"] == "外呼任务：确认预约"
        finally:
            await agent.stop()

    asyncio.run(scenario())


def test_manual_response_control_sets_create_response_false_and_debounces(monkeypatch):
    monkeypatch.setenv("MANUAL_RESPONSE_CONTROL", "true")
    monkeypatch.setenv("MANUAL_RESPONSE_SILENCE_MS", "25")
    monkeypatch.setenv("MANUAL_RESPONSE_MAX_WAIT_MS", "500")
    instances, _calls = _patch_connect(monkeypatch)
    agent = _make_agent()

    async def scenario():
        await agent.start(lambda pcm: None)
        try:
            ws = instances[0]
            turn_detection = ws.sent[0]["session"]["audio"]["input"]["turn_detection"]
            assert turn_detection == {
                "type": "server_vad",
                "silence_duration_ms": 300,
                "create_response": False,
            }
            for text in ("第一段菜单", "第二段菜单", "第三段菜单"):
                ws.feed(
                    {
                        "type": "conversation.item.input_audio_transcription.completed",
                        "transcript": text,
                    }
                )
                await asyncio.sleep(0.01)
            await _wait_for_sent_count(ws, "response.create", 1)
            creates = [item for item in ws.sent if item["type"] == "response.create"]
            assert creates == [{"type": "response.create"}]
        finally:
            await agent.stop()

    asyncio.run(scenario())


def test_manual_response_control_waits_for_active_response_done(monkeypatch):
    monkeypatch.setenv("MANUAL_RESPONSE_CONTROL", "true")
    monkeypatch.setenv("MANUAL_RESPONSE_SILENCE_MS", "20")
    monkeypatch.setenv("MANUAL_RESPONSE_MAX_WAIT_MS", "500")
    instances, _calls = _patch_connect(monkeypatch)
    agent = _make_agent()

    async def scenario():
        await agent.start(lambda pcm: None)
        try:
            ws = instances[0]
            ws.feed({"type": "response.created", "response": {"id": "r1"}})
            ws.feed(
                {
                    "type": "conversation.item.input_audio_transcription.completed",
                    "transcript": "AI 说话期间 IVR 继续播报",
                }
            )
            await _drain()
            await asyncio.sleep(0.05)
            assert "response.create" not in ws.sent_types()

            ws.feed({"type": "response.done", "response": {"id": "r1"}})
            await _drain()
            await _wait_for_sent_count(ws, "response.create", 1)
        finally:
            await agent.stop()

    asyncio.run(scenario())


def test_manual_response_control_stop_cancels_pending_timer(monkeypatch):
    monkeypatch.setenv("MANUAL_RESPONSE_CONTROL", "true")
    monkeypatch.setenv("MANUAL_RESPONSE_SILENCE_MS", "80")
    monkeypatch.setenv("MANUAL_RESPONSE_MAX_WAIT_MS", "500")
    instances, _calls = _patch_connect(monkeypatch)
    agent = _make_agent()

    async def scenario():
        await agent.start(lambda pcm: None)
        ws = instances[0]
        ws.feed(
            {
                "type": "conversation.item.input_audio_transcription.completed",
                "transcript": "稍后才该回复",
            }
        )
        await _drain()
        await agent.stop()
        await asyncio.sleep(0.12)
        assert "response.create" not in ws.sent_types()

    asyncio.run(scenario())


def test_manual_response_control_request_watchdog_recovers_missing_created(
    monkeypatch,
):
    monkeypatch.setenv("MANUAL_RESPONSE_CONTROL", "true")
    monkeypatch.setenv("MANUAL_RESPONSE_SILENCE_MS", "15")
    monkeypatch.setenv("MANUAL_RESPONSE_MAX_WAIT_MS", "0")
    monkeypatch.setattr(
        openai_agent, "_MANUAL_RESPONSE_CREATED_TIMEOUT_SECONDS", 0.04
    )
    instances, _calls = _patch_connect(monkeypatch)
    agent = _make_agent()

    async def scenario():
        await agent.start(lambda pcm: None)
        try:
            ws = instances[0]
            ws.feed(
                {
                    "type": "conversation.item.input_audio_transcription.completed",
                    "transcript": "第一段播报",
                }
            )
            await _drain()
            await _wait_for_sent_count(ws, "response.create", 1)

            ws.feed(
                {
                    "type": "conversation.item.input_audio_transcription.completed",
                    "transcript": "created 丢失后的新播报",
                }
            )
            await _drain()
            await _wait_for_sent_count(ws, "response.create", 2)
        finally:
            await agent.stop()

    asyncio.run(scenario())


def test_manual_response_control_max_wait_forces_response(monkeypatch):
    monkeypatch.setenv("MANUAL_RESPONSE_CONTROL", "true")
    monkeypatch.setenv("MANUAL_RESPONSE_SILENCE_MS", "1000")
    monkeypatch.setenv("MANUAL_RESPONSE_MAX_WAIT_MS", "45")
    instances, _calls = _patch_connect(monkeypatch)
    agent = _make_agent()

    async def scenario():
        await agent.start(lambda pcm: None)
        try:
            ws = instances[0]
            for index in range(5):
                ws.feed(
                    {
                        "type": "conversation.item.input_audio_transcription.completed",
                        "transcript": f"连续播报 {index}",
                    }
                )
                await asyncio.sleep(0.01)
            await _wait_for_sent_count(ws, "response.create", 1)
        finally:
            await agent.stop()

    asyncio.run(scenario())


def test_realtime_url_override(monkeypatch):
    """OPENAI_REALTIME_URL 覆盖 base（反向代理/Azure 兼容端点）。"""
    _instances, calls = _patch_connect(monkeypatch)
    agent = _make_agent(realtime_url="wss://proxy.example.com/v1/realtime")

    async def scenario():
        await agent.start(lambda pcm: None)
        await agent.stop()

    asyncio.run(scenario())
    assert calls[0][0] == "wss://proxy.example.com/v1/realtime?model=gpt-realtime-mini"


def test_realtime_url_with_model_used_verbatim(monkeypatch):
    """覆盖 URL 已自带 model 参数（如 Azure）时原样使用，不重复拼接。"""
    _instances, calls = _patch_connect(monkeypatch)
    url = "wss://azure.example.com/openai/realtime?deployment=x&model=gpt-realtime"
    agent = _make_agent(realtime_url=url)

    async def scenario():
        await agent.start(lambda pcm: None)
        await agent.stop()

    asyncio.run(scenario())
    assert calls[0][0] == url


# ---------------------------------------------------------------------------
# 音频收发
# ---------------------------------------------------------------------------


def test_send_audio_appends_base64(monkeypatch):
    instances, _calls = _patch_connect(monkeypatch)
    agent = _make_agent()

    async def scenario():
        await agent.start(lambda pcm: None)
        try:
            await agent.send_audio(b"\x01\x02\x03\x04")
            await agent.send_audio(b"")  # 空帧不发送
        finally:
            await agent.stop()

    asyncio.run(scenario())
    ws = instances[0]
    appends = [m for m in ws.sent if m["type"] == "input_audio_buffer.append"]
    assert len(appends) == 1
    assert base64.b64decode(appends[0]["audio"]) == b"\x01\x02\x03\x04"


@pytest.mark.parametrize(
    "event_name", ["response.audio.delta", "response.output_audio.delta"]
)
def test_audio_delta_both_event_names_produce_audio(monkeypatch, event_name):
    """beta 与 GA 两种下行音频事件名都必须出声。"""
    instances, _calls = _patch_connect(monkeypatch)
    agent = _make_agent()
    received: list[bytes] = []

    async def scenario():
        await agent.start(received.append)
        try:
            instances[0].feed({
                "type": event_name,
                "delta": base64.b64encode(b"\x10\x20").decode("ascii"),
            })
            await _drain()
        finally:
            await agent.stop()

    asyncio.run(scenario())
    assert received == [b"\x10\x20"]


# ---------------------------------------------------------------------------
# 转写回调
# ---------------------------------------------------------------------------


def test_transcripts_emitted_for_user_and_agent(monkeypatch):
    instances, _calls = _patch_connect(monkeypatch)
    agent = _make_agent()
    transcripts: list[tuple[str, str]] = []
    agent.set_transcript_handler(lambda role, text: transcripts.append((role, text)))

    async def scenario():
        await agent.start(lambda pcm: None)
        try:
            ws = instances[0]
            ws.feed({
                "type": "conversation.item.input_audio_transcription.completed",
                "transcript": " 你好 ",
            })
            ws.feed({
                "type": "response.audio_transcript.done",  # beta 事件名
                "transcript": "您好，我是助理",
            })
            ws.feed({
                "type": "response.output_audio_transcript.done",  # GA 事件名
                "transcript": "请讲",
            })
            ws.feed({
                "type": "response.audio_transcript.done",
                "transcript": "",  # 空转写不回调
            })
            await _drain()
        finally:
            await agent.stop()

    asyncio.run(scenario())
    assert transcripts == [
        ("user", "你好"),
        ("agent", "您好，我是助理"),
        ("agent", "请讲"),
    ]


def test_repetitive_agent_response_audio_is_suppressed(monkeypatch, caplog):
    """复读判定后，该 response 的后续音频分块不得再输出（streaming 语义，WIL-112）。"""
    monkeypatch.setenv("REPEAT_SUPPRESS_SIMILARITY", "0.9")
    instances, _calls = _patch_connect(monkeypatch)
    agent = _make_agent()
    received: list[bytes] = []
    first = "您好，我是张三的数字分身，想咨询一下套餐情况。"
    repeated = "您好！我是张三的数字分身，想咨询一下套餐情况"

    async def scenario():
        await agent.start(received.append)
        try:
            instances[0].feed({
                "type": "response.output_audio.delta",
                "response_id": "r1",
                "delta": base64.b64encode(b"first").decode("ascii"),
            })
            instances[0].feed({
                "type": "response.output_audio_transcript.done",
                "response_id": "r1",
                "transcript": first,
            })
            await _drain()
            assert received == [b"first"]

            with caplog.at_level("INFO"):
                instances[0].feed({
                    "type": "response.output_audio.delta",
                    "response_id": "r2",
                    "delta": base64.b64encode(b"repeat").decode("ascii"),
                })
                instances[0].feed({
                    "type": "response.output_audio_transcript.done",
                    "response_id": "r2",
                    "transcript": repeated,
                })
                instances[0].feed({
                    "type": "response.output_audio.delta",
                    "response_id": "r3",
                    "delta": base64.b64encode(b"repeat-again").decode("ascii"),
                })
                instances[0].feed({
                    "type": "response.output_audio_transcript.done",
                    "response_id": "r3",
                    "transcript": repeated,
                })
                # 判定为复读之后，同 response 的迟到分块必须被丢弃。
                instances[0].feed({
                    "type": "response.output_audio.delta",
                    "response_id": "r3",
                    "delta": base64.b64encode(b"late-tail").decode("ascii"),
                })
                await _drain()
        finally:
            await agent.stop()

    asyncio.run(scenario())
    # streaming 语义：判定前到达的分块（含 r3 首块）已放行，迟到分块被丢弃。
    assert received == [b"first", b"repeat", b"repeat-again"]
    assert "抑制复读" in caplog.text
    assert any(
        msg.get("type") == "response.create"
        and "换一种说法" in msg.get("response", {}).get("instructions", "")
        for msg in instances[0].sent
    )


# ---------------------------------------------------------------------------
# 工具调用往返
# ---------------------------------------------------------------------------


def test_tool_call_round_trip(monkeypatch):
    instances, _calls = _patch_connect(monkeypatch)
    agent = _make_agent()
    dispatched: list[tuple[str, dict]] = []
    registry = ToolRegistry()
    registry.register(
        {
            "type": "function",
            "function": {
                "name": "send_sms",
                "description": "发短信",
                "parameters": {"type": "object", "properties": {}},
            },
        },
        lambda args: (dispatched.append(("send_sms", args)) or {"success": True, "message": "已发送"}),
    )
    agent.set_tools(registry)

    async def scenario():
        await agent.start(lambda pcm: None)
        try:
            event = {
                "type": "response.function_call_arguments.done",
                "name": "send_sms",
                "call_id": "call-1",
                "arguments": json.dumps({"content": "你好"}),
            }
            instances[0].feed(event)
            instances[0].feed(event)  # 重复事件按 call_id 去重
            await _drain()
            await asyncio.sleep(0.05)  # 等 to_thread 的工具执行完
        finally:
            await agent.stop()

    asyncio.run(scenario())

    # 工具只执行一次，参数已解析
    assert dispatched == [("send_sms", {"content": "你好"})]

    # 回传 function_call_output + response.create
    ws = instances[0]
    outputs = [m for m in ws.sent if m["type"] == "conversation.item.create"]
    assert len(outputs) == 1
    item = outputs[0]["item"]
    assert item["type"] == "function_call_output"
    assert item["call_id"] == "call-1"
    assert json.loads(item["output"]) == {"success": True, "message": "已发送"}
    # function_call_output 之后必须跟 response.create 让模型接着说
    idx = ws.sent.index(outputs[0])
    assert ws.sent[idx + 1] == {"type": "response.create"}


def test_invalid_tool_arguments_log_omits_raw_payload(caplog):
    agent = _make_agent()
    ws = _FakeWs()
    agent._ws = ws
    arguments = '{"digits":"73#"'

    with caplog.at_level(logging.WARNING):
        asyncio.run(
            agent._dispatch_tool_call(
                "send_dtmf", "call-invalid-json", arguments, ws
            )
        )

    assert "73#" not in caplog.text
    assert arguments not in caplog.text
    assert "send_dtmf" in caplog.text
    assert f"arguments_length={len(arguments)}" in caplog.text
    assert "error_type=JSONDecodeError" in caplog.text


def test_terminal_tool_skips_response_create(monkeypatch):
    """hangup_call 是终结性工具：回传结果后不得再 response.create。

    否则模型会在挂断延迟里多说一句（如“电话已经挂断…”），对端听到多余的话。
    告别语已按提示词要求在调用 hangup_call 之前说完。
    """
    from agentcall.agents.tools import HANGUP_SPEC

    instances, _calls = _patch_connect(monkeypatch)
    agent = _make_agent()
    registry = ToolRegistry()
    registry.register(HANGUP_SPEC, lambda args: {"success": True, "message": "ok"})
    agent.set_tools(registry)

    async def scenario():
        await agent.start(lambda pcm: None)
        try:
            instances[0].feed({
                "type": "response.function_call_arguments.done",
                "name": "hangup_call",
                "call_id": "call-bye",
                "arguments": "{}",
            })
            await _drain()
            await asyncio.sleep(0.05)  # 等 to_thread 的工具执行完
        finally:
            await agent.stop()

    asyncio.run(scenario())

    ws = instances[0]
    # 工具结果照常回传
    outputs = [m for m in ws.sent if m["type"] == "conversation.item.create"]
    assert len(outputs) == 1
    assert outputs[0]["item"]["type"] == "function_call_output"
    # 但绝不能再要新回复（本会话开场没有 say，故整通 sent 里不应出现 response.create）
    assert "response.create" not in ws.sent_types()


def test_send_dtmf_result_waits_for_next_ivr_without_response_create(monkeypatch):
    instances, _calls = _patch_connect(monkeypatch)
    agent = _make_agent()
    registry = ToolRegistry()
    registry.register(
        SEND_DTMF_SPEC,
        lambda args: {"success": True, "count": 1, "mode": "inband"},
    )
    agent.set_tools(registry)

    async def scenario():
        await agent.start(lambda pcm: None)
        try:
            instances[0].feed({
                "type": "response.function_call_arguments.done",
                "name": "send_dtmf",
                "call_id": "call-key",
                "arguments": '{"digits":"1"}',
            })
            await _drain()
            await asyncio.sleep(0.05)
        finally:
            await agent.stop()

    asyncio.run(scenario())

    ws = instances[0]
    outputs = [m for m in ws.sent if m["type"] == "conversation.item.create"]
    assert len(outputs) == 1
    assert outputs[0]["item"]["type"] == "function_call_output"
    assert "response.create" not in ws.sent_types()


def test_external_tool_result_adds_system_fact_without_fake_call_or_response(monkeypatch):
    instances, _calls = _patch_connect(monkeypatch)
    agent = _make_agent()

    async def scenario():
        await agent.start(lambda pcm: None)
        try:
            accepted = await agent.external_tool_result(
                "send_dtmf",
                {"success": True, "count": 4, "mode": "inband"},
                source="spoken_followup",
            )
            assert accepted is True
        finally:
            await agent.stop()

    asyncio.run(scenario())

    ws = instances[0]
    created = [item for item in ws.sent if item["type"] == "conversation.item.create"]
    assert len(created) == 1
    item = created[0]["item"]
    assert item["type"] == "message"
    assert item["role"] == "system"
    assert item["content"][0]["type"] == "input_text"
    assert "send_dtmf" in item["content"][0]["text"]
    assert "call_id" not in item
    assert "response.create" not in ws.sent_types()


def test_tool_result_dropped_when_connection_replaced(monkeypatch):
    """工具执行期间断线重连：旧 call_id 的结果不得发进新会话（codex review P1）。"""
    import threading

    monkeypatch.setenv("OPENAI_RECONNECT_MAX", "2")
    instances, _calls = _patch_connect(monkeypatch)
    agent = _make_agent()
    release = threading.Event()
    registry = ToolRegistry()
    registry.register(
        {
            "type": "function",
            "function": {"name": "slow_tool", "description": "慢工具",
                         "parameters": {"type": "object", "properties": {}}},
        },
        lambda args: (release.wait(timeout=5), {"success": True})[1],
    )
    agent.set_tools(registry)

    async def scenario():
        await agent.start(lambda pcm: None)
        try:
            instances[0].feed({
                "type": "response.function_call_arguments.done",
                "name": "slow_tool",
                "call_id": "call-old",
                "arguments": "{}",
            })
            await _drain()          # 工具已在线程中阻塞
            instances[0].finish()   # 断线 → 重连成新连接
            await _drain()
            assert len(instances) == 2
            release.set()           # 放行工具，结果应被丢弃
            await asyncio.sleep(0.1)
        finally:
            await agent.stop()

    asyncio.run(scenario())
    for ws in instances:
        assert "conversation.item.create" not in ws.sent_types()


def test_tool_dispatch_exception_returns_error_output(monkeypatch):
    """工具分发自身异常也必须回错误结果，避免模型永远等待（codex review P1）。"""
    instances, _calls = _patch_connect(monkeypatch)
    agent = _make_agent()
    registry = ToolRegistry()
    registry.register(
        {
            "type": "function",
            "function": {"name": "boom", "description": "",
                         "parameters": {"type": "object", "properties": {}}},
        },
        lambda args: {"success": True},
    )
    monkeypatch.setattr(
        registry, "dispatch",
        lambda name, args: (_ for _ in ()).throw(RuntimeError("分发炸了")),
    )
    agent.set_tools(registry)

    async def scenario():
        await agent.start(lambda pcm: None)
        try:
            instances[0].feed({
                "type": "response.function_call_arguments.done",
                "name": "boom",
                "call_id": "call-x",
                "arguments": "{}",
            })
            await _drain()
            await asyncio.sleep(0.05)
        finally:
            await agent.stop()

    asyncio.run(scenario())
    ws = instances[0]
    outputs = [m for m in ws.sent if m["type"] == "conversation.item.create"]
    assert len(outputs) == 1
    result = json.loads(outputs[0]["item"]["output"])
    assert result["success"] is False
    assert "工具执行异常" in result["message"]


# ---------------------------------------------------------------------------
# say
# ---------------------------------------------------------------------------


def test_say_sends_response_create_with_instructions(monkeypatch):
    instances, _calls = _patch_connect(monkeypatch)
    agent = _make_agent()

    async def scenario():
        await agent.start(lambda pcm: None)
        try:
            await agent.say("请用中文做开场白")
        finally:
            await agent.stop()

    asyncio.run(scenario())
    ws = instances[0]
    creates = [m for m in ws.sent if m["type"] == "response.create"]
    assert creates == [
        {"type": "response.create", "response": {"instructions": "请用中文做开场白"}}
    ]


# ---------------------------------------------------------------------------
# 断线重连与 fatal
# ---------------------------------------------------------------------------


def test_disconnect_reconnect_success_says_notice(monkeypatch):
    """运行中断线 → 重连成功 → 发安抚语，音频恢复，fatal 保持 False。"""
    monkeypatch.setenv("OPENAI_RECONNECT_MAX", "2")
    instances, _calls = _patch_connect(monkeypatch)
    agent = _make_agent()

    async def scenario():
        await agent.start(lambda pcm: None)
        try:
            instances[0].finish()  # 模拟服务端断开
            await _drain()
            assert len(instances) == 2  # 已建立新连接
            new_ws = instances[1]
            assert new_ws.sent_types()[0] == "session.update"
            assert {
                "type": "response.create",
                "response": {"instructions": openai_agent.RECONNECT_NOTICE},
            } in new_ws.sent
            # 重连后音频走新连接
            await agent.send_audio(b"\x05\x06")
            assert "input_audio_buffer.append" in new_ws.sent_types()
            assert agent.fatal is False
        finally:
            await agent.stop()

    asyncio.run(scenario())


def test_disconnect_reconnect_all_fail_sets_fatal(monkeypatch):
    """运行中断线且重连全败 → fatal=True，主循环得以收尾整通电话。"""
    monkeypatch.setenv("OPENAI_RECONNECT_MAX", "2")
    instances, calls = _patch_connect(monkeypatch)
    agent = _make_agent()

    async def scenario():
        await agent.start(lambda pcm: None)
        try:
            monkeypatch.setattr(
                openai_agent.websockets,
                "connect",
                _always_fail_connect(calls),
            )
            instances[0].finish()
            assert agent._recv_task is not None
            await asyncio.wait_for(agent._recv_task, timeout=5)
        finally:
            await agent.stop()

    asyncio.run(scenario())
    assert agent.fatal is True
    # 初始 1 次 + 重连 2 次
    assert len(calls) == 3


def _always_fail_connect(calls):
    async def fail_connect(url, additional_headers=None, **kwargs):
        calls.append((url, additional_headers))
        raise OSError("模拟网络不可达")

    return fail_connect


def test_stop_keeps_fatal_false(monkeypatch):
    """主动 stop 收尾路径不误置 fatal，也不触发重连。"""
    instances, calls = _patch_connect(monkeypatch)
    agent = _make_agent()

    async def scenario():
        await agent.start(lambda pcm: None)
        await agent.stop()

    asyncio.run(scenario())
    assert agent.fatal is False
    assert instances[0].closed
    assert len(calls) == 1  # 没有重连


def test_send_audio_failure_drops_frame(monkeypatch):
    """连接刚死时 send_audio 静默丢帧，不抛异常（重连由接收循环负责）。"""
    instances, _calls = _patch_connect(monkeypatch)
    agent = _make_agent()

    async def scenario():
        await agent.start(lambda pcm: None)
        try:
            instances[0].closed = True  # send 会抛 ConnectionError
            await agent.send_audio(b"\x01\x02")  # 不应向外抛
        finally:
            await agent.stop()

    asyncio.run(scenario())


def test_say_failure_does_not_raise(monkeypatch):
    """断线窗口内 say 失败只告警，不把异常抛给开场白路径（codex review P2）。"""
    instances, _calls = _patch_connect(monkeypatch)
    agent = _make_agent()

    async def scenario():
        await agent.start(lambda pcm: None)
        try:
            instances[0].closed = True
            await agent.say("开场白")  # 不应向外抛
        finally:
            await agent.stop()

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# 工厂与凭证校验
# ---------------------------------------------------------------------------


def test_factory_creates_openai_agent(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("OPENAI_VOICE", "coral")
    monkeypatch.setenv("OPENAI_REALTIME_URL", "wss://proxy.example.com/v1/realtime")
    agent = factory.create_agent("openai")
    assert isinstance(agent, OpenAIVoiceAgent)
    assert agent.api_key == "sk-test"
    assert agent.model == "gpt-realtime-2.1-mini"  # 注册表默认
    assert agent.model_display_name == "OpenAI Realtime"
    assert agent.voice == "coral"
    assert agent.realtime_url == "wss://proxy.example.com/v1/realtime"
    assert agent.input_rate == 24000
    assert agent.output_rate == 24000


def test_factory_openai_missing_key_fails_fast(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(KeyError):
        factory.create_agent("openai")


def test_factory_unknown_provider_mentions_openai():
    with pytest.raises(ValueError) as excinfo:
        factory.create_agent("gpt")
    assert "openai" in str(excinfo.value)


# ---- barge-in：speech_started 打断 ----

def test_barge_in_speech_started_cancels_and_fires_interrupt(monkeypatch):
    """回调接受打断时：对端开口 → 发 response.cancel（仅回复生成中）+ 触发回调。"""
    instances, _calls = _patch_connect(monkeypatch)
    agent = _make_agent()
    interrupts: list[int] = []

    def accept_interrupt() -> bool:
        interrupts.append(1)
        return True

    agent.set_user_interrupt_handler(accept_interrupt)

    async def scenario():
        await agent.start(lambda pcm: None)
        try:
            ws = instances[0]
            ws.feed({"type": "response.created", "response": {"id": "r1"}})
            ws.feed({"type": "input_audio_buffer.speech_started"})
            await _drain()
            await _wait_for_sent_count(ws, "response.cancel", 1)
            assert interrupts
        finally:
            await agent.stop()

    asyncio.run(scenario())


def test_barge_in_no_active_response_skips_cancel_but_fires_interrupt(monkeypatch):
    """回复已结束（response.done 后）对端开口：不发 cancel（避免无谓 error），仍清积压。"""
    instances, _calls = _patch_connect(monkeypatch)
    agent = _make_agent()
    interrupts: list[int] = []

    def accept_interrupt() -> bool:
        interrupts.append(1)
        return True

    agent.set_user_interrupt_handler(accept_interrupt)

    async def scenario():
        await agent.start(lambda pcm: None)
        try:
            ws = instances[0]
            ws.feed({"type": "response.created", "response": {"id": "r1"}})
            ws.feed({"type": "response.done", "response": {"id": "r1"}})
            ws.feed({"type": "input_audio_buffer.speech_started"})
            await _drain()
            await asyncio.sleep(0.05)
            assert "response.cancel" not in ws.sent_types()
            assert interrupts
        finally:
            await agent.stop()

    asyncio.run(scenario())


def test_barge_in_rejected_by_handler_skips_cancel(monkeypatch):
    """回调拒绝打断（DTMF 护窗让位 / 自激兜底，WIL-94）：即使回复生成中也不 cancel。"""
    instances, _calls = _patch_connect(monkeypatch)
    agent = _make_agent()
    interrupts: list[int] = []

    def reject_interrupt() -> bool:
        interrupts.append(1)
        return False

    agent.set_user_interrupt_handler(reject_interrupt)

    async def scenario():
        await agent.start(lambda pcm: None)
        try:
            ws = instances[0]
            ws.feed({"type": "response.created", "response": {"id": "r1"}})
            ws.feed({"type": "input_audio_buffer.speech_started"})
            await _drain()
            await asyncio.sleep(0.05)
            assert "response.cancel" not in ws.sent_types()
            assert interrupts, "回调必须被问到（由它记录让位原因）"
        finally:
            await agent.stop()

    asyncio.run(scenario())


def test_speech_started_without_handler_stays_noop(monkeypatch):
    """未注册回调（半双工模式）：speech_started 维持原忽略行为，不发 cancel。"""
    instances, _calls = _patch_connect(monkeypatch)
    agent = _make_agent()

    async def scenario():
        await agent.start(lambda pcm: None)
        try:
            ws = instances[0]
            ws.feed({"type": "response.created", "response": {"id": "r1"}})
            ws.feed({"type": "input_audio_buffer.speech_started"})
            await _drain()
            await asyncio.sleep(0.05)
            assert "response.cancel" not in ws.sent_types()
        finally:
            await agent.stop()

    asyncio.run(scenario())


def test_vad_silence_zero_omits_silence_duration(monkeypatch):
    """OPENAI_VAD_SILENCE_MS<=0 时不下发 silence_duration_ms，用服务端默认。"""
    monkeypatch.setenv("OPENAI_VAD_SILENCE_MS", "0")
    instances, _calls = _patch_connect(monkeypatch)
    agent = _make_agent()

    async def scenario():
        await agent.start(lambda pcm: None)
        try:
            ws = instances[0]
            turn_detection = ws.sent[0]["session"]["audio"]["input"]["turn_detection"]
            assert turn_detection == {"type": "server_vad"}
        finally:
            await agent.stop()

    asyncio.run(scenario())


def test_turn_latency_logged_between_speech_stopped_and_first_audio(monkeypatch, caplog):
    """逐轮延迟度量：speech_stopped → 首个音频增量，打一条 latency 日志后复位。"""
    import logging as _logging

    instances, _calls = _patch_connect(monkeypatch)
    agent = _make_agent()

    async def scenario():
        await agent.start(lambda pcm: None)
        try:
            ws = instances[0]
            with caplog.at_level(_logging.INFO):
                ws.feed({"type": "input_audio_buffer.speech_stopped"})
                ws.feed(
                    {
                        "type": "response.output_audio.delta",
                        "response": {"id": "r1"},
                        "delta": base64.b64encode(b"\x00\x00").decode(),
                    }
                )
                ws.feed(  # 第二个增量不应再打延迟日志
                    {
                        "type": "response.output_audio.delta",
                        "response": {"id": "r1"},
                        "delta": base64.b64encode(b"\x00\x00").decode(),
                    }
                )
                await _drain()
            assert caplog.text.count("轮次响应延迟") == 1
        finally:
            await agent.stop()

    asyncio.run(scenario())


def test_turn_latency_emitted_via_latency_handler(monkeypatch):
    """判停→首音频经 set_latency_handler 上交（WIL-95 第一期：落盘管道）。"""
    instances, _calls = _patch_connect(monkeypatch)
    agent = _make_agent()
    samples: list[tuple[str, float]] = []
    agent.set_latency_handler(lambda stage, ms: samples.append((stage, ms)))

    async def scenario():
        await agent.start(lambda pcm: None)
        try:
            ws = instances[0]
            ws.feed({"type": "input_audio_buffer.speech_stopped"})
            ws.feed(
                {
                    "type": "response.output_audio.delta",
                    "response": {"id": "r1"},
                    "delta": base64.b64encode(b"\x00\x00").decode(),
                }
            )
            ws.feed(  # 第二个增量不应再产生样本（每轮一条）
                {
                    "type": "response.output_audio.delta",
                    "response": {"id": "r1"},
                    "delta": base64.b64encode(b"\x00\x00").decode(),
                }
            )
            await _drain()
            assert len(samples) == 1
            stage, ms = samples[0]
            assert stage == "first_audio_delta"
            assert ms >= 0
        finally:
            await agent.stop()

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# 失聪看门狗（WIL-122）
# ---------------------------------------------------------------------------

_VOICED_FRAME = b"\x00\x02" * 240  # int16=512，超过有声阈值 350
_SILENT_FRAME = b"\x00\x00" * 240


def test_deaf_watchdog_warns_then_reconnects_on_event_silence(monkeypatch):
    """有声上行持续但服务端零事件：先落 provider_deaf 告警，超限主动断开借道重连。"""
    instances, _calls = _patch_connect(monkeypatch)
    agent = _make_agent()
    samples: list[tuple[str, float]] = []
    agent.set_latency_handler(lambda stage, ms: samples.append((stage, ms)))

    async def scenario():
        await agent.start(lambda pcm: None)
        try:
            ws = instances[0]
            now = openai_agent.time.monotonic()
            await agent.send_audio(_VOICED_FRAME)  # 喂「有声上行」时钟
            # 事件静默超过告警阈但未到重连阈：只告警不断开
            agent._last_event_at = now - openai_agent._DEAF_WARN_SECONDS - 1
            await agent._deaf_tick(now)
            assert [s for s, _ in samples] == ["provider_deaf"]
            assert not ws.closed
            # 再次 tick 不重复告警（同一段失聪期只告警一次）
            await agent._deaf_tick(now)
            assert [s for s, _ in samples] == ["provider_deaf"]
            # 静默达到重连阈：主动 close，交给既有断线重连路径
            agent._last_event_at = now - openai_agent._DEAF_RECONNECT_SECONDS - 1
            await agent._deaf_tick(now)
            assert "provider_deaf_reconnect" in [s for s, _ in samples]
            assert ws.closed
        finally:
            await agent.stop()

    asyncio.run(scenario())


def test_deaf_watchdog_quiet_uplink_never_triggers(monkeypatch):
    """近窗口只有纯静默上行（安静等待）：服务端久无事件也不算失聪。"""
    instances, _calls = _patch_connect(monkeypatch)
    agent = _make_agent()
    samples: list[tuple[str, float]] = []
    agent.set_latency_handler(lambda stage, ms: samples.append((stage, ms)))

    async def scenario():
        await agent.start(lambda pcm: None)
        try:
            now = openai_agent.time.monotonic()
            await agent.send_audio(_SILENT_FRAME)  # 静默帧不喂有声时钟
            agent._last_event_at = now - openai_agent._DEAF_RECONNECT_SECONDS - 1
            await agent._deaf_tick(now)
            assert samples == []
            assert not instances[0].closed
        finally:
            await agent.stop()

    asyncio.run(scenario())


def test_deaf_watchdog_event_resets_warned_flag(monkeypatch):
    """任何服务端事件都喂时钟并复位告警位：恢复后再失聪要重新告警。"""
    instances, _calls = _patch_connect(monkeypatch)
    agent = _make_agent()

    async def scenario():
        await agent.start(lambda pcm: None)
        try:
            agent._deaf_warned = True
            before = agent._last_event_at
            instances[0].feed({"type": "rate_limits.updated"})
            await _drain()
            assert agent._deaf_warned is False
            assert agent._last_event_at is not None
            assert before is None or agent._last_event_at >= before
        finally:
            await agent.stop()

    asyncio.run(scenario())
