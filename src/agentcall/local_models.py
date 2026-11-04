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
import hashlib
import http.client
import logging
import os
import sys
import tarfile
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import config
from .app_version import VERSION

logger = logging.getLogger(__name__)

_RELEASE_BASE = "https://github.com/k2-fsa/sherpa-onnx/releases/download"

_asset_locks: dict[str, threading.Lock] = {}
_asset_locks_guard = threading.Lock()


def _user_agent() -> str:
    """非 Python 默认 UA；Cloudflare 会按 urllib 默认 UA 直接 403。"""
    version = str(VERSION).strip()
    if not version or version == "unknown":
        version = "1"
    return f"CallPilot-Edge/{version}"


def _asset_lock(asset_id: str) -> threading.Lock:
    with _asset_locks_guard:
        return _asset_locks.setdefault(asset_id, threading.Lock())


@dataclass(frozen=True)
class ModelAsset:
    """一件模型资产：下载地址 + 解压后必须存在的文件（相对 models 根目录）。

    ``size``/``sha256`` 为可选的下载产物校验值；``size`` 同时用于进度合计与
    断点续传（半成品超过该值即判损坏重下）。上游 GitHub Release 不发布哈希，
    清单当前只填 ``size``，``sha256`` 保留为空以支持未来镜像侧校验。
    """

    id: str
    description: str
    url: str
    # 归档内应出现的关键文件（相对 models_dir）；全部存在视为已就绪。
    required_files: tuple[str, ...]
    # False = 单文件直下（存为 required_files[0]）；否则 tar.bz2 解压到 models_dir。
    archive: bool = True
    required_dirs: tuple[str, ...] = ()
    size: int | None = None
    sha256: str | None = None


