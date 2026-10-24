"""Benchmark a WAV through real local VAD/ASR/configured LLM/TTS, without SIM/Edge.

    .venv/bin/python scripts/local_pipeline_bench.py input.wav --output reply.wav

This is offline *audio input*, not an offline LLM: running it sends recognized
text to the configured provider and consumes API credits. Models may download
on first use. Input is processed faster than real time, not replayed to a device.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
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

    def transcribe(self, segment):
        return self._timed("asr", self.pipeline.transcribe, segment)

    def synthesize(self, text):
        return self._timed("tts_compute", self.pipeline.synthesize, text)


async def run_benchmark(source: Path, output: Path, instructions: str) -> list[tuple[str, float]]:
    if source.resolve() == output.resolve():
        raise ValueError("Output must not overwrite the input WAV")
    pcm = read_wav(source)
    errors = config.validate_provider_credentials("local")
    if errors:
        raise ValueError("; ".join(errors))
    rows: list[tuple[str, float]] = []
    chunks: list[bytes] = []
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
    agent.set_session_instructions(instructions)
    agent.set_latency_handler(lambda stage, ms: rows.append((stage, ms)))
    agent.set_transcript_handler(lambda role, text: print(f"{role}: {text}"))
    start = time.monotonic()
    try:
        await agent.start(chunks.append)
        rows.append(("model_load_warmup_cache", (time.monotonic() - start) * 1000))
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
    parser.add_argument("--instructions", default="你是电话助理。请用简短、自然的中文回答对方。")
    args = parser.parse_args(argv)
    load_dotenv(config.env_file_path())
    # This benchmark opts into clause TTS unless explicitly comparing the baseline.
    # Only this process's environment changes; the user's .env is never modified.
    os.environ["LOCAL_TTS_STREAMING"] = "false" if args.whole_tts else "true"
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    output = args.output or args.wav.with_name(args.wav.stem + ".local-reply.wav")
    print("Uses configured LLM API credits; no SIM, Edge or audio device is used.")
    print(f"LLM={config.get_str('LOCAL_LLM_PROVIDER')}/{config.get_str('LOCAL_LLM_MODEL')} "
          f"TTS={config.get_str('LOCAL_TTS_MODEL')} streaming={config.get_bool('LOCAL_TTS_STREAMING')}")
    try:
        rows = asyncio.run(run_benchmark(args.wav, output, args.instructions))
    except (ValueError, RuntimeError, OSError, wave.Error) as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 1
    stages = dict.fromkeys(stage for stage, _ in rows)
    for stage in stages:
        values = [ms for name, ms in rows if name == stage]
        print(f"{stage:28s} sum={sum(values):9.1f} ms  n={len(values)}  "
              f"min={min(values):.1f} max={max(values):.1f}")
    print(f"WAV: {output}")
    print("Turn stages start at VAD endpoint delivery, like provider speech_stopped (includes ASR).")
    print("VAD compute is measured separately; real-time silence hangover and phone playout are not replayed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
