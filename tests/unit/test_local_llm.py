"""DeepSeek wire format, connection reuse and daemon timeout, all fake HTTP."""

import http.client
import io
import json
import ssl
import threading

import pytest

from agentcall.agents.local_llm import DeepSeekChat, deepseek_body, read_sse


def sse(*deltas):
    events = [json.dumps({"choices": [{"delta": d}]}, ensure_ascii=False) for d in deltas]
    return (": keepalive\n\n" + "".join(f"data: {e}\n\n" for e in events)
            + "data: [DONE]\n\n").encode()


@pytest.mark.parametrize("thinking", ["disabled", "low", "high"])
def test_thinking_mapping(monkeypatch, thinking):
    monkeypatch.setenv("LOCAL_LLM_THINKING", thinking)
    monkeypatch.setenv("LOCAL_LLM_MODEL", "deepseek-flash")
    body = deepseek_body([], None)
    assert body["stream"] is True
    assert body["model"] == "deepseek-flash"
    assert body["thinking"] == {"type": "disabled" if thinking == "disabled" else "enabled"}
    if thinking == "disabled":
        assert "reasoning_effort" not in body
    else:
        assert body["reasoning_effort"] == thinking


def test_sse_accumulates_content_and_interleaved_tool_call_fragments():
    seen = []
    data = sse(
        {"reasoning_content": "private"},
        {"content": "你好，"},
        {"content": "稍等。", "tool_calls": [
            {"index": 1, "id": "call_", "function": {"name": "send_", "arguments": '{"digits":'}},
            {"index": 0, "id": "sms", "function": {"name": "send_sms", "arguments": "{"}},
        ]},
        {"tool_calls": [
            {"index": 1, "id": "key", "function": {"name": "dtmf", "arguments": '"1"}'}},
            {"index": 0, "function": {"arguments": "}"}},
        ]},
    )
    result = read_sse(io.BytesIO(data), seen.append)
    assert seen == ["你好，", "稍等。"]
    assert result["content"] == "你好，稍等。"
    assert "reasoning_content" not in result
    assert result["tool_calls"] == [
        {"id": "sms", "type": "function", "function": {"name": "send_sms", "arguments": "{}"}},
        {"id": "call_key", "type": "function",
         "function": {"name": "send_dtmf", "arguments": '{"digits":"1"}'}},
    ]


def test_tool_start_notifies_before_content_in_same_delta_and_only_once():
    events = []
    data = sse(
        {"content": "不该播。", "tool_calls": [{"index": 0, "id": "x",
                                             "function": {"name": "send_sms", "arguments": "{"}}]},
        {"tool_calls": [{"index": 0, "function": {"arguments": "}"}}]},
    )
    read_sse(io.BytesIO(data), lambda t: events.append("content"), lambda: events.append("tools"))
    assert events == ["tools", "content"]


@pytest.mark.parametrize("data", [b"data: invalid\n\n", b'data: {"error":{"message":"bad"}}\n\n',
                                  b'data: {"choices":[]}\n\n'])
def test_bad_or_truncated_sse_is_a_failure(data):
    with pytest.raises((RuntimeError, ValueError)):
        read_sse(io.BytesIO(data), lambda _: None)


class FakeConnection:
    def __init__(self, *args, **kwargs):
        self.requests = []
        self.closed = False
        self.sock = None

    def request(self, method, path, body, headers):
        self.requests.append((method, path, json.loads(body), headers))

    def getresponse(self):
        response = io.BytesIO(sse({"content": "您好。"}))
        response.status = 200
        return response

    def close(self):
        self.closed = True


def test_connection_is_reused_and_request_uses_config(monkeypatch):
    from agentcall.agents import local_llm

    monkeypatch.setenv("DEEPSEEK_API_KEY", "fake-key")
    monkeypatch.setenv("LOCAL_LLM_BASE_URL", "https://example.invalid/v1/")
    created = []

    def connect(*args, **kwargs):
        conn = FakeConnection()
        created.append(conn)
        return conn

    monkeypatch.setattr(local_llm.http.client, "HTTPSConnection", connect)
    client = DeepSeekChat()
    try:
        for _ in range(2):
            assert client.chat([], None, 1, lambda _: None)["content"] == "您好。"
        assert len(created) == 1
        method, path, body, headers = created[0].requests[0]
        assert (method, path) == ("POST", "/v1/chat/completions")
        assert body["stream"] is True
        assert headers["Authorization"] == "Bearer fake-key"
    finally:
        client.close()
    assert created[0].closed


