"""minimax_agent 单测：beta session 形态、协议怪癖、客户端 VAD 断句。

协议契约来自 2026-08-04 对 api.minimaxi.com 的真机探测（见模块 docstring），
本文件用 fake websocket 把那些实测结论固定成回归护栏——这些怪癖一旦被"顺手
改回 OpenAI 写法"，真机上会直接报 1000/2013 而不是静默降级。
"""

from __future__ import annotations

import asyncio
import json
import logging

import numpy as np
import pytest

from agentcall.agents import factory, minimax_agent
from agentcall.agents.minimax_agent import MiniMaxVoiceAgent
from agentcall.agents.tools import SEND_DTMF_SPEC, ToolRegistry


class _FakeWs:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.closed = False
        self._queue: asyncio.Queue = asyncio.Queue()

    async def send(self, data: str) -> None:
        if self.closed:
            raise ConnectionError("connection closed")
        self.sent.append(json.loads(data))

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

    def first(self, event_type: str) -> dict:
        for msg in self.sent:
            if msg.get("type") == event_type:
                return msg
        raise AssertionError(f"未发出 {event_type}；实际: {self.sent_types()}")


def _patch_connect(monkeypatch):
    instances: list[_FakeWs] = []
    calls: list[tuple[str, dict | None]] = []

    async def fake_connect(url, additional_headers=None, **kwargs):
        calls.append((url, additional_headers))
        ws = _FakeWs()
        instances.append(ws)
        return ws

    monkeypatch.setattr(minimax_agent.websockets, "connect", fake_connect)
    return instances, calls


def _make_agent(**kwargs) -> MiniMaxVoiceAgent:
    defaults = dict(
        api_key="sk-cp-test",
        model="abab6.5s-chat",
        model_display_name="MiniMax 测试",
        voice="female-shaonv",
    )
    defaults.update(kwargs)
    return MiniMaxVoiceAgent(**defaults)


def _pcm(rms: float, ms: float, rate: int = 24000) -> bytes:
    """生成指定 RMS 的定长 PCM（直流即可，本端 VAD 只看能量）。"""
    samples = int(rate * ms / 1000)
    return np.full(samples, int(rms), dtype="<i2").tobytes()


# ---- session 形态与协议怪癖 ----


def test_session_uses_beta_shape_with_string_token_cap(monkeypatch):
    """max_response_output_tokens 必须是字符串：传数字真机报 1000 unmarshal。"""
    instances, calls = _patch_connect(monkeypatch)
    agent = _make_agent()

    asyncio.run(agent.start(lambda _pcm: None))

    session = instances[0].first("session.update")["session"]
    assert session["modalities"] == ["audio", "text"]
    assert session["input_audio_format"] == "pcm16"
    assert session["output_audio_format"] == "pcm16"
    assert session["voice"] == "female-shaonv"
    assert isinstance(session["max_response_output_tokens"], str)
    # GA 专属字段绝不能出现，否则 MiniMax 侧解析失败
    assert "type" not in session
    assert "audio" not in session
    assert "output_modalities" not in session


def test_url_omits_model_query_param(monkeypatch):
    """MiniMax 忽略 ?model=，拼上去只会造成"已选模型"的错觉。"""
    _instances, calls = _patch_connect(monkeypatch)
    asyncio.run(_make_agent().start(lambda _pcm: None))

    url = calls[0][0]
    assert url == minimax_agent.DEFAULT_REALTIME_URL
    assert "model=" not in url


def test_url_override_used_verbatim(monkeypatch):
    _instances, calls = _patch_connect(monkeypatch)
    agent = _make_agent(realtime_url="wss://proxy.example/ws/v1/realtime")

    asyncio.run(agent.start(lambda _pcm: None))

    assert calls[0][0] == "wss://proxy.example/ws/v1/realtime"


def test_tools_are_not_sent_and_gap_is_logged(monkeypatch, caplog):
    """服务端丢弃 tools：不发才不会让日志假报"已注册工具"，并显式告警。"""
    instances, _calls = _patch_connect(monkeypatch)
    agent = _make_agent()
    registry = ToolRegistry()
    registry.register(SEND_DTMF_SPEC, lambda **_kwargs: {"success": True})
    agent.set_tools(registry)

    with caplog.at_level(logging.WARNING):
        asyncio.run(agent.start(lambda _pcm: None))

    assert "tools" not in instances[0].first("session.update")["session"]
    assert any("不支持工具调用" in record.getMessage() for record in caplog.records)


