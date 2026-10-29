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
- ASR/TTS 进程常驻，启动后后台加载；首通未就绪时在 ``start()`` 线程池里加载，
  失败置 ``fatal`` 结束整通（不留死寂电话）。VAD 每通独立。

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
from pathlib import Path
from queue import Empty, Queue
from typing import Any, Callable, Protocol
from weakref import WeakSet

from .. import config
from ..audio_bridge import AgentPcm
from ..prompts import agent_language, repeat_nudge_instructions
from ..repeat_suppression import RepeatSuppressor
from .base import VoiceAgent
from .local_llm import DeepSeekChat
from .local_speech import ClauseSplitter, normalize_peak, speech_text
from .tools import SILENT_AFTER_TOOLS, TERMINAL_TOOLS

logger = logging.getLogger(__name__)

# LLM 连续失败达到该次数视为会话不可恢复（置 fatal，结束整通电话）。
_LLM_FATAL_FAILURES = 3
# 工具调用循环上限：防模型反复要求调工具不出话。
_MAX_TOOL_ROUNDS = 3


def _positive_seconds(key: str) -> float:
    seconds = config.get_float(key)
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError(f"{key} 必须是有限正数")
    return seconds


def _cache_phrases() -> str:
    phrases = config.get_str("LOCAL_TTS_CACHE_PHRASES").split("|")
    if config.get_float("LOCAL_SLOW_REPLY_FILLER_SECONDS") > 0:
        phrases.append(config.get_str("LOCAL_SLOW_REPLY_FILLER"))
    return "|".join(dict.fromkeys(speech_text(phrase) for phrase in phrases))


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
    sequence: int = 0
    cancelled: threading.Event = field(default_factory=threading.Event)
    stages: set[str] = field(default_factory=set)
    is_user: bool = True
    reply_started: bool = False


@dataclass
class _UserSpeech:
    pcm: bytes
    turn: _Turn


@dataclass
class _RecognizedSpeech:
    text: str
    turn: _Turn


@dataclass
class _AsrEnd:
    turn: _Turn


@dataclass
class _Speculation:
    messages: list[dict[str, Any]]
    claimed: bool = False
    cancelled: threading.Event = field(default_factory=threading.Event)
    events: Queue[tuple[str, Any]] = field(default_factory=Queue)
    thread: threading.Thread | None = None


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


class _SharedSpeechModels:
    """Process-resident kernels and raw phrase PCM; VAD stays per call."""

    def __init__(
        self, key: tuple[str, str, int, str], on_progress: Callable[[str], None] | None,
    ) -> None:
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
        self.root = root = Path(key[0])
        self._asr_lock = threading.Lock()
        self._tts_lock = threading.Lock()
        stt_dir = root / "sherpa-onnx-paraformer-zh-2023-09-14"
        self._recognizer = sherpa_onnx.OfflineRecognizer.from_paraformer(
            paraformer=str(stt_dir / "model.int8.onnx"),
            tokens=str(stt_dir / "tokens.txt"),
            num_threads=2,
        )
        tts_config = local_models.build_tts_config(sherpa_onnx, root, key[1])
        self._tts = sherpa_onnx.OfflineTts(tts_config)
        self._speaker_id = key[2]
        self.sample_rate = int(self._tts.sample_rate)
        self.cached_phrases: dict[str, bytes] = {}
        warmup = self.synthesize("你好。")
        for phrase in key[3].split("|"):
            if phrase.strip() and phrase not in self.cached_phrases:
                self.cached_phrases[phrase] = warmup if phrase == "你好。" else self.synthesize(phrase)

    def transcribe(self, segment_pcm16: bytes) -> str:
        with self._asr_lock:
            stream = self._recognizer.create_stream()
            stream.accept_waveform(16000, _to_float32(segment_pcm16))
            self._recognizer.decode_stream(stream)
            return (stream.result.text or "").strip()

    def synthesize(self, text: str) -> bytes:
        import numpy as np

        cached = self.cached_phrases.get(text)
        if cached is not None:
            return cached
        with self._tts_lock:
            audio = self._tts.generate(text, sid=self._speaker_id, speed=1.0)
            samples = np.asarray(audio.samples, dtype="float32")
            return (np.clip(samples, -1.0, 1.0) * 32767.0).astype(np.int16).tobytes()


