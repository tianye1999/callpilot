"""本地模型下载器的单测：镜像前缀、下载校验、断点续传、进度/取消、包内兜底。

所有测试都不访问公网：要么打桩 ``urllib.request.urlopen``，要么使用本机
临时 HTTP 服务。Mac 包内模型就绪的场景通过 ``_MEIPASS`` + ``sys.frozen`` 模拟。
"""

from __future__ import annotations

import sys
import threading
import time
import urllib.error
from pathlib import Path

import pytest

from agentcall import config, local_models
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
    monkeypatch.setenv("LOCAL_MODELS_MIRROR", "")
    assert local_models.resolved_url(MODEL_ASSETS[0]) == MODEL_ASSETS[0].url


def test_resolved_url_mirror_only_replaces_known_prefix(monkeypatch):
    asset = ModelAsset(id="x", description="x", url="https://evil.example.com/x.bin",
                       required_files=("x.bin",), archive=False)
    monkeypatch.setenv("LOCAL_MODELS_MIRROR", "https://r2.example.com")
    assert local_models.resolved_url(asset) == "https://evil.example.com/x.bin"


# ---- 镜像回退 ----

def _default_mirror() -> str:
    return config.get_spec("LOCAL_MODELS_MIRROR").default


def _mirror_asset() -> ModelAsset:
    return ModelAsset(
        id="x", description="x",
        url=f"{local_models._RELEASE_BASE}/x/model.bin",
        required_files=("model.bin",), archive=False,
    )


def _mirror_for(asset: ModelAsset) -> str:
    return _default_mirror().rstrip("/") + asset.url[len(local_models._RELEASE_BASE):]


def test_default_mirror_is_r2_registry():
    assert _default_mirror() == "https://sim-dl.bondings.ai/models"


def test_default_mirror_resolves_when_env_unset(monkeypatch):
    monkeypatch.delenv("LOCAL_MODELS_MIRROR", raising=False)
    assert local_models.resolved_url(_mirror_asset()) == _mirror_for(_mirror_asset())


def test_ensure_asset_mirror_200_uses_mirror_only(tmp_path, monkeypatch):
    asset = _mirror_asset()
    calls = []

    def fake_download(url, dest, **kwargs):
        calls.append(url)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"ok")

    monkeypatch.setattr(local_models, "_download", fake_download)
    assert local_models.ensure_asset(asset, tmp_path) is True
    assert calls == [_mirror_for(asset)]
    assert (tmp_path / "model.bin").read_bytes() == b"ok"


def test_ensure_asset_mirror_404_falls_back_to_origin(tmp_path, monkeypatch):
    asset = _mirror_asset()
    calls = []

    def fake_download(url, dest, **kwargs):
        calls.append(url)
        if url.startswith(_default_mirror()):
            raise urllib.error.HTTPError(url, 404, "Not Found", None, None)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"ok")

    monkeypatch.setattr(local_models, "_download", fake_download)
    assert local_models.ensure_asset(asset, tmp_path) is True
    assert calls == [_mirror_for(asset), asset.url]


def test_ensure_asset_mirror_connection_failure_falls_back(tmp_path, monkeypatch):
    asset = _mirror_asset()
    calls = []

    def fake_download(url, dest, **kwargs):
        calls.append(url)
        if url.startswith(_default_mirror()):
            raise urllib.error.URLError("connection reset")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"ok")

    monkeypatch.setattr(local_models, "_download", fake_download)
    assert local_models.ensure_asset(asset, tmp_path) is True
    assert calls == [_mirror_for(asset), asset.url]


def test_ensure_asset_both_sources_fail_raises_origin_error(tmp_path, monkeypatch):
    asset = _mirror_asset()

    def fake_download(url, dest, **kwargs):
        if url.startswith(_default_mirror()):
            raise urllib.error.HTTPError(url, 404, "Not Found", None, None)
        raise OSError("origin unavailable")

    monkeypatch.setattr(local_models, "_download", fake_download)
    with pytest.raises(OSError, match="origin unavailable"):
        local_models.ensure_asset(asset, tmp_path)


