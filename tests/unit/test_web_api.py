"""web API 单测：通话历史/设置面板/批量外呼接口。

用 aiohttp TestClient 做全链路请求，路径穿越校验用 make_mocked_request 直测 handler；
service 用最小替身（只实现 web 层用到的接口），CallLogger 落到 tmp_path。
"""

from __future__ import annotations

import asyncio
import os

from aiohttp.test_utils import TestClient, TestServer, make_mocked_request

from agentcall import config
from agentcall.call_log import CallLogger
from agentcall.web.server import _history_events, build_app


class FakeService:
    """最小 service 替身：只提供 web 层用到的接口。"""

    def __init__(self, call_logger: CallLogger | None = None) -> None:
        self.call_logger = call_logger
        self.batch_calls: list[tuple[list[str], str | None]] = []
        self.batch_result: dict = {"accepted": [], "rejected": []}
        self.queue: dict = {"pending": [], "current": None, "done": [], "active": False}

    def batch_dial(self, numbers: list[str], task: str | None = None) -> dict:
        self.batch_calls.append((list(numbers), task))
        return self.batch_result

    def dial_queue_status(self) -> dict:
        return dict(self.queue)


def make_app(service):
    return build_app(hub=None, modem=None, service=service)  # type: ignore[arg-type]


def api(app, fn):
    """起 TestServer 并在事件循环里执行 fn(client)，返回其结果。"""

    async def runner():
        async with TestClient(TestServer(app)) as client:
            return await fn(client)

    return asyncio.run(runner())


# ---- /api/config ----


def test_config_get_returns_all_specs():
    app = make_app(FakeService())

    async def fn(client):
        resp = await client.get("/api/config")
        assert resp.status == 200
        return await resp.json()

    rows = api(app, fn)
    assert len(rows) == len(config.CONFIG_SPECS)
    by_key = {row["key"]: row for row in rows}
    # secret 项不回传真实值
    assert by_key["DASHSCOPE_API_KEY"]["value"] in ("已设置", "未设置")
    assert by_key["AGENT_PROVIDER"]["kind"] == "select"
    assert by_key["AGENT_PROVIDER"]["choices"] == ["qwen", "doubao"]
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
        assert await resp.json() == {"accepted": ["10086"], "rejected": ["bad"]}

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
        assert await resp.json() == service.queue

    api(app, fn)


def test_endpoints_without_service_return_500():
    app = make_app(None)

    async def fn(client):
        for path in ("/api/call/queue", "/api/history"):
            resp = await client.get(path)
            assert resp.status == 500, path
        resp = await client.get("/api/history/abc/events")
        assert resp.status == 500
        resp = await client.post("/api/call/batch_dial", json={"numbers": ["1"]})
        assert resp.status == 500

    api(app, fn)
