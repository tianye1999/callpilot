"""Public, least-privilege HTTP gateway for paired Remote Web Dialer phones."""

from __future__ import annotations

import asyncio
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer

from agentcall.remote_pairing import RemotePairingStore
from agentcall.web.remote_gateway import DEVICE_COOKIE, build_remote_gateway


class _Service:
    def __init__(self) -> None:
        self.invite_calls = 0
        self.paired_invite_calls: list[str] = []
        self.invite_result: tuple[dict | None, str | None] = (
            {
                "session_id": "session-1",
                "url": "https://dial.example/#browser-token",
                "expires_at": 12345.0,
            },
            None,
        )

    def create_remote_dialer_invite(self) -> tuple[dict | None, str | None]:
        self.invite_calls += 1
        return self.invite_result

    def create_paired_remote_dialer_invite(
        self, device_id: str
    ) -> tuple[dict | None, str | None]:
        self.paired_invite_calls.append(device_id)
        return self.create_remote_dialer_invite()

    def remote_dialer_status(self) -> dict:
        return {
            "enabled": True,
            "configured": True,
            "active": False,
            "modem_online": True,
        }


def _api(app, fn):
    async def runner():
        async with TestClient(TestServer(app), cookie_jar=None) as client:
            return await fn(client)

    return asyncio.run(runner())


def _cookie_value(response) -> str:
    morsel = response.cookies[DEVICE_COOKIE]
    return morsel.value