_SHARED_MODELS_LOCK = threading.Lock()
_SHARED_MODELS: tuple[tuple[str, str, int, str], _SharedSpeechModels] | None = None


def reset_shared_speech_models() -> None:
    """Discard the resident cache (tests must join any preload worker first)."""
    global _SHARED_MODELS
    with _SHARED_MODELS_LOCK:
        _SHARED_MODELS = None


def _get_shared_models(on_progress: Callable[[str], None] | None = None) -> _SharedSpeechModels:
    from .. import local_models

    global _SHARED_MODELS
    key = (
        str(local_models.models_dir().resolve()), config.get_str("LOCAL_TTS_MODEL"),
        config.get_int("LOCAL_TTS_SPEAKER_ID"), _cache_phrases(),
    )
    if on_progress is not None and (_SHARED_MODELS is None or _SHARED_MODELS[0] != key):
        on_progress("正在加载本地语音模型…")
    # Serialize cold loads, including preload vs. first-call races. Publish only
    # after warmup/cache succeeds; failures leave the next caller free to retry.
    with _SHARED_MODELS_LOCK:
        if _SHARED_MODELS is None or _SHARED_MODELS[0] != key:
            models = _SharedSpeechModels(key, on_progress)
            _SHARED_MODELS = (key, models)
        return _SHARED_MODELS[1]


def start_model_preload() -> threading.Thread:
    """Best-effort daemon preload, independent of the web/event-loop executor."""
    def load() -> None:
        try:
            _get_shared_models(on_progress=logger.info)
            logger.info("local 语音模型预加载完成")
        except Exception as exc:  # noqa: BLE001 - first call will retry normally
            logger.warning("local 语音模型预加载失败，首通时重试: %s", exc)

    thread = threading.Thread(target=load, name="local-model-preload", daemon=True)
    thread.start()
    return thread


def _to_float32(pcm16: bytes):
    import numpy as np

    return np.frombuffer(pcm16, dtype=np.int16).astype("float32") / 32768.0


