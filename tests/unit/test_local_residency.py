"""Resident speech models: fake sherpa kernels, real cache/lifecycle code, no downloads."""

from __future__ import annotations

import asyncio
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import numpy as np
import pytest

from agentcall import local_models
from agentcall.agents import local_agent


@pytest.fixture
def sherpa(monkeypatch, tmp_path):
    counts = {"asr": 0, "tts": 0, "vad": 0}
    generated = []
    decoded = []

    def recognizer(**kwargs):
        counts["asr"] += 1
        return SimpleNamespace(
            create_stream=lambda: SimpleNamespace(
                accept_waveform=lambda *a: None, result=SimpleNamespace(text=" recognized "),
            ),
            decode_stream=lambda stream: decoded.append(stream),
        )

    def generate(text, sid, speed):
        generated.append((text, sid))
        return SimpleNamespace(samples=np.array([0.5, -0.5], dtype="float32"))

    def tts(cfg):
        counts["tts"] += 1
        return SimpleNamespace(sample_rate=8000, generate=generate)

    def vad(*args, **kwargs):
        counts["vad"] += 1
        return object()

    fake = SimpleNamespace(
        VadModelConfig=lambda: SimpleNamespace(silero_vad=SimpleNamespace()),
        VoiceActivityDetector=vad,
        OfflineRecognizer=SimpleNamespace(from_paraformer=recognizer),
        OfflineTts=tts,
    )
    monkeypatch.setitem(sys.modules, "sherpa_onnx", fake)
    monkeypatch.setattr(local_models, "missing_assets", lambda: [])
    monkeypatch.setattr(local_models, "build_tts_config", lambda *a, **kw: {})
    monkeypatch.setenv("LOCAL_MODELS_DIR", str(tmp_path))
    monkeypatch.setenv("LOCAL_TTS_MODEL", "vits-icefall-zh-aishell3")
    monkeypatch.setenv("LOCAL_TTS_SPEAKER_ID", "25")
    monkeypatch.setenv("LOCAL_TTS_CACHE_PHRASES", "请稍等。|你好。|请稍等。")
    monkeypatch.setenv("LOCAL_TTS_PEAK", "")
    return SimpleNamespace(api=fake, counts=counts, generated=generated, decoded=decoded)


def test_agents_reuse_resident_models_but_not_vad(sherpa):
    pipelines = []
    for _ in range(2):
        agent = local_agent.LocalPipelineAgent(llm_chat=lambda *a: {})
        audio = []
        asyncio.run(agent.start(audio.append))
        try:
            pipelines.append(agent._pipeline)
            assert agent.output_rate == 8000
            assert agent._tts_cache["请稍等。"]
            assert audio == []
        finally:
            asyncio.run(agent.stop())
    assert pipelines[0] is not pipelines[1]
    assert pipelines[0]._vad is not pipelines[1]._vad
    assert sherpa.counts == {"asr": 1, "tts": 1, "vad": 2}
    assert sherpa.generated == [("你好。", 25), ("请稍等。", 25)]
    assert pipelines[0].transcribe(b"\0\0") == "recognized"
    assert pipelines[1].transcribe(b"\0\0") == "recognized"
    assert len(sherpa.decoded) == 2


def test_slow_filler_is_precached_once_across_calls(sherpa, monkeypatch):
    monkeypatch.setenv("LOCAL_SLOW_REPLY_FILLER_SECONDS", "2.5")
    monkeypatch.setenv("LOCAL_SLOW_REPLY_FILLER", "嗯，您稍等。")
    for _ in range(2):
        agent = local_agent.LocalPipelineAgent(llm_chat=lambda *a: {})
        asyncio.run(agent.start(lambda _: None))
        try:
            assert agent._tts_cache["嗯，您稍等。"]
        finally:
            asyncio.run(agent.stop())
    assert sherpa.generated.count(("嗯，您稍等。", 25)) == 1
    monkeypatch.setenv("LOCAL_SLOW_REPLY_FILLER", "请稍候。")
    local_agent._get_shared_models()
    assert ("请稍候。", 25) in sherpa.generated