def test_ensure_asset_empty_mirror_uses_origin_only(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_MODELS_MIRROR", "")
    asset = _mirror_asset()
    calls = []

    def fake_download(url, dest, **kwargs):
        calls.append(url)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"ok")

    monkeypatch.setattr(local_models, "_download", fake_download)
    assert local_models.ensure_asset(asset, tmp_path) is True
    assert calls == [asset.url]


def test_mirror_partial_connection_error_does_not_fallback(tmp_path, monkeypatch):
    """镜像已收到字节后断线：不回退，同一源自动续传重试，连续零进展后原样抛出。"""
    asset = _mirror_asset()
    dest = tmp_path / "model.bin"
    part = dest.with_name(dest.name + ".part")
    calls = []

    def fake_download(url, dest, **kwargs):
        calls.append(url)
        dest.parent.mkdir(parents=True, exist_ok=True)
        part.write_bytes(b"mirror-partial")
        raise urllib.error.URLError("mirror reset")

    monkeypatch.setattr(local_models, "_download", fake_download)
    monkeypatch.setattr(local_models, "_wait_before_retry", lambda delay, cancel, url: None)
    with pytest.raises(urllib.error.URLError, match="mirror reset"):
        local_models.ensure_asset(asset, tmp_path)
    assert calls == [_mirror_for(asset)] * 4
    assert part.read_bytes() == b"mirror-partial"


def test_resume_retry_succeeds_after_one_interruption(tmp_path, monkeypatch):
    """中途断线一次后在同一源自动续传成功。"""
    asset = _mirror_asset()
    dest = tmp_path / "model.bin"
    part = dest.with_name(dest.name + ".part")
    sources = local_models.download_sources(asset)
    calls = []
    waits = []

    def fake_download(url, dest, **kwargs):
        calls.append(url)
        if len(calls) == 1:
            part.write_bytes(b"half")
            raise urllib.error.URLError("read timed out")
        part.unlink(missing_ok=True)
        dest.write_bytes(b"half-whole")

    monkeypatch.setattr(local_models, "_download", fake_download)
    monkeypatch.setattr(local_models, "_wait_before_retry",
                        lambda delay, cancel, url: waits.append(delay))

    local_models._download_from_sources(sources, dest)

    assert calls == [sources[0][1]] * 2
    assert waits == [1.0]
    assert dest.read_bytes() == b"half-whole"
    assert not part.exists()


def test_resume_retry_raises_after_three_zero_progress_and_keeps_part(tmp_path, monkeypatch):
    """连续 3 次零进展后放弃并原样抛出，保留 .part。"""
    asset = _mirror_asset()
    dest = tmp_path / "model.bin"
    part = dest.with_name(dest.name + ".part")
    sources = local_models.download_sources(asset)
    calls = []
    waits = []

    def fake_download(url, dest, **kwargs):
        calls.append(url)
        part.write_bytes(b"partial")
        raise urllib.error.URLError("read timed out")

    monkeypatch.setattr(local_models, "_download", fake_download)
    monkeypatch.setattr(local_models, "_wait_before_retry",
                        lambda delay, cancel, url: waits.append(delay))

    with pytest.raises(urllib.error.URLError, match="read timed out"):
        local_models._download_from_sources(sources, dest)

    assert calls == [sources[0][1]] * 4
    assert waits == [1.0, 2.0, 4.0]
    assert part.read_bytes() == b"partial"


def test_resume_retry_resets_budget_on_progress(tmp_path, monkeypatch):
    """有进展时重置零进展计数：断 5 次但每次都有进展最终成功。"""
    asset = _mirror_asset()
    dest = tmp_path / "model.bin"
    part = dest.with_name(dest.name + ".part")
    sources = local_models.download_sources(asset)
    calls = []

    def fake_download(url, dest, **kwargs):
        calls.append(url)
        if len(calls) <= 5:
            part.write_bytes(b"x" * len(calls))
            raise urllib.error.URLError("read timed out")
        part.unlink(missing_ok=True)
        dest.write_bytes(b"x" * len(calls))

    monkeypatch.setattr(local_models, "_download", fake_download)
    monkeypatch.setattr(local_models, "_wait_before_retry", lambda delay, cancel, url: None)

    local_models._download_from_sources(sources, dest)

    assert len(calls) == 6
    assert dest.read_bytes() == b"x" * 6
    assert not part.exists()