def test_timeout_discards_late_delta_and_closes_connection(monkeypatch):
    from agentcall.agents import local_llm

    entered, release, finished = threading.Event(), threading.Event(), threading.Event()

    class Slow(FakeConnection):
        def getresponse(self):
            entered.set()
            release.wait(1)
            finished.set()
            return super().getresponse()

    conn = Slow()
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fake-key")
    monkeypatch.setattr(local_llm.http.client, "HTTPSConnection", lambda *a, **kw: conn)
    seen = []
    client = DeepSeekChat()
    try:
        with pytest.raises(RuntimeError, match="超时"):
            client.chat([], None, 0.03, seen.append)
        assert entered.is_set()
        assert conn.closed
    finally:
        release.set()
        assert finished.wait(1)
        client.close()
    assert seen == []


def test_failed_http_connection_is_not_reused_and_next_request_succeeds(monkeypatch):
    from agentcall.agents import local_llm

    class Broken(FakeConnection):
        def getresponse(self):
            response = io.BytesIO(b"ignored error body")
            response.status = 503
            return response

    first, second = Broken(), FakeConnection()
    connections = [first, second]
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fake-key")
    monkeypatch.setattr(local_llm.http.client, "HTTPSConnection", lambda *a, **kw: connections.pop(0))
    client = DeepSeekChat()
    try:
        with pytest.raises(RuntimeError, match="HTTP 503"):
            client.chat([], None, 1, lambda _: None)
        assert first.closed
        assert client.chat([], None, 1, lambda _: None)["content"] == "您好。"
        assert not connections
    finally:
        client.close()


@pytest.mark.parametrize("error", [
    http.client.RemoteDisconnected, BrokenPipeError, ConnectionResetError, ConnectionAbortedError, ssl.SSLEOFError,
])
@pytest.mark.parametrize("phase", ["request", "response", "read"])
def test_idle_reused_connection_retries_once_before_any_sse(monkeypatch, error, phase):
    from agentcall.agents import local_llm

    class BrokenResponse:
        status = 200

        def __iter__(self):
            raise error("idle socket")

        def close(self):
            pass

    class Idle(FakeConnection):
        def request(self, *args, **kwargs):
            super().request(*args, **kwargs)
            if len(self.requests) == 2 and phase == "request":
                raise error("idle socket")

        def getresponse(self):
            if len(self.requests) == 2:
                if phase == "response":
                    raise error("idle socket")
                if phase == "read":
                    return BrokenResponse()
            return super().getresponse()

    old, new, created = Idle(), FakeConnection(), []

    def connect(*args, **kwargs):
        conn = old if not created else new
        created.append(conn)
        return conn

    monkeypatch.setenv("DEEPSEEK_API_KEY", "fake-key")
    monkeypatch.setattr(local_llm.http.client, "HTTPSConnection", connect)
    client, seen = DeepSeekChat(), []
    try:
        client.chat([], None, 1, lambda _: None)  # Establish the reusable socket.
        assert client.chat([], None, 1, seen.append)["content"] == "您好。"
        assert created == [old, new]
        assert len(old.requests) == 2 and len(new.requests) == 1
        assert old.closed and seen == ["您好。"]
    finally:
        client.close()


@pytest.mark.parametrize("reuse", [False, True])
def test_first_connection_failure_and_failed_replacement_are_not_retried(monkeypatch, reuse):
    from agentcall.agents import local_llm

    class Broken(FakeConnection):
        def getresponse(self):
            if reuse and len(created) == 1 and len(self.requests) == 1:
                return super().getresponse()
            raise ConnectionResetError("closed socket")

    created = []

    def connect(*args, **kwargs):
        conn = Broken()
        created.append(conn)
        return conn

    monkeypatch.setenv("DEEPSEEK_API_KEY", "fake-key")
    monkeypatch.setattr(local_llm.http.client, "HTTPSConnection", connect)
    client = DeepSeekChat()
    try:
        if reuse:
            client.chat([], None, 1, lambda _: None)
        with pytest.raises(RuntimeError, match="closed socket"):
            client.chat([], None, 1, lambda _: None)
        assert len(created) == (2 if reuse else 1)
    finally:
        client.close()


@pytest.mark.parametrize("delta", [
    {"content": "partial"}, {"reasoning_content": "private"},
    {"tool_calls": [{"index": 0, "id": "x", "function": {"name": "send_sms", "arguments": "{"}}]},
])
def test_connection_failure_after_sse_data_never_replays_request(monkeypatch, delta):
    from agentcall.agents import local_llm

    class Partial:
        status = 200

        def __iter__(self):
            yield from io.BytesIO(sse(delta).split(b"data: [DONE]")[0])
            raise ConnectionResetError("mid-stream")

        def close(self):
            pass

    class Connection(FakeConnection):
        def getresponse(self):
            return super().getresponse() if len(self.requests) == 1 else Partial()

    created = []

    def connect(*args, **kwargs):
        conn = Connection()
        created.append(conn)
        return conn

    monkeypatch.setenv("DEEPSEEK_API_KEY", "fake-key")
    monkeypatch.setattr(local_llm.http.client, "HTTPSConnection", connect)
    client = DeepSeekChat()
    try:
        client.chat([], None, 1, lambda _: None)
        with pytest.raises(RuntimeError, match="mid-stream"):
            client.chat([], None, 1, lambda _: None)
        assert len(created) == 1
    finally:
        client.close()


