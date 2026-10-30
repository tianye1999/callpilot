"""假 socket 驱动真实 HTTPResponse，首字节与完整状态行、SSE 分开验证。"""

import http.client
import io
import threading
from types import SimpleNamespace

import pytest

from agentcall import config
from agentcall.agents import local_llm

KEY = "LOCAL_LLM_FIRST_BYTE_TIMEOUT"
SSE = b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\ndata: [DONE]\n\n'
HEAD = b"HTTP/1.1 200 OK\r\nContent-Length: " + str(len(SSE)).encode() + b"\r\n\r\n"


@pytest.fixture
def transport(monkeypatch):
    clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr(local_llm, "time", SimpleNamespace(monotonic=lambda: clock.now))
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fake-private-key")
    monkeypatch.setenv("LOCAL_LLM_BASE_URL", "https://example.invalid")
    monkeypatch.setenv(KEY, "1")
    scripts, created = [], []

    class Raw(io.RawIOBase):
        def __init__(self, sock):
            self.sock = sock

        def readable(self):
            return True

        def readinto(self, buffer):
            if not self.sock.steps:
                return 0
            step = self.sock.steps.pop(0)
            if callable(step):
                step()
                step = TimeoutError()
            if isinstance(step, TimeoutError):
                clock.now += self.sock.timeout
            if isinstance(step, Exception):
                raise step
            size = min(len(buffer), len(step))
            buffer[:size] = step[:size]
            if size < len(step):
                self.sock.steps.insert(0, step[size:])
            return size

    class Socket:
        def __init__(self, steps):
            self.steps, self.timeout, self.shutdown_called = steps, 0.0, False
            self.timeouts = []
            self.released = threading.Event()
            self.reader = io.BufferedReader(Raw(self))

        def makefile(self, mode):
            return self.reader

        def settimeout(self, value):
            self.timeout = value
            self.timeouts.append(value)

        def shutdown(self, how):
            self.shutdown_called = True
            self.released.set()

    class Connection:
        response_class = http.client.HTTPResponse

        def __init__(self, *args, timeout, **kwargs):
            # 第二个请求必须等上一个连接关闭，且一直由同一请求槽保护。
            assert not created or created[-1].closed
            self.sock = Socket(scripts.pop(0))
            self.timeout = timeout
            self.closed = False
            self.requests = 0
            created.append(self)

        def request(self, *args, **kwargs):
            self.requests += 1

        def getresponse(self):
            response = self.response_class(self.sock)
            try:
                response.begin()
                return response
            except BaseException:
                response.close()
                raise

        def close(self):
            self.closed = True

    monkeypatch.setattr(local_llm.http.client, "HTTPSConnection", Connection)
    client = local_llm.DeepSeekChat()
    yield SimpleNamespace(client=client, scripts=scripts, created=created, clock=clock)
    client.close()


def test_first_byte_timeout_retries_new_connection_once(transport, caplog):
    transport.scripts.extend([[TimeoutError()], [HEAD + SSE]])
    with caplog.at_level("WARNING"):
        result = transport.client.chat([{"role": "user", "content": "private-body"}], None, 5, lambda _: None)
    assert result["content"] == "ok"
    first, second = transport.created
    assert first.closed and first.sock.shutdown_called
    assert first.requests == second.requests == 1
    assert second.timeout == 4
    assert second.response_class is http.client.HTTPResponse
    assert "attempt=1" in caplog.text and "waited_ms=1000" in caplog.text
    assert "private-body" not in caplog.text and "fake-private-key" not in caplog.text


@pytest.mark.parametrize("timeout,expected_attempts", [(5, 2), (0.5, 1), (1.5, 2)])
def test_first_byte_retry_still_times_out_with_original_deadline(transport, timeout, expected_attempts):
    transport.scripts.extend([[TimeoutError()], [TimeoutError()]])
    with pytest.raises(RuntimeError, match=rf"LLM 请求超时（>{timeout:g}s）"):
        transport.client.chat([], None, timeout, lambda _: None)
    assert len(transport.created) == expected_attempts
    assert transport.clock.now <= timeout
    assert all(conn.closed for conn in transport.created)


