"""本地三段式（local provider）的模型资产管理：manifest + 下载 + 校验。

设计迁移自 iphone-call-ai-poc 的 ``models.toml`` 模式：机器可读的资产清单 +
幂等的 prepare 命令。Mac 产品包内置默认模型，只读使用；开发版或额外音色
缺失时下载到用户数据目录，重复执行只补缺失文件。

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
from typing import Any

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
    required_dirs: tuple[str, ...] = ()


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

@dataclass(frozen=True)
class TtsModel:
    """One registry entry defines downloads and sherpa constructor arguments.

    Another vits/matcha (or kokoro multi-speaker) voice needs only an entry here;
    speaker selection is passed to generate(), independently of model kind.
    """

    kind: str
    config_class: str
    assets: tuple[ModelAsset, ...]
    # Tuples become comma-joined absolute paths (Kokoro's bilingual lexicon).
    files: dict[str, str | tuple[str, ...]]
    rule_fsts: tuple[str, ...] = ()


_CHAOWEN = "vits-piper-zh_CN-chaowen-medium-int8"
_BAKER = "matcha-icefall-zh-baker"
_KOKORO = "kokoro-int8-multi-lang-v1_1"
_AISHELL3 = "vits-icefall-zh-aishell3"
TTS_MODELS: dict[str, TtsModel] = {
    _CHAOWEN: TtsModel(
        kind="vits", config_class="OfflineTtsVitsModelConfig",
        assets=(MODEL_ASSETS[2],),
        files={"model": f"{_CHAOWEN}/zh_CN-chaowen-medium.onnx",
               "tokens": f"{_CHAOWEN}/tokens.txt", "lexicon": f"{_CHAOWEN}/lexicon.txt"},
    ),
    _BAKER: TtsModel(
        kind="matcha", config_class="OfflineTtsMatchaModelConfig",
        assets=(
            ModelAsset(
                id="tts", description="Matcha icefall zh baker",
                url=f"{_RELEASE_BASE}/tts-models/{_BAKER}.tar.bz2",
                required_files=tuple(f"{_BAKER}/{f}" for f in (
                    "model-steps-3.onnx", "lexicon.txt", "tokens.txt",
                    "phone.fst", "date.fst", "number.fst",
                )),
                required_dirs=(f"{_BAKER}/dict",),
            ),
            ModelAsset(
                id="vocoder", description="Vocos universal 22kHz vocoder",
                url=f"{_RELEASE_BASE}/vocoder-models/vocos-22khz-univ.onnx",
                required_files=("vocos-22khz-univ.onnx",), archive=False,
            ),
        ),
        files={"acoustic_model": f"{_BAKER}/model-steps-3.onnx",
               "vocoder": "vocos-22khz-univ.onnx", "lexicon": f"{_BAKER}/lexicon.txt",
               "tokens": f"{_BAKER}/tokens.txt", "dict_dir": f"{_BAKER}/dict"},
        rule_fsts=tuple(f"{_BAKER}/{f}.fst" for f in ("phone", "date", "number")),
    ),
    _KOKORO: TtsModel(
        kind="kokoro", config_class="OfflineTtsKokoroModelConfig",
        assets=(ModelAsset(
            id="tts", description="Kokoro int8 multi-lang v1.1 (multi-speaker)",
            url=f"{_RELEASE_BASE}/tts-models/{_KOKORO}.tar.bz2",
            required_files=tuple(f"{_KOKORO}/{f}" for f in (
                "model.int8.onnx", "voices.bin", "tokens.txt", "lexicon-us-en.txt", "lexicon-zh.txt",
                "date-zh.fst", "number-zh.fst", "phone-zh.fst",
            )),
            required_dirs=(f"{_KOKORO}/dict", f"{_KOKORO}/espeak-ng-data"),
        ),),
        files={"model": f"{_KOKORO}/model.int8.onnx", "voices": f"{_KOKORO}/voices.bin",
               "tokens": f"{_KOKORO}/tokens.txt",
               "lexicon": (f"{_KOKORO}/lexicon-us-en.txt", f"{_KOKORO}/lexicon-zh.txt"),
               "data_dir": f"{_KOKORO}/espeak-ng-data", "dict_dir": f"{_KOKORO}/dict"},
        rule_fsts=tuple(f"{_KOKORO}/{f}-zh.fst" for f in ("date", "number", "phone")),
    ),
    _AISHELL3: TtsModel(
        kind="vits", config_class="OfflineTtsVitsModelConfig",
        assets=(ModelAsset(
            id="tts", description="VITS icefall zh aishell3 (multi-speaker, 8kHz)",
            url=f"{_RELEASE_BASE}/tts-models/{_AISHELL3}.tar.bz2",
            required_files=tuple(f"{_AISHELL3}/{f}" for f in (
                "model.onnx", "lexicon.txt", "tokens.txt",
                "phone.fst", "date.fst", "number.fst", "new_heteronym.fst",
            )),
        ),),
        files={"model": f"{_AISHELL3}/model.onnx", "lexicon": f"{_AISHELL3}/lexicon.txt",
               "tokens": f"{_AISHELL3}/tokens.txt"},
        rule_fsts=tuple(f"{_AISHELL3}/{f}.fst" for f in ("phone", "date", "number", "new_heteronym")),
    ),
}


def tts_model(name: str | None = None) -> TtsModel:
    selected = name if name is not None else config.get_str("LOCAL_TTS_MODEL")
    try:
        return TTS_MODELS[selected]
    except KeyError:
        raise ValueError(f"未知的 LOCAL_TTS_MODEL: {selected}") from None


def selected_assets() -> tuple[ModelAsset, ...]:
    return MODEL_ASSETS[:2] + tts_model().assets


def build_tts_config(sherpa: Any, root: Path, name: str | None = None) -> Any:
    model = tts_model(name)
    root = root.resolve()
    voice = getattr(sherpa, model.config_class)(**{
        key: ",".join(str(root / rel) for rel in ((paths,) if isinstance(paths, str) else paths))
        for key, paths in model.files.items()
    })
    return sherpa.OfflineTtsConfig(
        model=sherpa.OfflineTtsModelConfig(**{model.kind: voice, "num_threads": 2}),
        rule_fsts=",".join(str(root / rel) for rel in model.rule_fsts if (root / rel).is_file()),
    )


def models_dir() -> Path:
    """覆盖目录 > 完整用户模型 > 完整只读包内模型 > 用户目录下载兜底。"""
    override = config.get_str("LOCAL_MODELS_DIR").strip()
    if override:
        return Path(override).expanduser()
    writable = config.data_dir() / "models"
    if all(asset_ready(asset, writable) for asset in selected_assets()):
        return writable
    bundled = bundled_models_dir()
    if bundled is not None and all(asset_ready(asset, bundled) for asset in selected_assets()):
        return bundled
    return writable


def bundled_models_dir() -> Path | None:
    """PyInstaller 将数据放在 Resources，并在 _MEIPASS 下提供链接。"""
    base = getattr(sys, "_MEIPASS", None)
    return Path(base) / "models" if base and getattr(sys, "frozen", False) else None


def bundled_assets() -> tuple[ModelAsset, ...]:
    return MODEL_ASSETS + TTS_MODELS[_AISHELL3].assets


def bundle_data_files(root: Path) -> list[tuple[str, str]]:
    """构建时只收清单文件；缺失即失败，不下载、不夹带浮点大模型。"""
    files = []
    for asset in bundled_assets():
        for rel in asset.required_files:
            path = root / rel
            if not path.is_file():
                raise RuntimeError(f"打包模型缺失: {path}")
            files.append((str(path), str(Path("models") / Path(rel).parent)))
    return files


def asset_paths(asset: ModelAsset, base: Path | None = None) -> dict[str, Path]:
    root = base if base is not None else models_dir()
    return {rel: root / rel for rel in asset.required_files}


def asset_ready(asset: ModelAsset, base: Path | None = None) -> bool:
    root = base if base is not None else models_dir()
    return (all(path.is_file() for path in asset_paths(asset, root).values())
            and all((root / rel).is_dir() for rel in asset.required_dirs))


def missing_assets(base: Path | None = None) -> list[ModelAsset]:
    return [asset for asset in selected_assets() if not asset_ready(asset, base)]


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
    bundled = bundled_models_dir()
    if bundled is not None and root.resolve().is_relative_to(bundled.resolve()):
        raise RuntimeError("包内模型目录只读，请在用户数据目录准备缺失模型")
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
    still_missing.extend(rel for rel in asset.required_dirs if not (root / rel).is_dir())
    if still_missing:
        raise RuntimeError(f"模型资产 {asset.id} 下载后仍缺文件: {', '.join(still_missing)}")
    logger.info("模型资产 %s 就绪", asset.id)
    return True


def ensure_all(base: Path | None = None) -> dict[str, Path]:
    """确保三件资产全部就绪，返回 {asset_id: 首个关键文件路径}。"""
    root = base if base is not None else models_dir()
    for asset in selected_assets():
        ensure_asset(asset, root)
    return resolved_paths(root)


def resolved_paths(base: Path | None = None) -> dict[str, Path]:
    """不下载，仅返回资产关键文件路径（调用方自查存在性）。"""
    root = base if base is not None else models_dir()
    return {asset.id: root / asset.required_files[0] for asset in selected_assets()}


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
