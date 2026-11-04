"""本地模型下载器的单测：镜像前缀、下载校验、断点续传、进度/取消、包内兜底。

所有测试都不访问公网：要么打桩 ``urllib.request.urlopen``，要么使用本机
临时 HTTP 服务。Mac 包内模型就绪的场景通过 ``_MEIPASS`` + ``sys.frozen`` 模拟。
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

from agentcall import local_models
from agentcall.local_models import MODEL_ASSETS, ModelAsset, ModelDownloadManager, _download


class _FakeResponse:
    """最小 urllib 响应替身：支持上下文管理与流式 read。"""

    def __init__(self, data: bytes, status: int = 200, headers: dict | None = None) -> None:
        self._data = data
        self.status = status
        self.headers = headers if headers is not None else {}
        self._pos = 0

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self, n: int = -1) -> bytes:
        chunk = self._data if n < 0 else self._data[self._pos:self._pos + n]
        self._pos += len(chunk)
        return chunk


def _populate(root: Path, asset: ModelAsset) -> None:
    for rel in asset.required_files:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")
    for rel in asset.required_dirs:
        (root / rel).mkdir(parents=True, exist_ok=True)


# ---- 镜像前缀替换 ----

def test_resolved_url_mirror_prefix(monkeypatch):
    monkeypatch.setenv("LOCAL_MODELS_MIRROR", "https://r2.example.com/models/")
    assert local_models.resolved_url(MODEL_ASSETS[0]) == (
        "https://r2.example.com/models/asr-models/silero_vad.onnx"
    )


def test_resolved_url_empty_mirror_uses_manifest(monkeypatch):
    monkeypatch.delenv("LOCAL_MODELS_MIRROR", raising=False)
    assert local_models.resolved_url(MODEL_ASSETS[0]) == MODEL_ASSETS[0].url


def test_resolved_url_mirror_only_replaces_known_prefix(monkeypatch):
    asset = ModelAsset(id="x", description="x", url="https://evil.example.com/x.bin",
                       required_files=("x.bin",), archive=False)
    monkeypatch.setenv("LOCAL_MODELS_MIRROR", "https://r2.example.com")
    assert local_models.resolved_url(asset) == "https://evil.example.com/x.bin"


# ---- 下载与校验 ----

def test_download_writes_atomic_file_and_reports_progress(tmp_path):
    payload = bytes(range(256)) * 128
    seen = []

    def fake_urlopen(request, timeout=60):
        return _FakeResponse(payload, status=200, headers={"Content-Length": str(len(payload))})

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    try:
        dest = tmp_path / "silero_vad.onnx"
        _download("https://example.com/silero_vad.onnx", dest, size=len(payload),
                  progress=lambda recv, total: seen.append((recv, total)))
        assert dest.read_bytes() == payload
        assert not (tmp_path / "silero_vad.onnx.part").exists()
        assert seen and seen[-1] == (len(payload), len(payload))
    finally:
        monkeypatch.undo()


def test_download_size_mismatch_raises_and_cleans_partial(tmp_path):
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr("urllib.request.urlopen",
                        lambda request, timeout=60: _FakeResponse(b"short", status=200))
    try:
        dest = tmp_path / "m.onnx"
        with pytest.raises(RuntimeError, match="大小校验失败"):
            _download("https://example.com/m.onnx", dest, size=100)
        assert not (tmp_path / "m.onnx.part").exists()
        assert not dest.exists()
    finally:
        monkeypatch.undo()


def test_download_resumes_with_range_header(tmp_path):
    full = b"ABCDEFGHIJ"
    (tmp_path / "m.onnx.part").write_bytes(full[:5])
    captured = {}

    def fake_urlopen(request, timeout=60):
        captured["range"] = request.get_header("Range")
        return _FakeResponse(full[5:], status=206, headers={"Content-Length": str(len(full) - 5)})

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    try:
        dest = tmp_path / "m.onnx"
        _download("https://example.com/m.onnx", dest, size=len(full))
        assert captured["range"] == "bytes=5-"
        assert dest.read_bytes() == full
        assert not (tmp_path / "m.onnx.part").exists()
    finally:
        monkeypatch.undo()


def test_download_oversized_partial_is_discarded(tmp_path):
    full = b"ABCDE"
    (tmp_path / "m.onnx.part").write_bytes(full + b"XXXX")  # 超过 size，应整段重下
    captured = {}

    def fake_urlopen(request, timeout=60):
        captured["range"] = request.get_header("Range")
        return _FakeResponse(full, status=200, headers={"Content-Length": str(len(full))})

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    try:
        dest = tmp_path / "m.onnx"
        _download("https://example.com/m.onnx", dest, size=len(full))
        assert captured["range"] is None
        assert dest.read_bytes() == full
    finally:
        monkeypatch.undo()


def test_download_against_local_http_server(tmp_path):
    import http.server

    payload = bytes(range(256)) * 97

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        dest = tmp_path / "m.bin"
        _download(f"http://127.0.0.1:{server.server_address[1]}/m.bin", dest, size=len(payload))
        assert dest.read_bytes() == payload
        assert not (tmp_path / "m.bin.part").exists()
    finally:
        server.shutdown()
        server.server_close()


# ---- 后台下载管理器：进度快照 / 取消 ----

def _fake_asset() -> ModelAsset:
    return ModelAsset(id="x", description="x",
                      url="https://github.com/k2-fsa/sherpa-onnx/releases/download/x/x.bin",
                      required_files=("x.bin",), archive=False, size=100)


def test_download_manager_progress_snapshot_and_done(tmp_path, monkeypatch):
    asset = _fake_asset()
    monkeypatch.setattr(local_models, "selected_assets", lambda: (asset,))
    entered = threading.Event()
    release = threading.Event()

    def fake_ensure(asset, base, *, progress=None, cancel=None):
        entered.set()
        assert release.wait(5)
        if progress:
            progress(100, 100)

    monkeypatch.setattr(local_models, "ensure_asset", fake_ensure)
    manager = ModelDownloadManager(base=tmp_path)
    assert manager.start()
    assert entered.wait(5)
    assert manager.is_running()
    snapshot = manager.snapshot()
    assert snapshot["status"] == "running"
    assert snapshot["total_bytes"] == 100

    release.set()
    deadline = time.monotonic() + 5
    while manager.is_running() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not manager.is_running()
    assert manager.snapshot()["status"] == "done"


def test_download_manager_cancel_sets_cancelled(tmp_path, monkeypatch):
    asset = _fake_asset()
    monkeypatch.setattr(local_models, "selected_assets", lambda: (asset,))
    entered = threading.Event()

    def fake_ensure(asset, base, *, progress=None, cancel=None):
        entered.set()
        assert cancel is not None
        assert cancel.wait(5)
        raise local_models.DownloadCancelled("x")

    monkeypatch.setattr(local_models, "ensure_asset", fake_ensure)
    manager = ModelDownloadManager(base=tmp_path)
    assert manager.start()
    assert entered.wait(5)
    manager.cancel()
    deadline = time.monotonic() + 5
    while manager.is_running() and time.monotonic() < deadline:
        time.sleep(0.01)
    snapshot = manager.snapshot()
    assert snapshot["status"] == "cancelled"
    assert snapshot["error"] == ""


def test_download_manager_rejects_second_start_while_running(tmp_path, monkeypatch):
    asset = _fake_asset()
    monkeypatch.setattr(local_models, "selected_assets", lambda: (asset,))
    entered = threading.Event()
    release = threading.Event()

    def fake_ensure(asset, base, *, progress=None, cancel=None):
        entered.set()
        assert release.wait(5)

    monkeypatch.setattr(local_models, "ensure_asset", fake_ensure)
    manager = ModelDownloadManager(base=tmp_path)
    assert manager.start()
    assert entered.wait(5)
    assert not manager.start()  # 已在运行，拒绝二次启动
    release.set()
    deadline = time.monotonic() + 5
    while manager.is_running() and time.monotonic() < deadline:
        time.sleep(0.01)


# ---- Mac 包内模型就绪：不触发下载 ----

def test_bundled_models_satisfy_models_dir_and_skip_download(tmp_path, monkeypatch):
    bundled = tmp_path / "app" / "models"
    for asset in local_models.selected_assets():
        _populate(bundled, asset)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path / "app"), raising=False)
    monkeypatch.delenv("LOCAL_MODELS_DIR", raising=False)

    assert local_models.models_dir() == bundled
    assert local_models.missing_assets() == []

    def fail_download(*args, **kwargs):
        raise AssertionError("包内模型已就绪，不应触发下载")

    monkeypatch.setattr(local_models, "_download", fail_download)
    assert local_models.ensure_asset(local_models.selected_assets()[0]) is False