@pytest.mark.parametrize("prefix", [
    b"H", HEAD, HEAD + b": keepalive\n\n", HEAD + b'data: {"choices":[]}\n\n',
    HEAD + b'data: {"choices":[{"delta":{"reasoning_content":"private"}}]}\n\n',
    HEAD + b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"x",'
           b'"function":{"name":"send_sms","arguments":"{"}}]}}]}\n\n',
])
def test_first_byte_or_partial_sse_disarms_short_timeout_and_never_replays(transport, prefix):
    transport.scripts.append([prefix, TimeoutError()])
    with pytest.raises(RuntimeError):
        transport.client.chat([], None, 5, lambda _: None)
    assert len(transport.created) == 1
    assert transport.created[0].sock.timeouts == [5, 1, 5]


@pytest.mark.parametrize("error", [ConnectionResetError(), http.client.IncompleteRead(b"data: partial")])
@pytest.mark.parametrize("prefix", [b"H", HEAD + b": keepalive\n\n", HEAD + b"data: partial"])
def test_received_partial_sse_connection_error_never_replays(transport, error, prefix):
    transport.scripts.append([prefix, error])
    # 预热已有连接，覆盖旧连接重试分支。
    connection = local_llm.http.client.HTTPSConnection("example.invalid", timeout=5)
    transport.client._connection = connection
    transport.client._endpoint = "https://example.invalid"
    with pytest.raises(RuntimeError):
        transport.client.chat([], None, 5, lambda _: None)
    assert len(transport.created) == 1


def test_default_zero_preserves_existing_timeout_without_retry(transport, monkeypatch):
    monkeypatch.delenv(KEY)
    assert config.get_float(KEY) == 0
    transport.scripts.append([TimeoutError()])
    with pytest.raises(RuntimeError):
        transport.client.chat([], None, 5, lambda _: None)
    assert len(transport.created) == 1
    assert transport.created[0].sock.timeouts == [5]


@pytest.mark.parametrize("action", ["cancel", "close"])
def test_cancel_during_first_byte_wait_prevents_retry(transport, action):
    cancelled = threading.Event()
    stop = cancelled.set if action == "cancel" else transport.client.close
    transport.scripts.append([stop])
    with pytest.raises(InterruptedError):
        transport.client.chat([], None, 5, lambda _: None, cancelled)
    assert len(transport.created) == 1 and transport.created[0].closed


@pytest.mark.parametrize("join_cancelled", [False, True])
def test_cancel_from_another_thread_unblocks_first_byte_without_retry(transport, join_cancelled):
    entered, cancelled = threading.Event(), threading.Event()
    failures = []
    transport.client._join_cancelled = join_cancelled

    def blocked_read():
        entered.set()
        assert transport.created[0].sock.released.wait(2)

    def run():
        try:
            transport.client.chat([], None, 5, lambda _: None, cancelled)
        except Exception as exc:
            failures.append(exc)

    transport.scripts.append([blocked_read])
    caller = threading.Thread(target=run)
    caller.start()
    try:
        assert entered.wait(2)
        cancelled.set()
        caller.join(2)
        assert not caller.is_alive()
        assert len(failures) == 1 and isinstance(failures[0], InterruptedError)
        assert len(transport.created) == 1 and transport.created[0].closed
        # 请求槽必须释放，不能遗留后台请求占着下一轮。
        assert transport.client._lock.acquire(timeout=2)
        transport.client._lock.release()
    finally:
        transport.client.close()
        caller.join(2)


@pytest.mark.parametrize("value", ["-1", "0.5", "20.1", "nan", "inf", "-inf", "bad"])
def test_first_byte_config_rejects_invalid_values_and_env_falls_back(monkeypatch, tmp_path, value):
    monkeypatch.setenv(KEY, value)
    with pytest.raises(ValueError):
        config.update_env_file({KEY: value}, tmp_path / ".env")
    assert not (tmp_path / ".env").exists()
    assert config.get_float(KEY) == 0


@pytest.mark.parametrize("value", ["0", "1", "1.5", "20"])
def test_first_byte_config_accepts_disabled_or_supported_range(monkeypatch, tmp_path, value):
    monkeypatch.delenv(KEY, raising=False)
    config.update_env_file({KEY: value}, tmp_path / ".env")
    assert config.get_float(KEY) == float(value)
    assert KEY in (tmp_path / ".env").read_text(encoding="utf-8")
