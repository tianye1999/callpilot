"""The standalone benchmark runs without modem, Edge, models or network in CI."""

import asyncio
import importlib.util
import threading
import wave
from pathlib import Path

import numpy as np
import pytest
from test_local_agent import FakePipeline


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


def test_opening_bench_reports_turn_without_assuming_opening_event_has_turn_id(tmp_path, monkeypatch, capsys):
    bench = load_bench()
    monkeypatch.setenv("LOCAL_FAST_OPENING", "true")
    monkeypatch.setenv("LOCAL_LLM_PROVIDER", "dashscope")
    monkeypatch.setattr(bench, "_SherpaPipeline", lambda **kw: FakePipeline())
    monkeypatch.setattr(bench, "_default_llm_chat", lambda *a: {"content": "您好。"})
    source, output = tmp_path / "in.wav", tmp_path / "out.wav"
    with wave.open(str(source), "wb") as wav:
        wav.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        wav.writeframes(b"\1\0" * 1600)
    rows = asyncio.run(bench.run_benchmark(source, output, "问候", "opening"))
    assert "first_pcm_written" in dict(rows)
    assert "turn=1 slow_filler=no filler_at=- first_audio=" in capsys.readouterr().out


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


def test_segmented_bench_empty_tail_does_not_cancel_measurement(tmp_path, monkeypatch):
    bench = load_bench()
    tail = threading.Event()

    class Pipeline:
        sample_rate = 8000
        count = 0

        def vad_push(self, pcm):
            self.count += 1
            if self.count == 1:
                return [b"first"]
            if self.count == 21:
                tail.set()
                return [b"empty"]
            return []

        def is_speech_detected(self):
            return self.count == 20

        def vad_flush(self):
            return []

        def transcribe(self, pcm):
            return "测试" if pcm == b"first" else ""

        def synthesize(self, text):
            return b"\1\0" * 10

    sleep = asyncio.sleep

    async def no_wait(seconds):
        await sleep(0)

    def chat(*args):
        assert tail.wait(1)
        return {"role": "assistant", "content": "好的。"}

    monkeypatch.setenv("LOCAL_ASR_SEGMENTED", "true")
    monkeypatch.setenv("LOCAL_TTS_STREAMING", "true")
    monkeypatch.setenv("LOCAL_LLM_PROVIDER", "dashscope")
    monkeypatch.setattr(bench, "_SherpaPipeline", lambda **kw: Pipeline())
    monkeypatch.setattr(bench, "_default_llm_chat", chat)
    monkeypatch.setattr(bench.asyncio, "sleep", no_wait)
    source, output = tmp_path / "in.wav", tmp_path / "out.wav"
    with wave.open(str(source), "wb") as wav:
        wav.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        wav.writeframes(b"\1\0" * 16000)
    rows = asyncio.run(bench.run_benchmark(source, output, "test"))
    assert "tts_first_audio" in dict(rows)


def test_opening_cli_repeats_and_reports_median_and_range(monkeypatch, capsys):
    bench = load_bench()
    samples = iter([10.0, 30.0, 20.0])

    async def run(source, output, instructions, scenario, barge_in, llm_delay_ms):
        assert scenario == "opening" and not barge_in
        assert llm_delay_ms == 0
        return [("answered_to_first_audio", next(samples))]

    monkeypatch.setattr(bench, "run_benchmark", run)
    monkeypatch.setattr(bench, "load_dotenv", lambda *a: None)
    assert bench.main(["fake.wav", "--scenario", "opening", "--runs", "3"]) == 0
    assert "median=20.0 min=10.0 max=30.0" in capsys.readouterr().out


@pytest.mark.parametrize("option", ["-1", "nan", "inf"])
def test_bench_rejects_invalid_llm_delay(option):
    with pytest.raises(SystemExit):
        load_bench().main(["fake.wav", "--llm-delay-ms", option])


def test_bench_cli_passes_llm_delay(monkeypatch, capsys):
    bench = load_bench()

    async def run(source, output, instructions, scenario, barge_in, llm_delay_ms):
        assert llm_delay_ms == 4000
        return []

    monkeypatch.setattr(bench, "run_benchmark", run)
    monkeypatch.setattr(bench, "load_dotenv", lambda *a: None)
    assert bench.main(["fake.wav", "--llm-delay-ms", "4000"]) == 0
    assert "llm_delay_ms=4000" in capsys.readouterr().out


@pytest.mark.parametrize("signal", ["turn", "stop"])
def test_bench_llm_delay_is_cancellable(signal):
    bench = load_bench()
    cancelled, stopped, done = threading.Event(), threading.Event(), threading.Event()

    def wait():
        with pytest.raises(InterruptedError):
            bench.wait_llm_delay(4000, cancelled, stopped)
        done.set()

    worker = threading.Thread(target=wait)
    worker.start()
    (cancelled if signal == "turn" else stopped).set()
    worker.join(1)
    assert not worker.is_alive() and done.is_set()