def test_wait_before_retry_cancels_immediately():
    """等待期间 cancel 触发：立即抛 DownloadCancelled，不睡满整个超时。"""
    cancel = threading.Event()

    def set_cancel() -> None:
        time.sleep(0.02)
        cancel.set()

    thread = threading.Thread(target=set_cancel, daemon=True)
    thread.start()
    started = time.monotonic()
    with pytest.raises(local_models.DownloadCancelled):
        local_models._wait_before_retry(60.0, cancel, "https://example.com/x.bin")
    assert time.monotonic() - started < 1.0
    thread.join(5)


def test_mirror_zero_byte_connection_error_falls_back(tmp_path, monkeypatch):
    """镜像连接失败且本次零字节：回退原地址，且不删除镜像已有的 .part。"""
    asset = _mirror_asset()
    dest = tmp_path / "model.bin"
    part = dest.with_name(dest.name + ".part")
    part.write_bytes(b"previous-run")
    calls = []

    def fake_download(url, dest, **kwargs):
        calls.append(url)
        if url.startswith(_default_mirror()):
            raise urllib.error.URLError("mirror reset")
        assert part.exists(), "回退到原地址不应删除镜像的 .part"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"ok")

    monkeypatch.setattr(local_models, "_download", fake_download)
    assert local_models.ensure_asset(asset, tmp_path) is True
    assert calls == [_mirror_for(asset), asset.url]
    assert dest.read_bytes() == b"ok"


def test_mirror_verify_failure_falls_back_to_origin(tmp_path, monkeypatch):
    """镜像大小/哈希校验失败：回退原地址从头下载。"""
    asset = _mirror_asset()
    calls = []

    def fake_download(url, dest, **kwargs):
        calls.append(url)
        if url.startswith(_default_mirror()):
            raise local_models.DownloadVerifyError("模型下载大小校验失败")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"ok")

    monkeypatch.setattr(local_models, "_download", fake_download)
    assert local_models.ensure_asset(asset, tmp_path) is True
    assert calls == [_mirror_for(asset), asset.url]


def test_mirror_cancel_does_not_fallback(tmp_path, monkeypatch):
    """取消不回退：原样向上抛 DownloadCancelled。"""
    asset = _mirror_asset()
    calls = []

    def fake_download(url, dest, **kwargs):
        calls.append(url)
        raise local_models.DownloadCancelled(url)

    monkeypatch.setattr(local_models, "_download", fake_download)
    with pytest.raises(local_models.DownloadCancelled):
        local_models.ensure_asset(asset, tmp_path)
    assert calls == [_mirror_for(asset)]


def test_ensure_asset_reports_source_labels(tmp_path, monkeypatch):
    asset = _mirror_asset()

    def fake_download(url, dest, **kwargs):
        if url.startswith(_default_mirror()):
            raise urllib.error.HTTPError(url, 404, "Not Found", None, None)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"ok")

    monkeypatch.setattr(local_models, "_download", fake_download)
    labels = []
    assert local_models.ensure_asset(asset, tmp_path, report_source=labels.append) is True
    assert labels == ["mirror", "origin"]


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
        with pytest.raises(local_models.DownloadVerifyError, match="大小校验失败"):
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


# ---- 下载 User-Agent / 单飞并发 ----

def test_download_sends_callpilot_edge_user_agent(tmp_path):
    captured = {}

    def fake_urlopen(request, timeout=60):
        captured["ua"] = request.get_header("User-agent")
        return _FakeResponse(b"x", status=200, headers={"Content-Length": "1"})

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    try:
        _download("https://example.com/x.bin", tmp_path / "x.bin", size=1)
    finally:
        monkeypatch.undo()

    assert captured["ua"] is not None
    assert not captured["ua"].startswith("Python-urllib")
    assert captured["ua"].startswith("CallPilot-Edge/")
    assert captured["ua"] == f"CallPilot-Edge/{local_models.VERSION}"


def test_user_agent_falls_back_to_1_when_version_unknown(monkeypatch):
    monkeypatch.setattr(local_models, "VERSION", "unknown")
    assert local_models._user_agent() == "CallPilot-Edge/1"


