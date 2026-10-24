"""DeepSeek's OpenAI-compatible SSE transport, with per-agent keep-alive."""

from __future__ import annotations

import http.client
import json
import socket
import ssl
import threading
import time
from collections.abc import Callable, Iterable
from typing import Any
from urllib.parse import urlsplit

from .. import config


def deepseek_body(messages: list[dict], tools: list[dict] | None) -> dict[str, Any]:
    thinking = config.get_str("LOCAL_LLM_THINKING")
    if thinking not in {"disabled", "low", "high"}:
        raise ValueError(f"未知的 LOCAL_LLM_THINKING: {thinking}")
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
            for value in (config.get_str("DEEPSEEK_API_KEY"), *sensitive_texts):
                if value:
                    detail = detail.replace(value, "[redacted]")
            detail = " ".join(detail.split())[:200]
            raise RuntimeError("DeepSeek SSE 返回错误" + (f": {detail}" if detail else ""))
        choices = event.get("choices") or []
        if not choices:  # optional usage chunk
            continue
        delta = choices[0].get("delta") or {}
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
        # reasoning_content is intentionally neither spoken nor put in history.
    raise RuntimeError("DeepSeek SSE 提前结束（缺少 [DONE]）")


class DeepSeekChat:
    def __init__(self) -> None:
        self._connection: http.client.HTTPConnection | None = None
        self._endpoint = ""
        self._lock = threading.Lock()
        self._closed = threading.Event()

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
        body = json.dumps(deepseek_body(messages, tools), ensure_ascii=False).encode("utf-8")
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
            message["content"] for message in messages if isinstance(message.get("content"), str)
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
            with self._lock:
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
                            response.read()  # Consume the chunked terminator for socket reuse.
                            if getattr(response, "will_close", False):
                                self._discard_connection(connection)
                            box["result"] = result
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
                self._abort()
                if self._closed.is_set() or (cancelled is not None and cancelled.is_set()):
                    raise InterruptedError("local LLM 轮次已取消")
                raise RuntimeError(f"LLM 请求超时（>{timeout:g}s）")
        if inactive():
            raise InterruptedError("local LLM 轮次已取消")
        if "error" in box:
            raise RuntimeError(f"DeepSeek 请求失败: {box['error']}") from box["error"]
        return dict(box["result"])
