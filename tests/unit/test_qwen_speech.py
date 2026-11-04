"""DashScope 云端语音：fake WS 与管线回落/流式集成单测（零网络）。"""

from __future__ import annotations

import base64
import json
import sys
import time
from types import SimpleNamespace

import pytest

from agentcall.agents import local_agent, qwen_speech


def _msg(event_type: str, **extra) -> str:
    payload = {"type": event_type, **extra}
    return json.dumps(payload)


class FakeWS:
    """串行 fake WebSocket：send 记录、recv 按脚本吐出，`_FAIL` 模拟断线。"""

    _FAIL = object()

    def __init__(self, events) -> None:
        self.events = list(events)
        self.sent: list[str] = []
        self.closed = False

    def send(self, message: str) -> None:
        self.sent.append(message)

    def recv(self, timeout=None):
        if not self.events:
            raise TimeoutError()
        event = self.events.pop(0)
        if event is FakeWS._FAIL:
            from websockets.exceptions import ConnectionClosed
            raise ConnectionClosed(None, None)
        return event

    def close(self) -> None:
        self.closed = True


def _connect_list(monkeypatch, *sockets):
    queue = list(sockets)

    def connect(*args, **kwargs):
        assert queue, "connect 被多余调用"
        return queue.pop(0)

    monkeypatch.setattr(qwen_speech, "connect", connect)
    return queue


def _sent_has(socket: FakeWS, fragment: str) -> bool:
    return any(fragment in message for message in socket.sent)


# ---- QwenTtsClient ----


def test_tts_streams_first_packet(monkeypatch):
    delta = base64.b64encode(b"\x01\x02").decode()
    socket = FakeWS([_msg("session.updated"),
                     _msg("response.audio.delta", delta=delta),
                     _msg("response.audio.done")])
    _connect_list(monkeypatch, socket)
    client = qwen_speech.QwenTtsClient("qwen3-tts-flash-realtime", "Ethan", "key")

    chunks: list[bytes] = []
    done = client.synthesize_stream("你好", chunks.append, lambda: False)

    assert done is True
    assert chunks == [b"\x01\x02"]
    assert _sent_has(socket, "input_text_buffer.append")
    assert _sent_has(socket, "input_text_buffer.commit")


def test_tts_cancellation_discards_socket_and_returns_false(monkeypatch):
    socket = FakeWS([_msg("session.updated")])
    _connect_list(monkeypatch, socket)
    client = qwen_speech.QwenTtsClient("qwen3-tts-flash-realtime", "Ethan", "key")

    chunks: list[bytes] = []
    done = client.synthesize_stream("你好", chunks.append, lambda: True)

    assert done is False
    assert chunks == []
    assert socket.closed


def test_tts_reconnects_once_then_streams(monkeypatch):
    delta = base64.b64encode(b"\x03\x04").decode()
    first = FakeWS([_msg("session.updated"), FakeWS._FAIL])
    second = FakeWS([_msg("session.updated"),
                     _msg("response.audio.delta", delta=delta),
                     _msg("response.audio.done")])
    queue = _connect_list(monkeypatch, first, second)
    client = qwen_speech.QwenTtsClient("qwen3-tts-flash-realtime", "Ethan", "key")

    chunks: list[bytes] = []
    done = client.synthesize_stream("你好", chunks.append, lambda: False)

    assert done is True
    assert chunks == [b"\x03\x04"]
    assert first.closed and not second.closed
    assert not queue


# ---- QwenAsrClient ----


def test_asr_commit_returns_final_transcript(monkeypatch):
    socket = FakeWS([_msg("session.updated"),
                     _msg("conversation.item.input_audio_transcription.completed",
                          transcript="余额八十四点三一元")])
    _connect_list(monkeypatch, socket)
    client = qwen_speech.QwenAsrClient("qwen3-asr-flash-realtime", "key")

    assert client.one_shot(b"\x01\x02") == "余额八十四点三一元"
    assert _sent_has(socket, "input_audio_buffer.append")
    assert _sent_has(socket, "input_audio_buffer.commit")


def test_asr_session_explicitly_disables_turn_detection(monkeypatch):
    socket = FakeWS([_msg("session.updated")])
    _connect_list(monkeypatch, socket)
    client = qwen_speech.QwenAsrClient("qwen3-asr-flash-realtime", "key")
    client.warmup()

    update = next(json.loads(message) for message in socket.sent if "session.update" in message)
    assert update["session"]["turn_detection"] is None
    assert update["session"]["input_audio_format"] == "pcm"
    assert update["session"]["sample_rate"] == 8000


