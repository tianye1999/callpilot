"""qwen_agent 单测：P2-3 连接预热 + P2-4 断线重连。"""

from __future__ import annotations

import asyncio
import base64
import threading
import time
from queue import Empty

from agentcall.agents import qwen_agent

# ---------------------------------------------------------------------------
# 夹具：伪造 OmniRealtimeConversation
# ---------------------------------------------------------------------------


def _make_fake_conversation_cls():
    """每个测试独立一份类属性，避免 instances 串味。"""

    class FakeConversation:
        instances: list = []
        fail_connect = False

        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs
            self.connected = False
            self.closed = False
            self.appended: list[str] = []
            self.responses: list[dict] = []
            self.session_kwargs: dict | None = None
            type(self).instances.append(self)

        def connect(self) -> None:
            if type(self).fail_connect:
                raise RuntimeError("模拟连接失败")
            self.connected = True

        def update_session(self, **kwargs) -> None:
            self.session_kwargs = kwargs

        def append_audio(self, audio_b64: str) -> None:
            self.appended.append(audio_b64)

        def create_response(self, **kwargs) -> None:
            self.responses.append(kwargs)

        def close(self) -> None:
            self.closed = True

    return FakeConversation


def _start_agent(monkeypatch, fake_cls) -> qwen_agent.QwenVoiceAgent:
    monkeypatch.setattr(qwen_agent, "OmniRealtimeConversation", fake_cls)
    agent = qwen_agent.QwenVoiceAgent(
        api_key="test-key",
        model="qwen-omni-turbo-realtime",
        model_display_name="千问测试",
    )
    asyncio.run(agent.start(lambda chunk: None))
    return agent


# ---------------------------------------------------------------------------
# P2-4 断线重连
# ---------------------------------------------------------------------------


def test_reconnect_success_after_disconnect(monkeypatch):
    """断线 → send_audio 触发后台重连 → 成功后 say 安抚语、音频恢复。"""
    monkeypatch.setenv("QWEN_RECONNECT_MAX", "2")
    fake_cls = _make_fake_conversation_cls()
    agent = _start_agent(monkeypatch, fake_cls)
    try:
        assert len(fake_cls.instances) == 1

        # 模拟运行中被动断线：回调线程收到 on_close
        agent._callback.on_close(1006, "abnormal closure")
        assert agent._disconnected.is_set()

        # 断线后的第一帧音频：静默丢弃并触发后台重连
        asyncio.run(agent.send_audio(b"\x01\x02"))
        assert agent._reconnect_thread is not None
        agent._reconnect_thread.join(timeout=5)
        assert not agent._reconnect_thread.is_alive()

        # 重连成功：新 conversation 已连接并完成 update_session
        assert not agent._disconnected.is_set()
        assert len(fake_cls.instances) == 2
        new_conv = fake_cls.instances[-1]
        assert new_conv.connected
        assert new_conv.session_kwargs is not None
        assert agent._conversation is new_conv

        # say 被调：安抚语通过新连接的 create_response 发出
        assert any(
            r.get("instructions") == qwen_agent.RECONNECT_NOTICE
            for r in new_conv.responses
        )

        # 断线期间那帧音频被静默丢弃（没进任何连接）
        assert fake_cls.instances[0].appended == []
        assert new_conv.appended == []

        # 重连后音频恢复发送
        asyncio.run(agent.send_audio(b"\x03\x04"))
        assert len(new_conv.appended) == 1

        # 下行泵线程未被断线杀死
        assert agent._pump_thread is not None and agent._pump_thread.is_alive()
    finally:
        asyncio.run(agent.stop())


