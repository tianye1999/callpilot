"""local provider 模型资产 manifest 单测（不做真实下载）。"""

from __future__ import annotations

import tarfile

from agentcall import local_models
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
        (src / "tokens.txt").write_text("t")
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
