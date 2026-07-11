"""LAN web dialer POC tests.

The hardware-facing bits are faked here; these tests pin the small contract that
the phone browser POC depends on: validate before dialing, wait for a real
connected signal, bridge browser PCM into modem PCM, and release the active slot
after a call ends.
"""

from __future__ import annotations

import asyncio
import time

import pytest
from aiohttp.test_utils import TestClient, TestServer
from fakes import FakeAudioBridge, FakeModem

from agentcall.lan_web_dialer import (
    LanDialerBusyError,
    LanDialerCall,
    LanDialerController,
    LanDialerError,
    LanDialerSettings,
    LanDialerValidationError,
    _send_downlink,
    build_lan_dialer_app,
    parse_start_payload,
)


class FakeWs:
    def __init__(self) -> None:
        self.closed = False
        self.json_messages: list[dict[str, object]] = []
        self.binary_messages: list[bytes] = []

    async def send_json(self, payload: dict[str, object]) -> None:
        self.json_messages.append(payload)

    async def send_bytes(self, payload: bytes) -> None:
        self.binary_messages.append(payload)

    async def close(self) -> None:
        self.closed = True


def test_parse_start_payload_rejects_bad_number_before_modem_use() -> None:
    with pytest.raises(LanDialerValidationError, match="号码格式不合法"):
        parse_start_payload({"type": "start", "number": "10000;ATH", "sampleRate": 48000})


def test_parse_start_payload_rejects_unreasonable_sample_rate() -> None:
    with pytest.raises(LanDialerValidationError, match="sampleRate"):
        parse_start_payload({"type": "start", "number": "10000", "sampleRate": 1000})


def test_call_waits_for_connected_then_starts_voice_bridge() -> None:
    async def run() -> None:
        modem = FakeModem()
        bridge = FakeAudioBridge()
        call = LanDialerCall(
            modem,  # type: ignore[arg-type]
            settings=LanDialerSettings(connect_timeout_seconds=1.0),
            bridge_factory=lambda **_kw: bridge,
        )

        begin_task = asyncio.create_task(call.begin("10000"))
        await asyncio.sleep(0.05)

        assert ("dial", ("10000",)) in modem.calls
        assert not bridge.started

        modem.trigger_call_connected("10000")
        await begin_task

        assert ("initialize_for_voice", ("uac",)) in modem.calls
        assert bridge.started

        call.end()
        assert bridge.stopped
        assert modem.calls[-1] == ("hangup", ())

    asyncio.run(run())


def test_call_times_out_if_modem_command_blocks() -> None:
    class SlowDialModem(FakeModem):
        def dial(self, number: str) -> str:
            time.sleep(0.05)
            return super().dial(number)

    async def run() -> None:
        modem = SlowDialModem()
        bridge = FakeAudioBridge()
        call = LanDialerCall(
            modem,  # type: ignore[arg-type]
            settings=LanDialerSettings(
                connect_timeout_seconds=1.0,
                modem_command_timeout_seconds=0.01,
            ),
            bridge_factory=lambda **_kw: bridge,
        )

        with pytest.raises(LanDialerError, match="模组指令超时"):
            await call.begin("10000")

        assert not bridge.started
        assert not call.active

    asyncio.run(run())


def test_call_bridges_browser_pcm_to_modem_and_modem_pcm_to_browser() -> None:
    async def run() -> None:
        modem = FakeModem()
        bridge = FakeAudioBridge()
        call = LanDialerCall(
            modem,  # type: ignore[arg-type]
            settings=LanDialerSettings(connect_timeout_seconds=1.0),
            bridge_factory=lambda **_kw: bridge,
        )

        begin_task = asyncio.create_task(call.begin("10000"))
        await asyncio.sleep(0.05)
        modem.trigger_call_connected("10000")
        await begin_task
        call.accept_browser_pcm(b"\x01\x00" * 480, browser_sample_rate=48000)
        bridge.feed_uplink(b"\x02\x00" * 160)

        assert bridge.downlink
        assert len(bridge.downlink[0]) == 160
        assert call.read_modem_pcm() == b"\x02\x00" * 160

        call.end()

    asyncio.run(run())


def test_downlink_loop_closes_websocket_when_modem_call_ends() -> None:
    async def run() -> None:
        modem = FakeModem()
        bridge = FakeAudioBridge()
        call = LanDialerCall(
            modem,  # type: ignore[arg-type]
            settings=LanDialerSettings(connect_timeout_seconds=1.0),
            bridge_factory=lambda **_kw: bridge,
        )
        begin_task = asyncio.create_task(call.begin("10000"))
        await asyncio.sleep(0.05)
        modem.trigger_call_connected("10000")
        await begin_task

        modem.connected_flag.clear()
        ws = FakeWs()
        await _send_downlink(ws, call)  # type: ignore[arg-type]

        assert ws.json_messages == [{"type": "status", "status": "ended"}]
        assert ws.closed is True

        call.end()

    asyncio.run(run())


def test_controller_rejects_second_call_while_active() -> None:
    async def run() -> None:
        modem = FakeModem()
        controller = LanDialerController(
            modem,  # type: ignore[arg-type]
            settings=LanDialerSettings(connect_timeout_seconds=1.0),
            bridge_factory=lambda **_kw: FakeAudioBridge(),
        )

        first = await controller.reserve_call()
        with pytest.raises(LanDialerBusyError, match="已有通话"):
            await controller.reserve_call()
        controller.release_call(first)

        second = await controller.reserve_call()
        assert second is not first
        controller.release_call(second)

    asyncio.run(run())


def test_lan_dialer_status_endpoint_requires_token() -> None:
    async def run() -> None:
        controller = LanDialerController(
            FakeModem(),  # type: ignore[arg-type]
            settings=LanDialerSettings(),
            bridge_factory=lambda **_kw: FakeAudioBridge(),
        )
        app = build_lan_dialer_app(controller, token="secret")
        async with TestClient(TestServer(app)) as client:
            denied = await client.get("/api/status")
            assert denied.status == 401

            allowed = await client.get("/api/status?token=secret")
            assert allowed.status == 200
            assert await allowed.json() == {"ok": True, "active": False}

    asyncio.run(run())