def test_retry_uses_remaining_deadline_and_respects_cancellation(monkeypatch):
    from types import SimpleNamespace

    from agentcall.agents import local_llm

    clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr(local_llm, "time", SimpleNamespace(monotonic=lambda: clock.now))
    cancelled = threading.Event()

    class Idle(FakeConnection):
        def getresponse(self):
            if len(self.requests) > 1:
                clock.now += 0.3
                raise ConnectionResetError("idle")
            return super().getresponse()

    created, timeouts = [], []

    def connect(*args, **kwargs):
        conn = Idle() if not created else FakeConnection()
        created.append(conn)
        timeouts.append(kwargs["timeout"])
        return conn

    monkeypatch.setenv("DEEPSEEK_API_KEY", "fake-key")
    monkeypatch.setattr(local_llm.http.client, "HTTPSConnection", connect)
    client = DeepSeekChat()
    try:
        client.chat([], None, 1, lambda _: None)
        client.chat([], None, 1, lambda _: None, cancelled)
        assert timeouts == pytest.approx([1.0, 0.7])
        cancelled.set()
        with pytest.raises(InterruptedError):
            client.chat([], None, 1, lambda _: None, cancelled)
        assert len(created) == 2
    finally:
        client.close()


@pytest.mark.parametrize("action", ["cancel", "deadline", "timeout", "http"])
def test_stale_socket_is_not_retried_after_cancel_deadline_timeout_or_http_error(monkeypatch, action):
    from types import SimpleNamespace

    from agentcall.agents import local_llm

    clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr(local_llm, "time", SimpleNamespace(monotonic=lambda: clock.now))
    cancelled, created = threading.Event(), []

    class Idle(FakeConnection):
        def getresponse(self):
            if len(self.requests) == 1:
                return super().getresponse()
            if action == "cancel":
                cancelled.set()
            elif action == "deadline":
                clock.now += 2
            elif action == "timeout":
                raise TimeoutError("socket timed out")
            else:
                response = io.BytesIO(b"not SSE")
                response.status = 503
                return response
            raise ConnectionResetError("idle")

    def connect(*args, **kwargs):
        conn = Idle()
        created.append(conn)
        return conn

    monkeypatch.setenv("DEEPSEEK_API_KEY", "fake-key")
    monkeypatch.setattr(local_llm.http.client, "HTTPSConnection", connect)
    client = DeepSeekChat()
    try:
        client.chat([], None, 1, lambda _: None)
        expected = InterruptedError if action == "cancel" else RuntimeError
        with pytest.raises(expected):
            client.chat([], None, 1, lambda _: None, cancelled)
        assert len(created) == 1
    finally:
        client.close()


def test_sse_error_reports_only_bounded_message_and_code(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fake-secret-key")
    error = {"code": "invalid_request", "message": "服务繁忙" * 100,
             "request": {"content": "private request", "key": "fake-secret-key"}}
    with pytest.raises(RuntimeError) as exc:
        read_sse(io.BytesIO(("data: " + json.dumps({"error": error}) + "\n\n").encode()), lambda _: None)
    message = str(exc.value)
    assert "invalid_request" in message and "服务繁忙" in message
    assert "private request" not in message and "fake-secret-key" not in message
    assert len(message.split(": ", 1)[1]) <= 200


def test_sse_error_redacts_reflected_key_and_request_content(monkeypatch):
    from agentcall.agents import local_llm

    secret, private = "fake-secret-key", "private user utterance"

    class Error(FakeConnection):
        def getresponse(self):
            payload = {"error": {"code": "bad", "message": f"Rejected {private} using {secret}"}}
            response = io.BytesIO(("data: " + json.dumps(payload) + "\n\n").encode())
            response.status = 200
            return response

    monkeypatch.setenv("DEEPSEEK_API_KEY", secret)
    monkeypatch.setattr(local_llm.http.client, "HTTPSConnection", lambda *a, **kw: Error())
    client = DeepSeekChat()
    try:
        with pytest.raises(RuntimeError) as exc:
            client.chat([{"role": "user", "content": private}], None, 1, lambda _: None)
        assert "bad" in str(exc.value) and "Rejected" in str(exc.value)
        assert secret not in str(exc.value) and private not in str(exc.value)
    finally:
        client.close()