MODEL_ASSETS: tuple[ModelAsset, ...] = (
    ModelAsset(
        id="vad",
        description="Silero VAD v5 (onnx)",
        url=f"{_RELEASE_BASE}/asr-models/silero_vad.onnx",
        required_files=("silero_vad.onnx",),
        archive=False,
        size=643854,
    ),
    ModelAsset(
        id="stt",
        description="Paraformer-zh int8 (sherpa-onnx offline ASR)",
        url=f"{_RELEASE_BASE}/asr-models/sherpa-onnx-paraformer-zh-2023-09-14.tar.bz2",
        required_files=(
            "sherpa-onnx-paraformer-zh-2023-09-14/model.int8.onnx",
            "sherpa-onnx-paraformer-zh-2023-09-14/tokens.txt",
        ),
        size=234051698,
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
        size=14011298,
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
                size=53884024,
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
            size=31559701,
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
    """按语音提供方挑选需要就绪的资产。

    本地 VAD（silero）始终需要；ASR/TTS 切到 DashScope 后对应模型不再要求
    就绪、也不触发下载，避免把 paraformer / VITS 两件大模型当成本地依赖。
    """
    assets: list[ModelAsset] = [MODEL_ASSETS[0]]
    if config.get_str("LOCAL_ASR_PROVIDER") == "local":
        assets.append(MODEL_ASSETS[1])
    if config.get_str("LOCAL_TTS_PROVIDER") == "local":
        assets.extend(tts_model().assets)
    return tuple(assets)

def download_sources(asset: ModelAsset) -> tuple[tuple[str, str], ...]:
    """按 LOCAL_MODELS_MIRROR 拼出候选下载源：镜像在前，原地址兜底。

    镜像只替换 ``_RELEASE_BASE`` 前缀，其后资产路径原样拼接；不接收前端传入的
    任意 URL，也不会让配置项脱离固定清单地址。空镜像只返回原地址。
    """
    mirror = config.get_str("LOCAL_MODELS_MIRROR").strip()
    if mirror and asset.url.startswith(_RELEASE_BASE):
        return (("mirror", mirror.rstrip("/") + asset.url[len(_RELEASE_BASE):]),
                ("origin", asset.url))
    return (("origin", asset.url),)


def resolved_url(asset: ModelAsset) -> str:
    """首选下载地址（镜像可用时即镜像，否则清单原地址）。"""
    return download_sources(asset)[0][1]


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


class DownloadCancelled(Exception):
    """后台下载被用户取消；调用方据此归为已取消而非错误。"""


class DownloadVerifyError(RuntimeError):
    """下载产物大小或哈希校验失败；``.part`` 已删除，回退时可从头下载。"""


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 256), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _download(
    url: str,
    dest: Path,
    *,
    size: int | None = None,
    sha256: str | None = None,
    progress: Callable[[int, int], None] | None = None,
    cancel: threading.Event | None = None,
) -> None:
    """流式下载到 ``<dest>.part`` 后原子替换，支持 Range 断点续传。

    半成品留在 ``.part``：取消/网络中断下次从已有字节续传；校验失败则删除
    半成品，避免把损坏文件当续传起点。
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    partial = dest.with_name(dest.name + ".part")
    headers: dict[str, str] = {"User-Agent": _user_agent()}
    received = 0
    if partial.is_file():
        received = partial.stat().st_size
        if size is not None and received >= size:
            partial.unlink(missing_ok=True)
            received = 0
        else:
            headers["Range"] = f"bytes={received}-"
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=60) as response:
        status = getattr(response, "status", 200)
        if status == 206:
            total = received + int(response.headers.get("Content-Length") or 0)
            mode = "ab"
        else:
            total = int(response.headers.get("Content-Length") or 0)
            received = 0
            mode = "wb"
        with partial.open(mode) as handle:
            while True:
                if cancel is not None and cancel.is_set():
                    raise DownloadCancelled(url)
                chunk = response.read(1024 * 256)
                if not chunk:
                    break
                handle.write(chunk)
                received += len(chunk)
                if progress is not None:
                    progress(received, total)
    if size is not None and received != size:
        partial.unlink(missing_ok=True)
        raise DownloadVerifyError(
            f"模型下载大小校验失败: {dest.name} 期望 {size} 字节，实际 {received} 字节"
        )
    if sha256 is not None:
        actual = _sha256_file(partial)
        if actual.lower() != sha256.lower():
            partial.unlink(missing_ok=True)
            raise DownloadVerifyError(
                f"模型下载哈希校验失败: {dest.name} 期望 {sha256}，实际 {actual}"
            )
    os.replace(partial, dest)


def _extract_archive(archive_path: Path, target_dir: Path) -> None:
    """解压 tar.bz2 到模型根目录；``filter="data"`` 阻断路径穿越/危险成员。"""
    target_dir.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive_path, "r:bz2") as tar:
        tar.extractall(target_dir, filter="data")


def _artifact_valid(path: Path, asset: ModelAsset) -> bool:
    """已存在的归档/单文件是否与清单一致，一致则可免重下直接进入解压。"""
    if not path.is_file():
        return False
    if asset.size is not None and path.stat().st_size != asset.size:
        return False
    if asset.sha256 is not None and _sha256_file(path).lower() != asset.sha256.lower():
        return False
    return True


def _http_miss(exc: BaseException) -> bool:
    """镜像返回 404/403：文件不在镜像，可回退原地址。"""
    return isinstance(exc, urllib.error.HTTPError) and exc.code in (404, 403)


def _connection_error(exc: BaseException) -> bool:
    """连接类错误（排除 HTTP 状态码错误）；是否续传取决于本次是否收到字节。"""
    return (isinstance(exc, (urllib.error.URLError, TimeoutError, OSError,
                             http.client.HTTPException))
            and not isinstance(exc, urllib.error.HTTPError))


def _partial_size(dest: Path) -> int:
    partial = dest.with_name(dest.name + ".part")
    return partial.stat().st_size if partial.is_file() else 0


_MAX_RESUME_RETRIES = 3
_RESUME_RETRY_BACKOFFS = (1.0, 2.0, 4.0)


def _wait_before_retry(delay: float, cancel: threading.Event | None, url: str) -> None:
    """自动续传重试前的等待；cancel 触发时立即抛出 DownloadCancelled。"""
    if cancel is not None:
        if cancel.wait(delay):
            raise DownloadCancelled(url)
    else:
        time.sleep(delay)


def _download_from_sources(
    sources: tuple[tuple[str, str], ...],
    dest: Path,
    *,
    size: int | None = None,
    sha256: str | None = None,
    progress: Callable[[int, int], None] | None = None,
    cancel: threading.Event | None = None,
    report_source: Callable[[str], None] | None = None,
) -> None:
    """依次尝试候选源；镜像缺文件/零字节连接失败/校验失败时回退原地址。

    镜像与原地址内容相同，回退保留 ``.part`` 由原地址 Range 续传；校验失败时
    ``_download`` 已删除 ``.part``，原地址从头下载。已收到字节后的连接类错误
    在同一源上做有限次自动续传重试，连续零进展才放弃并原样抛出。
    """
    for index, (label, url) in enumerate(sources):
        if report_source is not None:
            report_source(label)
        before = _partial_size(dest)
        try:
            _download(url, dest, size=size, sha256=sha256, progress=progress, cancel=cancel)
            return
        except DownloadCancelled:
            raise
        except DownloadVerifyError as exc:
            if index == len(sources) - 1:
                raise
            logger.info("镜像校验失败（%s），回退原地址从头下载: %s -> %s",
                        _readable_error(exc), url, sources[index + 1][1])
            continue
        except Exception as exc:  # noqa: BLE001
            if _http_miss(exc):
                if index == len(sources) - 1:
                    raise
                logger.info("镜像缺少文件（%s），回退原地址: %s -> %s",
                            _readable_error(exc), url, sources[index + 1][1])
                continue
            if not _connection_error(exc):
                raise
            if _partial_size(dest) == before:
                if index == len(sources) - 1:
                    raise
                logger.info("镜像连接失败且未收到字节（%s），回退原地址: %s -> %s",
                            _readable_error(exc), url, sources[index + 1][1])
                continue
            zero_streak = 0
            attempt = 0
            while True:
                attempt += 1
                logger.info("%s 下载中断（%s），已下 %s 字节，第 %s 次续传重试: %s",
                            label, _readable_error(exc), _partial_size(dest), attempt, url)
                _wait_before_retry(_RESUME_RETRY_BACKOFFS[zero_streak], cancel, url)
                before_retry = _partial_size(dest)
                try:
                    _download(url, dest, size=size, sha256=sha256, progress=progress,
                              cancel=cancel)
                    return
                except DownloadCancelled:
                    raise
                except DownloadVerifyError as verify_exc:
                    if index == len(sources) - 1:
                        raise
                    logger.info("镜像校验失败（%s），回退原地址从头下载: %s -> %s",
                                _readable_error(verify_exc), url, sources[index + 1][1])
                    break
                except Exception as retry_exc:  # noqa: BLE001
                    if _http_miss(retry_exc):
                        if index == len(sources) - 1:
                            raise
                        logger.info("镜像缺少文件（%s），回退原地址: %s -> %s",
                                    _readable_error(retry_exc), url, sources[index + 1][1])
                        break
                    if not _connection_error(retry_exc):
                        raise
                    exc = retry_exc
                    if _partial_size(dest) > before_retry:
                        zero_streak = 0
                    else:
                        zero_streak += 1
                        if zero_streak >= _MAX_RESUME_RETRIES:
                            raise exc


def ensure_asset(
    asset: ModelAsset,
    base: Path | None = None,
    *,
    progress: Callable[[int, int], None] | None = None,
    cancel: threading.Event | None = None,
    report_source: Callable[[str], None] | None = None,
) -> bool:
    """确保一件资产就绪；返回是否发生了下载。失败抛异常（调用方决定降级）。"""
    root = base if base is not None else models_dir()
    with _asset_lock(asset.id):
        if cancel is not None and cancel.is_set():
            raise DownloadCancelled(asset.id)
        if asset_ready(asset, root):
            return False
        bundled = bundled_models_dir()
        if bundled is not None and root.resolve().is_relative_to(bundled.resolve()):
            raise RuntimeError("包内模型目录只读，请在用户数据目录准备缺失模型")
        url = resolved_url(asset)
        sources = download_sources(asset)
        if not asset.archive:
            _download_from_sources(sources, root / asset.required_files[0], size=asset.size,
                                   sha256=asset.sha256, progress=progress, cancel=cancel,
                                   report_source=report_source)
        else:
            archive_name = url.rsplit("/", 1)[-1]
            archive_path = root / archive_name
            if not _artifact_valid(archive_path, asset):
                _download_from_sources(sources, archive_path, size=asset.size, sha256=asset.sha256,
                                       progress=progress, cancel=cancel, report_source=report_source)
            _extract_archive(archive_path, root)
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


def _readable_error(exc: Exception) -> str:
    text = str(exc).strip()
    return text if text else type(exc).__name__


class ModelDownloadManager:
    """后台串行下载缺失模型，线程安全暴露进度供 Web 接口查询。"""

    def __init__(self, base: Path | None = None) -> None:
        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self._thread: threading.Thread | None = None
        self._base = base
        self._state: dict[str, object] = {
            "status": "idle", "received_bytes": 0, "total_bytes": 0,
            "current_file": "", "current_asset": "", "error": "", "source": "",
        }

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            return dict(self._state)

    def is_running(self) -> bool:
        with self._lock:
            return self._state["status"] == "running"

    def start(self, base: Path | None = None) -> bool:
        with self._lock:
            if self._state["status"] == "running":
                return False
            self._cancel.clear()
            self._thread = threading.Thread(
                target=self._run, args=(base if base is not None else self._base,), daemon=True
            )
            self._thread.start()
            return True

    def cancel(self) -> None:
        self._cancel.set()

    def _set(self, **fields: object) -> None:
        with self._lock:
            self._state.update(fields)

    def _run(self, base: Path | None) -> None:
        root = base if base is not None else models_dir()
        missing = missing_assets(root)
        total = sum(asset.size or 0 for asset in missing)
        self._set(status="running", received_bytes=0, total_bytes=total,
                  current_file="", current_asset="", error="", source="")
        completed = 0
        try:
            for asset in missing:
                name = (resolved_url(asset).rsplit("/", 1)[-1] if asset.archive
                        else asset.required_files[0])
                self._set(current_asset=asset.id, current_file=name)

                def on_progress(received: int, _total: int) -> None:
                    self._set(received_bytes=completed + received, current_file=name)

                ensure_asset(asset, root, progress=on_progress, cancel=self._cancel,
                             report_source=lambda source: self._set(source=source))
                completed += asset.size or 0
        except DownloadCancelled:
            self._set(status="cancelled", error="", source="")
        except Exception as exc:  # noqa: BLE001
            logger.warning("本地模型下载失败: %s", exc)
            self._set(status="error", error=_readable_error(exc), source="")
        else:
            self._set(status="done", received_bytes=total, current_file="", current_asset="",
                      source="")


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
