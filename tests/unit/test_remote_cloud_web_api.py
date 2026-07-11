"""Local dashboard bridge to the hosted control plane."""

from __future__ import annotations

import asyncio

from aiohttp.test_utils import TestClient, TestServer
from fakes import FakeModem

from agentcall.cloud_control import CloudControlStatus
from agentcall.cloud_credentials import EdgeCredential
from agentcall.events import EventHub
from agentcall.web.server import build_app


class _Service:
    def remote_dialer_status(self):
        return {
            "enabled": True,
            "cloud_enabled": True,
            "configured": True,
            "missing": [],
            "active": False,
        }


class _Store:
    def __init__(self) -> None:
        self.credential = None
        self.saved: list[str] = []

    def load(self):
        return self.credential

    def load_or_create_public_key(self):
        return "public-key-material-for-test-1234567890"

    def save(self, value):
        self.saved.append(value)
        self.credential = EdgeCredential("edge_abcdefghijkl", value)
        return self.credential


class _Api:
    def __init__(self) -> None:
        self.enrollments = []
        self.revoked = []

    def enroll(self, code, display_name, public_key):
        self.enrollments.append((code, display_name, public_key))
        return {"credential": "edge_abcdefghijkl." + "s" * 40}

    def create_pairing(self, credential):
        assert credential.edge_id == "edge_abcdefghijkl"
        return {"code": "ABCD-EFGH", "expiresAt": 2_000_000}

    def list_devices(self, credential):
        assert credential.edge_id == "edge_abcdefghijkl"
        return {
            "devices": [
                {
                    "device_id": "device_abcdefghijkl",
                    "display_name": "Phone",
                    "created_at": 1_000_000,
                    "last_used_at": 1_500_000,
                }
            ]
        }

    def revoke_device(self, credential, device_id):
        self.revoked.append((credential.edge_id, device_id))
        return {"revoked": True}


class _Client:
    def __init__(self, store: _Store) -> None:
        self.store = store

    def status(self):
        credential = self.store.load()
        return CloudControlStatus(
            enabled=True,
            enrolled=credential is not None,
            connected=False,
            edge_id=credential.edge_id if credential else None,
            last_error="EDGE_NOT_ENROLLED" if credential is None else None,
        )


def test_dashboard_enrolls_then_manages_cloud_pairing_without_returning_secret(monkeypatch) -> None:
    monkeypatch.setenv("REMOTE_WEB_DIALER_ENABLED", "true")
    monkeypatch.setenv("REMOTE_CLOUD_ENABLED", "true")
    store = _Store()
    api = _Api()
    app = build_app(
        EventHub(asyncio.new_event_loop()),
        FakeModem(),  # type: ignore[arg-type]
        service=_Service(),
        remote_cloud_api=api,
        remote_cloud_store=store,
        remote_cloud_client=_Client(store),
    )

    async def scenario() -> None:
        async with TestClient(TestServer(app)) as client:
            status = await client.get("/api/remote_cloud/status")
            assert (await status.json())["enrolled"] is False

            enroll = await client.post(
                "/api/remote_cloud/enroll",
                json={"code": "x" * 40, "display_name": "Office Mac"},
            )
            body = await enroll.json()
            assert body == {"ok": True, "edge_id": "edge_abcdefghijkl"}
            assert "s" * 40 not in str(body)
            assert api.enrollments == [
                ("x" * 40, "Office Mac", "public-key-material-for-test-1234567890")
            ]

            pairing = await client.post("/api/remote_dialer/pairing", json={})
            pairing_body = await pairing.json()
            assert pairing_body["pairing"]["url"] == (
                "https://dial.bondings.ai/#pair=ABCD-EFGH"
            )

            devices = await client.get("/api/remote_dialer/devices")
            assert (await devices.json())["devices"][0]["display_name"] == "Phone"

            revoke = await client.delete(
                "/api/remote_dialer/devices/device_abcdefghijkl"
            )
            assert revoke.status == 200
            assert api.revoked == [
                ("edge_abcdefghijkl", "device_abcdefghijkl")
            ]

    asyncio.run(scenario())