class _SherpaPipeline:
    """Per-call VAD backed by shared, lazily imported sherpa-onnx models."""

    def __init__(self, on_progress: Callable[[str], None] | None = None) -> None:
        self._models = _get_shared_models(on_progress)
        import sherpa_onnx

        root = self._models.root
        vad_config = sherpa_onnx.VadModelConfig()
        vad_config.silero_vad.model = str(root / "silero_vad.onnx")
        vad_config.silero_vad.threshold = 0.5
        vad_config.silero_vad.min_silence_duration = (
            _positive_seconds("LOCAL_ASR_SEGMENT_SILENCE")
            if config.get_bool("LOCAL_ASR_SEGMENTED") else 0.6
        )
        vad_config.silero_vad.min_speech_duration = 0.25
        vad_config.sample_rate = 16000
        self._vad = sherpa_onnx.VoiceActivityDetector(vad_config, buffer_size_in_seconds=30)

        self.sample_rate = self._models.sample_rate
        self.cached_phrases = self._models.cached_phrases

    def is_speech_detected(self) -> bool:
        return bool(self._vad.is_speech_detected())

    def vad_push(self, pcm16: bytes) -> list[bytes]:
        import numpy as np

        self._vad.accept_waveform(_to_float32(pcm16))
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
        return self._models.transcribe(segment_pcm16)

    def synthesize(self, text: str) -> bytes:
        return self._models.synthesize(text)


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
        self._slow_filler_seconds = config.get_float("LOCAL_SLOW_REPLY_FILLER_SECONDS")
        if not math.isfinite(self._slow_filler_seconds) or self._slow_filler_seconds < 0:
            raise ValueError("LOCAL_SLOW_REPLY_FILLER_SECONDS 必须是有限非负数")
        self._slow_filler = (speech_text(config.get_str("LOCAL_SLOW_REPLY_FILLER"))
                             if self._slow_filler_seconds else "")
        self._waiting_batch: _SpeechBatch | None = None
        self._last_slow_filler_at = float("-inf")
        self._filler_stop = threading.Event()
        self._filler_thread: threading.Thread | None = None
        self._segmented_asr = config.get_bool("LOCAL_ASR_SEGMENTED")
        turn_silence = config.get_float("LOCAL_TURN_SILENCE") if self._segmented_asr else 0.0
        if not math.isfinite(turn_silence):
            raise ValueError("LOCAL_TURN_SILENCE 必须是有限数")
        self._turn_silence_samples = round(turn_silence * self.input_rate)
        self._segment_silence_samples = (
            round(_positive_seconds("LOCAL_ASR_SEGMENT_SILENCE") * self.input_rate) if self._segmented_asr else 0
        )
        self._vad_speaking = False
        self._asr_pending = False
        self._silence_samples = 0
        self._pipeline: SpeechPipeline | None = None
        self._on_audio_out: Callable[[bytes], None] | None = None
        self._running = False
        self._audio_queue: Queue[bytes] = Queue()
        self._brain_queue: Queue[_UserSpeech | _RecognizedSpeech | _SayRequest] = Queue()
        self._asr_queue: Queue[bytes | _AsrEnd] = Queue()
        self._asr_thread: threading.Thread | None = None
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
        self._opening_lock = threading.Lock()
        self._opening_cancelled = threading.Event()
        self._opening: tuple[str, str, str, bytes] | None = None
        self._warm_stop = threading.Event()
        self._warm_thread: threading.Thread | None = None
        self._speculative = config.get_bool("LOCAL_SPECULATIVE_LLM") and self._segmented_asr
        self._speculation: _Speculation | None = None
        self._spec_jobs: Queue[tuple[_Speculation, Callable[[], None]]] = Queue(maxsize=1)
        self._spec_thread: threading.Thread | None = None
        self._spec_lock = threading.RLock()
        self._speech_revision = 0
        self._metrics = config.get_bool("LOCAL_LATENCY_METRICS")
        self._turn_sequence = 0
        self._on_turn_event: Callable[[dict[str, Any]], None] | None = None
        self._opening_duration_ms: float | None = None

    def set_turn_event_handler(self, handler: Callable[[dict[str, Any]], None]) -> None:
        self._on_turn_event = handler

    def _turn_event(self, **fields: Any) -> None:
        if self._metrics and self._on_turn_event is not None:
            try:
                self._on_turn_event(fields)
            except Exception:  # noqa: BLE001 - 埋点不能影响语音
                logger.debug("local 逐轮埋点失败", exc_info=True)

    def _output_pcm(self, pcm: bytes, turn: _Turn) -> None:
        if self._on_audio_out is None:
            return
        if self._metrics:
            tagged = AgentPcm(pcm)
            tagged.on_written = lambda: self._mark_latency(turn, "first_pcm_written")
            pcm = tagged
        self._on_audio_out(pcm)

    def _cancel_speculation(self) -> None:
        with self._spec_lock:
            if self._speculation is not None:
                self._speculation.cancelled.set()

    def _start_speculation(self, text: str, revision: int) -> None:
        # 仅可取消的 DeepSeek 传输支持抢跑；旧同步回调保持原路径。
        if not self._speculative or self._deepseek is None or not text:
            return
        with self._spec_lock:
            previous = self._speculation
            if previous is not None:
                previous.cancelled.set()
        with self._spec_lock:
            if not self._running or revision != self._speech_revision or self._vad_speaking:
                return
            # 已确认但还在等旧连接清理的任务不可被下一段抢跑挤掉。
            try:
                obsolete, runner = self._spec_jobs.get_nowait()
                self._spec_jobs.task_done()
                if obsolete.claimed:
                    self._spec_jobs.put_nowait((obsolete, runner))
                    return
                obsolete.cancelled.set()
            except Empty:
                pass
            with self._messages_lock:
                messages = [*self._messages, {"role": "user", "content": text}]
            job = _Speculation(messages)
            self._speculation = job
            tools = self._tools.specs() if self._tools and self._tools.has_tools() else None

            def generate() -> None:
                assert self._deepseek is not None
                splitter = ClauseSplitter()

                def buffer(delta: str) -> None:
                    at = time.monotonic()
                    if self._streaming and splitter.feed(delta):
                        job.events.put(("clause_ready", at))
                    job.events.put(("content", (delta, at)))

                try:
                    result = self._deepseek.chat(
                        messages, tools, config.get_float("LOCAL_LLM_TIMEOUT"),
                        buffer,
                        job.cancelled, on_tools=lambda: job.events.put(("tools", None)),
                    )
                    if self._streaming and splitter.finish():
                        job.events.put(("clause_ready", time.monotonic()))
                    job.events.put(("result", result))
                except Exception as exc:  # noqa: BLE001 - 由确认后的消费方回退
                    job.events.put(("error", exc))

            # 固定一个生成 worker、最多一项待执行任务；网络取消不能阻塞 ASR。
            job.thread = self._spec_thread
            self._spec_jobs.put_nowait((job, generate))
            logger.info("[local_speculative] started revision=%d", revision)

    def _spec_worker(self) -> None:
        while self._running:
            try:
                job, generate = self._spec_jobs.get(timeout=0.1)
            except Empty:
                continue
            try:
                if not job.cancelled.is_set():
                    generate()
            finally:
                self._spec_jobs.task_done()

    def _consume_speculation(
        self, messages: list[dict], turn: _Turn, batch: _SpeechBatch, on_content: Callable[[str], None],
    ) -> dict[str, Any] | None:
        with self._spec_lock:
            job, self._speculation = self._speculation, None
            if job is not None:
                job.claimed = True
        if job is None:
            return None
        try:
            if job.cancelled.is_set() or job.messages != messages:
                logger.info("[local_speculative] miss reason=changed")
                return None
            logger.info("[local_speculative] hit")
            while self._running and not turn.cancelled.is_set():
                if job.cancelled.is_set():
                    raise InterruptedError("抢跑任务已取消")
                try:
                    kind, value = job.events.get(timeout=0.02)
                except Empty:
                    continue
                if kind == "content":
                    delta, at = value
                    self._mark_latency(turn, "llm_first_token", at=at)
                    on_content(delta)
                elif kind == "tools":
                    self._discard_batch(batch)
                elif kind == "clause_ready":
                    self._mark_latency(turn, "first_clause_ready", at=value)
                elif kind == "result":
                    return dict(value)
                else:
                    raise value
            raise InterruptedError("抢跑轮次已取消")
        finally:
            job.cancelled.set()

    def start_connection_warmup(self) -> None:
        if (not config.get_bool("LOCAL_LLM_PREWARM") or self._deepseek is None
                or self._warm_thread is not None):
            return
        idle = _positive_seconds("LOCAL_LLM_PREWARM_IDLE")

        def warm() -> None:
            assert self._deepseek is not None
            while not self._warm_stop.is_set():
                try:
                    self._deepseek.prewarm(min(3.0, config.get_float("LOCAL_LLM_TIMEOUT")), idle_seconds=idle)
                except Exception as exc:  # noqa: BLE001 - 首轮请求仍可正常重试
                    logger.warning("[local_prewarm] fallback error=%s", type(exc).__name__)
                self._warm_stop.wait(min(1.0, idle))

        self._warm_thread = threading.Thread(target=warm, name="local-prewarm", daemon=True)
        self._warm_thread.start()

    def prepare_opening(self, instructions: str, *, phrase: str = "") -> None:
        """振铃期间只生成文本和 PCM，不写历史、不播放、不执行工具。"""
        if not config.get_bool("LOCAL_FAST_OPENING") or self._opening_cancelled.is_set():
            return
        started = time.monotonic()
        system = self._session_instructions or ""
        try:
            if phrase:
                text = phrase
            else:
                messages = [
                    {"role": "system", "content": system},
                    self._say_message(instructions),
                ]
                timeout = config.get_float("LOCAL_LLM_TIMEOUT")
                if self._deepseek is not None:
                    message = self._deepseek.chat(messages, None, timeout, lambda text: None,
                                                  self._opening_cancelled)
                elif self._llm_stream is not None:
                    message = self._llm_stream(messages, None, timeout, lambda text: None)
                else:
                    message = self._llm_chat(messages, None, timeout)
                if message.get("tool_calls"):
                    raise ValueError("开场预合成不接受工具调用")
                text = str(message.get("content") or "").strip()
            if not text or self._opening_cancelled.is_set():
                return
            spoken = speech_text(text)
            pipeline = self._pipeline or self._pipeline_factory()
            raw_peak = config.get_str("LOCAL_TTS_PEAK").strip()
            pcm = (normalize_peak(pipeline.synthesize(spoken), float(raw_peak) if raw_peak else None)
                   if spoken else b"")
            with self._opening_lock:
                if (pcm or not spoken) and not self._opening_cancelled.is_set():
                    self._opening = (instructions, system, text, pcm)
            logger.info("[local_opening] prepared duration_ms=%.1f", (time.monotonic() - started) * 1000)
            self._opening_duration_ms = round((time.monotonic() - started) * 1000, 1)
        except Exception as exc:  # noqa: BLE001 - 预合成失败不影响原开场
            logger.warning("[local_opening] fallback error=%s", type(exc).__name__)

    @staticmethod
    def _say_message(instructions: str) -> dict[str, Any]:
        return {"role": "user", "content": f"[系统指令，对方听不到这句] {instructions}\n"
                "请直接输出你要对对方说的话本身，不要解释。"}

    async def start(self, on_audio_out: Callable[[bytes], None]) -> None:
        self._on_audio_out = on_audio_out
        self.start_connection_warmup()
        if self._segmented_asr and self._turn_silence_samples <= self._segment_silence_samples:
            logger.warning(
                "LOCAL_TURN_SILENCE (%gs) <= LOCAL_ASR_SEGMENT_SILENCE (%gs)：吐段即判停",
                self._turn_silence_samples / self.input_rate,
                self._segment_silence_samples / self.input_rate,
            )
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
            if self._segmented_asr and not callable(getattr(pipeline, "is_speech_detected", None)):
                raise ValueError("分段识别管线需要 is_speech_detected()")
            for phrase in _cache_phrases().split("|"):
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
        if self._speculative and self._deepseek is not None:
            self._spec_thread = threading.Thread(target=self._spec_worker, name="local-speculative", daemon=True)
            self._spec_thread.start()
        self._vad_thread = threading.Thread(
            target=self._vad_worker, name="local-vad", daemon=True
        )
        self._brain_thread = threading.Thread(
            target=self._brain_worker, name="local-brain", daemon=True
        )
        if self._segmented_asr:
            self._asr_thread = threading.Thread(target=self._asr_worker, name="local-asr", daemon=True)
            self._asr_thread.start()
        self._vad_thread.start()
        self._brain_thread.start()
        if self._streaming:
            self._tts_thread = threading.Thread(target=self._tts_worker, name="local-tts", daemon=True)
            self._tts_thread.start()
        if self._slow_filler_seconds:
            self._filler_thread = threading.Thread(target=self._filler_worker, name="local-slow-filler", daemon=True)
            self._filler_thread.start()
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
            if config.get_bool("LOCAL_FAST_OPENING") and not self._opening_cancelled.is_set():
                with self._opening_lock:
                    self._opening_cancelled.set()
                    opening, self._opening = self._opening, None
                hit = (opening is not None and opening[0] == instructions
                       and opening[1] == (self._session_instructions or ""))
                logger.info("[local_opening] hit=%s", hit)
                self._turn_event(type="local_opening", hit=hit, prepare_ms=self._opening_duration_ms)
                if hit and opening is not None:
                    _, _, text, pcm = opening
                    with self._messages_lock:
                        self._messages.extend([self._say_message(instructions),
                                               {"role": "assistant", "content": text}])
                    self._emit_transcript("agent", text)
                    turn = self._new_turn(None)
                    if pcm:
                        self._mark_latency(turn, "tts_first_audio")
                        self._output_pcm(pcm, turn)
                    return
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
        self._opening_cancelled.set()
        self._warm_stop.set()
        self._filler_stop.set()
        self._cancel_speculation()
        with self._turn_lock:
            self._running = False
            self._cancel_turn()
        if self._deepseek is not None:
            self._deepseek.close()
        # Never block the event loop while a model kernel / network call finishes.
        for thread in (self._vad_thread, self._asr_thread, self._brain_thread, self._tts_thread, self._warm_thread):
            if thread is not None:
                await asyncio.to_thread(thread.join, 3)
        if self._filler_thread is not None:
            await asyncio.to_thread(self._filler_thread.join)
            self._filler_thread = None
        self._vad_thread = None
        self._asr_thread = None
        self._brain_thread = None
        self._tts_thread = None
        self._pipeline = None
        if self._spec_thread is not None:
            await asyncio.to_thread(self._spec_thread.join)
        logger.info("local 三段式 Agent 已停止")

    # ---- worker 线程 ----

    def _vad_worker(self) -> None:
        while self._running:
            try:
                pcm = self._audio_queue.get(timeout=0.2)
            except Empty:
                continue
            try:
                self._process_audio(pcm)
            except Exception as exc:  # noqa: BLE001 - VAD 异常丢帧继续
                logger.warning("VAD 处理失败（丢弃本帧）: %s", exc)
            finally:
                self._audio_queue.task_done()

    def _process_audio(self, pcm: bytes, *, flush: bool = False) -> int:
        pipeline = self._pipeline
        assert pipeline is not None
        segments = pipeline.vad_flush() if flush else pipeline.vad_push(pcm)
        detector = getattr(pipeline, "is_speech_detected", None)
        speaking = bool(detector()) if (self._streaming or self._segmented_asr
                                       or self._slow_filler_seconds) and detector else False
        if self._speculative and speaking and not self._vad_speaking:
            with self._spec_lock:
                self._speech_revision += 1
                self._cancel_speculation()
        if (self._streaming or self._slow_filler_seconds) and not self._vad_speaking and (speaking or segments):
            # Cancellation belongs to responses, never to unsubmitted ASR input.
            with self._turn_lock:
                if self._on_user_interrupt is None or self._emit_user_interrupt():
                    self._cancel_turn()
        self._vad_speaking = speaking
        if not self._segmented_asr:
            for segment in segments:
                self._brain_queue.put(_UserSpeech(segment, self._new_turn(time.monotonic())))
            return len(segments)
        for segment in segments:
            self._asr_pending = True
            self._asr_queue.put(segment)
        if flush:
            self._finish_asr_turn()
        elif speaking:
            self._silence_samples = 0
        elif self._asr_pending:
            if segments:
                # Silero delivers a segment only after observing this tail
                # silence; credit it once, without also counting this frame.
                self._silence_samples = self._segment_silence_samples
            else:
                self._silence_samples += len(pcm) // 2
            if self._silence_samples >= self._turn_silence_samples:
                self._finish_asr_turn()
        return len(segments)

    def _finish_asr_turn(self) -> None:
        if self._asr_pending:
            self._asr_pending = False
            self._silence_samples = 0
            self._asr_queue.put(_AsrEnd(self._new_turn(time.monotonic())))

    def _asr_worker(self) -> None:
        pipeline = self._pipeline
        assert pipeline is not None
        texts: list[str] = []
        completed_at = 0.0
        failed = False
        while self._running:
            try:
                item = self._asr_queue.get(timeout=0.2)
            except Empty:
                continue
            try:
                if isinstance(item, bytes):
                    revision = self._speech_revision
                    try:
                        texts.append(pipeline.transcribe(item))
                    except Exception as exc:  # noqa: BLE001 - do not submit a partial amount/menu
                        failed = True
                        self._cancel_speculation()
                        logger.warning("ASR 小段识别失败（跳过本轮）: %s", exc)
                    finally:
                        completed_at = time.monotonic()
                    if not failed:
                        self._start_speculation("".join(texts), revision)
                else:
                    # The FIFO marker is a barrier after the last segment. If ASR
                    # finished during speech, tail is zero, not marker scheduling time.
                    self._mark_latency(item.turn, "asr_tail", at=completed_at)
                    if not failed and self._running:
                        self._brain_queue.put(_RecognizedSpeech("".join(texts), item.turn))
                    texts = []
                    failed = False
            finally:
                self._asr_queue.task_done()

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
                utterances = [item]
                turn = item.turn
                while True:
                    try:
                        follow = self._brain_queue.get_nowait()
                    except Empty:
                        break
                    self._brain_queue.task_done()
                    if isinstance(follow, _SayRequest):
                        self._brain_queue.put(follow)  # say 不并轮，退回队列
                        break
                    utterances.append(follow)
                    turn = follow.turn
                if isinstance(item, _RecognizedSpeech):
                    self._handle_user_text("".join(
                        u.text for u in utterances if isinstance(u, _RecognizedSpeech)
                    ), turn)
                elif not turn.cancelled.is_set():
                    self._handle_user_speech(b"".join(
                        u.pcm for u in utterances if isinstance(u, _UserSpeech)
                    ), turn)
            except Exception as exc:  # noqa: BLE001 - 单轮失败不杀线程
                logger.warning("brain 处理一轮失败: %s", exc)
            finally:
                self._brain_queue.task_done()

    # ---- 每轮语义 ----

    def _handle_user_speech(self, segment: bytes, turn: _Turn) -> None:
        pipeline = self._pipeline
        assert pipeline is not None
        text = pipeline.transcribe(segment)
        if self._metrics:
            self._mark_latency(turn, "asr_tail")
        if not text or turn.cancelled.is_set() or not self._running:
            return
        self._handle_user_text(text, turn)

    def _handle_user_text(self, text: str, turn: _Turn) -> None:
        # A new utterance can cancel a response before its ASR barrier completes.
        # Preserve recognized input in history even when that response is obsolete.
        if not text or not self._running:
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
            self._messages.append(self._say_message(instructions))
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
            with self._turn_lock:
                if _round == 0 and turn.is_user and self._slow_filler_seconds:
                    self._waiting_batch = batch
            try:
                speculative = self._consume_speculation(messages, turn, batch, on_content) if _round == 0 else None
                if speculative is not None:
                    message = speculative
                elif self._deepseek is not None:
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
            finally:
                with self._turn_lock:
                    if self._waiting_batch is batch:
                        self._waiting_batch = None
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
            self._turn_sequence += 1
            opening = ended_at is None
            if self._metrics and ended_at is None:
                ended_at = time.monotonic()
            self._turn = _Turn(ended_at, self._turn_sequence, is_user=not opening)
            self._turns.add(self._turn)
            self._turn_event(type="local_turn", turn_id=self._turn.sequence, stage="endpoint",
                             at=ended_at, ms=0.0, kind="say" if opening else "user")
            return self._turn

    def _cancel_turn(self) -> None:
        with self._turn_lock:
            for turn in list(self._turns):
                if not turn.cancelled.is_set():
                    self._turn_event(type="local_turn", turn_id=turn.sequence, stage="cancelled",
                                     at=time.monotonic())
                turn.cancelled.set()
            while True:
                try:
                    self._tts_queue.get_nowait()
                except Empty:
                    break
                self._tts_queue.task_done()

    def _mark_latency(self, turn: _Turn, stage: str, *, at: float | None = None) -> None:
        with self._turn_lock:
            if (turn.ended_at is None or stage in turn.stages
                    or turn.cancelled.is_set() and stage != "first_pcm_written"):
                return
            turn.stages.add(stage)
            measured_at = time.monotonic() if at is None else at
            elapsed = measured_at - turn.ended_at
            self._turn_event(type="local_turn", turn_id=turn.sequence, stage=stage,
                             at=measured_at, ms=round(elapsed * 1000, 1))
            self._emit_latency(stage, round(max(0.0, elapsed) * 1000, 1))

    def _discard_batch(self, batch: _SpeechBatch) -> None:
        with self._turn_lock:
            if self._waiting_batch is batch:
                self._waiting_batch = None
            batch.cancelled.set()
            if batch.audio_emitted and not batch.turn.cancelled.is_set() and self._running:
                self._emit_late_cut()

    def _queue_clause(self, text: str, batch: _SpeechBatch) -> None:
        with self._turn_lock:
            if not self._running or batch.turn.cancelled.is_set() or batch.cancelled.is_set():
                return
            batch.turn.reply_started = True
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

    def _filler_worker(self) -> None:
        # 每通仅一个计时 worker，避免上一轮不可取消的合成拖住兜底；停机时 join。
        while not self._filler_stop.wait(0.02):
            try:
                self._maybe_slow_filler()
            except Exception:  # noqa: BLE001 - 输出回调失败不能杀死后续轮次的兜底
                logger.warning("[local_slow_filler] 输出失败", exc_info=True)

    def _maybe_slow_filler(self) -> None:
        # 与取消/正常分句共用锁，锁内只走已预合成的缓存输出。
        with self._turn_lock:
            batch = self._waiting_batch
            if not self._slow_filler_seconds or batch is None:
                return
            turn = batch.turn
            now = time.monotonic()
            if (not self._running or turn.cancelled.is_set()
                    or batch.cancelled.is_set() or turn.reply_started or turn.ended_at is None
                    or now - turn.ended_at < self._slow_filler_seconds
                    or now - self._last_slow_filler_at < 8.0
                    or not self._tts_cache.get(self._slow_filler)):
                return
            # 这里只播放预合成 PCM，绝不在锁内运行模型，也不写 LLM 历史。
            if self._speak(self._slow_filler, batch):
                self._last_slow_filler_at = now
                self._emit_transcript("agent", self._slow_filler)
                self._mark_latency(turn, "slow_filler", at=now)
                logger.info("[local_slow_filler] played turn=%d waited_ms=%.0f",
                            turn.sequence, (now - turn.ended_at) * 1000)

    def _speak(self, text: str, batch: _SpeechBatch) -> bool:
        text = speech_text(text)
        if not text:
            return False
        pipeline = self._pipeline
        if (pipeline is None or self._on_audio_out is None or not self._running
                or batch.cancelled.is_set() or batch.turn.cancelled.is_set()):
            return False
        with self._turn_lock:
            batch.turn.reply_started = True
            self._mark_latency(batch.turn, "first_clause_ready")
        try:
            pcm = self._tts_cache.get(text)
            if pcm is None:
                pcm = normalize_peak(pipeline.synthesize(text), self._peak)
        except Exception as exc:  # noqa: BLE001
            logger.warning("TTS 合成失败: %s", exc)
            return False
        # Generation may finish after stop / a new utterance. Check under the same
        # lock as cancellation so no stale output can cross the cancellation edge.
        with self._turn_lock:
            if pcm and self._running and not batch.cancelled.is_set() and not batch.turn.cancelled.is_set():
                self._mark_latency(batch.turn, "tts_first_audio")
                batch.audio_emitted = True
                self._output_pcm(pcm, batch.turn)
                return True
        return False