def test_default_aishell3_downloads_missing_assets_and_uses_owner_speaker(
    sherpa, monkeypatch, product_latency_defaults,
):
    selected = local_models.selected_assets()
    assert "vits-icefall-zh-aishell3" in selected[-1].url
    assert "chaowen" not in selected[-1].url
    downloaded = []
    monkeypatch.setattr(local_models, "missing_assets", lambda: [selected[-1]])
    monkeypatch.setattr(local_models, "ensure_asset", lambda asset: downloaded.append(asset))
    agent = local_agent.LocalPipelineAgent(llm_chat=lambda *a: {})
    asyncio.run(agent.start(lambda pcm: None))
    try:
        assert downloaded == [selected[-1]]
        assert agent.output_rate == 8000
        assert sherpa.generated and all(speaker == 25 for _, speaker in sherpa.generated)
        assert not agent.fatal
    finally:
        asyncio.run(agent.stop())


def test_default_model_download_failure_is_explicit_not_silent_voice_fallback(
    sherpa, monkeypatch, product_latency_defaults,
):
    monkeypatch.setattr(local_models, "missing_assets", lambda: [local_models.selected_assets()[-1]])

    def unavailable(asset):
        raise OSError("download unavailable")

    monkeypatch.setattr(local_models, "ensure_asset", unavailable)
    statuses = []
    agent = local_agent.LocalPipelineAgent(llm_chat=lambda *a: {})
    agent.set_status_handler(statuses.append)
    try:
        with pytest.raises(OSError, match="download unavailable"):
            asyncio.run(agent.start(lambda pcm: None))
        assert agent.fatal and agent._brain_thread is None
        assert any("初始化失败" in status for status in statuses)
        assert not sherpa.generated
    finally:
        asyncio.run(agent.stop())


@pytest.mark.parametrize("key,value", [
    ("LOCAL_MODELS_DIR", "/different-model-root"),
    ("LOCAL_TTS_MODEL", "matcha-icefall-zh-baker"),
    ("LOCAL_TTS_SPEAKER_ID", "26"),
    ("LOCAL_TTS_CACHE_PHRASES", "新的短句。"),
])
def test_speech_config_change_rebuilds_models(sherpa, monkeypatch, key, value):
    local_agent._SherpaPipeline()
    monkeypatch.setenv(key, value)
    local_agent._SherpaPipeline()
    assert sherpa.counts == {"asr": 2, "tts": 2, "vad": 2}


def test_llm_and_peak_changes_do_not_reload_speech_models(sherpa, monkeypatch):
    local_agent._SherpaPipeline()
    monkeypatch.setenv("LOCAL_LLM_MODEL", "another-llm")
    monkeypatch.setenv("LOCAL_TTS_PEAK", "0.9")
    agent = local_agent.LocalPipelineAgent(llm_chat=lambda *a: {})
    asyncio.run(agent.start(lambda pcm: None))
    try:
        assert max(np.frombuffer(agent._tts_cache["请稍等。"], dtype="<i2")) > 29000
    finally:
        asyncio.run(agent.stop())
    assert sherpa.counts == {"asr": 1, "tts": 1, "vad": 2}


def test_reset_hook_discards_resident_models(sherpa):
    local_agent._SherpaPipeline()
    local_agent.reset_shared_speech_models()
    local_agent._SherpaPipeline()
    assert sherpa.counts == {"asr": 2, "tts": 2, "vad": 2}


def test_concurrent_builds_publish_only_one_model_set(sherpa):
    with ThreadPoolExecutor(max_workers=4) as pool:
        pipelines = list(pool.map(lambda _: local_agent._SherpaPipeline(), range(4)))
    assert len({id(p._vad) for p in pipelines}) == 4
    assert sherpa.counts == {"asr": 1, "tts": 1, "vad": 4}
    assert sherpa.generated == [("你好。", 25), ("请稍等。", 25)]


