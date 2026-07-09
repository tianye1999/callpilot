"""web API 单测：通话历史/设置面板/批量外呼接口。

用 aiohttp TestClient 做全链路请求，路径穿越校验用 make_mocked_request 直测 handler；
service 用最小替身（只实现 web 层用到的接口），CallLogger 落到 tmp_path。
"""

from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace

from aiohttp.test_utils import TestClient, TestServer, make_mocked_request

from agentcall import config, platforms
from agentcall.call_log import CallLogger
from agentcall.web import server
from agentcall.web.server import _history_audio, _history_delete, _history_events, build_app


class _SessionStub:
    """CallSession 替身：只提供发短信目标校验用到的两个属性。"""

    def __init__(self, current_caller: str | None = None, is_active: bool = False) -> None:
        self.current_caller = current_caller
        self.is_active = is_active


class FakeService:
    """最小 service 替身：只提供 web 层用到的接口。"""

    def __init__(
        self,
        call_logger: CallLogger | None = None,
        session: "_SessionStub | None" = None,
    ) -> None:
        self.call_logger = call_logger
        self.session = session
        self.batch_calls: list[tuple[list[str], str | None]] = []
        self.batch_result: dict = {"accepted": [], "rejected": []}
        self.queue: dict = {"pending": [], "current": None, "done": [], "active": False}
        # 通话相关高层方法的可控返回值（默认：无通话）。
        self.dial_calls: list[tuple[str, str | None, str | None]] = []
        self.dial_result: tuple[bool, str | None] = (True, None)
        self.hangup_result: tuple[bool, str | None] = (True, None)
        self.dtmf_calls: list[str] = []
        self.dtmf_result: tuple[bool, str | None] = (True, None)

    def batch_dial(self, numbers: list[str], task: str | None = None) -> dict:
        self.batch_calls.append((list(numbers), task))
        return self.batch_result

    def dial_queue_status(self) -> dict:
        return dict(self.queue)

    def dial(
        self, number: str, task: str | None = None, preset_hint: str | None = None
    ) -> tuple[bool, str | None]:
        self.dial_calls.append((number, task, preset_hint))
        return self.dial_result

    def hangup(self) -> tuple[bool, str | None]:
        return self.hangup_result

    def send_dtmf(self, digits: str) -> tuple[bool, str | None]:
        self.dtmf_calls.append(digits)
        return self.dtmf_result


class FakeHub:
    """最小事件总线替身：记录 publish；history 供发短信目标校验读已联系号码。"""

    def __init__(self, history: list[dict] | None = None) -> None:
        self.events: list[dict] = []
        self._history = history or []

    def publish(self, event: dict) -> None:
        self.events.append(event)

    def history(self) -> list[dict]:
        return list(self._history)


class FakeModem:
    """最小模组替身：只提供 _send_sms 用到的接口。"""

    def __init__(self, send_result: bool = True) -> None:
        self.send_result = send_result
        self.sms_calls: list[tuple[str, str]] = []

    def send_sms(self, number: str, text: str) -> bool:
        self.sms_calls.append((number, text))
        return self.send_result


def make_app(service):
    return build_app(hub=None, modem=None, service=service)  # type: ignore[arg-type]


def api(app, fn):
    """起 TestServer 并在事件循环里执行 fn(client)，返回其结果。"""

    async def runner():
        async with TestClient(TestServer(app)) as client:
            return await fn(client)

    return asyncio.run(runner())


# ---- /api/config ----


def test_meta_reports_missing_credentials_without_blocking(monkeypatch):
    monkeypatch.setenv("AGENT_PROVIDER", "qwen")
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    app = build_app(
        hub=None,  # type: ignore[arg-type]
        modem=None,  # type: ignore[arg-type]
        service=FakeService(),
        meta=config.runtime_meta(provider="qwen", model="Qwen3.5-Omni", port="/tmp/ec20-at"),
    )

    async def fn(client):
        resp = await client.get("/api/meta")
        assert resp.status == 200
        return await resp.json()

    meta = api(app, fn)
    assert meta["credentials"]["ok"] is False
    assert any("DASHSCOPE_API_KEY" in err for err in meta["credentials"]["errors"])