def test_reconnect_gives_up_after_max(monkeypatch):
    """重连超限后不再尝试，维持原有关闭行为（下行泵线程退出）。"""
    monkeypatch.setenv("QWEN_RECONNECT_MAX", "1")
    fake_cls = _make_fake_conversation_cls()
    agent = _start_agent(monkeypatch, fake_cls)
    try:
        fake_cls.fail_connect = True
        agent._callback.on_close(1006, "abnormal closure")
        asyncio.run(agent.send_audio(b"\x01\x02"))
        assert agent._reconnect_thread is not None
        agent._reconnect_thread.join(timeout=5)

        # 1 次重连尝试 = _connect_session 内部 QWEN_CONNECT_MAX_ATTEMPTS 次 connect
        expected = 1 + qwen_agent.QWEN_CONNECT_MAX_ATTEMPTS
        assert len(fake_cls.instances) == expected
        assert agent._disconnected.is_set()
        assert agent._reconnect_attempts == 1

        # 超限后再送音频：静默丢弃，不再拉起新的重连线程
        prev_thread = agent._reconnect_thread
        asyncio.run(agent.send_audio(b"\x05\x06"))
        assert agent._reconnect_thread is prev_thread
        assert len(fake_cls.instances) == expected

        # 维持原有关闭行为：audio_queue 收到 None，下行泵线程退出
        assert agent._pump_thread is not None
        agent._pump_thread.join(timeout=2)
        assert not agent._pump_thread.is_alive()
    finally:
        asyncio.run(agent.stop())


def test_on_close_after_stop_keeps_original_behavior(monkeypatch):
    """主动 stop 后的 on_close 不触发重连标记（维持原关闭路径）。"""
    fake_cls = _make_fake_conversation_cls()
    agent = _start_agent(monkeypatch, fake_cls)
    asyncio.run(agent.stop())
    agent._callback.on_close(1000, "normal closure")
    assert not agent._disconnected.is_set()
    assert agent._reconnect_thread is None


# ---------------------------------------------------------------------------
# P2-3 连接预热
# ---------------------------------------------------------------------------


class _FakeSocket:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class _FakeSSLContext:
    def __init__(self) -> None:
        self.server_hostnames: list[str | None] = []

    def wrap_socket(self, sock, server_hostname=None):
        self.server_hostnames.append(server_hostname)
        return _FakeSocket()


def _patch_handshake(monkeypatch):
    """mock socket/ssl，返回 (连接地址记录, ssl 上下文)。"""
    calls: list[tuple] = []

    def fake_create_connection(address, timeout=None):
        calls.append((address, timeout))
        return _FakeSocket()

    context = _FakeSSLContext()
    monkeypatch.setattr(
        qwen_agent.socket, "create_connection", fake_create_connection
    )
    monkeypatch.setattr(
        qwen_agent.ssl, "create_default_context", lambda: context
    )
    return calls, context


def test_prewarm_default_host(monkeypatch):
    monkeypatch.delenv("DASHSCOPE_REALTIME_URL", raising=False)
    calls, context = _patch_handshake(monkeypatch)

    elapsed = qwen_agent.prewarm_connection()

    assert isinstance(elapsed, float) and elapsed >= 0
    assert calls == [(("dashscope.aliyuncs.com", 443), calls[0][1])]
    assert context.server_hostnames == ["dashscope.aliyuncs.com"]


def test_prewarm_host_from_env_url(monkeypatch):
    monkeypatch.setenv(
        "DASHSCOPE_REALTIME_URL",
        "wss://dashscope-intl.aliyuncs.com/api-ws/v1/realtime?model=x",
    )
    calls, context = _patch_handshake(monkeypatch)

    elapsed = qwen_agent.prewarm_connection()

    assert elapsed is not None
    assert calls[0][0] == ("dashscope-intl.aliyuncs.com", 443)
    assert context.server_hostnames == ["dashscope-intl.aliyuncs.com"]


def test_prewarm_failure_returns_none(monkeypatch):
    monkeypatch.delenv("DASHSCOPE_REALTIME_URL", raising=False)

    def boom(address, timeout=None):
        raise OSError("模拟网络不可达")

    monkeypatch.setattr(qwen_agent.socket, "create_connection", boom)

    assert qwen_agent.prewarm_connection() is None


def test_resolve_prewarm_target_variants(monkeypatch):
    monkeypatch.delenv("DASHSCOPE_REALTIME_URL", raising=False)
    assert qwen_agent._resolve_prewarm_target() == ("dashscope.aliyuncs.com", 443)

    monkeypatch.setenv("DASHSCOPE_REALTIME_URL", "wss://example.com:8443/ws")
    assert qwen_agent._resolve_prewarm_target() == ("example.com", 8443)

    # 缺 scheme 的裸 host 也能解析
    monkeypatch.setenv("DASHSCOPE_REALTIME_URL", "myhost.example.com")
    assert qwen_agent._resolve_prewarm_target() == ("myhost.example.com", 443)