@pytest.mark.parametrize("operation", ["asr", "tts"])
def test_shared_kernels_are_serialized(sherpa, monkeypatch, operation):
    first, second = local_agent._SherpaPipeline(), local_agent._SherpaPipeline()
    entered, release, second_acquiring = threading.Event(), threading.Event(), threading.Event()
    finished = []
    models = first._models
    real_lock = threading.Lock()

    class ObservedLock:
        def __enter__(self):
            if entered.is_set():
                second_acquiring.set()
            real_lock.acquire()

        def __exit__(self, *args):
            real_lock.release()

    def kernel(*args, **kwargs):
        entered.set()
        assert release.wait(1)
        finished.append(True)
        return SimpleNamespace(samples=np.zeros(2, dtype="float32"))

    if operation == "asr":
        monkeypatch.setattr(models, "_asr_lock", ObservedLock())
        monkeypatch.setattr(models._recognizer, "decode_stream", kernel)
    else:
        monkeypatch.setattr(models, "_tts_lock", ObservedLock())
        monkeypatch.setattr(models._tts, "generate", kernel)

    def invoke(pipeline):
        return pipeline.transcribe(b"\0\0") if operation == "asr" else pipeline.synthesize("uncached")

    with ThreadPoolExecutor(max_workers=2) as pool:
        one = pool.submit(invoke, first)
        try:
            assert entered.wait(1)
            two = pool.submit(invoke, second)
            assert second_acquiring.wait(1)
            assert not finished
        finally:
            release.set()
        one.result(timeout=1)
        two.result(timeout=1)
    assert len(finished) == 2


def test_preload_is_nonblocking_and_reused_by_first_call(sherpa, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    original = sherpa.api.OfflineTts

    def delayed(cfg):
        entered.set()
        assert release.wait(1)
        return original(cfg)

    monkeypatch.setattr(sherpa.api, "OfflineTts", delayed)
    thread = local_agent.start_model_preload()
    try:
        assert thread.daemon
        assert entered.wait(1)
        assert thread.is_alive()  # The caller already returned while loading is blocked.
    finally:
        release.set()
        thread.join(1)
    assert not thread.is_alive()
    assert sherpa.counts == {"asr": 1, "tts": 1, "vad": 0}
    local_agent._SherpaPipeline()
    assert sherpa.counts == {"asr": 1, "tts": 1, "vad": 1}


def test_failed_preload_logs_and_foreground_retries_with_progress(sherpa, monkeypatch, caplog):
    original = sherpa.api.OfflineTts

    def broken(cfg):
        raise RuntimeError("fake load failure")

    monkeypatch.setattr(sherpa.api, "OfflineTts", broken)
    thread = local_agent.start_model_preload()
    thread.join(1)
    assert not thread.is_alive()
    assert "fake load failure" in caplog.text
    monkeypatch.setattr(sherpa.api, "OfflineTts", original)
    progress = []
    agent = local_agent.LocalPipelineAgent(llm_chat=lambda *a: {})
    agent.set_status_handler(progress.append)
    asyncio.run(agent.start(lambda pcm: None))
    try:
        assert not agent.fatal
        assert progress
        assert sherpa.counts == {"asr": 2, "tts": 1, "vad": 1}
    finally:
        asyncio.run(agent.stop())


def test_first_call_joins_inflight_preload_without_duplicate_load(sherpa, monkeypatch):
    entered, release, progress_seen = threading.Event(), threading.Event(), threading.Event()
    original = sherpa.api.OfflineTts

    def delayed(cfg):
        entered.set()
        assert release.wait(2)
        return original(cfg)

    monkeypatch.setattr(sherpa.api, "OfflineTts", delayed)
    thread = local_agent.start_model_preload()
    agent = local_agent.LocalPipelineAgent(llm_chat=lambda *a: {})
    agent.set_status_handler(lambda _: progress_seen.set())

    async def run():
        task = asyncio.create_task(agent.start(lambda pcm: None))
        try:
            assert await asyncio.to_thread(progress_seen.wait, 1)
            assert not task.done()
        finally:
            release.set()
            await task
            await agent.stop()

    try:
        assert entered.wait(1)
        asyncio.run(run())
    finally:
        release.set()
        thread.join(1)
    assert not thread.is_alive()
    assert sherpa.counts == {"asr": 1, "tts": 1, "vad": 1}


def test_injected_pipeline_never_uses_resident_models(sherpa, monkeypatch):
    from test_local_agent import FakePipeline

    built = []

    def factory():
        pipeline = FakePipeline()
        built.append(pipeline)
        return pipeline

    for _ in range(2):
        agent = local_agent.LocalPipelineAgent(pipeline_factory=factory, llm_chat=lambda *a: {})
        asyncio.run(agent.start(lambda pcm: None))
        asyncio.run(agent.stop())
    assert len(built) == 2 and built[0] is not built[1]
    assert sherpa.counts == {"asr": 0, "tts": 0, "vad": 0}
