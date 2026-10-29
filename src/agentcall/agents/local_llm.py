"""DeepSeek's OpenAI-compatible SSE transport, with per-agent keep-alive."""

from __future__ import annotations

import contextlib
import http.client
import json
import logging
import socket
import ssl
import threading
import time
from collections.abc import Callable, Iterable, Iterator
from typing import Any
from urllib.parse import urlsplit

from .. import config

logger = logging.getLogger(__name__)


def deepseek_body(messages: list[dict], tools: list[dict] | None) -> dict[str, Any]:
    thinking = config.get_str("LOCAL_LLM_THINKING")
    if thinking not in {"disabled", "low", "high"}:
        raise ValueError(f"未知的 LOCAL_LLM_THINKING: {thinking}")
    # With tools, DeepSeek requires all prior reasoning, even after a new user
    # turn (https://api-docs.deepseek.com/guides/thinking_mode/#tool-calls).
    # Strip only on the wire otherwise; never erase the agent's stored history.
    if thinking == "disabled" or not tools:
        messages = [{k: v for k, v in message.items() if k != "reasoning_content"}
                    if "reasoning_content" in message else message for message in messages]
    body: dict[str, Any] = {
        "model": config.get_str("LOCAL_LLM_MODEL"), "messages": messages, "stream": True,
        "thinking": {"type": "disabled" if thinking == "disabled" else "enabled"},
    }
    if thinking != "disabled":
        body["reasoning_effort"] = thinking
    if tools:
        body["tools"] = tools
    return body


def read_sse(
    lines: Iterable[bytes], on_content: Callable[[str], None],
    on_tools: Callable[[], None] | None = None,
    *, sensitive_texts: Iterable[str] = (),
) -> dict[str, Any]:
    content: list[str] = []
    reasoning: list[str] = []
    calls: dict[int, dict[str, Any]] = {}
    data: list[str] = []
    for raw in lines:
        line = raw.decode("utf-8").rstrip("\r\n")
        if line.startswith("data:"):
            data.append(line[5:].lstrip(" "))
        if line or not data:
            continue
        payload, data = "\n".join(data), []
        if payload == "[DONE]":
            result: dict[str, Any] = {"role": "assistant", "content": "".join(content)}
            if reasoning:
                result["reasoning_content"] = "".join(reasoning)
            if calls:
                result["tool_calls"] = [calls[i] for i in sorted(calls)]
            return result
        event = json.loads(payload)
        if "error" in event:
            error = event["error"]
            fields = error if isinstance(error, dict) else {"message": error}
            detail = "; ".join(f"{name}={fields[name]}" for name in ("code", "message")
                               if isinstance(fields.get(name), (str, int, float)))
            # Whitelist diagnostic fields, not the event/request object. Servers
            # can reflect credentials or input, so redact before truncating.
            for value in (config.get_str("DEEPSEEK_API_KEY"), *sensitive_texts, "".join(reasoning), *reasoning):
                if value:
                    detail = detail.replace(value, "[redacted]")
            detail = " ".join(detail.split())[:200]
            raise RuntimeError("DeepSeek SSE 返回错误" + (f": {detail}" if detail else ""))
        choices = event.get("choices") or []
        if not choices:  # optional usage chunk
            continue
        delta = choices[0].get("delta") or {}
        if isinstance(delta.get("reasoning_content"), str):
            reasoning.append(delta["reasoning_content"])
        fragments = delta.get("tool_calls") or []
        if fragments and not calls and on_tools is not None:
            on_tools()  # Stop speculative TTS before waiting for long argument fragments.
        text = delta.get("content")
        if text:
            content.append(text)
            on_content(text)
        for fragment in fragments:
            call = calls.setdefault(fragment["index"], {
                "id": "", "type": "function", "function": {"name": "", "arguments": ""},
            })
            call["id"] += fragment.get("id") or ""
            function = fragment.get("function") or {}
            for key in ("name", "arguments"):
                call["function"][key] += function.get(key) or ""
    raise RuntimeError("DeepSeek SSE 提前结束（缺少 [DONE]）")