def test_asr_disconnect_raises_for_pipeline_fallback(monkeypatch):
    socket = FakeWS([_msg("session.updated"), FakeWS._FAIL])
    _connect_list(monkeypatch, socket)
    client = qwen_speech.QwenAsrClient("qwen3-asr-flash-realtime", "key")

    with pytest.raises(qwen_speech.QwenSpeechError):
        client.commit()
    assert socket.closed


# ---- 管线层：断线回落本地 ----


class _FakeModels:
    def __init__(self, root) -> None:
        self.root = root
        self.sample_rate = 8000
        self.cached_phrases: dict[str, bytes] = {}
        self.has_asr = True
        self.has_tts = True

    def transcribe(self, segment_pcm16: bytes) -> str:
        return "本地转写"

    def synthesize(self, text: str) -> bytes:
        return b"local-pcm"


class _FailingTtsClient:
    def __init__(self, *args, **kwargs) -> None:
        pass

    def warmup(self) -> None:
        pass

    def close(self) -> None:
        pass

    def synthesize_stream(self, text, on_audio, should_stop):
        raise qwen_speech.QwenSpeechError("tts down")


class _FailingAsrClient:
    def __init__(self, *args, **kwargs) -> None:
        self.committed = 0
        self.cleared = 0

    def warmup(self) -> None:
        pass

    def close(self) -> None:
        pass

    def append(self, pcm8k: bytes) -> None:
        raise qwen_speech.QwenSpeechError("asr down")

    def commit(self) -> str:
        self.committed += 1
        raise qwen_speech.QwenSpeechError("asr down")

    def clear(self) -> None:
        self.cleared += 1


def _build_pipeline(monkeypatch, tmp_path, models, *, asr_provider, tts_provider):
    monkeypatch.setitem(sys.modules, "sherpa_onnx", SimpleNamespace(
        VadModelConfig=lambda: SimpleNamespace(silero_vad=SimpleNamespace()),
        VoiceActivityDetector=lambda cfg, **kw: cfg,
    ))
    monkeypatch.setattr(local_agent, "_get_shared_models", lambda *args, **kwargs: models)
    monkeypatch.setattr(qwen_speech, "dashscope_api_key", lambda: "test-key")
    monkeypatch.setenv("LOCAL_ASR_PROVIDER", asr_provider)
    monkeypatch.setenv("LOCAL_TTS_PROVIDER", tts_provider)
    monkeypatch.setenv("LOCAL_ASR_DASHSCOPE_MODEL", "qwen3-asr-flash-realtime")
    monkeypatch.setenv("LOCAL_TTS_DASHSCOPE_MODEL", "qwen3-tts-flash-realtime")
    monkeypatch.setenv("LOCAL_TTS_DASHSCOPE_VOICE", "Kai")
    return local_agent._SherpaPipeline()


def test_tts_cloud_failure_falls_back_to_local_pcm(monkeypatch, tmp_path):
    monkeypatch.setattr(qwen_speech, "QwenTtsClient", _FailingTtsClient)
    pipeline = _build_pipeline(monkeypatch, tmp_path, _FakeModels(tmp_path),
                               asr_provider="local", tts_provider="dashscope")
    chunks: list[bytes] = []

    done = pipeline.synthesize_stream("你好", chunks.append, lambda: False)

    assert done is True
    assert chunks == [b"local-pcm"]
    assert pipeline.streaming_tts and pipeline.local_tts_available


def test_asr_cloud_failure_falls_back_to_local_transcribe(monkeypatch, tmp_path):
    monkeypatch.setattr(qwen_speech, "QwenAsrClient", _FailingAsrClient)
    pipeline = _build_pipeline(monkeypatch, tmp_path, _FakeModels(tmp_path),
                               asr_provider="dashscope", tts_provider="local")

    pipeline.feed_segment(b"\x01\x02")
    assert pipeline.end_turn() == "本地转写"


# ---- Agent 集成：云端 ASR 判停仍由本地 VAD 决定 ----


