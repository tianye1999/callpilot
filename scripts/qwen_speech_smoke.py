"""真实调用一次云端 TTS 与 ASR，验证 DashScope realtime 语音链路。

仅从项目 .env 读取 DASHSCOPE_API_KEY，不打印 Key、不打印通话内容全文。
TTS 输出 8k PCM 到 /tmp/qwen_tts_smoke.wav，并打印首包延迟；ASR 接收一个 wav
路径参数，打印识别耗时与脱敏后的转写文本。

用法::

    .venv/bin/python scripts/qwen_speech_smoke.py /path/to/audio.wav
"""

from __future__ import annotations

import argparse
import re
import sys
import time
import wave
from pathlib import Path

import numpy as np
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agentcall.agents.qwen_speech import (  # noqa: E402 - 需要先插入 src 路径
    QwenAsrClient,
    QwenTtsClient,
    dashscope_api_key,
)

TTS_MODEL = "qwen3-tts-flash-realtime"
ASR_MODEL = "qwen3-asr-flash-realtime"
VOICE = "Kai"
TTS_OUT = Path("/tmp/qwen_tts_smoke.wav")

_REDACT = re.compile(r"[0-9]{3,}")


def _redact(text: str) -> str:
    return _REDACT.sub("***", text)


def _resample_to_8k(pcm16: bytes, src_rate: int) -> bytes:
    samples = np.frombuffer(pcm16, dtype="<i2").astype(np.float32)
    if src_rate == 8000 or not samples.size:
        return pcm16
    dst_len = max(1, int(len(samples) * 8000 / src_rate))
    out = np.interp(np.linspace(0.0, 1.0, num=dst_len, endpoint=False),
                    np.linspace(0.0, 1.0, num=len(samples), endpoint=False), samples)
    return out.astype("<i2").tobytes()


def _read_wav(path: Path) -> tuple[int, bytes]:
    with wave.open(str(path), "rb") as w:
        rate = w.getframerate()
        if w.getsampwidth() != 2:
            raise RuntimeError(f"仅支持 16-bit PCM wav，实际 {w.getsampwidth() * 8} bit")
        if w.getnchannels() != 1:
            raise RuntimeError(f"仅支持单声道 wav，实际 {w.getnchannels()} 声道")
        return rate, w.readframes(w.getnframes())


def _write_wav(path: Path, pcm16: bytes, rate: int) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm16)


def run_tts(key: str) -> None:
    client = QwenTtsClient(TTS_MODEL, VOICE, key)
    chunks: list[bytes] = []
    first_ms: float | None = None
    t0 = time.perf_counter()

    def on_audio(pcm: bytes) -> None:
        nonlocal first_ms
        if pcm:
            if first_ms is None:
                first_ms = (time.perf_counter() - t0) * 1000
            chunks.append(pcm)

    client.warmup()
    done = client.synthesize_stream("您好，请问有什么可以帮您？", on_audio, lambda: False)
    client.close()
    pcm = b"".join(chunks)
    if not done or not pcm:
        raise RuntimeError("TTS 未收到完整音频")
    _write_wav(TTS_OUT, pcm, 8000)
    print(f"[qwen_tts] voice={VOICE} first_packet_ms={first_ms:.1f} "
          f"bytes={len(pcm)} done={done} wav={TTS_OUT}")


def run_asr(key: str, wav_path: Path) -> None:
    rate, pcm = _read_wav(wav_path)
    pcm8k = _resample_to_8k(pcm, rate)
    client = QwenAsrClient(ASR_MODEL, key)
    client.warmup()
    started = time.perf_counter()
    text = client.one_shot(pcm8k)
    elapsed_ms = (time.perf_counter() - started) * 1000
    client.close()
    print(f"[qwen_asr] model={ASR_MODEL} src_rate={rate} duration_ms={elapsed_ms:.1f} "
          f"chars={len(text)} transcript={_redact(text)}")


def main() -> int:
    parser = argparse.ArgumentParser(description="DashScope 语音冒烟：TTS + ASR")
    parser.add_argument("wav", type=Path, help="ASR 输入的 16-bit 单声道 wav")
    args = parser.parse_args()
    load_dotenv(ROOT / ".env")
    key = dashscope_api_key()
    run_tts(key)
    run_asr(key, args.wav)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
