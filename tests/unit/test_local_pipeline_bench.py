"""The standalone benchmark runs without modem, Edge, models or network in CI."""

import asyncio
import importlib.util
import wave
from pathlib import Path

import numpy as np
import pytest


def load_bench():
    path = Path(__file__).resolve().parents[2] / "scripts/local_pipeline_bench.py"
    spec = importlib.util.spec_from_file_location("local_pipeline_bench", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_bench_uses_vad_flush_asr_configured_llm_and_writes_tts(tmp_path, monkeypatch):
    bench = load_bench()
    calls = []

    class Pipeline:
        sample_rate = 22050

        def vad_push(self, pcm):
            calls.append("vad")
            return []

        def vad_flush(self):
            calls.append("flush")
            return [b"segment"]

        def transcribe(self, segment):
            calls.append("asr")
            return "你好"

        def synthesize(self, text):
            calls.append(text)
            return b"\x01\x00" * 100

    monkeypatch.setattr(bench, "_SherpaPipeline", lambda **kw: Pipeline())
    monkeypatch.setattr(bench, "_default_llm_chat", lambda *args: {"role": "assistant", "content": "您好，再见。"})
    monkeypatch.setenv("LOCAL_LLM_PROVIDER", "dashscope")
    monkeypatch.setenv("LOCAL_TTS_STREAMING", "true")
    source, output = tmp_path / "in.wav", tmp_path / "out.wav"
    with wave.open(str(source), "wb") as wav:
        wav.setparams((2, 2, 8000, 0, "NONE", "not compressed"))
        wav.writeframes(np.ones(800, dtype="<i2").tobytes())
    result = asyncio.run(bench.run_benchmark(source, output, "test instructions"))
    assert "asr" in calls and "flush" in calls
    assert calls[-2:] == ["您好，", "再见。"]
    assert {"vad_compute", "asr", "llm_total", "tts_compute", "llm_first_token",
            "first_clause_ready", "tts_first_audio"} <= {row[0] for row in result}
    with wave.open(str(output), "rb") as wav:
        assert (wav.getnchannels(), wav.getsampwidth(), wav.getframerate(), wav.getnframes()) == (1, 2, 22050, 200)


def test_bench_rejects_empty_audio(tmp_path):
    bench = load_bench()
    source = tmp_path / "empty.wav"
    with wave.open(str(source), "wb") as wav:
        wav.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
    with pytest.raises(ValueError, match="empty"):
        bench.read_wav(source)
