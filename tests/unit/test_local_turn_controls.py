"""B5/B6/B7：外呼强制切轮、每轮单行汇总、启动核对模型名（fake 管线，零模型零网络）。"""

from __future__ import annotations

import io
import json
import threading
import time

from agentcall.agents import local_llm
from agentcall.agents.local_agent import LocalPipelineAgent


class SpeakingPipeline:
    """像 Silero 分段器：每 push 一次给一段；is_speech_detected 由测试控制。"""

    sample_rate = 16000

    def __init__(self) -> None:
        self.speaking = True
        self.transcripts: list[str] = []

    def vad_push(self, pcm16: bytes) -> list[bytes]:
        # 只有在说话时才吐段；静音帧只推进轮末静音计数。
        return [pcm16] if (pcm16 and self.speaking) else []

    def vad_flush(self) -> list[bytes]:
        return []

    def is_speech_detected(self) -> bool:
        return self.speaking

    def transcribe(self, segment_pcm16: bytes) -> str:
        text = segment_pcm16.decode("utf-8", errors="ignore")
        self.transcripts.append(text)
        return text

    def synthesize(self, text: str) -> bytes:
        return b"\x01\x02" * 160


def _agent(monkeypatch, pipeline, replies):
    monkeypatch.setenv("LOCAL_ASR_SEGMENTED", "true")
    monkeypatch.setenv("LOCAL_SPECULATIVE_LLM", "false")
    monkeypatch.setenv("LOCAL_TTS_STREAMING", "false")
    monkeypatch.setenv("LOCAL_SLOW_REPLY_FILLER_SECONDS", "0")
    monkeypatch.setenv("LOCAL_LLM_PREWARM", "false")
    monkeypatch.setenv("LOCAL_FAST_OPENING", "false")
    monkeypatch.setenv("LOCAL_TURN_SILENCE", "0.6")
    monkeypatch.setenv("LOCAL_LATENCY_METRICS", "true")

    def scripted_llm(messages, tools, timeout):
        return replies.pop(0) if replies else {"role": "assistant", "content": ""}

    return LocalPipelineAgent(pipeline_factory=lambda: pipeline, llm_chat=scripted_llm)


def _wait(cond, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.02)
    return cond()