def test_meta_includes_setup_and_hardware_status(monkeypatch):
    monkeypatch.delenv("SETUP_DONE", raising=False)
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    monkeypatch.delenv("DOUBAO_APP_ID", raising=False)
    monkeypatch.delenv("DOUBAO_ACCESS_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(server, "detect_quectel_usb_online", lambda: True)
    service = FakeService()
    service.modem_connected = False
    app = build_app(
        hub=None,  # type: ignore[arg-type]
        modem=None,  # type: ignore[arg-type]
        service=service,
        meta=config.runtime_meta(provider="qwen", model="Qwen3.5-Omni", port="/tmp/ec20-at"),
    )

    async def fn(client):
        resp = await client.get("/api/meta")
        assert resp.status == 200
        return await resp.json()

    meta = api(app, fn)
    assert meta["setup_required"] is True
    assert meta["hardware"] == {
        "usb_online": True,
        "modem_connected": False,
        "port": "/tmp/ec20-at",
    }


def test_quectel_usb_detection_on_macos_uses_usb_scan(monkeypatch):
    monkeypatch.setattr(platforms, "IS_MACOS", True)
    monkeypatch.setattr(server, "_detect_quectel_usb_pyusb", lambda: True)
    monkeypatch.setattr(server, "_detect_quectel_usb_system_profiler", lambda: False)
    monkeypatch.setattr(
        server.list_ports,
        "comports",
        lambda: [SimpleNamespace(vid=None)],
    )

    assert server.detect_quectel_usb_online() is True


def test_quectel_usb_detection_on_macos_falls_back_to_system_profiler(monkeypatch):
    monkeypatch.setattr(platforms, "IS_MACOS", True)
    monkeypatch.setattr(server, "_detect_quectel_usb_pyusb", lambda: False)
    monkeypatch.setattr(server, "_detect_quectel_usb_system_profiler", lambda: True)

    assert server.detect_quectel_usb_online() is True


def test_quectel_usb_detection_on_non_macos_keeps_serial_scan(monkeypatch):
    monkeypatch.setattr(platforms, "IS_MACOS", False)

    def fail_pyusb():
        raise AssertionError("non-mac path must not use pyusb")

    monkeypatch.setattr(server, "_detect_quectel_usb_pyusb", fail_pyusb)
    monkeypatch.setattr(
        server.list_ports,
        "comports",
        lambda: [SimpleNamespace(vid=server.QUECTEL_VID)],
    )

    assert server.detect_quectel_usb_online() is True


def test_validate_key_endpoint_valid_invalid_and_network(monkeypatch):
    outcomes = {
        "good": config.KeyValidationResult(True, "valid"),
        "bad": config.KeyValidationResult(False, "invalid"),
        "net": config.KeyValidationResult(False, "network"),
    }
    monkeypatch.setattr(config, "validate_provider_key_online", lambda provider, secret, timeout=5.0: outcomes[secret])
    app = make_app(FakeService())

    async def fn(client):
        resp = await client.post("/api/config/validate_key", json={"provider": "qwen", "api_key": "good"})
        assert resp.status == 200
        assert await resp.json() == {"ok": True, "status": "valid"}

        resp = await client.post("/api/config/validate_key", json={"provider": "qwen", "api_key": "bad"})
        assert resp.status == 200
        assert await resp.json() == {"ok": False, "status": "invalid"}

        resp = await client.post("/api/config/validate_key", json={"provider": "qwen", "api_key": "net"})
        assert resp.status == 200
        assert await resp.json() == {"ok": False, "status": "network"}

        resp = await client.post("/api/config/validate_key", json={"provider": "qwen"})
        assert resp.status == 400

    api(app, fn)


def test_setup_complete_marks_setup_done(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    monkeypatch.setattr(
        config,
        "mark_setup_done",
        lambda env_path=None: config.update_env_file(
            {"SETUP_DONE": "true"},
            env_path=env_file,
            allow_hidden=True,
        ),
    )
    app = make_app(FakeService())

    async def fn(client):
        resp = await client.post("/api/setup/complete", json={})
        assert resp.status == 200
        return await resp.json()

    assert api(app, fn) == {"ok": True, "updated": ["SETUP_DONE"]}
    assert "SETUP_DONE=true" in env_file.read_text(encoding="utf-8")


def test_config_get_returns_all_visible_specs():
    app = make_app(FakeService())

    async def fn(client):
        resp = await client.get("/api/config")
        assert resp.status == 200
        return await resp.json()

    rows = api(app, fn)
    # hidden 内部项不进面板，其余 spec 全量返回
    visible = [spec for spec in config.CONFIG_SPECS if not spec.hidden]
    assert {row["key"] for row in rows} == {spec.key for spec in visible}
    assert len(rows) == len(visible)
    by_key = {row["key"]: row for row in rows}
    # secret 项不回传真实值
    assert by_key["DASHSCOPE_API_KEY"]["value"] in ("已设置", "未设置")
    assert by_key["AGENT_PROVIDER"]["kind"] == "select"
    assert by_key["AGENT_PROVIDER"]["choices"] == ["qwen", "doubao", "openai"]
    assert by_key["MODEM_PORT"]["requires_restart"] is True


def test_config_post_roundtrip(monkeypatch, tmp_path):
    """POST /api/config 写入 .env（重定向到 tmp）并返回 requires_restart 标注。"""
    env_file = tmp_path / ".env"
    real_update = config.update_env_file
    monkeypatch.setattr(
        config,
        "update_env_file",
        lambda updates, env_path=".env": real_update(updates, env_path=env_file),
    )
    # 先 setenv 注册清理，避免 update_env_file 同步 os.environ 污染其他测试。
    monkeypatch.setenv("QWEN_VOICE", "Raymond")
    monkeypatch.setenv("AGENT_PROVIDER", "qwen")
    monkeypatch.setenv("RECORDING_ENABLED", "true")

    app = make_app(FakeService())

    async def fn(client):
        resp = await client.post(
            "/api/config",
            json={
                "QWEN_VOICE": "Cherry",
                "AGENT_PROVIDER": "doubao",
                "RECORDING_ENABLED": False,  # JSON bool 应被宽容转成 "false"
            },
        )
        assert resp.status == 200
        return await resp.json()

    data = api(app, fn)
    assert data["ok"] is True
    assert data["updated"] == ["QWEN_VOICE", "AGENT_PROVIDER", "RECORDING_ENABLED"]
    assert data["requires_restart"] == ["AGENT_PROVIDER"]

    text = env_file.read_text(encoding="utf-8")
    assert "QWEN_VOICE=Cherry" in text
    assert "AGENT_PROVIDER=doubao" in text
    assert "RECORDING_ENABLED=false" in text
    assert os.environ["QWEN_VOICE"] == "Cherry"


def test_config_post_invalid_rejected(monkeypatch, tmp_path):
    """非法值/非 editable/未注册 key 整批拒绝（400），.env 不落盘。"""
    env_file = tmp_path / ".env"
    real_update = config.update_env_file
    monkeypatch.setattr(
        config,
        "update_env_file",
        lambda updates, env_path=".env": real_update(updates, env_path=env_file),
    )
    app = make_app(FakeService())

    async def fn(client):
        for body in (
            {"MODEM_BAUD": "abc"},  # int 项收到非整数
            {"WEB_PORT": "9000"},  # 非 editable
            {"NO_SUCH_KEY": "1"},  # 未注册
            {"AGENT_PROVIDER": "gpt"},  # select 不在 choices
            {"QWEN_VOICE": ["x"]},  # 值类型不支持
        ):
            resp = await client.post("/api/config", json=body)
            assert resp.status == 400, body
        resp = await client.post("/api/config", data=b"not json")
        assert resp.status == 400

    api(app, fn)
    assert not env_file.exists()


# ---- /api/history ----


def test_history_api_lists_and_limits(tmp_path):
    call_logger = CallLogger(base_dir=tmp_path / "calls")
    rec1 = call_logger.begin_call("inbound", "13800000000")
    rec1.finish("completed")
    rec2 = call_logger.begin_call("outbound", "10086")
    rec2.finish("failed")
    app = make_app(FakeService(call_logger=call_logger))

    async def fn(client):
        resp = await client.get("/api/history")
        assert resp.status == 200
        calls = await resp.json()
        assert {c["number"] for c in calls} == {"13800000000", "10086"}
        assert all(c["status"] in ("completed", "failed") for c in calls)

        resp = await client.get("/api/history?limit=1")
        assert len(await resp.json()) == 1

        resp = await client.get("/api/history?limit=abc")
        assert resp.status == 400
        resp = await client.get("/api/history?limit=0")
        assert resp.status == 400

    api(app, fn)


def test_history_events_returns_timeline(tmp_path):
    call_logger = CallLogger(base_dir=tmp_path / "calls")
    rec = call_logger.begin_call("inbound", "13800000000")
    rec.log_event("transcript", role="user", text="你好")
    rec.finish("completed")
    app = make_app(FakeService(call_logger=call_logger))

    async def fn(client):
        resp = await client.get(f"/api/history/{rec.id}/events")
        assert resp.status == 200
        return await resp.json()

    events = api(app, fn)
    types = [ev["type"] for ev in events]
    assert types[0] == "call_started"
    assert "transcript" in types
    assert types[-1] == "call_finished"
    transcript = next(ev for ev in events if ev["type"] == "transcript")
    assert transcript["text"] == "你好"


def test_history_events_call_id_validation(tmp_path):
    """call_id 只允许 [A-Za-z0-9_-]：路径穿越 400，合法但不存在 404。"""
    call_logger = CallLogger(base_dir=tmp_path / "calls")
    app = make_app(FakeService(call_logger=call_logger))

    async def run(call_id: str):
        request = make_mocked_request(
            "GET",
            f"/api/history/{call_id}/events",
            match_info={"call_id": call_id},
            app=app,
        )
        return await _history_events(request)

    for bad in ("../secret", "..", "a/b", "a.b", "id with space", ""):
        resp = asyncio.run(run(bad))
        assert resp.status == 400, bad

    resp = asyncio.run(run("20260707-183000-inbound-100"))
    assert resp.status == 404


def test_history_delete_single_and_missing(tmp_path):
    call_logger = CallLogger(base_dir=tmp_path / "calls")
    rec = call_logger.begin_call("inbound", "13800000000")
    rec.finish("completed")
    app = make_app(FakeService(call_logger=call_logger))

    async def fn(client):
        resp = await client.delete(f"/api/history/{rec.id}")
        assert resp.status == 200
        assert await resp.json() == {"ok": True, "deleted": [rec.id], "skipped": []}
        assert not (call_logger.base_dir / rec.id).exists()

        resp = await client.delete(f"/api/history/{rec.id}")
        assert resp.status == 404

    api(app, fn)


def test_history_delete_rejects_bad_id_and_skips_active(tmp_path):
    call_logger = CallLogger(base_dir=tmp_path / "calls")
    rec = call_logger.begin_call("inbound", "13800000000")
    service = FakeService(
        call_logger=call_logger,
        session=SimpleNamespace(is_active=True, _record=SimpleNamespace(id=rec.id)),
    )
    app = make_app(service)

    async def run_bad(call_id: str):
        request = make_mocked_request(
            "DELETE",
            f"/api/history/{call_id}",
            match_info={"call_id": call_id},
            app=app,
        )
        return await _history_delete(request)

    async def fn(client):
        resp = await client.delete(f"/api/history/{rec.id}")
        assert resp.status == 200
        assert await resp.json() == {"ok": True, "deleted": [], "skipped": [rec.id]}
        assert (call_logger.base_dir / rec.id).exists()

    resp = asyncio.run(run_bad("../secret"))
    assert resp.status == 400
    api(app, fn)


def test_history_clear_all_deletes_finished_and_skips_active(tmp_path):
    call_logger = CallLogger(base_dir=tmp_path / "calls")
    active = call_logger.begin_call("inbound", "13800000000")
    done = call_logger.begin_call("outbound", "10086")
    done.finish("completed")
    service = FakeService(
        call_logger=call_logger,
        session=SimpleNamespace(is_active=True, _record=SimpleNamespace(id=active.id)),
    )
    app = make_app(service)

    async def fn(client):
        resp = await client.delete("/api/history")
        assert resp.status == 200
        assert await resp.json() == {"ok": True, "deleted": [done.id], "skipped": [active.id]}

    api(app, fn)
    assert (call_logger.base_dir / active.id).exists()
    assert not (call_logger.base_dir / done.id).exists()


# ---- /api/history/{id}/audio/{track}：录音回放（浏览器播放）----


def test_history_audio_serves_recorded_wav(tmp_path):
    call_logger = CallLogger(base_dir=tmp_path / "calls")
    rec = call_logger.begin_call("outbound", "10086")
    rec.write_downlink(b"\x01\x00" * 200)
    rec.finish("completed")
    app = make_app(FakeService(call_logger=call_logger))

    async def fn(client):
        resp = await client.get(f"/api/history/{rec.id}/audio/downlink")
        assert resp.status == 200
        assert resp.headers["Content-Type"] == "audio/wav"
        body = await resp.read()
        assert body[:4] == b"RIFF"  # WAV 头
        return body

    assert len(api(app, fn)) > 44  # WAV 头 + 采样数据


def test_history_audio_uplink_amplified(tmp_path, monkeypatch):
    """上行回放前放大到可闻：原始极轻的样本经路由后峰值明显变大。"""
    monkeypatch.setenv("MONITOR_UPLINK_GAIN", "10")
    call_logger = CallLogger(base_dir=tmp_path / "calls")
    rec = call_logger.begin_call("outbound", "10086")
    rec.write_uplink((100).to_bytes(2, "little", signed=True) * 400)  # 很轻的上行
    rec.finish("completed")
    app = make_app(FakeService(call_logger=call_logger))

    async def fn(client):
        resp = await client.get(f"/api/history/{rec.id}/audio/uplink")
        assert resp.status == 200
        assert resp.headers["Content-Type"] == "audio/wav"
        return await resp.read()

    body = api(app, fn)
    import io as _io
    import wave as _wave

    import numpy as np

    with _wave.open(_io.BytesIO(body), "rb") as w:
        samples = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    assert int(np.abs(samples).max()) >= 900  # 原始 100 → 约 1000（放大 10x）


def test_history_audio_validation(tmp_path):
    """track 仅 downlink/uplink；call_id 防路径穿越；合法但不存在 404。"""
    call_logger = CallLogger(base_dir=tmp_path / "calls")
    app = make_app(FakeService(call_logger=call_logger))

    async def run(call_id: str, track: str):
        request = make_mocked_request(
            "GET",
            f"/api/history/{call_id}/audio/{track}",
            match_info={"call_id": call_id, "track": track},
            app=app,
        )
        return await _history_audio(request)

    assert asyncio.run(run("../secret", "downlink")).status == 400  # 路径穿越
    assert asyncio.run(run("20260707-183000-inbound-100", "evil")).status == 400  # 非法 track
    assert asyncio.run(run("20260707-183000-inbound-100", "downlink")).status == 404  # 不存在


# ---- /api/call/batch_dial 与 /api/call/queue ----


def test_batch_dial_delegates_to_service():
    service = FakeService()
    service.batch_result = {"accepted": ["10086"], "rejected": ["bad"]}
    app = make_app(service)

    async def fn(client):
        resp = await client.post(
            "/api/call/batch_dial",
            json={"numbers": ["10086", "bad"], "task": "催快递"},
        )
        assert resp.status == 200
        assert await resp.json() == {
            "ok": True,
            "accepted": ["10086"],
            "rejected": ["bad"],
        }

        # task 缺省 / 空串都应透传 None
        resp = await client.post("/api/call/batch_dial", json={"numbers": ["10010"]})
        assert resp.status == 200
        resp = await client.post(
            "/api/call/batch_dial", json={"numbers": ["10000"], "task": "  "}
        )
        assert resp.status == 200

    api(app, fn)
    assert service.batch_calls == [
        (["10086", "bad"], "催快递"),
        (["10010"], None),
        (["10000"], None),
    ]


def test_batch_dial_rejects_bad_params():
    service = FakeService()
    app = make_app(service)

    async def fn(client):
        for body in (
            {},  # 缺 numbers
            {"numbers": []},  # 空列表
            {"numbers": "10086"},  # 不是列表
            {"numbers": [123]},  # 项不是字符串
            {"numbers": ["10086"], "task": 5},  # task 类型错
        ):
            resp = await client.post("/api/call/batch_dial", json=body)
            assert resp.status == 400, body
        resp = await client.post("/api/call/batch_dial", data=b"not json")
        assert resp.status == 400

    api(app, fn)
    assert service.batch_calls == []


def test_queue_status_passthrough():
    service = FakeService()
    service.queue = {
        "pending": ["10086"],
        "current": "10010",
        "done": [{"number": "10000", "ok": True, "error": None}],
        "active": True,
    }
    app = make_app(service)

    async def fn(client):
        resp = await client.get("/api/call/queue")
        assert resp.status == 200
        # 成功响应统一补 ok=true；其余队列字段原样透传。
        assert await resp.json() == {**service.queue, "ok": True}

    api(app, fn)


def test_number_profiles_api_lists_profiles(tmp_path, monkeypatch):
    profile_file = tmp_path / "number_profiles.json"
    profile_file.write_text(
        '{"profiles":[{"label":"Preset <safe>","number":"10000","task":"查流量","scenario":"策略"}]}',
        encoding="utf-8",
    )
    monkeypatch.setenv("NUMBER_PROFILES_ENABLED", "true")
    monkeypatch.setenv("NUMBER_PROFILES_FILE", str(profile_file))
    app = make_app(FakeService())

    async def fn(client):
        resp = await client.get("/api/number_profiles")
        assert resp.status == 200
        return await resp.json()

    assert api(app, fn) == {
        "profiles": [{"number": "10000", "task": "查流量", "label": "Preset <safe>"}]
    }


def test_number_profiles_api_returns_empty_when_disabled(tmp_path, monkeypatch):
    profile_file = tmp_path / "number_profiles.json"
    profile_file.write_text(
        '{"profiles":[{"label":"Preset","number":"10000","task":"查流量","scenario":"策略"}]}',
        encoding="utf-8",
    )
    monkeypatch.setenv("NUMBER_PROFILES_ENABLED", "false")
    monkeypatch.setenv("NUMBER_PROFILES_FILE", str(profile_file))
    app = make_app(FakeService())

    async def fn(client):
        resp = await client.get("/api/number_profiles")
        assert resp.status == 200
        return await resp.json()

    assert api(app, fn) == {"profiles": []}


def test_endpoints_without_service_return_500():
    app = make_app(None)

    async def fn(client):
        for path in ("/api/call/queue", "/api/history"):
            resp = await client.get(path)
            assert resp.status == 500, path
        resp = await client.get("/api/history/abc/events")
        assert resp.status == 500
        # service 缺失时，所有依赖 service 的 POST 端点统一 500（middleware 转换）。
        for path, body in (
            ("/api/call/batch_dial", {"numbers": ["1"]}),
            ("/api/call/dial", {"number": "10086"}),
            ("/api/call/hangup", {}),
            ("/api/call/dtmf", {"digits": "1"}),
        ):
            resp = await client.post(path, json=body)
            assert resp.status == 500, path

    api(app, fn)


# ---- /api/meta ----


def test_meta_returns_injected_metadata(monkeypatch):
    monkeypatch.delenv("SETUP_DONE", raising=False)
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    monkeypatch.delenv("DOUBAO_APP_ID", raising=False)
    monkeypatch.delenv("DOUBAO_ACCESS_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    app = build_app(hub=None, modem=None, service=FakeService(), meta={"model": "qwen-x"})  # type: ignore[arg-type]

    async def fn(client):
        resp = await client.get("/api/meta")
        assert resp.status == 200
        return await resp.json()

    meta = api(app, fn)
    assert meta["model"] == "qwen-x"
    assert meta["credentials"]["ok"] is False
    assert meta["setup_required"] is True
    assert set(meta["hardware"]) == {"usb_online", "modem_connected", "port"}


# ---- /api/call/dial ----


def test_dial_delegates_and_validates():
    service = FakeService()
    app = make_app(service)

    async def fn(client):
        # 正常外呼 → 200 且透传到 service.dial
        resp = await client.post("/api/call/dial", json={"number": "10086", "task": "催快递"})
        assert resp.status == 200
        assert await resp.json() == {"ok": True}

        # 号码空 → 400，且不落到 service
        resp = await client.post("/api/call/dial", json={"number": "   "})
        assert resp.status == 400

        # task 非字符串 → 400
        resp = await client.post("/api/call/dial", json={"number": "10086", "task": 5})
        assert resp.status == 400

        # 非法 JSON → 400
        resp = await client.post("/api/call/dial", data=b"not json")
        assert resp.status == 400

    api(app, fn)
    assert service.dial_calls == [("10086", "催快递", None)]


def test_dial_forwards_preset_hint():
    """选中预设时 preset_task 作为命中键透传 service.dial；子主题走 task；非字符串→400。"""
    service = FakeService()
    app = make_app(service)

    async def fn(client):
        resp = await client.post(
            "/api/call/dial",
            json={"number": "12345", "task": "退休金怎么领取", "preset_task": "政务咨询"},
        )
        assert resp.status == 200
        resp = await client.post("/api/call/dial", json={"number": "12345", "preset_task": 5})
        assert resp.status == 400

    api(app, fn)
    assert service.dial_calls == [("12345", "退休金怎么领取", "政务咨询")]


def test_dial_conflict_when_service_rejects():
    """service.dial 返回 (False, err) → 409（如通话中）。"""
    service = FakeService()
    service.dial_result = (False, "当前正在通话中，请稍后再拨")
    app = make_app(service)

    async def fn(client):
        resp = await client.post("/api/call/dial", json={"number": "10086"})
        assert resp.status == 409
        body = await resp.json()
        assert body["ok"] is False
        assert body["error"] == "当前正在通话中，请稍后再拨"

    api(app, fn)


# ---- /api/call/hangup ----


def test_hangup_success_and_no_active_call():
    # 有通话 → 200
    service = FakeService()
    service.hangup_result = (True, None)
    app = make_app(service)

    async def ok_fn(client):
        resp = await client.post("/api/call/hangup", json={})
        assert resp.status == 200
        assert await resp.json() == {"ok": True}

    api(app, ok_fn)

    # 无通话 → 409
    service2 = FakeService()
    service2.hangup_result = (False, "当前没有进行中的通话")
    app2 = make_app(service2)

    async def conflict_fn(client):
        resp = await client.post("/api/call/hangup", json={})
        assert resp.status == 409
        body = await resp.json()
        assert body["ok"] is False
        assert body["error"] == "当前没有进行中的通话"

    api(app2, conflict_fn)


# ---- /api/call/dtmf ----


def test_dtmf_success_validation_and_no_active_call():
    service = FakeService()
    app = make_app(service)

    async def fn(client):
        # 正常按键 → 200，透传到 service.send_dtmf
        resp = await client.post("/api/call/dtmf", json={"digits": "12*#"})
        assert resp.status == 200
        assert await resp.json() == {"ok": True}

        # digits 空 → 400
        resp = await client.post("/api/call/dtmf", json={"digits": ""})
        assert resp.status == 400
        # digits 含非法字符 → 400
        resp = await client.post("/api/call/dtmf", json={"digits": "12a"})
        assert resp.status == 400
        # 非法 JSON → 400
        resp = await client.post("/api/call/dtmf", data=b"not json")
        assert resp.status == 400

    api(app, fn)
    # 只有合法请求会落到 service
    assert service.dtmf_calls == ["12*#"]


def test_dtmf_no_active_call_returns_409():
    service = FakeService()
    service.dtmf_result = (False, "当前没有进行中的通话")
    app = make_app(service)

    async def fn(client):
        resp = await client.post("/api/call/dtmf", json={"digits": "1"})
        assert resp.status == 409
        body = await resp.json()
        assert body["ok"] is False
        assert body["error"] == "当前没有进行中的通话"

    api(app, fn)


def test_dtmf_send_failure_keeps_200():
    """模组发送失败沿用旧行为：200 + {"ok": false}（非无通话场景）。"""
    service = FakeService()
    service.dtmf_result = (False, "按键发送失败")
    app = make_app(service)

    async def fn(client):
        resp = await client.post("/api/call/dtmf", json={"digits": "1"})
        assert resp.status == 200
        assert await resp.json() == {"ok": False}

    api(app, fn)


def test_dtmf_validation_precedes_call_state_check():
    """有意的行为决策（2026-07 重构评审确认）：参数校验优先于通话状态。

    无通话 + 非法参数的双重错误场景返回 400（旧实现返回 409）——
    「先修好请求再谈状态冲突」是标准 REST 语义；唯一前端消费者只读
    res.ok 不看状态码。此测试锁定该决策，防止无意回摆。
    """
    service = FakeService()
    service.dtmf_result = (False, "当前没有进行中的通话")
    app = make_app(service)

    async def fn(client):
        resp = await client.post("/api/call/dtmf", json={"digits": "abc"})
        assert resp.status == 400
        resp = await client.post("/api/call/dtmf", data=b"not json")
        assert resp.status == 400

    api(app, fn)
    assert service.dtmf_calls == []  # 非法请求不触达 service


# ---- /api/sms/send ----


def test_send_sms_success_and_validation():
    # 10086 曾发来短信 → 属已联系号码,可回复。
    hub = FakeHub(history=[{"type": "sms_in", "sender": "10086", "text": "余额"}])
    modem = FakeModem(send_result=True)
    app = build_app(hub=hub, modem=modem, service=FakeService())  # type: ignore[arg-type]

    async def fn(client):
        # 正常发送（已联系号码）→ 200 且透传到 modem.send_sms
        resp = await client.post(
            "/api/sms/send", json={"number": "10086", "text": "余额查询"}
        )
        assert resp.status == 200
        assert await resp.json() == {"ok": True}

        # 号码空 → 400
        resp = await client.post("/api/sms/send", json={"number": "", "text": "x"})
        assert resp.status == 400
        # 内容空 → 400
        resp = await client.post("/api/sms/send", json={"number": "10086", "text": ""})
        assert resp.status == 400
        # 非法 JSON → 400
        resp = await client.post("/api/sms/send", data=b"not json")
        assert resp.status == 400

    api(app, fn)
    assert modem.sms_calls == [("10086", "余额查询")]


def test_send_sms_rejects_uncontacted_number():
    """未联系过的号码（无来电/来信）→ 403,且不触发发送。"""
    hub = FakeHub()  # 无历史联系人
    modem = FakeModem(send_result=True)
    app = build_app(hub=hub, modem=modem, service=FakeService())  # type: ignore[arg-type]

    async def fn(client):
        resp = await client.post(
            "/api/sms/send", json={"number": "18800000000", "text": "陌生号码"}
        )
        assert resp.status == 403
        body = await resp.json()
        assert body["ok"] is False

    api(app, fn)
    assert modem.sms_calls == []  # 拦截不触发发送


def test_send_sms_current_caller_bypass_requires_active_session():
    """当前对端放行必须绑定 is_active:会话结束后 current_caller 残留不得绕过网关。

    回归 codex review 发现的 P1:current_caller 通话结束不清空,且 /api/call/dial
    会把任意外呼目标写进它,不 gate on is_active 会被 CSRF 利用(先拨号再发短信)。
    """
    hub = FakeHub()  # 无历史联系人

    # 会话已结束(is_active=False)但 current_caller 残留 → 陌生号码仍被拒。
    stale_modem = FakeModem(send_result=True)
    stale = FakeService(
        session=_SessionStub(current_caller="18800000000", is_active=False)
    )
    app = build_app(hub=hub, modem=stale_modem, service=stale)  # type: ignore[arg-type]

    async def rejected(client):
        resp = await client.post(
            "/api/sms/send", json={"number": "18800000000", "text": "x"}
        )
        assert resp.status == 403

    api(app, rejected)
    assert stale_modem.sms_calls == []

    # 通话进行中(is_active=True)可给当前对端回短信。
    active_modem = FakeModem(send_result=True)
    active = FakeService(
        session=_SessionStub(current_caller="18800000000", is_active=True)
    )
    app2 = build_app(hub=hub, modem=active_modem, service=active)  # type: ignore[arg-type]

    async def allowed(client):
        resp = await client.post(
            "/api/sms/send", json={"number": "18800000000", "text": "x"}
        )
        assert resp.status == 200

    api(app2, allowed)
    assert active_modem.sms_calls == [("18800000000", "x")]


def test_send_sms_api_uses_shared_rate_limit(monkeypatch):
    monkeypatch.setenv("SMS_RATE_LIMIT_PER_HOUR", "1")
    from agentcall import rate_limit

    rate_limit.reset_sms_rate_limit_state()
    hub = FakeHub(history=[{"type": "sms_in", "sender": "10086", "text": "余额"}])
    modem = FakeModem(send_result=True)
    app = build_app(hub=hub, modem=modem, service=FakeService())  # type: ignore[arg-type]

    async def fn(client):
        resp = await client.post("/api/sms/send", json={"number": "10086", "text": "one"})
        assert resp.status == 200

        resp = await client.post("/api/sms/send", json={"number": "10086", "text": "two"})
        assert resp.status == 429
        body = await resp.json()
        assert body["ok"] is False
        assert "频控" in body["error"]

    api(app, fn)
    assert modem.sms_calls == [("10086", "one")]
    rate_limit.reset_sms_rate_limit_state()


# ---- /api/restart ----

def test_restart_without_event_returns_501():
    """未注入 restart_event（如非受管运行）时优雅拒绝，不假装成功。"""
    app = make_app(FakeService())  # build_app 不带 restart_event
    async def fn(client):
        resp = await client.post("/api/restart", json={})
        assert resp.status == 501
        assert (await resp.json())["ok"] is False
    api(app, fn)


def test_restart_sets_event_and_returns_ok():
    """注入 restart_event 时：置位事件并返回 ok（主循环据此 execv 重启）。"""
    import threading

    from agentcall.web.server import build_app as _build
    ev = threading.Event()
    app = _build(hub=None, modem=None, service=FakeService(), restart_event=ev)
    async def fn(client):
        resp = await client.post("/api/restart", json={})
        assert resp.status == 200
        assert (await resp.json())["ok"] is True
    api(app, fn)
    assert ev.is_set()
