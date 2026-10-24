"""local provider 模型资产 manifest 单测（不做真实下载）。"""

from __future__ import annotations

import asyncio
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentcall import config, local_models
from agentcall.local_models import MODEL_ASSETS, ModelAsset


def test_manifest_shape_and_ids():
    ids = [asset.id for asset in MODEL_ASSETS]
    assert ids == ["vad", "stt", "tts"]
    for asset in MODEL_ASSETS:
        assert asset.url.startswith("https://github.com/k2-fsa/sherpa-onnx/releases/")
        assert asset.required_files
        if not asset.archive:
            assert len(asset.required_files) == 1


def test_asset_ready_and_missing(tmp_path):
    asset = MODEL_ASSETS[0]  # vad 单文件
    assert not local_models.asset_ready(asset, tmp_path)
    assert asset in local_models.missing_assets(tmp_path)

    (tmp_path / asset.required_files[0]).write_bytes(b"onnx")
    assert local_models.asset_ready(asset, tmp_path)
    assert asset not in local_models.missing_assets(tmp_path)


def test_models_dir_override(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCAL_MODELS_DIR", str(tmp_path / "custom"))
    assert local_models.models_dir() == tmp_path / "custom"


def test_ensure_asset_skips_when_ready(tmp_path, monkeypatch):
    asset = MODEL_ASSETS[0]
    (tmp_path / asset.required_files[0]).write_bytes(b"onnx")

    def fail_download(url, dest):
        raise AssertionError("已就绪的资产不应重新下载")

    monkeypatch.setattr(local_models, "_download", fail_download)
    assert local_models.ensure_asset(asset, tmp_path) is False


def test_ensure_asset_archive_download_and_extract(tmp_path, monkeypatch):
    """归档资产：下载 tar.bz2 → 解压 → 校验 required_files → 清理归档。"""
    asset = ModelAsset(
        id="fake",
        description="fake archive",
        url="https://github.com/k2-fsa/sherpa-onnx/releases/download/x/pkg.tar.bz2",
        required_files=("pkg/model.onnx", "pkg/tokens.txt"),
    )

    def fake_download(url, dest):
        src = tmp_path / "src" / "pkg"
        src.mkdir(parents=True)
        (src / "model.onnx").write_bytes(b"m")
        (src / "tokens.txt").write_text("t", encoding="utf-8")
        with tarfile.open(dest, "w:bz2") as tar:
            tar.add(src, arcname="pkg")

    monkeypatch.setattr(local_models, "_download", fake_download)
    assert local_models.ensure_asset(asset, tmp_path) is True
    assert (tmp_path / "pkg" / "model.onnx").read_bytes() == b"m"
    assert not (tmp_path / "pkg.tar.bz2").exists()  # 归档已清理


def test_main_check_reports_missing(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("LOCAL_MODELS_DIR", str(tmp_path))
    assert local_models.main(["--check"]) == 1
    out = capsys.readouterr().out
    assert "MISSING vad" in out


def test_matcha_selects_only_its_assets_and_requires_dictionary(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_TTS_MODEL", "matcha-icefall-zh-baker")
    assets = local_models.selected_assets()
    assert [a.id for a in assets] == ["vad", "stt", "tts", "vocoder"]
    tts = assets[2]
    for rel in tts.required_files:
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    assert not local_models.asset_ready(tts, tmp_path)
    (tmp_path / "matcha-icefall-zh-baker/dict").mkdir()
    assert local_models.asset_ready(tts, tmp_path)
    assert "matcha" in str(local_models.resolved_paths(tmp_path)["tts"])


@pytest.mark.parametrize(("name", "kind"), [
    ("vits-piper-zh_CN-chaowen-medium-int8", "vits"),
    ("matcha-icefall-zh-baker", "matcha"),
    ("kokoro-int8-multi-lang-v1_1", "kokoro"),
    ("vits-icefall-zh-aishell3", "vits"),
])
def test_tts_config_uses_registry_and_existing_rule_fsts(tmp_path, name, kind):
    sherpa = SimpleNamespace(
        OfflineTtsConfig=lambda **kw: kw,
        OfflineTtsModelConfig=lambda **kw: kw,
        OfflineTtsVitsModelConfig=lambda **kw: kw,
        OfflineTtsMatchaModelConfig=lambda **kw: kw,
        OfflineTtsKokoroModelConfig=lambda **kw: kw,
    )
    model_dir = tmp_path / name
    model_dir.mkdir()
    (model_dir / "phone.fst").touch()
    (model_dir / "phone-zh.fst").touch()
    cfg = local_models.build_tts_config(sherpa, tmp_path, name)
    assert cfg["model"]["num_threads"] == 2
    assert "tokens" in cfg["model"][kind]
    if kind == "matcha":
        assert cfg["model"][kind]["vocoder"] == str(tmp_path / "vocos-22khz-univ.onnx")
        assert cfg["model"][kind]["dict_dir"] == str(model_dir / "dict")
        assert cfg["rule_fsts"] == str(model_dir / "phone.fst")
    elif kind == "kokoro":
        voice = cfg["model"][kind]
        assert voice["model"] == str(model_dir / "model.int8.onnx")
        assert voice["voices"] == str(model_dir / "voices.bin")
        assert voice["lexicon"] == ",".join(str(model_dir / file) for file in (
            "lexicon-us-en.txt", "lexicon-zh.txt",
        ))
        assert voice["data_dir"] == str(model_dir / "espeak-ng-data")
        assert voice["dict_dir"] == str(model_dir / "dict")
        assert cfg["rule_fsts"] == str(model_dir / "phone-zh.fst")
    elif name == "vits-icefall-zh-aishell3":
        assert cfg["model"][kind] == {
            "model": str(model_dir / "model.onnx"), "lexicon": str(model_dir / "lexicon.txt"),
            "tokens": str(model_dir / "tokens.txt"),
        }
        assert cfg["rule_fsts"] == str(model_dir / "phone.fst")
    else:
        assert cfg["rule_fsts"] == ""


def test_aishell3_manifest_and_fst_order_without_far_or_speaker_table(monkeypatch, tmp_path):
    name = "vits-icefall-zh-aishell3"
    monkeypatch.setenv("LOCAL_TTS_MODEL", name)
    assets = local_models.selected_assets()
    assert [a.id for a in assets] == ["vad", "stt", "tts"]
    tts = assets[-1]
    assert tts.url == f"{local_models._RELEASE_BASE}/tts-models/{name}.tar.bz2"
    assert set(tts.required_files) == {f"{name}/{file}" for file in (
        "model.onnx", "lexicon.txt", "tokens.txt", "phone.fst", "date.fst", "number.fst", "new_heteronym.fst",
    )}
    assert tts.required_dirs == ()
    for rel in tts.required_files:
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.name != "new_heteronym.fst":
            path.touch()
    assert not local_models.asset_ready(tts, tmp_path)
    (tmp_path / name / "new_heteronym.fst").touch()
    assert local_models.asset_ready(tts, tmp_path)  # No rule.far / speakers.txt needed.
    sherpa = SimpleNamespace(OfflineTtsConfig=lambda **kw: kw, OfflineTtsModelConfig=lambda **kw: kw,
                             OfflineTtsVitsModelConfig=lambda **kw: kw)
    cfg = local_models.build_tts_config(sherpa, tmp_path)
    assert cfg["rule_fsts"] == ",".join(str(tmp_path / name / f"{rule}.fst")
                                     for rule in ("phone", "date", "number", "new_heteronym"))
    assert "rule_fars" not in cfg
    (tmp_path / name / "date.fst").unlink()
    cfg = local_models.build_tts_config(sherpa, tmp_path)
    assert cfg["rule_fsts"] == ",".join(str(tmp_path / name / f"{rule}.fst")
                                     for rule in ("phone", "number", "new_heteronym"))


def test_aishell3_env_recommendations_do_not_change_defaults():
    example = (Path(__file__).resolve().parents[2] / ".env.example").read_text(encoding="utf-8")
    assert "vits-icefall-zh-aishell3" in example
    assert "男声建议 LOCAL_TTS_SPEAKER_ID=25" in example
    assert "建议同时设 LOCAL_TTS_PEAK=0.9" in example
    assert "LOCAL_TTS_SPEAKER_ID=0" in example
    assert config.get_spec("LOCAL_TTS_SPEAKER_ID").default == "0"
    assert config.get_spec("LOCAL_TTS_MODEL").default == "vits-piper-zh_CN-chaowen-medium-int8"
    assert config.get_spec("LOCAL_TTS_PEAK").default == ""


def test_kokoro_manifest_and_complete_fst_configuration(monkeypatch, tmp_path):
    name = "kokoro-int8-multi-lang-v1_1"
    monkeypatch.setenv("LOCAL_TTS_MODEL", name)
    assets = local_models.selected_assets()
    assert [a.id for a in assets] == ["vad", "stt", "tts"]
    tts = assets[-1]
    assert tts.url.endswith(f"/tts-models/{name}.tar.bz2")
    assert set(tts.required_files) == {f"{name}/{file}" for file in (
        "model.int8.onnx", "voices.bin", "tokens.txt", "lexicon-us-en.txt", "lexicon-zh.txt",
        "date-zh.fst", "number-zh.fst", "phone-zh.fst",
    )}
    assert set(tts.required_dirs) == {f"{name}/dict", f"{name}/espeak-ng-data"}
    for rel in tts.required_files:
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    assert not local_models.asset_ready(tts, tmp_path)
    for rel in tts.required_dirs:
        (tmp_path / rel).mkdir()
    assert local_models.asset_ready(tts, tmp_path)
    sherpa = SimpleNamespace(OfflineTtsConfig=lambda **kw: kw, OfflineTtsModelConfig=lambda **kw: kw,
                             OfflineTtsKokoroModelConfig=lambda **kw: kw)
    cfg = local_models.build_tts_config(sherpa, tmp_path)
    assert cfg["rule_fsts"] == ",".join(str(tmp_path / name / f"{rule}-zh.fst")
                                     for rule in ("date", "number", "phone"))


def test_multi_path_fields_are_absolute_even_with_relative_root(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    sherpa = SimpleNamespace(OfflineTtsConfig=lambda **kw: kw, OfflineTtsModelConfig=lambda **kw: kw,
                             OfflineTtsKokoroModelConfig=lambda **kw: kw)
    cfg = local_models.build_tts_config(sherpa, Path("models"), "kokoro-int8-multi-lang-v1_1")
    assert all(Path(part).is_absolute() for part in cfg["model"]["kokoro"]["lexicon"].split(","))


def test_unknown_tts_model_fails_before_download(monkeypatch):
    monkeypatch.setenv("LOCAL_TTS_MODEL", "typo")
    with pytest.raises(ValueError, match="LOCAL_TTS_MODEL"):
        local_models.selected_assets()


def test_registry_can_add_kokoro_without_a_pipeline_branch(monkeypatch, tmp_path):
    model = local_models.TtsModel(
        kind="kokoro", config_class="OfflineTtsKokoroModelConfig",
        assets=(MODEL_ASSETS[2],),
        files={"model": "male/model.onnx", "voices": "male/voices.bin", "tokens": "male/tokens.txt"},
    )
    monkeypatch.setitem(local_models.TTS_MODELS, "future-male", model)
    monkeypatch.setenv("LOCAL_TTS_MODEL", "future-male")
    sherpa = SimpleNamespace(OfflineTtsKokoroModelConfig=lambda **kw: kw,
                             OfflineTtsModelConfig=lambda **kw: kw, OfflineTtsConfig=lambda **kw: kw)
    cfg = local_models.build_tts_config(sherpa, tmp_path)
    assert cfg["model"]["kokoro"]["voices"] == str(tmp_path / "male/voices.bin")


@pytest.mark.parametrize(("model", "sample_rate", "speaker"), [
    ("vits-piper-zh_CN-chaowen-medium-int8", 24000, 7),
    ("matcha-icefall-zh-baker", 24000, 7),
    ("kokoro-int8-multi-lang-v1_1", 24000, 7),
    ("vits-icefall-zh-aishell3", 8000, 25),
])
def test_real_pipeline_warms_up_with_configured_speaker(monkeypatch, tmp_path, model, sample_rate, speaker):
    import sys

    import numpy as np

    from agentcall.agents.local_agent import LocalPipelineAgent, _SherpaPipeline

    generated = []

    def generate(text, sid, speed):
        generated.append((text, sid, speed))
        return SimpleNamespace(samples=np.zeros(100, dtype="float32"))

    sherpa = SimpleNamespace(
        VadModelConfig=lambda: SimpleNamespace(silero_vad=SimpleNamespace()),
        VoiceActivityDetector=lambda *a, **kw: None,
        OfflineRecognizer=SimpleNamespace(from_paraformer=lambda **kw: None),
        OfflineTtsConfig=lambda **kw: kw, OfflineTtsModelConfig=lambda **kw: kw,
        OfflineTtsVitsModelConfig=lambda **kw: kw,
        OfflineTtsMatchaModelConfig=lambda **kw: kw,
        OfflineTtsKokoroModelConfig=lambda **kw: kw,
        OfflineTts=lambda cfg: SimpleNamespace(sample_rate=sample_rate, generate=generate),
    )
    monkeypatch.setitem(sys.modules, "sherpa_onnx", sherpa)
    monkeypatch.setattr(local_models, "missing_assets", lambda: [])
    monkeypatch.setenv("LOCAL_MODELS_DIR", str(tmp_path))
    monkeypatch.setenv("LOCAL_TTS_SPEAKER_ID", str(speaker))
    monkeypatch.setenv("LOCAL_TTS_MODEL", model)
    pipeline = _SherpaPipeline()
    assert generated == [("你好。", speaker, 1.0)]
    assert pipeline.sample_rate == sample_rate
    assert len(pipeline.synthesize("测试。")) == 200
    assert generated[-1] == ("测试。", speaker, 1.0)
    agent = LocalPipelineAgent(pipeline_factory=lambda: pipeline, llm_chat=lambda *args: {})
    asyncio.run(agent.start(lambda pcm: None))
    try:
        assert agent.output_rate == sample_rate
    finally:
        asyncio.run(agent.stop())