def test_start_prewarm_keepalive_loops_and_stops(monkeypatch):
    count = threading.Event()
    calls: list[float] = []

    def fake_prewarm(timeout=None):
        calls.append(time.monotonic())
        if len(calls) >= 2:
            count.set()
        return 0.01

    monkeypatch.setattr(qwen_agent, "prewarm_connection", fake_prewarm)
    stop_event = threading.Event()
    thread = qwen_agent.start_prewarm_keepalive(
        interval_seconds=0.01, stop_event=stop_event
    )

    assert thread.daemon
    assert thread.stop_event is stop_event
    assert count.wait(timeout=5), "预热循环未按周期执行"
    stop_event.set()
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert len(calls) >= 2


# ---- 竞态回归：stop 与重连线程并发（codex review P0） ----


def test_stop_during_reconnect_closes_new_connection(monkeypatch):
    """重连线程建好新连接时会话已 stop：新连接必须被关闭且不挂回。"""
    import asyncio as _asyncio
    import threading as _threading

    from agentcall.agents import qwen_agent as qa

    fake_conversation_cls = _make_fake_conversation_cls()
    monkeypatch.setattr(qa, "OmniRealtimeConversation", fake_conversation_cls)
    agent = qa.QwenVoiceAgent(api_key="k", model="m", model_display_name="d")
    _asyncio.run(agent.start(lambda pcm: None))
    first_conn = agent._conversation

    # 让重连线程在 connect 内等待，模拟连接建立期间 stop() 抢先完成。
    hold = _threading.Event()
    entered = _threading.Event()

    class SlowConversation(fake_conversation_cls):
        def connect(self):
            entered.set()
            hold.wait(timeout=5)
            super().connect()

    monkeypatch.setattr(qa, "OmniRealtimeConversation", SlowConversation)
    agent._mark_disconnected()
    _asyncio.run(agent.send_audio(b"\x01\x00"))  # 触发后台重连
    assert entered.wait(timeout=5)

    _asyncio.run(agent.stop())  # stop 在重连 connect 期间完成
    hold.set()  # 放行重连线程
    agent._reconnect_thread.join(timeout=5)

    # 新连接必须被关闭，且不得挂在 _conversation 上
    assert agent._conversation is None
    live = [c for c in SlowConversation.instances if not c.closed]
    assert not live, "重连线程泄漏了未关闭的新连接"
    assert first_conn.closed


def test_repetitive_agent_response_audio_is_suppressed(monkeypatch, caplog):
    """下行转写命中复读判重时，该 response 已缓存音频不得进入下行队列。"""
    monkeypatch.setenv("REPEAT_SUPPRESS_SIMILARITY", "0.9")
    agent = qwen_agent.QwenVoiceAgent(
        api_key="test-key",
        model="qwen-omni-turbo-realtime",
        model_display_name="千问测试",
    )
    callback = agent._callback
    first = "您好，我是张三的数字分身，想咨询一下套餐情况。"
    repeated = "您好！我是张三的数字分身，想咨询一下套餐情况"

    callback.on_event({
        "type": "response.audio.delta",
        "response_id": "r1",
        "delta": base64.b64encode(b"first").decode("ascii"),
    })
    callback.on_event({
        "type": "response.audio_transcript.done",
        "response_id": "r1",
        "transcript": first,
    })
    assert agent._audio_queue.get_nowait() == b"first"

    with caplog.at_level("INFO"):
        callback.on_event({
            "type": "response.audio.delta",
            "response_id": "r2",
            "delta": base64.b64encode(b"repeat").decode("ascii"),
        })
        callback.on_event({
            "type": "response.audio_transcript.done",
            "response_id": "r2",
            "transcript": repeated,
        })

    try:
        agent._audio_queue.get_nowait()
    except Empty:
        pass
    else:
        raise AssertionError("复读响应音频不应进入下行队列")
    assert "抑制复读" in caplog.text
