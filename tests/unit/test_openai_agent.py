"""openai_agent 单测：session 配置、音频收发、转写、工具往返、say 与断线 fatal。

无真实 OPENAI_API_KEY，全部走 fake websocket；协议实现基于官方文档，
待真实 key 验证。
"""

from __future__ import annotations

import asyncio
import base64
import json

import pytest

from agentcall.agents import factory, openai_agent
from agentcall.agents.openai_agent import OpenAIVoiceAgent
from agentcall.agents.tools import ToolRegistry


# ---------------------------------------------------------------------------
# 夹具：伪造 websocket 与 websockets.connect
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# 连接与 session.update
# ---------------------------------------------------------------------------


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
            # URL 与鉴权 header（beta 兼容 header 必须在）
            url, headers = calls[0]
            assert url == "wss://api.openai.com/v1/realtime?model=gpt-realtime-mini"
            assert headers["Authorization"] == "Bearer sk-test"
            assert headers["OpenAI-Beta"] == "realtime=v1"

            # 首条消息即 session.update
            ws = instances[0]
            assert ws.sent_types()[0] == "session.update"
            session = ws.sent[0]["session"]
            assert session["voice"] == "alloy"
            assert session["turn_detection"] == {"type": "server_vad"}
            assert session["input_audio_format"] == "pcm16"
            assert session["output_audio_format"] == "pcm16"
            assert session["input_audio_transcription"] == {
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


def test_realtime_url_override(monkeypatch):
    """OPENAI_REALTIME_URL 覆盖 base（大陆用户反代/Azure 兼容端点）。"""
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
    assert agent.model == "gpt-realtime-mini"  # 注册表默认
    assert agent.model_display_name == "OpenAI Realtime Mini"
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