def test_say_writes_context_item_then_creates_response(monkeypatch):
    """response.create 不接受 instructions（真机报 2013 no context items）。"""
    instances, _calls = _patch_connect(monkeypatch)
    agent = _make_agent()

    async def scenario() -> None:
        await agent.start(lambda _pcm: None)
        await agent.say("请直接用中文说：您好")

    asyncio.run(scenario())

    ws = instances[0]
    assert ws.sent_types() == [
        "session.update", "conversation.item.create", "response.create",
    ]
    item = ws.first("conversation.item.create")["item"]
    # 三个必须项：status、input_text，以及 role 必须是 user——system item 虽能
    # 创建成功却不算 "chat content"，随后的 response.create 会报 2013。
    assert item["status"] == "completed"
    assert item["role"] == "user"
    assert item["content"][0]["type"] == "input_text"
    # 绝不能把 instructions 塞进 response.create
    assert "response" not in ws.first("response.create")


def test_external_tool_result_item_carries_status(monkeypatch):
    instances, _calls = _patch_connect(monkeypatch)
    agent = _make_agent()

    async def scenario() -> bool:
        await agent.start(lambda _pcm: None)
        return await agent.external_tool_result(
            "query_verification_code", {"success": True, "count": 1, "mode": "sms"},
            source="remote",
        )

    assert asyncio.run(scenario()) is True
    item = instances[0].first("conversation.item.create")["item"]
    assert item["status"] == "completed"
    assert item["content"][0]["type"] == "input_text"
    # 纯上下文，不得顺带触发回复
    assert "response.create" not in instances[0].sent_types()


def test_vibe_line_not_applied(monkeypatch):
    """说话 Vibe 是 OpenAI 专属；MiniMax 上追加只会污染提示词。"""
    instances, _calls = _patch_connect(monkeypatch)
    monkeypatch.setenv("OPENAI_VIBE", "cheerful")
    agent = _make_agent()
    agent.set_session_instructions("你是电话助手。")

    asyncio.run(agent.start(lambda _pcm: None))

    assert instances[0].first("session.update")["session"]["instructions"] == "你是电话助手。"


# ---- 客户端 VAD（MiniMax 无服务端 VAD，不断句就全程沉默）----


def test_silence_after_speech_triggers_commit_and_response(monkeypatch):
    instances, _calls = _patch_connect(monkeypatch)
    monkeypatch.setenv("MINIMAX_VAD_RMS_THRESHOLD", "400")
    monkeypatch.setenv("MANUAL_RESPONSE_SILENCE_MS", "300")
    monkeypatch.setenv("MANUAL_RESPONSE_MAX_WAIT_MS", "8000")
    agent = _make_agent()

    async def scenario() -> None:
        await agent.start(lambda _pcm: None)
        for _ in range(5):                       # 说话 100ms
            await agent.send_audio(_pcm(3000, 20))
        for _ in range(10):                      # 静默 200ms —— 还不够
            await agent.send_audio(_pcm(0, 20))
        assert "input_audio_buffer.commit" not in instances[0].sent_types()
        for _ in range(6):                       # 再静默 120ms —— 越过 300ms
            await agent.send_audio(_pcm(0, 20))

    asyncio.run(scenario())

    types = instances[0].sent_types()
    assert types.count("input_audio_buffer.commit") == 1
    assert types.index("input_audio_buffer.commit") < types.index("response.create")


def test_silence_without_speech_never_commits(monkeypatch):
    """没人说话就 commit 会提交空 buffer，服务端报错且白跑一轮。"""
    instances, _calls = _patch_connect(monkeypatch)
    monkeypatch.setenv("MINIMAX_VAD_RMS_THRESHOLD", "400")
    monkeypatch.setenv("MANUAL_RESPONSE_SILENCE_MS", "100")
    agent = _make_agent()

    async def scenario() -> None:
        await agent.start(lambda _pcm: None)
        for _ in range(50):
            await agent.send_audio(_pcm(0, 20))

    asyncio.run(scenario())

    assert "input_audio_buffer.commit" not in instances[0].sent_types()