def test_forced_endpoint_hands_continuous_speech_to_the_model(monkeypatch):
    pipeline = SpeakingPipeline()
    replies = [{"role": "assistant", "content": "按一。"}, {"role": "assistant", "content": "再按一。"}]
    agent = _agent(monkeypatch, pipeline, replies)
    events: list[dict] = []
    agent.set_turn_event_handler(events.append)
    agent.configure_turn(max_seconds=1.0)
    # 直接驱动 VAD 逻辑：每帧 0.25 s 有声段，对方一直在说（永远不静音）。
    import asyncio
    asyncio.run(agent.start(lambda pcm: None))
    try:
        frame = b"x" * (16000 // 4 * 2)  # 0.25 s @16k, 16-bit
        for _ in range(5):
            agent._process_audio(frame)
        assert _wait(lambda: any(e.get("stage") == "forced_endpoint" for e in events))
        assert _wait(lambda: any(e.get("stage") == "endpoint" and e.get("kind") == "user" for e in events))
        # 强制切轮后计数归零：再说 1 s 才会再切。
        assert agent._turn_speech_samples < 16000
    finally:
        asyncio.run(agent.stop())


def test_forced_endpoint_is_off_by_default(monkeypatch):
    pipeline = SpeakingPipeline()
    agent = _agent(monkeypatch, pipeline, [])
    events: list[dict] = []
    agent.set_turn_event_handler(events.append)
    import asyncio
    asyncio.run(agent.start(lambda pcm: None))
    try:
        frame = b"x" * (16000 // 4 * 2)
        for _ in range(12):  # 3 s continuous speech
            agent._process_audio(frame)
        time.sleep(0.2)
        assert not any(e.get("stage") == "forced_endpoint" for e in events)
    finally:
        asyncio.run(agent.stop())


def test_turn_summary_emitted_once_audio_is_written(monkeypatch):
    pipeline = SpeakingPipeline()
    agent = _agent(monkeypatch, pipeline, [{"role": "assistant", "content": "好的。"}])
    events: list[dict] = []
    agent.set_turn_event_handler(events.append)
    import asyncio
    written: list[bytes] = []

    def on_audio(pcm: bytes) -> None:
        written.append(pcm)
        # 桥把 PCM 真正写进模组时回调；单测里没有桥，这里当场模拟写出。
        on_written = getattr(pcm, "on_written", None)
        if callable(on_written):
            on_written()

    asyncio.run(agent.start(on_audio))
    try:
        frame = "我想查话费".encode("utf-8")
        agent._process_audio(frame)
        pipeline.speaking = False
        # 0.6 s 静音 → 判停 → LLM → TTS → 首帧写出
        for _ in range(4):
            agent._process_audio(b"\x00" * (16000 // 4 * 2))
        assert _wait(lambda: any(e.get("type") == "local_turn_summary" for e in events), timeout=5.0)
        summary = next(e for e in events if e.get("type") == "local_turn_summary")
        assert summary["kind"] == "user"
        stages = {"asr_tail", "llm_first_token", "first_clause_ready", "tts_first_audio", "first_pcm_written"}
        assert set(summary) >= stages
        assert isinstance(summary["first_pcm_written"], float)
    finally:
        asyncio.run(agent.stop())


def _fake_urlopen(payload: dict, status: int = 200):
    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.close()

    class Opener:
        def open(self, request, timeout=0):
            assert request.full_url.endswith("/models")
            assert request.get_header("Authorization", "").startswith("Bearer ")
            return Response(json.dumps(payload).encode("utf-8"))

    return Opener()


def test_verify_model_reads_the_provider_list(monkeypatch):
    monkeypatch.setenv("LOCAL_LLM_MODEL", "deepseek-flash")
    monkeypatch.setenv("LOCAL_LLM_BASE_URL", "https://api.example.test")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key-not-real")
    listed = {"data": [{"id": "deepseek-flash"}, {"id": "other"}]}
    monkeypatch.setenv("LOCAL_LLM_PROVIDER", "deepseek")
    monkeypatch.setattr(local_llm, "_models_opener", _fake_urlopen(listed))
    ok, message = local_llm.verify_model()
    assert ok and "deepseek-flash" in message

    monkeypatch.setattr(local_llm, "_models_opener", _fake_urlopen({"data": [{"id": "deepseek-v9"}]}))
    ok, message = local_llm.verify_model()
    assert not ok and "不在供应方列表" in message and "deepseek-v9" in message and "通话会因此失败" in message


def test_verify_model_reports_offline_without_claiming_missing(monkeypatch):
    monkeypatch.setenv("LOCAL_LLM_MODEL", "deepseek-flash")
    monkeypatch.setenv("LOCAL_LLM_BASE_URL", "https://api.example.test")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key-not-real")

    class Offline:
        def open(self, request, timeout=0):
            raise OSError("offline")

    monkeypatch.setenv("LOCAL_LLM_PROVIDER", "deepseek")
    monkeypatch.setattr(local_llm, "_models_opener", Offline())
    ok, message = local_llm.verify_model()
    assert not ok and "无法核对" in message and "不在供应方列表" not in message
    results: list[tuple[bool, str]] = []
    done = threading.Event()
    local_llm.start_model_check(lambda ok, msg: (results.append((ok, msg)), done.set())).join(timeout=3)
    assert done.is_set() and results[0][0] is False


def test_forced_turn_runs_tools_but_withholds_speech_and_history(monkeypatch):
    pipeline = SpeakingPipeline()
    agent = _agent(monkeypatch, pipeline, [{"role": "assistant", "content": "请按一。"}])
    events: list[dict] = []
    written: list[bytes] = []
    agent.set_turn_event_handler(events.append)
    agent.configure_turn(max_seconds=1.0)
    import asyncio
    asyncio.run(agent.start(written.append))
    try:
        frame = b"x" * (16000 // 4 * 2)
        for _ in range(5):
            agent._process_audio(frame)
        assert _wait(lambda: any(e.get("stage") == "reply_withheld" for e in events), timeout=5.0)
        time.sleep(0.2)
        assert written == []
        with agent._messages_lock:
            assert all(m.get("role") != "assistant" for m in agent._messages)
    finally:
        asyncio.run(agent.stop())


def test_forced_turn_counts_from_speech_onset_not_first_segment(monkeypatch):
    class LateSegmenter(SpeakingPipeline):
        """真实分段器：说了很久才吐出第一段。"""

        def __init__(self) -> None:
            super().__init__()
            self.pushed = 0

        def vad_push(self, pcm16: bytes) -> list[bytes]:
            self.pushed += 1
            return [b"late"] if self.pushed == 6 else []

    pipeline = LateSegmenter()
    agent = _agent(monkeypatch, pipeline, [{"role": "assistant", "content": "好。"}])
    events: list[dict] = []
    agent.set_turn_event_handler(events.append)
    agent.configure_turn(max_seconds=1.0)
    import asyncio
    asyncio.run(agent.start(lambda pcm: None))
    try:
        frame = b"x" * (16000 // 4 * 2)
        for _ in range(5):  # 1.25 s of speech, no segment yet → nothing to hand over yet
            agent._process_audio(frame)
        assert not any(e.get("stage") == "forced_endpoint" for e in events)
        agent._process_audio(frame)  # first segment arrives: onset count already past the limit
        assert _wait(lambda: any(e.get("stage") == "forced_endpoint" for e in events))
    finally:
        asyncio.run(agent.stop())


def test_slow_filler_write_does_not_freeze_the_turn_summary(monkeypatch):
    monkeypatch.setenv("LOCAL_SLOW_REPLY_FILLER_SECONDS", "0.05")
    monkeypatch.setenv("LOCAL_SLOW_REPLY_FILLER", "嗯。")
    pipeline = SpeakingPipeline()
    events: list[dict] = []
    released = threading.Event()

    def slow_llm(messages, tools, timeout):
        released.wait(3.0)
        return {"role": "assistant", "content": "好的。"}

    monkeypatch.setenv("LOCAL_ASR_SEGMENTED", "true")
    monkeypatch.setenv("LOCAL_SPECULATIVE_LLM", "false")
    monkeypatch.setenv("LOCAL_TTS_STREAMING", "false")
    monkeypatch.setenv("LOCAL_LLM_PREWARM", "false")
    monkeypatch.setenv("LOCAL_FAST_OPENING", "false")
    monkeypatch.setenv("LOCAL_LATENCY_METRICS", "true")
    agent = LocalPipelineAgent(pipeline_factory=lambda: pipeline, llm_chat=slow_llm)
    agent.set_turn_event_handler(events.append)
    import asyncio

    def on_audio(pcm: bytes) -> None:
        on_written = getattr(pcm, "on_written", None)
        if callable(on_written):
            on_written()

    asyncio.run(agent.start(on_audio))
    try:
        agent._process_audio("我想查话费".encode("utf-8"))
        pipeline.speaking = False
        for _ in range(4):
            agent._process_audio(b"\x00" * (16000 // 4 * 2))
        assert _wait(lambda: any(e.get("stage") == "slow_filler_written" for e in events), timeout=5.0)
        assert not any(e.get("type") == "local_turn_summary" for e in events)
        released.set()
        assert _wait(lambda: any(e.get("type") == "local_turn_summary" for e in events), timeout=5.0)
        summary = next(e for e in events if e.get("type") == "local_turn_summary")
        assert summary["llm_first_token"] is not None and summary["first_pcm_written"] is not None
    finally:
        released.set()
        asyncio.run(agent.stop())


def test_verify_model_never_follows_a_redirect_with_the_key(monkeypatch):
    import urllib.error

    monkeypatch.setenv("LOCAL_LLM_PROVIDER", "deepseek")
    monkeypatch.setenv("LOCAL_LLM_MODEL", "deepseek-flash")
    monkeypatch.setenv("LOCAL_LLM_BASE_URL", "https://api.example.test")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key-not-real")
    handler = local_llm._NoRedirect()
    assert handler.redirect_request(None, None, 302, "Found", {}, "https://evil.example/models") is None

    class RedirectingOpener:
        def open(self, request, timeout=0):
            raise urllib.error.HTTPError(request.full_url, 302, "Found", {}, None)

    monkeypatch.setattr(local_llm, "_models_opener", RedirectingOpener())
    ok, message = local_llm.verify_model()
    assert not ok and "无法核对" in message
    monkeypatch.setenv("LOCAL_LLM_BASE_URL", "http://api.example.test")
    ok, message = local_llm.verify_model()
    assert not ok and "HTTPS" in message


def test_verify_model_skips_non_deepseek_provider(monkeypatch):
    monkeypatch.setenv("LOCAL_LLM_PROVIDER", "dashscope")
    ok, message = local_llm.verify_model()
    assert ok and "跳过" in message


def test_withheld_reply_is_owed_and_spoken_after_a_natural_pause(monkeypatch):
    """最后一段触发强制切轮、之后只有静音：对方停下后补答，而不是一直沉默。"""
    pipeline = SpeakingPipeline()
    agent = _agent(monkeypatch, pipeline, [{"role": "assistant", "content": "请按一。"},
                                           {"role": "assistant", "content": "好的，我按一。"}])
    events: list[dict] = []
    written: list[bytes] = []
    agent.set_turn_event_handler(events.append)
    agent.configure_turn(max_seconds=1.0)
    import asyncio
    asyncio.run(agent.start(written.append))
    try:
        frame = b"x" * (16000 // 4 * 2)
        for _ in range(4):  # exactly 1 s of speech → forced on the last segment
            agent._process_audio(frame)
        assert _wait(lambda: any(e.get("stage") == "reply_withheld" for e in events), timeout=5.0)
        assert written == []
        pipeline.speaking = False
        for _ in range(4):  # 1 s of silence, no new segment
            agent._process_audio(b"\x00" * (16000 // 4 * 2))
        assert _wait(lambda: len(written) >= 1, timeout=5.0)
        with agent._messages_lock:
            assert agent._messages[-1] == {"role": "assistant", "content": "好的，我按一。"}
    finally:
        asyncio.run(agent.stop())


def test_natural_turn_after_a_forced_turn_answers_once(monkeypatch):
    pipeline = SpeakingPipeline()
    agent = _agent(monkeypatch, pipeline, [{"role": "assistant", "content": "请按一。"},
                                           {"role": "assistant", "content": "好的。"},
                                           {"role": "assistant", "content": "多余的一句。"}])
    events: list[dict] = []
    written: list[bytes] = []
    agent.set_turn_event_handler(events.append)
    agent.configure_turn(max_seconds=1.0)
    import asyncio
    asyncio.run(agent.start(written.append))
    try:
        frame = b"x" * (16000 // 4 * 2)
        for _ in range(4):
            agent._process_audio(frame)
        assert _wait(lambda: any(e.get("stage") == "reply_withheld" for e in events), timeout=5.0)
        agent._process_audio(frame)  # the caller keeps talking, then pauses naturally
        pipeline.speaking = False
        for _ in range(6):
            agent._process_audio(b"\x00" * (16000 // 4 * 2))
        assert _wait(lambda: len(written) >= 1, timeout=5.0)
        time.sleep(0.5)
        assert len(written) == 1  # one spoken answer, no extra owed reply
    finally:
        asyncio.run(agent.stop())


def test_slow_filler_does_not_claim_the_reply_clause_mark(monkeypatch):
    monkeypatch.setenv("LOCAL_SLOW_REPLY_FILLER_SECONDS", "0.05")
    monkeypatch.setenv("LOCAL_SLOW_REPLY_FILLER", "嗯。")
    monkeypatch.setenv("LOCAL_ASR_SEGMENTED", "true")
    monkeypatch.setenv("LOCAL_SPECULATIVE_LLM", "false")
    monkeypatch.setenv("LOCAL_TTS_STREAMING", "false")
    monkeypatch.setenv("LOCAL_LLM_PREWARM", "false")
    monkeypatch.setenv("LOCAL_FAST_OPENING", "false")
    monkeypatch.setenv("LOCAL_LATENCY_METRICS", "true")
    pipeline = SpeakingPipeline()
    events: list[dict] = []
    released = threading.Event()

    def slow_llm(messages, tools, timeout):
        released.wait(3.0)
        return {"role": "assistant", "content": "好的。"}

    agent = LocalPipelineAgent(pipeline_factory=lambda: pipeline, llm_chat=slow_llm)
    agent.set_turn_event_handler(events.append)
    import asyncio

    def on_audio(pcm: bytes) -> None:
        on_written = getattr(pcm, "on_written", None)
        if callable(on_written):
            on_written()

    asyncio.run(agent.start(on_audio))
    try:
        agent._process_audio("我想查话费".encode("utf-8"))
        pipeline.speaking = False
        for _ in range(4):
            agent._process_audio(b"\x00" * (16000 // 4 * 2))
        assert _wait(lambda: any(e.get("stage") == "slow_filler_written" for e in events), timeout=5.0)
        time.sleep(0.3)
        released.set()
        assert _wait(lambda: any(e.get("type") == "local_turn_summary" for e in events), timeout=5.0)
        summary = next(e for e in events if e.get("type") == "local_turn_summary")
        # 正式回复的首句一定在模型首字之后，不会被更早的垫话占用。
        assert summary["first_clause_ready"] >= summary["llm_first_token"]
    finally:
        released.set()
        asyncio.run(agent.stop())


def test_old_silent_turn_cannot_answer_ahead_of_newer_speech(monkeypatch):
    """自然判停先发生、旧 silent 轮 LLM 后返回、新 ASR 最后完成：只答一次，且带上新话。"""
    pipeline = SpeakingPipeline()
    old_llm = threading.Event()
    seen: list[list[str]] = []
    replies = iter(["请按一。", "好的。", "多余。"])

    def llm(messages, tools, timeout):
        users = [m["content"] for m in messages if m.get("role") == "user"]
        seen.append(users)
        if len(seen) == 1:
            old_llm.wait(3.0)
        return {"role": "assistant", "content": next(replies)}

    for key, value in {"LOCAL_ASR_SEGMENTED": "true", "LOCAL_SPECULATIVE_LLM": "false",
                       "LOCAL_TTS_STREAMING": "false", "LOCAL_SLOW_REPLY_FILLER_SECONDS": "0",
                       "LOCAL_LLM_PREWARM": "false", "LOCAL_FAST_OPENING": "false",
                       "LOCAL_TURN_SILENCE": "0.6", "LOCAL_LATENCY_METRICS": "true"}.items():
        monkeypatch.setenv(key, value)
    agent = LocalPipelineAgent(pipeline_factory=lambda: pipeline, llm_chat=llm)
    written: list[bytes] = []
    agent.configure_turn(max_seconds=1.0)
    import asyncio
    asyncio.run(agent.start(written.append))
    try:
        frame = b"x" * (16000 // 4 * 2)
        for _ in range(4):
            agent._process_audio(frame)  # forced turn; its LLM blocks
        assert _wait(lambda: len(seen) == 1)
        agent._process_audio("新".encode("utf-8") * 10)  # caller keeps talking
        pipeline.speaking = False
        for _ in range(6):
            agent._process_audio(b"\x00" * (16000 // 4 * 2))  # natural pause
        old_llm.set()
        assert _wait(lambda: len(written) >= 1, timeout=5.0)
        time.sleep(0.5)
        assert len(written) == 1
        assert any("新" in "".join(users) for users in seen[1:])
    finally:
        old_llm.set()
        asyncio.run(agent.stop())


def test_dtmf_on_a_later_forced_turn_cancels_an_earlier_withheld_reply(monkeypatch):
    from agentcall.agents.tools import SEND_DTMF_SPEC, ToolRegistry

    pipeline = SpeakingPipeline()
    replies = [
        {"role": "assistant", "content": "请稍等。"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "t1", "type": "function", "function": {"name": "send_dtmf", "arguments": "{\"digits\": \"1\"}"}}]},
        {"role": "assistant", "content": "不该出现的补答。"},
    ]
    agent = _agent(monkeypatch, pipeline, replies)
    pressed: list[str] = []
    registry = ToolRegistry()
    registry.register(SEND_DTMF_SPEC, lambda args: (pressed.append(args.get("digits", "")), {"success": True})[1])
    agent.set_tools(registry)
    events: list[dict] = []
    written: list[bytes] = []
    agent.set_turn_event_handler(events.append)
    agent.configure_turn(max_seconds=1.0)
    import asyncio
    asyncio.run(agent.start(written.append))
    try:
        frame = b"x" * (16000 // 4 * 2)
        for _ in range(4):
            agent._process_audio(frame)  # forced turn 1 → reply withheld
        assert _wait(lambda: any(e.get("stage") == "reply_withheld" for e in events), timeout=5.0)
        for _ in range(4):
            agent._process_audio(frame)  # forced turn 2 → DTMF
        assert _wait(lambda: pressed == ["1"], timeout=5.0)
        pipeline.speaking = False
        for _ in range(6):
            agent._process_audio(b"\x00" * (16000 // 4 * 2))
        time.sleep(0.6)
        assert written == []
    finally:
        asyncio.run(agent.stop())
