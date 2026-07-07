"""doubao_agent 单测：P0 豆包会话不可恢复感知（fatal）+ factory say 限制提示。"""

from __future__ import annotations

import asyncio
import logging

import websockets
from websockets.frames import Close

from agentcall.agents import factory
from agentcall.agents.doubao_agent import DoubaoVoiceAgent


def _make_agent() -> DoubaoVoiceAgent:
    return DoubaoVoiceAgent(
        app_id="app",
        access_key="key",
        resource_id="volc.speech.dialog",
        app_key="appkey",
        model_display_name="豆包测试",
    )


# ---------------------------------------------------------------------------
# 夹具：伪造 websocket，只需支撑 _recv_loop 的 async for 迭代
# ---------------------------------------------------------------------------


class _FakeWs:
    """迭代产出 frames 后按 final 收场：None=正常结束，异常实例=抛出。"""

    def __init__(self, frames: list[bytes] | None = None, final: Exception | None = None):
        self._frames = list(frames or [])
        self._final = final

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._frames:
            return self._frames.pop(0)
        if self._final is not None:
            raise self._final
        raise StopAsyncIteration


def _closed_error() -> websockets.exceptions.ConnectionClosedError:
    return websockets.exceptions.ConnectionClosedError(Close(1006, "abnormal"), None)


# ---------------------------------------------------------------------------
# fatal：接收循环运行中退出 = 会话不可恢复
# ---------------------------------------------------------------------------


def test_recv_loop_exception_sets_fatal():
    """接收循环运行中异常退出 → fatal=True，主循环得以感知会话已死。"""
    agent = _make_agent()
    agent._running = True
    agent._ws = _FakeWs(final=RuntimeError("模拟接收异常"))
    asyncio.run(agent._recv_loop())
    assert agent.fatal is True


def test_recv_loop_connection_closed_sets_fatal():
    """通话进行中服务端异常断开（ConnectionClosed）→ fatal=True。"""
    agent = _make_agent()
    agent._running = True
    agent._ws = _FakeWs(final=_closed_error())
    asyncio.run(agent._recv_loop())
    assert agent.fatal is True


def test_recv_loop_server_end_sets_fatal():
    """通话进行中服务端正常关闭（迭代结束）同样不可恢复 → fatal=True。"""
    agent = _make_agent()
    agent._running = True
    agent._ws = _FakeWs()
    asyncio.run(agent._recv_loop())
    assert agent.fatal is True


def test_recv_loop_after_stop_keeps_fatal_false():
    """主动 stop 收尾路径（_running 已置 False）不误置 fatal。"""
    agent = _make_agent()
    agent._running = False
    agent._ws = _FakeWs(final=_closed_error())
    asyncio.run(agent._recv_loop())
    assert agent.fatal is False


# ---------------------------------------------------------------------------
# factory：豆包 say 未实现的显式提示
# ---------------------------------------------------------------------------


def test_factory_warns_doubao_say_unsupported(monkeypatch, caplog):
    monkeypatch.setenv("DOUBAO_APP_ID", "app")
    monkeypatch.setenv("DOUBAO_ACCESS_KEY", "key")
    with caplog.at_level(logging.WARNING, logger="agentcall.agents.factory"):
        agent = factory.create_agent("doubao")
    assert isinstance(agent, DoubaoVoiceAgent)
    assert any("say 未实现" in record.message for record in caplog.records)