def test_long_utterance_force_commits(monkeypatch):
    """对方一直不停也要让 AI 插得上话，否则永远等不到断句。"""
    instances, _calls = _patch_connect(monkeypatch)
    monkeypatch.setenv("MINIMAX_VAD_RMS_THRESHOLD", "400")
    monkeypatch.setenv("MANUAL_RESPONSE_SILENCE_MS", "100000")   # 静默判据不可能触发
    monkeypatch.setenv("MANUAL_RESPONSE_MAX_WAIT_MS", "200")
    agent = _make_agent()

    async def scenario() -> None:
        await agent.start(lambda _pcm: None)
        for _ in range(15):                      # 连续说 300ms，越过 200ms 上限
            await agent.send_audio(_pcm(3000, 20))

    asyncio.run(scenario())

    assert instances[0].sent_types().count("input_audio_buffer.commit") == 1


def test_no_second_commit_while_response_in_flight(monkeypatch):
    """回复在飞时再断句会并发多个 response，对方听到重叠语音。"""
    instances, _calls = _patch_connect(monkeypatch)
    monkeypatch.setenv("MINIMAX_VAD_RMS_THRESHOLD", "400")
    monkeypatch.setenv("MANUAL_RESPONSE_SILENCE_MS", "40")
    monkeypatch.setenv("MANUAL_RESPONSE_MAX_WAIT_MS", "8000")
    agent = _make_agent()

    async def scenario() -> None:
        await agent.start(lambda _pcm: None)
        for _ in range(3):
            await agent.send_audio(_pcm(3000, 20))
        for _ in range(4):
            await agent.send_audio(_pcm(0, 20))          # 第一次断句
        for _ in range(3):
            await agent.send_audio(_pcm(3000, 20))
        for _ in range(4):
            await agent.send_audio(_pcm(0, 20))          # 在飞中，不应再断
        assert instances[0].sent_types().count("input_audio_buffer.commit") == 1
        agent._on_response_done()                        # 轮次结束后才放开
        for _ in range(3):
            await agent.send_audio(_pcm(3000, 20))
        for _ in range(4):
            await agent.send_audio(_pcm(0, 20))

    asyncio.run(scenario())

    assert instances[0].sent_types().count("input_audio_buffer.commit") == 2


def test_commit_failure_releases_in_flight_flag(monkeypatch):
    """断线时若不放开在飞标志，重连后永远不再断句 —— AI 从此沉默。"""
    instances, _calls = _patch_connect(monkeypatch)
    monkeypatch.setenv("MINIMAX_VAD_RMS_THRESHOLD", "400")
    monkeypatch.setenv("MANUAL_RESPONSE_SILENCE_MS", "40")
    agent = _make_agent()

    async def scenario() -> None:
        await agent.start(lambda _pcm: None)
        instances[0].closed = True               # send 抛 ConnectionError
        for _ in range(3):
            await agent.send_audio(_pcm(3000, 20))
        for _ in range(4):
            await agent.send_audio(_pcm(0, 20))

    asyncio.run(scenario())

    assert agent._vad_response_in_flight is False


def test_frame_rms_tolerates_odd_length():
    """奇数字节直接喂 np.frombuffer(int16) 会抛，桥上出现过半个采样。"""
    assert MiniMaxVoiceAgent._frame_rms(b"\x00") == 0.0
    assert MiniMaxVoiceAgent._frame_rms(b"") == 0.0
    odd = _pcm(1000, 20) + b"\x01"
    assert MiniMaxVoiceAgent._frame_rms(odd) == pytest.approx(1000, rel=0.01)


# ---- 工厂 ----


def test_factory_builds_minimax_agent(monkeypatch):
    monkeypatch.setenv("MINIMAX_API_KEY", "sk-cp-test")
    monkeypatch.setenv("AGENT_PROVIDER", "minimax")

    agent = factory.create_agent()

    assert isinstance(agent, MiniMaxVoiceAgent)
    assert agent.input_rate == 24000 and agent.output_rate == 24000
    assert agent.reconnect_max_key == "MINIMAX_RECONNECT_MAX"


def test_factory_missing_key_fails_fast(monkeypatch):
    monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
    monkeypatch.setenv("AGENT_PROVIDER", "minimax")

    with pytest.raises(KeyError):
        factory.create_agent()