class _StreamingAsrPipeline:
    sample_rate = 8000
    streaming_asr = True
    streaming_tts = False
    local_asr_available = False
    local_tts_available = False

    def __init__(self) -> None:
        self.speaking = False
        self.fed: list[bytes] = []
        self.final = "余额八十四点三一元"

    def vad_push(self, pcm):
        code = pcm[0]
        self.speaking = code == 1
        return [b"first"] if code == 2 else [b"second"] if code == 3 else []

    def vad_flush(self):
        return []

    def is_speech_detected(self):
        return self.speaking

    def feed_segment(self, segment):
        self.fed.append(segment)

    def end_turn(self):
        return self.final


def _wait_until(cond, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.02)
    return False


def _feed(agent, code, seconds=0.1):
    agent._process_audio(bytes([code, 0]) * round(16000 * seconds))


def test_streaming_asr_feeds_segments_and_commits_once(monkeypatch):
    monkeypatch.setenv("LOCAL_ASR_SEGMENTED", "true")
    monkeypatch.setenv("LOCAL_ASR_SEGMENT_SILENCE", "0.25")
    monkeypatch.setenv("LOCAL_TURN_SILENCE", "0.6")
    pipeline = _StreamingAsrPipeline()
    requests: list[list[dict]] = []
    agent = local_agent.LocalPipelineAgent(
        pipeline_factory=lambda: pipeline,
        llm_chat=lambda messages, *a: requests.append(messages) or {"role": "assistant", "content": ""},
    )

    import asyncio
    asyncio.run(agent.start(lambda pcm: None))
    try:
        _feed(agent, 1)
        _feed(agent, 2)
        assert _wait_until(lambda: pipeline.fed == [b"first"])
        assert requests == []
        _feed(agent, 1)
        _feed(agent, 3)
        _feed(agent, 0, 0.35)
        assert _wait_until(lambda: len(requests) == 1)
        assert requests[0][-1] == {"role": "user", "content": "余额八十四点三一元"}
        assert pipeline.fed == [b"first", b"second"]
    finally:
        asyncio.run(agent.stop())


# ---- Agent 集成：云端 TTS 流式下发与取消 ----


class _StreamingTtsPipeline:
    sample_rate = 8000
    streaming_asr = False
    streaming_tts = True
    local_asr_available = False
    local_tts_available = False

    def __init__(self) -> None:
        self.streamed: list[str] = []

    def synthesize_stream(self, text, on_audio, should_stop):
        self.streamed.append(text)
        on_audio(b"a")
        if should_stop():
            return False
        on_audio(b"b")
        return True


def _speaking_agent(pipeline):
    agent = local_agent.LocalPipelineAgent(pipeline_factory=lambda: pipeline,
                                           llm_chat=lambda *a: {})
    agent._pipeline = pipeline
    agent._running = True
    agent._peak = None
    return agent


def test_speak_streams_deltas_and_marks_first_audio(monkeypatch):
    monkeypatch.setenv("LOCAL_LATENCY_METRICS", "true")
    pipeline = _StreamingTtsPipeline()
    agent = _speaking_agent(pipeline)
    turn = agent._new_turn(None)
    batch = local_agent._SpeechBatch(turn)
    collected: list[bytes] = []
    agent._on_audio_out = collected.append

    assert agent._speak("你好", batch) is True
    assert collected == [b"a", b"b"]
    assert batch.audio_emitted and turn.reply_started
    assert "tts_first_audio" in turn.stages


def test_speak_streaming_drops_audio_after_barge_in(monkeypatch):
    pipeline = _StreamingTtsPipeline()
    agent = _speaking_agent(pipeline)
    turn = agent._new_turn(None)
    batch = local_agent._SpeechBatch(turn)
    collected: list[bytes] = []

    def on_out(pcm):
        collected.append(pcm)
        batch.cancelled.set()

    agent._on_audio_out = on_out

    assert agent._speak("你好", batch) is False
    assert collected == [b"a"]


def test_turn_summary_records_asr_and_tts_providers():
    class _BothDashscope:
        sample_rate = 8000
        streaming_asr = True
        streaming_tts = True

    agent = local_agent.LocalPipelineAgent(pipeline_factory=lambda: _BothDashscope(),
                                           llm_chat=lambda *a: {})
    agent._pipeline = _BothDashscope()
    agent._metrics = True
    events = []
    agent.set_turn_event_handler(events.append)
    turn = agent._new_turn(None)

    agent._mark_latency(turn, "first_pcm_written")

    summary = next(e for e in events if e.get("type") == "local_turn_summary")
    assert summary["asr_provider"] == "dashscope"
    assert summary["tts_provider"] == "dashscope"