class DeepSeekChat:
    def __init__(self) -> None:
        self._connection: http.client.HTTPConnection | None = None
        self._endpoint = ""
        self._lock = threading.Lock()
        self._closed = threading.Event()
        self._last_used = 0.0
        self._join_cancelled = config.get_bool("LOCAL_SPECULATIVE_LLM") or config.get_bool("LOCAL_FAST_OPENING")

    @contextlib.contextmanager
    def _request_slot(self, inactive: Callable[[], bool]) -> Iterator[bool]:
        # 等待连接的取消者不能关闭另一条请求；也不能永远堵在它的锁后面。
        while not self._lock.acquire(timeout=0.02):
            if inactive():
                yield False
                return
        try:
            yield True
        finally:
            self._lock.release()

    def prewarm(self, timeout: float, *, idle_seconds: float) -> bool:
        """只建立 TCP/TLS；空闲后换新连接，不发送 HTTP 或计费 token。"""
        if self._closed.is_set() or not self._lock.acquire(blocking=False):
            return False
        try:
            endpoint = config.get_str("LOCAL_LLM_BASE_URL").rstrip("/")
            if (self._connection is not None and self._endpoint == endpoint
                    and time.monotonic() - self._last_used < idle_seconds):
                return False
            url = urlsplit(endpoint)
            if (url.scheme not in {"http", "https"} or not url.hostname
                    or url.username or url.query or url.fragment):
                raise ValueError("LOCAL_LLM_BASE_URL 必须为 HTTP(S) API 根地址")
            self._abort()
            cls = http.client.HTTPSConnection if url.scheme == "https" else http.client.HTTPConnection
            connection = cls(url.hostname, port=url.port, timeout=timeout)
            self._connection, self._endpoint = connection, endpoint
            started = time.monotonic()
            connection.connect()
            if self._closed.is_set():
                self._discard_connection(connection)
                return False
            self._last_used = time.monotonic()
            logger.info("[local_prewarm] connected duration_ms=%.1f billed_tokens=0",
                        (self._last_used - started) * 1000)
            return True
        except Exception:
            self._abort()
            raise
        finally:
            self._lock.release()

    def _abort(self) -> None:
        connection, self._connection = self._connection, None
        if connection is not None:
            # close() alone need not wake a blocking readline() on another thread.
            if connection.sock is not None:
                try:
                    connection.sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            connection.close()

    def close(self) -> None:
        self._closed.set()
        self._abort()

    def _discard_connection(self, connection: http.client.HTTPConnection) -> None:
        connection.close()
        if self._connection is connection:
            self._connection = None

    def chat(
        self, messages: list[dict], tools: list[dict] | None, timeout: float,
        on_content: Callable[[str], None], cancelled: threading.Event | None = None,
        on_tools: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        payload = deepseek_body(messages, tools)
        thinking_enabled = payload["thinking"]["type"] == "enabled"
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        endpoint = config.get_str("LOCAL_LLM_BASE_URL").rstrip("/")
        url = urlsplit(endpoint)
        if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.query or url.fragment:
            raise ValueError("LOCAL_LLM_BASE_URL 必须为 HTTP(S) API 根地址")
        hostname = url.hostname
        key = config.get_str("DEEPSEEK_API_KEY").strip()
        if not key:
            raise ValueError("缺少 DEEPSEEK_API_KEY")
        abandoned, done = threading.Event(), threading.Event()
        box: dict[str, Any] = {}
        deadline = time.monotonic() + timeout
        sensitive_texts = (key, body.decode("utf-8"), *(
            message[field] for message in messages for field in ("content", "reasoning_content")
            if isinstance(message.get(field), str)
        ))

        def inactive() -> bool:
            return abandoned.is_set() or self._closed.is_set() or (cancelled is not None and cancelled.is_set())

        def content(text: str) -> None:
            if inactive():
                raise InterruptedError("local LLM 轮次已取消")
            on_content(text)

        def tool_event() -> None:
            if not inactive() and on_tools is not None:
                on_tools()

        def worker() -> None:
            connection: http.client.HTTPConnection | None = None
            received_sse = False

            def response_lines(response: Iterable[bytes]) -> Iterable[bytes]:
                nonlocal received_sse
                for line in response:
                    if line:
                        received_sse = True
                    yield line

            # Socket cleanup must finish before another request acquires this lock.
            with self._request_slot(inactive) as acquired:
                if not acquired:
                    done.set()
                    return
                try:
                    reused = self._connection is not None and self._endpoint == endpoint
                    for attempt in range(2):
                        if inactive():
                            return
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise TimeoutError(f"LLM 请求超时（>{timeout:g}s）")
                        response = None
                        try:
                            if self._connection is None or self._endpoint != endpoint:
                                self._abort()
                                connection_type = (http.client.HTTPSConnection if url.scheme == "https"
                                                   else http.client.HTTPConnection)
                                self._connection = connection_type(hostname, port=url.port, timeout=remaining)
                                self._endpoint = endpoint
                            connection = self._connection
                            box["connection"] = connection
                            if self._join_cancelled and connection.sock is None:
                                # 低延迟模式单独限制建连预算；SSE 读取仍使用本轮剩余时间。
                                connection.timeout = min(3.0, remaining)
                                connection.connect()
                                if inactive():
                                    raise InterruptedError("local LLM 建连期间已取消")
                                remaining = max(0.001, deadline - time.monotonic())
                            connection.timeout = remaining
                            if connection.sock is not None:
                                connection.sock.settimeout(remaining)
                            connection.request("POST", url.path + "/chat/completions", body=body, headers={
                                "Authorization": f"Bearer {key}", "Content-Type": "application/json",
                                "Accept": "text/event-stream",
                            })
                            response = connection.getresponse()
                            if response.status != 200:
                                raise RuntimeError(f"DeepSeek HTTP {response.status}")
                            result = read_sse(response_lines(response), content, tool_event,
                                              sensitive_texts=sensitive_texts)
                            if not thinking_enabled:
                                result.pop("reasoning_content", None)
                            response.read()  # Consume the chunked terminator for socket reuse.
                            if getattr(response, "will_close", False):
                                self._discard_connection(connection)
                            box["result"] = result
                            self._last_used = time.monotonic()
                            return
                        except (ConnectionError, http.client.IncompleteRead, ssl.SSLEOFError) as exc:
                            if connection is not None:
                                self._discard_connection(connection)
                            if inactive():
                                raise InterruptedError("local LLM 轮次已取消") from exc
                            if time.monotonic() >= deadline:
                                raise TimeoutError(f"LLM 请求超时（>{timeout:g}s）") from exc
                            # Never replay fresh requests or a partially received
                            # stream (including reasoning / tools / keepalives).
                            partial_sse = isinstance(exc, http.client.IncompleteRead) and bool(exc.partial)
                            if attempt or not reused or received_sse or partial_sse:
                                raise
                        finally:
                            if response is not None:
                                response.close()
                except Exception as exc:  # daemon errors are reported to the brain worker
                    if connection is not None:
                        self._discard_connection(connection)
                    box["error"] = exc
                finally:
                    if inactive() and connection is not None:
                        self._discard_connection(connection)
                    done.set()

        thread = threading.Thread(target=worker, name="local-deepseek", daemon=True)
        thread.start()
        while not done.wait(min(0.02, max(0, deadline - time.monotonic()))):
            if inactive() or time.monotonic() >= deadline:
                abandoned.set()
                # 仅中止本请求拥有的 socket，排队中的取消绝不影响正在生成的回复。
                owned = box.get("connection")
                if owned is not None:
                    if owned.sock is not None:
                        try:
                            owned.sock.shutdown(socket.SHUT_RDWR)
                        except OSError:
                            pass
                    owned.close()
                if self._join_cancelled:
                    thread.join()
                if self._closed.is_set() or (cancelled is not None and cancelled.is_set()):
                    raise InterruptedError("local LLM 轮次已取消")
                raise RuntimeError(f"LLM 请求超时（>{timeout:g}s）")
        if inactive():
            raise InterruptedError("local LLM 轮次已取消")
        if "error" in box:
            raise RuntimeError(f"DeepSeek 请求失败: {box['error']}") from box["error"]
        return dict(box["result"])
