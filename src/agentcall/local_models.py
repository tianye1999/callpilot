"""本地三段式（local provider）的模型资产管理：manifest + 下载 + 校验。

设计迁移自 iphone-call-ai-poc 的 ``models.toml`` 模式：机器可读的资产清单 +
幂等的 prepare 命令。模型**不进打包 DMG**（体积数百 MB），首次启用 local
provider 时下载到用户数据目录；重复执行只补缺失文件。

三件资产（sherpa-onnx 全家桶，无 torch 依赖）：
- VAD：silero-vad v5 onnx（~2MB）
- STT：paraformer-zh int8（~230MB，FunASR 同源模型的 onnx 量化版）
- TTS：vits-piper zh_CN chaowen medium int8（poc 真机验证过的中文音色）

用法::

    .venv/bin/python -m agentcall.local_models          # 下载全部缺失资产
    .venv/bin/python -m agentcall.local_models --check  # 只检查不下载
"""

from __future__ import annotations

import argparse
import logging
import sys
import tarfile
import tempfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from . import config

logger = logging.getLogger(__name__)

_RELEASE_BASE = "https://github.com/k2-fsa/sherpa-onnx/releases/download"


@dataclass(frozen=True)
class ModelAsset:
    """一件模型资产：下载地址 + 解压后必须存在的文件（相对 models 根目录）。"""

    id: str
    description: str
    url: str
    # 归档内应出现的关键文件（相对 models_dir）；全部存在视为已就绪。
    required_files: tuple[str, ...]
    # None = 单文件直下（存为 required_files[0]）；否则 tar.bz2 解压到 models_dir。
    archive: bool = True


MODEL_ASSETS: tuple[ModelAsset, ...] = (
    ModelAsset(
        id="vad",
        description="Silero VAD v5 (onnx)",
        url=f"{_RELEASE_BASE}/asr-models/silero_vad.onnx",
        required_files=("silero_vad.onnx",),
        archive=False,
    ),
    ModelAsset(
        id="stt",
        description="Paraformer-zh int8 (sherpa-onnx offline ASR)",
        url=f"{_RELEASE_BASE}/asr-models/sherpa-onnx-paraformer-zh-2023-09-14.tar.bz2",
        required_files=(
            "sherpa-onnx-paraformer-zh-2023-09-14/model.int8.onnx",
            "sherpa-onnx-paraformer-zh-2023-09-14/tokens.txt",
        ),
    ),
    ModelAsset(
        id="tts",
        description="Piper zh_CN chaowen medium int8 (sherpa-onnx VITS TTS)",
        url=f"{_RELEASE_BASE}/tts-models/vits-piper-zh_CN-chaowen-medium-int8.tar.bz2",
        required_files=(
            "vits-piper-zh_CN-chaowen-medium-int8/zh_CN-chaowen-medium.onnx",
            "vits-piper-zh_CN-chaowen-medium-int8/tokens.txt",
            "vits-piper-zh_CN-chaowen-medium-int8/lexicon.txt",
        ),
    ),
)

_ASSETS_BY_ID = {asset.id: asset for asset in MODEL_ASSETS}


def models_dir() -> Path:
    """模型根目录：``LOCAL_MODELS_DIR`` 覆盖，否则数据目录下 ``models/``。"""
    override = config.get_str("LOCAL_MODELS_DIR").strip()
    if override:
        return Path(override).expanduser()
    return config.data_dir() / "models"


def asset_paths(asset: ModelAsset, base: Path | None = None) -> dict[str, Path]:
    root = base if base is not None else models_dir()
    return {rel: root / rel for rel in asset.required_files}


def asset_ready(asset: ModelAsset, base: Path | None = None) -> bool:
    return all(path.is_file() for path in asset_paths(asset, base).values())


def missing_assets(base: Path | None = None) -> list[ModelAsset]:
    return [asset for asset in MODEL_ASSETS if not asset_ready(asset, base)]


def _download(url: str, dest: Path) -> None:
    """流式下载到临时文件后原子替换；进度按 10% 粒度打日志。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    logger.info("下载 %s", url)
    with urllib.request.urlopen(url, timeout=60) as response:
        total = int(response.headers.get("Content-Length") or 0)
        received = 0
        next_report = 10
        with tempfile.NamedTemporaryFile(dir=dest.parent, delete=False) as tmp:
            tmp_path = Path(tmp.name)
            try:
                while True:
                    chunk = response.read(1024 * 256)
                    if not chunk:
                        break
                    tmp.write(chunk)
                    received += len(chunk)
                    if total and received * 100 // total >= next_report:
                        logger.info("  %d%% (%.1f/%.1f MB)", next_report, received / 1e6, total / 1e6)
                        next_report += 10
            except BaseException:
                tmp_path.unlink(missing_ok=True)
                raise
    tmp_path.replace(dest)


def _extract_archive(archive_path: Path, target_dir: Path) -> None:
    """解压 tar.bz2 到模型根目录；``filter="data"`` 阻断路径穿越/危险成员。"""
    target_dir.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive_path, "r:bz2") as tar:
        tar.extractall(target_dir, filter="data")


def ensure_asset(asset: ModelAsset, base: Path | None = None) -> bool:
    """确保一件资产就绪；返回是否发生了下载。失败抛异常（调用方决定降级）。"""
    root = base if base is not None else models_dir()
    if asset_ready(asset, root):
        return False
    if not asset.archive:
        _download(asset.url, root / asset.required_files[0])
    else:
        archive_name = asset.url.rsplit("/", 1)[-1]
        archive_path = root / archive_name
        _download(asset.url, archive_path)
        try:
            _extract_archive(archive_path, root)
        finally:
            archive_path.unlink(missing_ok=True)
    still_missing = [rel for rel, path in asset_paths(asset, root).items() if not path.is_file()]
    if still_missing:
        raise RuntimeError(f"模型资产 {asset.id} 下载后仍缺文件: {', '.join(still_missing)}")
    logger.info("模型资产 %s 就绪", asset.id)
    return True


def ensure_all(base: Path | None = None) -> dict[str, Path]:
    """确保三件资产全部就绪，返回 {asset_id: 首个关键文件路径}。"""
    root = base if base is not None else models_dir()
    for asset in MODEL_ASSETS:
        ensure_asset(asset, root)
    return {asset.id: root / asset.required_files[0] for asset in MODEL_ASSETS}


def resolved_paths(base: Path | None = None) -> dict[str, Path]:
    """不下载，仅返回资产关键文件路径（调用方自查存在性）。"""
    root = base if base is not None else models_dir()
    return {asset.id: root / asset.required_files[0] for asset in MODEL_ASSETS}


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="下载/检查 local provider 的模型资产")
    parser.add_argument("--check", action="store_true", help="只报告缺失，不下载")
    args = parser.parse_args(argv)

    root = models_dir()
    missing = missing_assets(root)
    if args.check:
        if missing:
            for asset in missing:
                print(f"MISSING {asset.id}: {asset.description}")
            return 1
        print(f"OK 全部模型资产就绪: {root}")
        return 0
    if not missing:
        print(f"OK 全部模型资产已就绪: {root}")
        return 0
    for asset in missing:
        ensure_asset(asset, root)
    print(f"OK 下载完成: {root}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
