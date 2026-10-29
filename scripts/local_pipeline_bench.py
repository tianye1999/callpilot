"""Benchmark a WAV through real local VAD/ASR/configured LLM/TTS, without SIM/Edge.

    .venv/bin/python scripts/local_pipeline_bench.py input.wav --output reply.wav

This is offline *audio input*, not an offline LLM: running it sends recognized
text to the configured provider and consumes API credits. Models may download
on first use. Input is not replayed to a device. The default is faster than real
time; --segmented paces input in real time to overlap ASR with incoming speech.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import math
import os
import platform
import statistics
import sys
import time
import wave
from pathlib import Path

import numpy as np
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agentcall import config  # noqa: E402
from agentcall.agents.local_agent import (  # noqa: E402
    LocalPipelineAgent,
    _default_llm_chat,
    _SherpaPipeline,
)
from agentcall.agents.local_llm import DeepSeekChat  # noqa: E402
from agentcall.audio_bridge import resample_pcm  # noqa: E402


def read_wav(path: Path) -> bytes:
    with wave.open(str(path), "rb") as wav:
        if wav.getsampwidth() != 2 or wav.getcomptype() != "NONE":
            raise ValueError("Input must be an uncompressed 16-bit PCM WAV")
        channels, rate = wav.getnchannels(), wav.getframerate()
        pcm = wav.readframes(wav.getnframes())
    if not pcm:
        raise ValueError("Input WAV is empty")
    if channels != 1:
        samples = np.frombuffer(pcm, dtype="<i2").reshape(-1, channels)
        pcm = samples.astype("float32").mean(axis=1).astype("<i2").tobytes()
    return resample_pcm(pcm, rate, 16000)


class TimedPipeline:
    def __init__(self, pipeline, rows):
        self.pipeline = pipeline
        self.sample_rate = pipeline.sample_rate
        self.rows = rows

    def _timed(self, stage, fn, *args):
        start = time.monotonic()
        try:
            return fn(*args)
        finally:
            self.rows.append((stage, (time.monotonic() - start) * 1000))

    def vad_push(self, pcm):
        return self._timed("vad_compute", self.pipeline.vad_push, pcm)

    def vad_flush(self):
        return self._timed("vad_compute", self.pipeline.vad_flush)

    def is_speech_detected(self):
        return self.pipeline.is_speech_detected()

    def transcribe(self, segment):
        return self._timed("asr", self.pipeline.transcribe, segment)

    def synthesize(self, text):
        return self._timed("tts_compute", self.pipeline.synthesize, text)


async def run_benchmark(
    source: Path, output: Path, instructions: str, scenario: str = "turn", barge_in: bool = False,
) -> list[tuple[str, float]]:
    if source.resolve() == output.resolve():
        raise ValueError("Output must not overwrite the input WAV")
    pcm = read_wav(source)
    errors = config.validate_provider_credentials("local")
    if errors:
        raise ValueError("; ".join(errors))
    rows: list[tuple[str, float]] = []
    chunks: list[bytes] = []
    opening_at: float | None = None

    def audio_out(pcm: bytes) -> None:
        if not chunks and opening_at is not None:
            rows.append(("answered_to_first_audio", (time.monotonic() - opening_at) * 1000))
        chunks.append(pcm)
    client = DeepSeekChat() if config.get_str("LOCAL_LLM_PROVIDER") == "deepseek" else None

    def build(**kwargs):
        return TimedPipeline(_SherpaPipeline(**kwargs), rows)

    def chat(messages, tools, timeout, on_content):
        start = time.monotonic()
        try:
            if client is not None:
                return client.chat(messages, tools, timeout, on_content)
            return _default_llm_chat(messages, tools, timeout)
        finally:
            rows.append(("llm_total", (time.monotonic() - start) * 1000))

    agent = LocalPipelineAgent(pipeline_factory=build, llm_stream=chat)
    # 使用实际可取消的传输；不能通过旧四参回调把预热/抢跑能力遮掉。
    if client is not None:
        agent._deepseek = client
    agent.set_session_instructions(instructions)
    # 无设备基准默认禁用打断；这不是 CallSession 半双工调度的端到端模拟。
    agent.set_user_interrupt_handler(lambda: barge_in)
    agent.set_latency_handler(lambda stage, ms: rows.append((stage, ms)))
    agent.set_transcript_handler(lambda role, text: print(f"{role}: {text}"))
    start = time.monotonic()
    try:
        await agent.start(audio_out)
        rows.append(("model_load_warmup_cache", (time.monotonic() - start) * 1000))
        if scenario == "opening" and config.get_bool("LOCAL_FAST_OPENING"):
            started = time.monotonic()
            await asyncio.to_thread(agent.prepare_opening, instructions)
            rows.append(("opening_prepare", (time.monotonic() - started) * 1000))
        ready_at = time.monotonic()
        pipeline = agent._pipeline
        assert pipeline is not None
        segments_seen = 0

        async def process(segments):
            nonlocal segments_seen
            for segment in segments:
                segments_seen += 1
                turn = agent._new_turn(time.monotonic())
                await asyncio.to_thread(agent._handle_user_speech, segment, turn)
                # Wait for the final clause as well, not just first audio / transcript.
                await asyncio.to_thread(agent._tts_queue.join)
                if agent._llm_failures:
                    raise RuntimeError("LLM failed; see the preceding provider error")

        # Pace only the segmented comparison: otherwise a fast WAV producer
        # artificially queues every segment before the ASR worker can catch up.
        if scenario == "opening":
            opening_at = time.monotonic()
            await agent.say(instructions)
            await asyncio.to_thread(agent._brain_queue.join)
            await asyncio.to_thread(agent._tts_queue.join)
            segments_seen = 1
        elif config.get_bool("LOCAL_ASR_SEGMENTED"):
            padding = max(1.0, config.get_float("LOCAL_TURN_SILENCE")
                          + config.get_float("LOCAL_ASR_SEGMENT_SILENCE") + 0.1)
            padded = pcm + b"\0\0" * round(padding * 16000)
            for offset in range(0, len(padded), 1024):
                frame = padded[offset:offset + 1024]
                segments_seen += await asyncio.to_thread(agent._process_audio, frame)
                deadline = ready_at + (offset + len(frame)) / 32000
                await asyncio.sleep(max(0.0, deadline - time.monotonic()))
            segments_seen += await asyncio.to_thread(agent._process_audio, b"", flush=True)
            await asyncio.to_thread(agent._asr_queue.join)
            await asyncio.to_thread(agent._brain_queue.join)
            await asyncio.to_thread(agent._tts_queue.join)
            if agent._llm_failures:
                raise RuntimeError("LLM failed; see the preceding provider error")
        else:
            # Append silence so the last phrase can endpoint normally; flush any tail.
            padded = pcm + b"\0\0" * 16000
            for offset in range(0, len(padded), 1024):
                await process(pipeline.vad_push(padded[offset:offset + 1024]))
            await process(pipeline.vad_flush())
        if not segments_seen:
            raise RuntimeError("VAD found no speech in the input WAV")
        if not chunks:
            raise RuntimeError("No reply audio generated (empty ASR, LLM or TTS result)")
        output.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(output), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(agent.output_rate)
            wav.writeframes(b"".join(chunks))
        rows.append(("total_without_startup", (time.monotonic() - ready_at) * 1000))
        return rows
    finally:
        await agent.stop()
        if client is not None:
            client.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wav", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--whole-tts", action="store_true", help="Use whole-response TTS as a baseline")
    parser.add_argument("--segmented", action="store_true", help="Segmented ASR with real-time paced input")
    parser.add_argument("--scenario", choices=("turn", "opening"), default="turn")
    parser.add_argument("--speculative", action="store_true", help="Enable segmented speculative DeepSeek")
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--turn-silence", type=float, help="Override segmented endpoint silence for this process")
    parser.add_argument("--barge-in", action="store_true", help="Allow resumed VAD to cancel pending replies")
    parser.add_argument("--instructions", default="你是电话助理。请用简短、自然的中文回答对方。")
    args = parser.parse_args(argv)
    if args.runs < 1:
        parser.error("--runs must be positive")
    if args.turn_silence is not None:
        if not math.isfinite(args.turn_silence) or args.turn_silence <= 0:
            parser.error("--turn-silence must be finite and positive")
        os.environ["LOCAL_TURN_SILENCE"] = str(args.turn_silence)
    load_dotenv(config.env_file_path())
    # This benchmark opts into clause TTS unless explicitly comparing the baseline.
    # Only this process's environment changes; the user's .env is never modified.
    os.environ["LOCAL_TTS_STREAMING"] = "false" if args.whole_tts else "true"
    if args.segmented or args.speculative:
        os.environ["LOCAL_ASR_SEGMENTED"] = "true"
    if args.speculative:
        os.environ["LOCAL_SPECULATIVE_LLM"] = "true"
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    output = args.output or args.wav.with_name(args.wav.stem + ".local-reply.wav")
    print("Uses configured LLM API credits; no SIM, Edge or audio device is used.")
    print(f"Architecture={platform.machine()} scenario={args.scenario} runs={args.runs}")
    print(f"LLM={config.get_str('LOCAL_LLM_PROVIDER')}/{config.get_str('LOCAL_LLM_MODEL')} "
          f"TTS={config.get_str('LOCAL_TTS_MODEL')} streaming={config.get_bool('LOCAL_TTS_STREAMING')} "
          f"segmented={config.get_bool('LOCAL_ASR_SEGMENTED')}")
    print(f"turn_silence={config.get_float('LOCAL_TURN_SILENCE')} "
          f"speculative={config.get_bool('LOCAL_SPECULATIVE_LLM')} "
          f"prewarm={config.get_bool('LOCAL_LLM_PREWARM')} fast_opening={config.get_bool('LOCAL_FAST_OPENING')}")
    try:
        rows = []
        for _ in range(args.runs):
            arguments = (args.wav, output, args.instructions)
            rows.extend(asyncio.run(run_benchmark(*arguments, args.scenario, args.barge_in))
                        if args.scenario != "turn" or args.barge_in
                        else asyncio.run(run_benchmark(*arguments)))
    except (ValueError, RuntimeError, OSError, wave.Error) as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 1
    stages = dict.fromkeys(stage for stage, _ in rows)
    for stage in stages:
        values = [ms for name, ms in rows if name == stage]
        print(f"{stage:28s} sum={sum(values):9.1f} ms  n={len(values)}  "
              f"median={statistics.median(values):.1f} min={min(values):.1f} max={max(values):.1f}")
    print(f"WAV: {output}")
    print("Turn stages start at turn endpoint, like provider speech_stopped (includes remaining ASR tail).")
    print("Segmented input is real-time paced; baseline input is faster than real time. Phone playout is not replayed.")
    print(f"Barge-in={args.barge_in} (synthetic, not CallSession duplex); "
          "opening assumes models/media ready and preparation finished before answer.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
