"""本地三段式语音 Agent（local provider）：VAD → STT → 文本 LLM → TTS。

与 realtime provider 的分工差异：
- 音频不出本机——本地 silero VAD 切句、本地 paraformer 转写、本地 piper 合成；
  只有**转写文本**上云（LLM 脑默认 dashscope ``qwen-plus``，复用 DASHSCOPE_API_KEY，
  文本 token 成本比 realtime 音频低一个量级）。
- 半双工/录音/摘要/工具审计等仍由 CallSession 统一编排，本类只实现 VoiceAgent 接口。

管线（roadmap 附录 A 的关键约束逐条落实）：
- ``send_audio()`` 非阻塞：入队即返回，VAD/brain worker 消费，流式模式另有顺序 TTS worker；
- utterance 合并：brain 忙时堆积的语音段在出队时拼成一轮，不逐段打断 LLM；
- TTS PCM 适配：sherpa float32 → int16 bytes，采样率由 ``output_rate`` 告知
  CallSession（audio_bridge 负责 → 8k 重采样）；
- 模型加载放 ``start()`` 的线程池里，失败置 ``fatal`` 结束整通（不留死寂电话）。

sherpa-onnx 为可选依赖（``pip install 'callpilot[local]'``）；单测经
``pipeline_factory`` 注入 fake 管线，CI 零模型可跑。
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import threading
import time
from dataclasses import dataclass, field
from queue import Empty, Queue
from typing import Any, Callable, Protocol
from weakref import WeakSet

from .. import config
from ..prompts import agent_language, repeat_nudge_instructions
from ..repeat_suppression import RepeatSuppressor
from .base import VoiceAgent
from .local_llm import DeepSeekChat
from .local_speech import ClauseSplitter, normalize_peak
from .tools import SILENT_AFTER_TOOLS, TERMINAL_TOOLS

logger = logging.getLogger(__name__)

# LLM 连续失败达到该次数视为会话不可恢复（置 fatal，结束整通电话）。
_LLM_FATAL_FAILURES = 3
# 工具调用循环上限：防模型反复要求调工具不出话。
_MAX_TOOL_ROUNDS = 3


class SpeechPipeline(Protocol):
    """三段式的本地管线接口：真实实现包 sherpa-onnx，测试注入 fake。"""

    sample_rate: int  # TTS 输出采样率

    def vad_push(self, pcm16: bytes) -> list[bytes]:
        """喂 16k int16 PCM，返回已切出的完整语音段（可能为空）。"""
        ...

    def vad_flush(self) -> list[bytes]:
        """冲出 VAD 内部缓存的未完段（通话收尾时用）。"""
        ...

    def transcribe(self, segment_pcm16: bytes) -> str:
        ...

    def synthesize(self, text: str) -> bytes:
        """返回 ``sample_rate`` 采样率的 int16 mono PCM。"""
        ...


@dataclass
class _SayRequest:
    instructions: str


@dataclass(eq=False)
class _Turn:
    ended_at: float | None
    cancelled: threading.Event = field(default_factory=threading.Event)
    stages: set[str] = field(default_factory=set)


@dataclass
class _UserSpeech:
    pcm: bytes
    turn: _Turn


@dataclass
class _SpeechBatch:
    turn: _Turn
    splitter: ClauseSplitter = field(default_factory=ClauseSplitter)
    cancelled: threading.Event = field(default_factory=threading.Event)
    received_text: bool = False
    audio_emitted: bool = False


@dataclass
class _TtsRequest:
    text: str
    batch: _SpeechBatch


class _SherpaPipeline:
    """sherpa-onnx 实现（惰性 import；缺库抛错，缺模型则首启自动下载）。"""

    def __init__(self, on_progress: "Callable[[str], None] | None" = None) -> None:
        try:
            import sherpa_onnx  # noqa: PLC0415 - 可选依赖，惰性导入
        except ImportError as exc:
            raise RuntimeError(
                "local provider 需要 sherpa-onnx：pip install 'callpilot[local]'"
            ) from exc
        from .. import local_models

        # 首启自动下载缺失模型（~300MB，不进 DMG）；进度经 on_progress 播给 UI。
        missing = local_models.missing_assets()
        if missing:
            ids = ", ".join(asset.id for asset in missing)
            logger.info("local 模型资产缺失（%s），开始首次下载…", ids)
            if on_progress is not None:
                on_progress(f"首次使用本地模式：正在下载语音模型（{ids}，约 300MB）…")
            for asset in missing:
                if on_progress is not None:
                    on_progress(f"下载 {asset.id} 模型中…")
                local_models.ensure_asset(asset)
            if on_progress is not None:
                on_progress("语音模型下载完成，正在加载…")
            logger.info("local 模型下载完成")
        root = local_models.models_dir()
        vad_config = sherpa_onnx.VadModelConfig()
        vad_config.silero_vad.model = str(root / "silero_vad.onnx")
        vad_config.silero_vad.threshold = 0.5
        vad_config.silero_vad.min_silence_duration = 0.6
        vad_config.silero_vad.min_speech_duration = 0.25
        vad_config.sample_rate = 16000
        self._vad = sherpa_onnx.VoiceActivityDetector(vad_config, buffer_size_in_seconds=30)

        stt_dir = root / "sherpa-onnx-paraformer-zh-2023-09-14"
        self._recognizer = sherpa_onnx.OfflineRecognizer.from_paraformer(
            paraformer=str(stt_dir / "model.int8.onnx"),
            tokens=str(stt_dir / "tokens.txt"),
            num_threads=2,
        )

        tts_config = local_models.build_tts_config(sherpa_onnx, root)
        self._tts = sherpa_onnx.OfflineTts(tts_config)
        self._speaker_id = config.get_int("LOCAL_TTS_SPEAKER_ID")
        self.sample_rate = int(self._tts.sample_rate)
        self.synthesize("你好。")  # Load lazy kernels before the first real turn; never output.

    def is_speech_detected(self) -> bool:
        return bool(self._vad.is_speech_detected())

    # numpy 仅在真实管线里用（核心依赖已有）；fake 管线不需要。
    def _to_float32(self, pcm16: bytes):
        import numpy as np

        return np.frombuffer(pcm16, dtype=np.int16).astype("float32") / 32768.0

    def vad_push(self, pcm16: bytes) -> list[bytes]:
        import numpy as np

        self._vad.accept_waveform(self._to_float32(pcm16))
        segments: list[bytes] = []
        while not self._vad.empty():
            samples = self._vad.front.samples
            self._vad.pop()
            segments.append((np.asarray(samples) * 32768.0).astype(np.int16).tobytes())
        return segments

    def vad_flush(self) -> list[bytes]:
        import numpy as np

        self._vad.flush()
        segments: list[bytes] = []
        while not self._vad.empty():
            samples = self._vad.front.samples
            self._vad.pop()
            segments.append((np.asarray(samples) * 32768.0).astype(np.int16).tobytes())
        return segments

    def transcribe(self, segment_pcm16: bytes) -> str:
        stream = self._recognizer.create_stream()
        stream.accept_waveform(16000, self._to_float32(segment_pcm16))
        self._recognizer.decode_stream(stream)
        return (stream.result.text or "").strip()

    def synthesize(self, text: str) -> bytes:
        import numpy as np

        audio = self._tts.generate(text, sid=self._speaker_id, speed=1.0)
        samples = np.asarray(audio.samples, dtype="float32")
        return (np.clip(samples, -1.0, 1.0) * 32767.0).astype(np.int16).tobytes()


def _default_llm_chat(
    messages: list[dict[str, Any]],
    tools: list[dict] | None,
    timeout: float,
) -> dict[str, Any]:
    """LLM 脑：dashscope 文本模型（默认 qwen-plus），返回 assistant message dict。

    与 summarizer 一样走守护线程 + 超时；含 tool_calls 时由调用方继续工具循环。
    """
    import dashscope  # 项目核心依赖已有

    box: dict[str, Any] = {}

    def worker() -> None:
        try:
            kwargs: dict[str, Any] = {
                "model": config.get_str("LOCAL_LLM_MODEL"),
                "messages": messages,
                "result_format": "message",
                "api_key": os.environ.get("DASHSCOPE_API_KEY"),
            }
            if tools:
                kwargs["tools"] = tools
            box["response"] = dashscope.Generation.call(**kwargs)
        except Exception as exc:  # noqa: BLE001 - 后台线程不允许异常外逸
            box["error"] = f"{type(exc).__name__}: {exc}"

    thread = threading.Thread(target=worker, name="local-llm", daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        raise RuntimeError(f"LLM 请求超时（>{timeout:g}s）")
    if "error" in box:
        raise RuntimeError(box["error"])
    response = box.get("response")
    if response is None:
        raise RuntimeError("LLM 无响应")
    status = getattr(response, "status_code", None)
    if status is not None and status != 200:
        raise RuntimeError(
            f"dashscope 返回 {status}: "
            f"{getattr(response, 'message', '') or getattr(response, 'code', '')}"
        )
    try:
        message = response.output.choices[0].message
    except (AttributeError, IndexError, KeyError, TypeError) as exc:
        raise RuntimeError(f"LLM 响应结构异常: {exc}") from exc
    # dashscope message 对象 → 纯 dict（content / tool_calls）
    content = getattr(message, "content", "")
    if isinstance(content, list):  # 兼容多模态分片
        content = "".join(
            part.get("text", "") for part in content if isinstance(part, dict)
        )
    result: dict[str, Any] = {"role": "assistant", "content": content or ""}
    tool_calls = getattr(message, "tool_calls", None)
    if tool_calls:
        result["tool_calls"] = [
            {
                "id": tc.get("id") or f"call_{index}",
                "type": "function",
                "function": {
                    "name": (tc.get("function") or {}).get("name", ""),
                    "arguments": (tc.get("function") or {}).get("arguments", "") or "{}",
                },
            }
            for index, tc in enumerate(tool_calls)
            if isinstance(tc, dict)
        ]
    return result


class LocalPipelineAgent(VoiceAgent):
    """三段式 local provider：接口与 realtime provider 完全对齐。"""

    input_rate = 16000
    # piper zh_CN chaowen medium 的输出采样率；真实管线加载后以实际值覆盖。
    output_rate = 22050

    def __init__(
        self,
        *,
        pipeline_factory: "Callable[[], SpeechPipeline] | None" = None,
        llm_chat: Callable[[list[dict], list[dict] | None, float], dict] | None = None,
        llm_stream: Callable[[list[dict], list[dict] | None, float, Callable[[str], None]], dict] | None = None,
    ) -> None:
        self._pipeline_factory = pipeline_factory or _SherpaPipeline
        self._llm_chat = llm_chat or _default_llm_chat
        self._llm_stream = llm_stream
        self._deepseek = (DeepSeekChat() if llm_chat is None and llm_stream is None
                          and config.get_str("LOCAL_LLM_PROVIDER") == "deepseek" else None)
        self._streaming = config.get_bool("LOCAL_TTS_STREAMING")
        self._pipeline: SpeechPipeline | None = None
        self._on_audio_out: Callable[[bytes], None] | None = None
        self._running = False
        self._audio_queue: Queue[bytes] = Queue()
        self._brain_queue: Queue[_UserSpeech | _SayRequest] = Queue()
        self._tts_queue: Queue[_TtsRequest] = Queue()
        self._tts_thread: threading.Thread | None = None
        self._turn: _Turn | None = None
        self._turns: WeakSet[_Turn] = WeakSet()
        self._turn_lock = threading.RLock()
        self._tts_cache: dict[str, bytes] = {}
        self._peak: float | None = None
        self._vad_thread: threading.Thread | None = None
        self._brain_thread: threading.Thread | None = None
        self._messages: list[dict[str, Any]] = []
        self._messages_lock = threading.Lock()
        self._llm_failures = 0
        self._suppressor = RepeatSuppressor()
        self._last_nudge_at = 0.0

    async def start(self, on_audio_out: Callable[[bytes], None]) -> None:
        self._on_audio_out = on_audio_out
        # 模型加载重（首次含 ~300MB 下载），放线程池，不卡 CallSession 事件循环。
        # 工厂支持 on_progress 时把下载进度播给 UI（首启体验）；fake 工厂不支持则退回无参。
        def _build() -> SpeechPipeline:
            raw_peak = config.get_str("LOCAL_TTS_PEAK").strip()
            self._peak = float(raw_peak) if raw_peak else None
            if self._peak is not None and (not math.isfinite(self._peak) or not 0 < self._peak <= 1):
                raise ValueError("LOCAL_TTS_PEAK 必须为空或 (0, 1] 内的峰值")
            try:
                pipeline = self._pipeline_factory(on_progress=self._emit_status)  # type: ignore[call-arg]
            except TypeError:
                pipeline = self._pipeline_factory()
            for phrase in config.get_str("LOCAL_TTS_CACHE_PHRASES").split("|"):
                if phrase.strip() and phrase not in self._tts_cache:
                    self._tts_cache[phrase] = normalize_peak(pipeline.synthesize(phrase), self._peak)
            return pipeline
        try:
            if config.get_bool("PARALLEL_AGENT_CONNECT"):
                building = asyncio.create_task(asyncio.to_thread(_build))
                try:
                    self._pipeline = await asyncio.shield(building)
                except asyncio.CancelledError:
                    # Model loading cannot be interrupted. Keep the call's owner
                    # until this worker is joined; do not start VAD/brain afterward.
                    await asyncio.gather(building, return_exceptions=True)
                    raise
            else:
                self._pipeline = await asyncio.to_thread(_build)
        except Exception as exc:
            logger.error("local 管线初始化失败: %s", exc)
            self._emit_status(f"本地语音模型初始化失败：{exc}")
            self.fatal = True
            raise
        # 采样率以真实管线为准（fake/未来模型可能不同）。
        rate = int(getattr(self._pipeline, "sample_rate", 0) or 0)
        if rate > 0:
            self.output_rate = rate
        with self._messages_lock:
            self._messages = [
                {"role": "system", "content": self._session_instructions or ""}
            ]
        self._running = True
        self._vad_thread = threading.Thread(
            target=self._vad_worker, name="local-vad", daemon=True
        )
        self._brain_thread = threading.Thread(
            target=self._brain_worker, name="local-brain", daemon=True
        )
        self._vad_thread.start()
        self._brain_thread.start()
        if self._streaming:
            self._tts_thread = threading.Thread(target=self._tts_worker, name="local-tts", daemon=True)
            self._tts_thread.start()
        logger.info(
            "local 三段式 Agent 已启动（LLM=%s, TTS %dHz）",
            config.get_str("LOCAL_LLM_MODEL"),
            self.output_rate,
        )

    async def send_audio(self, pcm: bytes) -> None:
        if pcm and self._running:
            self._audio_queue.put(pcm)  # 无界队列，put 不阻塞

    async def say(self, instructions: str) -> None:
        if self._running:
            self._brain_queue.put(_SayRequest(instructions))

    async def external_tool_result(
        self,
        name: str,
        result: dict[str, Any],
        *,
        source: str,
    ) -> bool:
        success = result.get("success") is True
        count = result.get("count")
        safe_count = count if isinstance(count, int) and count >= 0 else 0
        mode = result.get("mode")
        safe_mode = mode if isinstance(mode, str) else "unknown"
        with self._messages_lock:
            self._messages.append(
                {
                    "role": "system",
                    "content": (
                        f"[external_tool_result] {name} was executed by {source}; "
                        f"success={str(success).lower()}, count={safe_count}, "
                        f"mode={safe_mode}. Do not speak merely to acknowledge it."
                    ),
                }
            )
        return True

    async def stop(self) -> None:
        with self._turn_lock:
            self._running = False
            self._cancel_turn()
        if self._deepseek is not None:
            self._deepseek.close()
        # Never block the event loop while a model kernel / network call finishes.
        for thread in (self._vad_thread, self._brain_thread, self._tts_thread):
            if thread is not None:
                await asyncio.to_thread(thread.join, 3)
        self._vad_thread = None
        self._brain_thread = None
        self._tts_thread = None
        self._pipeline = None
        logger.info("local 三段式 Agent 已停止")

    # ---- worker 线程 ----

    def _vad_worker(self) -> None:
        pipeline = self._pipeline
        assert pipeline is not None
        was_speaking = False
        while self._running:
            try:
                pcm = self._audio_queue.get(timeout=0.2)
            except Empty:
                continue
            try:
                segments = pipeline.vad_push(pcm)
                detector = getattr(pipeline, "is_speech_detected", None)
                speaking = bool(detector()) if self._streaming and detector is not None else False
                if self._streaming and ((speaking and not was_speaking) or (segments and not was_speaking)):
                    # CallSession may reject barge-in during DTMF guard windows.
                    with self._turn_lock:
                        if self._on_user_interrupt is None or self._emit_user_interrupt():
                            self._cancel_turn()
                was_speaking = speaking
                for segment in segments:
                    turn = self._new_turn(time.monotonic())
                    self._brain_queue.put(_UserSpeech(segment, turn))
            except Exception as exc:  # noqa: BLE001 - VAD 异常丢帧继续
                logger.warning("VAD 处理失败（丢弃本帧）: %s", exc)

    def _brain_worker(self) -> None:
        pipeline = self._pipeline
        assert pipeline is not None
        while self._running:
            try:
                item = self._brain_queue.get(timeout=0.2)
            except Empty:
                continue
            try:
                if isinstance(item, _SayRequest):
                    self._handle_say(item.instructions)
                    continue
                # utterance 合并：LLM 忙期间堆积的语音段拼成同一轮。
                segments = [item.pcm]
                turn = item.turn
                while True:
                    try:
                        follow = self._brain_queue.get_nowait()
                    except Empty:
                        break
                    if isinstance(follow, _SayRequest):
                        self._brain_queue.put(follow)  # say 不并轮，退回队列
                        break
                    segments.append(follow.pcm)
                    turn = follow.turn
                if not turn.cancelled.is_set():
                    self._handle_user_speech(b"".join(segments), turn)
            except Exception as exc:  # noqa: BLE001 - 单轮失败不杀线程
                logger.warning("brain 处理一轮失败: %s", exc)

    # ---- 每轮语义 ----

    def _handle_user_speech(self, segment: bytes, turn: _Turn) -> None:
        pipeline = self._pipeline
        assert pipeline is not None
        text = pipeline.transcribe(segment)
        if not text or turn.cancelled.is_set() or not self._running:
            return
        logger.info("[上行·用户] %s", text)
        self._emit_transcript("user", text)
        with self._messages_lock:
            self._messages.append({"role": "user", "content": text})
        self._respond(turn)

    def _handle_say(self, instructions: str) -> None:
        # say 语义（开场白/收尾道别/复读提示）：临时把指令并进本轮请求，
        # 产出的话进入正式对话历史，保持后续上下文连贯。
        with self._messages_lock:
            self._messages.append(
                {
                    "role": "user",
                    "content": f"[系统指令，对方听不到这句] {instructions}\n"
                    "请直接输出你要对对方说的话本身，不要解释。",
                }
            )
        self._respond(self._new_turn(None))

    def _respond(self, turn: _Turn) -> None:
        response = self._chat_with_tools(turn)
        if response is None or turn.cancelled.is_set() or not self._running:
            return
        reply, batch = response
        if not reply.strip():
            return
        if self._suppressor.should_suppress(reply):
            logger.info("[local] 抑制复读: %s", reply)
            self._discard_batch(batch)
            self._nudge_after_suppressed()
            return
        logger.info("[下行·Agent] %s", reply)
        self._emit_transcript("agent", reply)
        if self._streaming:
            for clause in batch.splitter.finish():
                self._queue_clause(clause, batch)
        else:
            self._mark_latency(turn, "first_clause_ready")
            self._speak(reply, batch)

    def _nudge_after_suppressed(self) -> None:
        now = time.monotonic()
        if now - self._last_nudge_at < 8.0:
            return
        self._last_nudge_at = now
        with self._messages_lock:
            self._messages.append(
                {
                    "role": "user",
                    "content": f"[系统指令] {repeat_nudge_instructions(agent_language())}",
                }
            )

    def _chat_with_tools(self, turn: _Turn) -> tuple[str, _SpeechBatch] | None:
        """LLM 对话 + 工具循环；失败计数达到阈值置 fatal。返回最终应答文本。"""
        tools = self._tools.specs() if self._tools and self._tools.has_tools() else None
        timeout = config.get_float("LOCAL_LLM_TIMEOUT")
        for _round in range(_MAX_TOOL_ROUNDS + 1):
            if turn.cancelled.is_set() or not self._running:
                return None
            batch = _SpeechBatch(turn)

            def on_content(text: str) -> None:
                if turn.cancelled.is_set() or batch.cancelled.is_set() or not self._running:
                    return
                batch.received_text = True
                self._mark_latency(turn, "llm_first_token")
                if self._streaming:
                    for clause in batch.splitter.feed(text):
                        self._queue_clause(clause, batch)

            with self._messages_lock:
                messages = list(self._messages)
            try:
                if self._deepseek is not None:
                    message = self._deepseek.chat(
                        messages, tools, timeout, on_content, turn.cancelled,
                        on_tools=lambda: self._discard_batch(batch),
                    )
                elif self._llm_stream is not None:
                    message = self._llm_stream(messages, tools, timeout, on_content)
                else:
                    message = self._llm_chat(messages, tools, timeout)
            except InterruptedError:
                self._discard_batch(batch)
                return None
            except Exception as exc:  # noqa: BLE001
                self._discard_batch(batch)
                if turn.cancelled.is_set() or not self._running:
                    return None
                self._llm_failures += 1
                logger.warning(
                    "LLM 请求失败(%d/%d): %s", self._llm_failures, _LLM_FATAL_FAILURES, exc
                )
                if self._llm_failures >= _LLM_FATAL_FAILURES:
                    logger.error("LLM 连续失败，标记会话不可恢复")
                    self.fatal = True
                return None
            if turn.cancelled.is_set() or not self._running:
                self._discard_batch(batch)
                return None
            self._llm_failures = 0
            tool_calls = message.get("tool_calls") or []
            with self._messages_lock:
                self._messages.append(message)
            if not tool_calls:
                reply = str(message.get("content") or "")
                if reply and not batch.received_text:
                    on_content(reply)
                return reply, batch
            self._discard_batch(batch)
            wait_for_remote = self._run_tool_calls(tool_calls, turn)
            if wait_for_remote:
                # 挂断后结束；DTMF 后等待对端下一段话，不生成口头确认。
                return None
        logger.warning("工具调用超过 %d 轮仍未产出应答，跳过本轮", _MAX_TOOL_ROUNDS)
        return None

    def _run_tool_calls(self, tool_calls: list[dict], turn: _Turn) -> bool:
        """执行工具并回填结果；返回本轮是否应停止生成后续回复。"""
        wait_for_remote = False
        for call in tool_calls:
            if turn.cancelled.is_set() or not self._running:
                return True
            function = call.get("function") or {}
            name = str(function.get("name") or "")
            call_id = str(call.get("id") or "")
            try:
                args = json.loads(function.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            logger.info("local Agent 调用工具 %s", name)
            if self._tools is not None:
                result = self._tools.dispatch(name, args)
            else:
                result = {"success": False, "message": "无可用工具"}
            if name in TERMINAL_TOOLS or name in SILENT_AFTER_TOOLS:
                wait_for_remote = True
            with self._messages_lock:
                self._messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call_id,
                        "content": json.dumps(result, ensure_ascii=False),
                    }
                )
        return wait_for_remote

    def _new_turn(self, ended_at: float | None) -> _Turn:
        with self._turn_lock:
            self._turn = _Turn(ended_at)
            self._turns.add(self._turn)
            return self._turn

    def _cancel_turn(self) -> None:
        with self._turn_lock:
            for turn in list(self._turns):
                turn.cancelled.set()
            while True:
                try:
                    self._tts_queue.get_nowait()
                except Empty:
                    break
                self._tts_queue.task_done()

    def _mark_latency(self, turn: _Turn, stage: str) -> None:
        with self._turn_lock:
            if turn.ended_at is None or stage in turn.stages or turn.cancelled.is_set():
                return
            turn.stages.add(stage)
            self._emit_latency(stage, round((time.monotonic() - turn.ended_at) * 1000, 1))

    def _discard_batch(self, batch: _SpeechBatch) -> None:
        with self._turn_lock:
            batch.cancelled.set()
            if batch.audio_emitted and not batch.turn.cancelled.is_set() and self._running:
                self._emit_late_cut()

    def _queue_clause(self, text: str, batch: _SpeechBatch) -> None:
        with self._turn_lock:
            if not self._running or batch.turn.cancelled.is_set() or batch.cancelled.is_set():
                return
            self._mark_latency(batch.turn, "first_clause_ready")
            self._tts_queue.put(_TtsRequest(text, batch))

    def _tts_worker(self) -> None:
        while self._running:
            try:
                request = self._tts_queue.get(timeout=0.1)
            except Empty:
                continue
            try:
                self._speak(request.text, request.batch)
            except Exception as exc:  # noqa: BLE001 - keep the dedicated worker alive
                logger.warning("TTS 输出失败: %s", exc)
            finally:
                self._tts_queue.task_done()

    def _speak(self, text: str, batch: _SpeechBatch) -> None:
        pipeline = self._pipeline
        if (pipeline is None or self._on_audio_out is None or not self._running
                or batch.cancelled.is_set() or batch.turn.cancelled.is_set()):
            return
        try:
            pcm = self._tts_cache.get(text)
            if pcm is None:
                pcm = normalize_peak(pipeline.synthesize(text), self._peak)
        except Exception as exc:  # noqa: BLE001
            logger.warning("TTS 合成失败: %s", exc)
            return
        # Generation may finish after stop / a new utterance. Check under the same
        # lock as cancellation so no stale output can cross the cancellation edge.
        with self._turn_lock:
            if pcm and self._running and not batch.cancelled.is_set() and not batch.turn.cancelled.is_set():
                self._mark_latency(batch.turn, "tts_first_audio")
                batch.audio_emitted = True
                self._on_audio_out(pcm)