def test_fixed_entry_pairs_once_then_issues_short_lived_call_session(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("REMOTE_WEB_DIALER_ENABLED", "true")
    service = _Service()
    store = RemotePairingStore(tmp_path / "devices.json")
    offer = store.create_pairing("https://dial.example/")
    app = build_remote_gateway(service, store, public_url="https://dial.example/")

    async def fn(client: TestClient) -> None:
        page = await client.get("/")
        assert page.status == 200
        assert "no-store" in page.headers["Cache-Control"]
        assert "frame-ancestors 'none'" in page.headers["Content-Security-Policy"]

        pair = await client.post(
            "/api/pair",
            json={"code": offer.code, "display_name": "My iPhone"},
            headers={"Origin": "https://dial.example"},
        )
        assert pair.status == 200
        pair_body = await pair.json()
        assert pair_body["device"]["display_name"] == "My iPhone"
        body_device_id = pair_body["device"]["device_id"]
        cookie = pair.cookies[DEVICE_COOKIE]
        assert DEVICE_COOKIE.startswith("__Host-")
        assert cookie["secure"]
        assert cookie["httponly"]
        assert cookie["samesite"] == "Strict"
        assert not cookie["domain"]

        auth = {"Cookie": f"{DEVICE_COOKIE}={_cookie_value(pair)}"}
        device = await client.get("/api/device", headers=auth)
        device_body = await device.json()
        assert device_body["paired"] is True
        assert device_body["edge"]["modem_online"] is True

        session = await client.post(
            "/api/session",
            json={},
            headers={**auth, "Origin": "https://dial.example"},
        )
        assert session.status == 200
        assert (await session.json())["invite"]["session_id"] == "session-1"
        assert session.headers["Cache-Control"] == "no-store"
        assert service.invite_calls == 1
        assert service.paired_invite_calls == [body_device_id]

        unpair = await client.post(
            "/api/unpair",
            json={},
            headers={**auth, "Origin": "https://dial.example"},
        )
        assert unpair.status == 200
        assert unpair.cookies[DEVICE_COOKIE]["max-age"] == "0"
        denied = await client.post(
            "/api/session",
            json={},
            headers={**auth, "Origin": "https://dial.example"},
        )
        assert denied.status == 401

    _api(app, fn)


def test_public_gateway_denies_unpaired_revoked_cross_origin_and_admin_requests(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("REMOTE_WEB_DIALER_ENABLED", "true")
    service = _Service()
    store = RemotePairingStore(tmp_path / "devices.json")
    offer = store.create_pairing("https://dial.example/")
    app = build_remote_gateway(service, store, public_url="https://dial.example/")

    async def fn(client: TestClient) -> None:
        service.remote_dialer_status = lambda: {  # type: ignore[method-assign]
            "enabled": True,
            "configured": True,
            "active": True,
            "session_id": "private-session",
            "call_active": True,
        }
        anonymous_status = await client.get("/api/device")
        assert await anonymous_status.json() == {
            "ok": True,
            "paired": False,
            "edge": {"enabled": True, "configured": True},
        }

        no_cookie = await client.post(
            "/api/session", json={}, headers={"Origin": "https://dial.example"}
        )
        assert no_cookie.status == 401

        cross_origin = await client.post(
            "/api/pair",
            json={"code": offer.code, "display_name": "phone"},
            headers={"Origin": "https://evil.example"},
        )
        assert cross_origin.status == 403

        pair = await client.post(
            "/api/pair",
            json={"code": offer.code, "display_name": "phone"},
            headers={"Origin": "https://dial.example"},
        )
        body = await pair.json()
        auth = {"Cookie": f"{DEVICE_COOKIE}={_cookie_value(pair)}"}
        assert store.revoke(body["device"]["device_id"])

        revoked = await client.post(
            "/api/session",
            json={},
            headers={**auth, "Origin": "https://dial.example"},
        )
        assert revoked.status == 401
        assert service.invite_calls == 0

        for path in ("/api/config", "/api/sms/send", "/api/history", "/ws"):
            response = await client.get(path)
            assert response.status == 404

    _api(app, fn)


def test_gateway_is_default_off_and_pairing_attempts_are_rate_limited(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("REMOTE_WEB_DIALER_ENABLED", "false")
    service = _Service()
    store = RemotePairingStore(tmp_path / "devices.json")
    app = build_remote_gateway(service, store, public_url="https://dial.example/")

    async def fn(client: TestClient) -> None:
        disabled = await client.post(
            "/api/pair",
            json={"code": "WRONG123", "display_name": "phone"},
            headers={"Origin": "https://dial.example"},
        )
        assert disabled.status == 403

    _api(app, fn)

    monkeypatch.setenv("REMOTE_WEB_DIALER_ENABLED", "true")
    limited_app = build_remote_gateway(
        service,
        store,
        public_url="https://dial.example/",
        max_pair_attempts=2,
    )

    async def limited(client: TestClient) -> None:
        for expected in (401, 401, 429):
            response = await client.post(
                "/api/pair",
                json={"code": "WRONG123", "display_name": "phone"},
                headers={
                    "Origin": "https://dial.example",
                    "CF-Connecting-IP": "203.0.113.10",
                },
            )
            assert response.status == expected
        separate_client = await client.post(
            "/api/pair",
            json={"code": "WRONG123", "display_name": "phone"},
            headers={
                "Origin": "https://dial.example",
                "CF-Connecting-IP": "203.0.113.11",
            },
        )
        assert separate_client.status == 401

    _api(limited_app, limited)


def test_gateway_rejects_malformed_json_and_oversized_device_name(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("REMOTE_WEB_DIALER_ENABLED", "true")
    app = build_remote_gateway(
        _Service(),
        RemotePairingStore(tmp_path / "devices.json"),
        public_url="https://dial.example/",
    )

    async def fn(client: TestClient) -> None:
        malformed = await client.post(
            "/api/pair",
            data="not-json",
            headers={"Origin": "https://dial.example", "Content-Type": "application/json"},
        )
        assert malformed.status == 400

        oversized = await client.post(
            "/api/pair",
            json={"code": "ABCDEFGH", "display_name": "x" * 65},
            headers={"Origin": "https://dial.example"},
        )
        assert oversized.status == 400

    _api(app, fn)


def test_gateway_rejects_public_url_with_fragment(tmp_path: Path) -> None:
    store = RemotePairingStore(tmp_path / "devices.json")
    try:
        build_remote_gateway(_Service(), store, public_url="https://dial.example/#secret")
    except ValueError as exc:
        assert "HTTPS" in str(exc)
    else:
        raise AssertionError("fragment-bearing public URL must be rejected")
