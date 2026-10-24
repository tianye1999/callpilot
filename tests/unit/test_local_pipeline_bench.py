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


def test_segmented_bench_reuses_runtime_asr_and_reports_tail(tmp_path, monkeypatch):
    bench = load_bench()
    transcripts, slept = [], []

    class Pipeline:
        sample_rate = 8000
        count = 0

        def vad_push(self, pcm):
            self.count += 1
            if self.count == 1:
                return [b"first"]
            if self.count == 3:
                return [b"second"]
            return []

        def is_speech_detected(self):
            return self.count == 2

        def vad_flush(self):
            return []

        def transcribe(self, pcm):
            return pcm.decode()

        def synthesize(self, text):
            return b"\1\0" * 10

    async def no_wait(seconds):
        slept.append(seconds)

    def chat(messages, *args):
        transcripts.append(messages[-1]["content"])
        return {"role": "assistant", "content": "好的。"}

    monkeypatch.setenv("LOCAL_ASR_SEGMENTED", "true")
    monkeypatch.setenv("LOCAL_LLM_PROVIDER", "dashscope")
    monkeypatch.setattr(bench, "_SherpaPipeline", lambda **kw: Pipeline())
    monkeypatch.setattr(bench, "_default_llm_chat", chat)
    monkeypatch.setattr(bench.asyncio, "sleep", no_wait)
    source, output = tmp_path / "in.wav", tmp_path / "out.wav"
    with wave.open(str(source), "wb") as wav:
        wav.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        wav.writeframes(b"\1\0" * 1600)
    rows = asyncio.run(bench.run_benchmark(source, output, "test"))
    assert transcripts == ["firstsecond"]
    assert {"asr_tail", "asr", "tts_first_audio"} <= {name for name, _ in rows}
    assert slept and all(s >= 0 for s in slept)
    assert sum(slept) > 0  # Real runs are paced; this test never actually sleeps.


def test_segmented_cli_opts_in_and_prints_tail(monkeypatch, capsys):
    bench = load_bench()

    async def run(*args):
        assert bench.config.get_bool("LOCAL_ASR_SEGMENTED")
        return [("asr_tail", 120.0)]

    monkeypatch.setattr(bench, "run_benchmark", run)
    monkeypatch.setattr(bench, "load_dotenv", lambda *a: None)
    monkeypatch.setenv("LOCAL_ASR_SEGMENTED", "false")
    monkeypatch.setenv("LOCAL_TTS_STREAMING", "false")
    assert bench.main(["fake.wav", "--segmented"]) == 0
    output = capsys.readouterr().out
    assert "asr_tail" in output and "120.0" in output