def _single_flight_asset(asset_id: str) -> ModelAsset:
    return ModelAsset(
        id=asset_id, description=asset_id,
        url=f"https://github.com/k2-fsa/sherpa-onnx/releases/download/{asset_id}/x.bin",
        required_files=("x.bin",), archive=False, size=1,
    )


def _stub_download_from_sources(monkeypatch, download_calls, first_entered, finish):
    def fake_download_from_sources(sources, dest, *, size=None, sha256=None,
                                   progress=None, cancel=None, report_source=None):
        download_calls.append(dest.name)
        first_entered.set()
        assert finish.wait(5)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"x")

    monkeypatch.setattr(local_models, "_download_from_sources", fake_download_from_sources)


def test_ensure_asset_single_flight_two_threads(tmp_path, monkeypatch):
    asset = _single_flight_asset("single-flight")
    download_calls = []
    first_entered = threading.Event()
    finish = threading.Event()
    _stub_download_from_sources(monkeypatch, download_calls, first_entered, finish)

    results: dict[str, object] = {}
    errors: list[Exception] = []

    def run(name: str) -> None:
        try:
            results[name] = local_models.ensure_asset(asset, tmp_path)
        except Exception as exc:
            errors.append(exc)

    first = threading.Thread(target=run, args=("first",))
    first.start()
    assert first_entered.wait(5)
    second = threading.Thread(target=run, args=("second",))
    second.start()
    time.sleep(0.2)
    finish.set()
    first.join(5)
    second.join(5)

    assert not first.is_alive() and not second.is_alive()
    assert errors == []
    assert len(download_calls) == 1
    assert (tmp_path / "x.bin").read_bytes() == b"x"
    assert results == {"first": True, "second": False}


def test_ensure_asset_cancel_while_waiting_for_lock_does_not_download(tmp_path, monkeypatch):
    asset = _single_flight_asset("single-flight-cancel")
    download_calls = []
    first_entered = threading.Event()
    finish = threading.Event()
    _stub_download_from_sources(monkeypatch, download_calls, first_entered, finish)

    first = threading.Thread(target=local_models.ensure_asset, args=(asset, tmp_path))
    first.start()
    assert first_entered.wait(5)

    cancel = threading.Event()
    second_outcome: list[str] = []

    def run_second() -> None:
        try:
            local_models.ensure_asset(asset, tmp_path, cancel=cancel)
            second_outcome.append("returned")
        except local_models.DownloadCancelled:
            second_outcome.append("cancelled")
        except Exception as exc:
            second_outcome.append(f"error:{exc}")

    second = threading.Thread(target=run_second)
    second.start()
    time.sleep(0.2)
    cancel.set()
    finish.set()
    first.join(5)
    second.join(5)

    assert not first.is_alive() and not second.is_alive()
    assert second_outcome == ["cancelled"]
    assert len(download_calls) == 1
    assert (tmp_path / "x.bin").read_bytes() == b"x"


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

    def fake_ensure(asset, base, *, progress=None, cancel=None, report_source=None):
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

    def fake_ensure(asset, base, *, progress=None, cancel=None, report_source=None):
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

    def fake_ensure(asset, base, *, progress=None, cancel=None, report_source=None):
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


def test_download_manager_exposes_current_source(tmp_path, monkeypatch):
    asset = _fake_asset()
    monkeypatch.setattr(local_models, "selected_assets", lambda: (asset,))
    entered = threading.Event()
    release = threading.Event()
    captured = {}

    def fake_ensure(asset, base, *, progress=None, cancel=None, report_source=None):
        captured["report_source"] = report_source
        entered.set()
        assert release.wait(5)

    monkeypatch.setattr(local_models, "ensure_asset", fake_ensure)
    manager = ModelDownloadManager(base=tmp_path)
    assert manager.start()
    assert entered.wait(5)
    assert callable(captured["report_source"])
    captured["report_source"]("mirror")
    assert manager.snapshot()["source"] == "mirror"
    release.set()
    deadline = time.monotonic() + 5
    while manager.is_running() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert manager.snapshot()["source"] == ""


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
