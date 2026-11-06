"""Route-aware gathering and cancellation contracts, independent of real networks."""

import asyncio
import socket
import time
from types import SimpleNamespace

from agentcall import webrtc_ice


def test_ice_completed_summary_survives_dependency_log_suppression(monkeypatch, caplog):
    import logging

    from aiortc import RTCPeerConnection

    async def scenario():
        pc = RTCPeerConnection()
        pc.addTransceiver("audio")
        marks = []
        try:
            webrtc_ice.observe_setup(pc, marks.append)
            transport = pc.getTransceivers()[0].receiver.transport.transport
            transport._RTCIceTransport__state = "completed"
            transport.emit("statechange")
            assert "ice_completed" in marks
        finally:
            await pc.close()

    for name in ("aioice", "aiortc"):
        monkeypatch.setattr(logging.getLogger(name), "level", logging.WARNING)
    with caplog.at_level("INFO", logger="agentcall.webrtc_ice"):
        asyncio.run(scenario())
    assert "Direct ICE completed peer=" in caplog.text


def test_candidate_timings_distinguish_ready_relay_from_stalled_stun(monkeypatch, caplog):
    from aioice import Candidate, Connection

    async def scenario():
        async def relay(**_):
            await asyncio.sleep(0.005)
            return Candidate(foundation="relay", component=1, transport="udp", priority=1,
                             host="127.0.0.1", port=12345, type="relay"), None

        async def stalled(*_):
            await asyncio.Event().wait()

        monkeypatch.setattr(webrtc_ice, "GATHER_MAX_SECONDS", 0.05)
        monkeypatch.setattr(webrtc_ice, "physical_host_addresses", lambda: ["127.0.0.1"])
        monkeypatch.setattr(webrtc_ice, "default_stun_sources", lambda _: {"127.0.0.1": ("127.0.0.1", 9)})
        monkeypatch.setattr(webrtc_ice.ice, "relayed_candidate", relay)
        monkeypatch.setattr(webrtc_ice, "_server_reflexive", stalled)
        connection = Connection(ice_controlling=False, stun_server=("unused", 9), turn_server=("unused", 9),
                                turn_username="private-user", turn_password="private-secret")
        webrtc_ice.configure_connection(connection)
        try:
            await connection.gather_candidates()
            assert {c.type for c in connection.local_candidates} == {"host", "relay"}
        finally:
            await connection.close()

    with caplog.at_level("INFO", logger="agentcall.webrtc_ice"):
        asyncio.run(scenario())
    assert "type=host outcome=ready" in caplog.text
    assert "type=relay outcome=ready" in caplog.text
    assert "type=srflx outcome=cancelled" in caplog.text
    assert "total_ms=" in caplog.text and "ready_ms=" in caplog.text
    assert "private-user" not in caplog.text and "private-secret" not in caplog.text


def adapter(name, *addresses):
    return SimpleNamespace(name=name, nice_name=name, ips=[SimpleNamespace(ip=ip) for ip in addresses])


def test_only_physical_routable_host_addresses():
    interfaces = [adapter("en0", "192.168.1.2", ("240e::1", 0, 0), ("fe80::1", 0, 1)),
                  adapter("utun12", "100.100.1.1"), adapter("ipsec0", "10.0.0.3"),
                  adapter("en1", "169.254.1.1", "100.64.0.1"), adapter("lo0", "127.0.0.1"),
                  adapter("bridge0", "192.168.2.1"), adapter("Tailscale", ("fd7a::1", 0, 0))]
    # VPN/Tailscale only by interface name; CGNAT ranges stay (cloud VMs and some LANs use them).
    assert webrtc_ice.physical_host_addresses(interfaces) == ["192.168.1.2", "240e::1", "100.64.0.1"]


def test_container_and_vm_bridges_are_not_host_candidates():
    interfaces = [adapter("wlan0", "192.168.9.219"), adapter("docker0", "172.17.0.1"),
                  adapter("br-agentnet", "172.18.0.1"), adapter("veth3e7c5fc", ("fe80::5c", 0, 9)),
                  adapter("virbr0", "192.168.122.1"), adapter("vmnet8", "192.168.56.1"),
                  adapter("ztabcdef12", "10.147.17.5"), adapter("br0", "192.168.1.20")]
    # A plain host bridge (br0) usually carries the real LAN address and stays.
    assert webrtc_ice.physical_host_addresses(interfaces) == ["192.168.9.219", "192.168.1.20"]


def test_windows_virtual_adapters_are_matched_by_description():
    # ifaddr on Windows: name = adapter GUID, nice_name = hardware Description.
    def win(description, address):
        return SimpleNamespace(name="{6F1E2A3B-0000-4C5D-8E9F-000000000001}", nice_name=description,
                               ips=[SimpleNamespace(ip=address)])
    interfaces = [win("Intel(R) Wi-Fi 6 AX201 160MHz", "192.168.1.12"),
                  win("Hyper-V Virtual Ethernet Adapter", "172.22.0.1"),
                  win("VirtualBox Host-Only Ethernet Adapter", "192.168.56.1"),
                  win("VMware Virtual Ethernet Adapter for VMnet8", "192.168.75.1")]
    assert webrtc_ice.physical_host_addresses(interfaces) == ["192.168.1.12"]


def test_no_physical_interface_falls_back_to_every_routable_address(caplog):
    interfaces = [adapter("utun3", "10.8.0.2", ("fd00::2", 0, 0)), adapter("lo0", "127.0.0.1"),
                  adapter("awdl0", ("fe80::9", 0, 7))]
    with caplog.at_level("WARNING"):
        assert webrtc_ice.physical_host_addresses(interfaces) == ["10.8.0.2", "fd00::2"]
    assert any("no physical interface" in record.getMessage() for record in caplog.records)


def test_default_source_uses_kernel_socket_route_and_closes(monkeypatch):
    calls = []

    class Socket:
        def __init__(self, family, kind):
            calls.append((family, kind))

        def __enter__(self):
            return self

        def __exit__(self, *_):
            calls.append("closed")

        def connect(self, target):
            calls.append(target)

        def getsockname(self):
            return ("10.254.137.231", 40000)

    monkeypatch.setattr(socket, "getaddrinfo", lambda *_: [(socket.AF_INET, 2, 17, "", ("1.1.1.1", 3478))])
    monkeypatch.setattr(socket, "socket", Socket)
    assert webrtc_ice.default_stun_sources(("stun.example", 3478)) == {"10.254.137.231": ("1.1.1.1", 3478)}
    assert calls == [(socket.AF_INET, socket.SOCK_DGRAM), ("1.1.1.1", 3478), "closed"]


def test_missing_ipv6_route_does_not_generate_stun_source(monkeypatch):
    class Socket:
        def __init__(self, *_):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def connect(self, _):
            raise OSError("No route")

    monkeypatch.setattr(socket, "getaddrinfo", lambda *_: [(socket.AF_INET6, 2, 17, "", ("2606::1", 3478, 0, 0))])
    monkeypatch.setattr(socket, "socket", Socket)
    assert webrtc_ice.default_stun_sources(("stun.example", 3478)) == {}


def test_dead_stun_is_bounded_and_cancelled_before_return(monkeypatch):
    from aioice import Connection

    async def scenario():
        entered, cancelled = asyncio.Event(), asyncio.Event()

        async def dead_stun(*_):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        monkeypatch.setattr(webrtc_ice, "GATHER_MAX_SECONDS", 0.05)
        monkeypatch.setattr(webrtc_ice, "physical_host_addresses", lambda *_: ["127.0.0.1"])
        monkeypatch.setattr(webrtc_ice, "default_stun_sources", lambda *_: {"127.0.0.1": ("127.0.0.1", 9)})
        monkeypatch.setattr(webrtc_ice, "_server_reflexive", dead_stun)
        connection = Connection(ice_controlling=False, stun_server=("unused", 9))
        assert webrtc_ice.configure_connection(connection)
        start = time.monotonic()
        try:
            await asyncio.wait_for(connection.gather_candidates(), 5)
            assert entered.is_set() and cancelled.is_set()
            assert connection.local_candidates[0].type == "host"
            assert time.monotonic() - start < 3  # CI liveness bound, not a routing policy.
        finally:
            await webrtc_ice.stop_connection_checks(connection)
            await connection.close()

    asyncio.run(scenario())


def _srflx_candidate(local):
    from aioice import Candidate

    return Candidate(foundation="srflx", component=local.component, transport="udp", priority=2,
                     host="203.0.113.9", port=45678, type="srflx",
                     related_address=local.host, related_port=local.port)


def test_skip_local_relay_ends_gather_with_peer_relay_and_srflx_ready(monkeypatch, caplog):
    from aioice import Candidate, Connection

    relay_cancelled = asyncio.Event()

    async def stalled_relay(**_):
        try:
            await asyncio.Event().wait()
        finally:
            relay_cancelled.set()

    async def reflexive(protocol, target):
        await asyncio.sleep(0.01)
        return _srflx_candidate(protocol.local_candidate), None

    async def scenario():
        monkeypatch.setenv("DIRECT_GATHER_SKIP_LOCAL_RELAY_ENABLED", "true")
        monkeypatch.setattr(webrtc_ice, "GATHER_MAX_SECONDS", 0.5)
        monkeypatch.setattr(webrtc_ice, "physical_host_addresses", lambda: ["127.0.0.1"])
        monkeypatch.setattr(webrtc_ice, "default_stun_sources", lambda _: {"127.0.0.1": ("127.0.0.1", 9)})
        monkeypatch.setattr(webrtc_ice.ice, "relayed_candidate", stalled_relay)
        monkeypatch.setattr(webrtc_ice, "_server_reflexive", reflexive)
        connection = Connection(ice_controlling=False, stun_server=("unused", 9), turn_server=("unused", 9),
                                turn_username="private-user", turn_password="private-secret")
        webrtc_ice.configure_connection(connection)
        try:
            await connection.add_remote_candidate(Candidate(
                foundation="relay", component=1, transport="udp", priority=1,
                host="104.30.0.1", port=3478, type="relay"))
            started = time.monotonic()
            await connection.gather_candidates()
            assert time.monotonic() - started < 0.25
            assert {c.type for c in connection.local_candidates} == {"host", "srflx"}
            assert relay_cancelled.is_set()
        finally:
            await connection.close()

    with caplog.at_level("INFO", logger="agentcall.webrtc_ice"):
        asyncio.run(scenario())
    assert "reason=skip_local_relay" in caplog.text
    assert "private-user" not in caplog.text and "private-secret" not in caplog.text


def test_skip_local_relay_waits_budget_without_remote_relay(monkeypatch, caplog):
    from aioice import Candidate, Connection

    relay_cancelled = asyncio.Event()

    async def stalled_relay(**_):
        try:
            await asyncio.Event().wait()
        finally:
            relay_cancelled.set()

    async def reflexive(protocol, target):
        await asyncio.sleep(0.01)
        return _srflx_candidate(protocol.local_candidate), None

    async def scenario():
        monkeypatch.setenv("DIRECT_GATHER_SKIP_LOCAL_RELAY_ENABLED", "true")
        monkeypatch.setattr(webrtc_ice, "GATHER_MAX_SECONDS", 0.05)
        monkeypatch.setattr(webrtc_ice, "physical_host_addresses", lambda: ["127.0.0.1"])
        monkeypatch.setattr(webrtc_ice, "default_stun_sources", lambda _: {"127.0.0.1": ("127.0.0.1", 9)})
        monkeypatch.setattr(webrtc_ice.ice, "relayed_candidate", stalled_relay)
        monkeypatch.setattr(webrtc_ice, "_server_reflexive", reflexive)
        connection = Connection(ice_controlling=False, stun_server=("unused", 9), turn_server=("unused", 9),
                                turn_username="private-user", turn_password="private-secret")
        webrtc_ice.configure_connection(connection)
        try:
            await connection.add_remote_candidate(Candidate(
                foundation="host", component=1, transport="udp", priority=1,
                host="192.0.2.1", port=9, type="host"))
            started = time.monotonic()
            await connection.gather_candidates()
            assert time.monotonic() - started >= 0.04
            assert relay_cancelled.is_set()
            assert "relay" not in {c.type for c in connection.local_candidates}
        finally:
            await connection.close()

    with caplog.at_level("INFO", logger="agentcall.webrtc_ice"):
        asyncio.run(scenario())
    assert "reason=budget" in caplog.text


def test_skip_local_relay_requires_local_srflx_ready(monkeypatch, caplog):
    from aioice import Candidate, Connection

    relay_cancelled = asyncio.Event()
    srflx_cancelled = asyncio.Event()

    async def stalled_relay(**_):
        try:
            await asyncio.Event().wait()
        finally:
            relay_cancelled.set()

    async def stalled_srflx(*_):
        try:
            await asyncio.Event().wait()
        finally:
            srflx_cancelled.set()

    async def scenario():
        monkeypatch.setenv("DIRECT_GATHER_SKIP_LOCAL_RELAY_ENABLED", "true")
        monkeypatch.setattr(webrtc_ice, "GATHER_MAX_SECONDS", 0.05)
        monkeypatch.setattr(webrtc_ice, "physical_host_addresses", lambda: ["127.0.0.1"])
        monkeypatch.setattr(webrtc_ice, "default_stun_sources", lambda _: {"127.0.0.1": ("127.0.0.1", 9)})
        monkeypatch.setattr(webrtc_ice.ice, "relayed_candidate", stalled_relay)
        monkeypatch.setattr(webrtc_ice, "_server_reflexive", stalled_srflx)
        connection = Connection(ice_controlling=False, stun_server=("unused", 9), turn_server=("unused", 9),
                                turn_username="private-user", turn_password="private-secret")
        webrtc_ice.configure_connection(connection)
        try:
            await connection.add_remote_candidate(Candidate(
                foundation="relay", component=1, transport="udp", priority=1,
                host="104.30.0.1", port=3478, type="relay"))
            started = time.monotonic()
            await connection.gather_candidates()
            assert time.monotonic() - started >= 0.04
            assert srflx_cancelled.is_set() and relay_cancelled.is_set()
        finally:
            await connection.close()

    with caplog.at_level("INFO", logger="agentcall.webrtc_ice"):
        asyncio.run(scenario())
    assert "reason=budget" in caplog.text


def test_skip_local_relay_flag_off_keeps_budget_behavior(monkeypatch, caplog):
    from aioice import Candidate, Connection

    relay_cancelled = asyncio.Event()

    async def stalled_relay(**_):
        try:
            await asyncio.Event().wait()
        finally:
            relay_cancelled.set()

    async def reflexive(protocol, target):
        await asyncio.sleep(0.01)
        return _srflx_candidate(protocol.local_candidate), None

    async def scenario():
        monkeypatch.setenv("DIRECT_GATHER_SKIP_LOCAL_RELAY_ENABLED", "false")
        monkeypatch.setattr(webrtc_ice, "GATHER_MAX_SECONDS", 0.05)
        monkeypatch.setattr(webrtc_ice, "physical_host_addresses", lambda: ["127.0.0.1"])
        monkeypatch.setattr(webrtc_ice, "default_stun_sources", lambda _: {"127.0.0.1": ("127.0.0.1", 9)})
        monkeypatch.setattr(webrtc_ice.ice, "relayed_candidate", stalled_relay)
        monkeypatch.setattr(webrtc_ice, "_server_reflexive", reflexive)
        connection = Connection(ice_controlling=False, stun_server=("unused", 9), turn_server=("unused", 9),
                                turn_username="private-user", turn_password="private-secret")
        webrtc_ice.configure_connection(connection)
        try:
            await connection.add_remote_candidate(Candidate(
                foundation="relay", component=1, transport="udp", priority=1,
                host="104.30.0.1", port=3478, type="relay"))
            started = time.monotonic()
            await connection.gather_candidates()
            assert time.monotonic() - started >= 0.04
            assert relay_cancelled.is_set()
            assert "relay" not in {c.type for c in connection.local_candidates}
        finally:
            await connection.close()

    with caplog.at_level("INFO", logger="agentcall.webrtc_ice"):
        asyncio.run(scenario())
    assert "reason=budget" in caplog.text


def test_have_srflx_and_relay_ends_gather_early_and_keeps_completed_relay(monkeypatch, caplog):
    from aioice import Candidate, Connection

    relay_ready = asyncio.Event()
    stalled_srflx_cancelled = asyncio.Event()

    async def relay(**_):
        await asyncio.sleep(0.03)
        relay_ready.set()
        return Candidate(foundation="relay", component=1, transport="udp", priority=1,
                         host="127.0.0.1", port=12345, type="relay"), None

    async def reflexive(protocol, target):
        if target == ("::1", 9):
            try:
                await asyncio.Event().wait()
            finally:
                stalled_srflx_cancelled.set()
        await asyncio.sleep(0.005)
        return _srflx_candidate(protocol.local_candidate), None

    async def scenario():
        monkeypatch.setenv("DIRECT_GATHER_SKIP_LOCAL_RELAY_ENABLED", "false")
        monkeypatch.setattr(webrtc_ice, "GATHER_MAX_SECONDS", 0.5)
        monkeypatch.setattr(webrtc_ice, "physical_host_addresses", lambda: ["127.0.0.1"])
        monkeypatch.setattr(webrtc_ice, "default_stun_sources",
                            lambda _: {"127.0.0.1": ("127.0.0.1", 9), "::1": ("::1", 9)})
        monkeypatch.setattr(webrtc_ice.ice, "relayed_candidate", relay)
        monkeypatch.setattr(webrtc_ice, "_server_reflexive", reflexive)
        connection = Connection(ice_controlling=False, stun_server=("unused", 9), turn_server=("unused", 9),
                                turn_username="private-user", turn_password="private-secret")
        webrtc_ice.configure_connection(connection)
        try:
            await connection.add_remote_candidate(Candidate(
                foundation="host", component=1, transport="udp", priority=1,
                host="192.0.2.1", port=9, type="host"))
            started = time.monotonic()
            await connection.gather_candidates()
            assert time.monotonic() - started < 0.25
            local_types = {c.type for c in connection.local_candidates}
            assert {"srflx", "relay"} <= local_types
            assert relay_ready.is_set()
            assert stalled_srflx_cancelled.is_set()
        finally:
            await connection.close()

    with caplog.at_level("INFO", logger="agentcall.webrtc_ice"):
        asyncio.run(scenario())
    assert "reason=have_srflx_and_relay" in caplog.text
    assert "private-user" not in caplog.text and "private-secret" not in caplog.text


def test_have_srflx_and_relay_waits_budget_when_relay_never_ready(monkeypatch, caplog):
    from aioice import Candidate, Connection

    relay_cancelled = asyncio.Event()

    async def stalled_relay(**_):
        try:
            await asyncio.Event().wait()
        finally:
            relay_cancelled.set()

    async def reflexive(protocol, target):
        await asyncio.sleep(0.01)
        return _srflx_candidate(protocol.local_candidate), None

    async def scenario():
        monkeypatch.setenv("DIRECT_GATHER_SKIP_LOCAL_RELAY_ENABLED", "false")
        monkeypatch.setattr(webrtc_ice, "GATHER_MAX_SECONDS", 0.05)
        monkeypatch.setattr(webrtc_ice, "physical_host_addresses", lambda: ["127.0.0.1"])
        monkeypatch.setattr(webrtc_ice, "default_stun_sources", lambda _: {"127.0.0.1": ("127.0.0.1", 9)})
        monkeypatch.setattr(webrtc_ice.ice, "relayed_candidate", stalled_relay)
        monkeypatch.setattr(webrtc_ice, "_server_reflexive", reflexive)
        connection = Connection(ice_controlling=False, stun_server=("unused", 9), turn_server=("unused", 9),
                                turn_username="private-user", turn_password="private-secret")
        webrtc_ice.configure_connection(connection)
        try:
            await connection.add_remote_candidate(Candidate(
                foundation="relay", component=1, transport="udp", priority=1,
                host="104.30.0.1", port=3478, type="relay"))
            started = time.monotonic()
            await connection.gather_candidates()
            assert time.monotonic() - started >= 0.04
            assert relay_cancelled.is_set()
            assert "relay" not in {c.type for c in connection.local_candidates}
        finally:
            await connection.close()

    with caplog.at_level("INFO", logger="agentcall.webrtc_ice"):
        asyncio.run(scenario())
    assert "reason=budget" in caplog.text
    assert "reason=have_srflx_and_relay" not in caplog.text


def test_gather_log_reports_remote_counts_without_addresses(monkeypatch, caplog):
    from aioice import Candidate, Connection

    async def relay(**_):
        await asyncio.sleep(0.02)
        return Candidate(foundation="relay", component=1, transport="udp", priority=1,
                         host="127.0.0.1", port=12345, type="relay"), None

    async def reflexive(protocol, target):
        await asyncio.sleep(0.005)
        return _srflx_candidate(protocol.local_candidate), None

    async def scenario():
        monkeypatch.setenv("DIRECT_GATHER_SKIP_LOCAL_RELAY_ENABLED", "false")
        monkeypatch.setattr(webrtc_ice, "GATHER_MAX_SECONDS", 0.5)
        monkeypatch.setattr(webrtc_ice, "physical_host_addresses", lambda: ["127.0.0.1"])
        monkeypatch.setattr(webrtc_ice, "default_stun_sources", lambda _: {"127.0.0.1": ("127.0.0.1", 9)})
        monkeypatch.setattr(webrtc_ice.ice, "relayed_candidate", relay)
        monkeypatch.setattr(webrtc_ice, "_server_reflexive", reflexive)
        connection = Connection(ice_controlling=False, stun_server=("unused", 9), turn_server=("unused", 9),
                                turn_username="private-user", turn_password="private-secret")
        webrtc_ice.configure_connection(connection)
        try:
            for kind, host, port in (("host", "198.51.100.1", 40001),
                                     ("srflx", "198.51.100.2", 40002),
                                     ("relay", "198.51.100.3", 40003)):
                await connection.add_remote_candidate(Candidate(
                    foundation=kind, component=1, transport="udp", priority=1,
                    host=host, port=port, type=kind))
            await connection.gather_candidates()
        finally:
            await connection.close()

    with caplog.at_level("INFO", logger="agentcall.webrtc_ice"):
        asyncio.run(scenario())
    assert "remote=host:1,srflx:1,relay:1" in caplog.text
    for address in ("198.51.100.1", "198.51.100.2", "198.51.100.3"):
        assert address not in caplog.text


def test_gather_wait_loop_does_not_busy_spin_when_relay_pends(monkeypatch, caplog):
    import asyncio as _asyncio

    from aioice import Candidate, Connection

    real_wait = _asyncio.wait
    wait_calls = 0

    async def counting_wait(fs, *args, **kwargs):
        nonlocal wait_calls
        wait_calls += 1
        return await real_wait(fs, *args, **kwargs)

    relay_cancelled = asyncio.Event()

    async def stalled_relay(**_):
        try:
            await asyncio.Event().wait()
        finally:
            relay_cancelled.set()

    async def reflexive(protocol, target):
        await asyncio.sleep(0.01)
        return _srflx_candidate(protocol.local_candidate), None

    async def scenario():
        monkeypatch.setenv("DIRECT_GATHER_SKIP_LOCAL_RELAY_ENABLED", "true")
        monkeypatch.setattr(webrtc_ice, "GATHER_MAX_SECONDS", 0.05)
        monkeypatch.setattr(webrtc_ice, "physical_host_addresses", lambda: ["127.0.0.1"])
        monkeypatch.setattr(webrtc_ice, "default_stun_sources", lambda _: {"127.0.0.1": ("127.0.0.1", 9)})
        monkeypatch.setattr(webrtc_ice.ice, "relayed_candidate", stalled_relay)
        monkeypatch.setattr(webrtc_ice, "_server_reflexive", reflexive)
        monkeypatch.setattr(_asyncio, "wait", counting_wait)
        connection = Connection(ice_controlling=False, stun_server=("unused", 9), turn_server=("unused", 9),
                                turn_username="private-user", turn_password="private-secret")
        webrtc_ice.configure_connection(connection)
        try:
            await connection.add_remote_candidate(Candidate(
                foundation="host", component=1, transport="udp", priority=1,
                host="192.0.2.1", port=9, type="host"))
            await connection.gather_candidates()
            assert relay_cancelled.is_set()
        finally:
            await connection.close()

    with caplog.at_level("INFO", logger="agentcall.webrtc_ice"):
        asyncio.run(scenario())
    assert wait_calls < 10
    assert "reason=budget" in caplog.text


def test_endpoint_produces_answer_while_stun_never_replies(monkeypatch):
    from aiortc import RTCConfiguration, RTCPeerConnection

    from agentcall.webrtc_media import WebRtcRemoteMediaEndpoint

    async def scenario():
        cancelled = asyncio.Event()

        async def dead_stun(*_):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        monkeypatch.setattr(webrtc_ice, "GATHER_MAX_SECONDS", 0.05)
        monkeypatch.setattr(webrtc_ice, "physical_host_addresses", lambda: ["127.0.0.1"])
        monkeypatch.setattr(webrtc_ice, "default_stun_sources", lambda _: {"127.0.0.1": ("127.0.0.1", 9)})
        monkeypatch.setattr(webrtc_ice, "_server_reflexive", dead_stun)
        phone = RTCPeerConnection(RTCConfiguration(iceServers=[]))
        phone.addTransceiver("audio")
        phone.createDataChannel("callpilot.control")
        await phone.setLocalDescription(await phone.createOffer())
        endpoint = WebRtcRemoteMediaEndpoint({"type": "offer", "sdp": phone.localDescription.sdp},
                                            [{"urls": ["stun:unused:9"]}], call_id="stun-blackhole")
        try:
            await asyncio.wait_for(endpoint.connect(), 5)
            assert cancelled.is_set()
            assert endpoint.answer and "typ host" in endpoint.answer["sdp"]
        finally:
            await endpoint.close()
            await phone.close()

    asyncio.run(scenario())


def test_dns_stall_keeps_already_bound_host_candidate(monkeypatch):
    import threading

    from aioice import Connection

    async def scenario():
        entered, release = threading.Event(), threading.Event()

        def blocked_dns(_):
            entered.set()
            assert release.wait(5)
            return {}

        monkeypatch.setattr(webrtc_ice, "GATHER_MAX_SECONDS", 0.05)
        monkeypatch.setattr(webrtc_ice, "physical_host_addresses", lambda: ["127.0.0.1"])
        monkeypatch.setattr(webrtc_ice, "default_stun_sources", blocked_dns)
        connection = Connection(ice_controlling=False, stun_server=("unused", 9))
        webrtc_ice.configure_connection(connection)
        try:
            await asyncio.wait_for(connection.gather_candidates(), 5)
            assert entered.is_set()
            assert [c.type for c in connection.local_candidates] == ["host"]
        finally:
            release.set()
            await connection.close()

    asyncio.run(scenario())


def test_unsupported_aioice_falls_back_without_patching(monkeypatch, caplog):
    from aioice import Connection

    async def scenario():
        connection = Connection(ice_controlling=False)
        original = connection.get_component_candidates
        original_consent = connection.query_consent
        monkeypatch.setattr(webrtc_ice.aioice, "__version__", "99.0.0")
        assert not webrtc_ice.configure_connection(connection)
        assert connection.get_component_candidates == original
        assert connection.query_consent == original_consent
        assert "unsupported" in caplog.text
        await connection.close()

    asyncio.run(scenario())


def test_only_default_source_sends_stun_not_other_host_interfaces(monkeypatch):
    from aioice import Connection

    async def scenario():
        calls = []

        async def reflexive(protocol, target):
            calls.append((protocol.local_candidate.host, target))
            return protocol.local_candidate, None

        monkeypatch.setattr(webrtc_ice, "physical_host_addresses", lambda: ["127.0.0.1", "::1"])
        monkeypatch.setattr(webrtc_ice, "default_stun_sources", lambda _: {"127.0.0.1": ("127.0.0.1", 9)})
        monkeypatch.setattr(webrtc_ice, "_server_reflexive", reflexive)
        connection = Connection(ice_controlling=False, stun_server=("unused", 9))
        webrtc_ice.configure_connection(connection)
        try:
            await connection.gather_candidates()
            assert calls == [("127.0.0.1", ("127.0.0.1", 9))]
        finally:
            await connection.close()

    asyncio.run(scenario())


def test_cancel_inflight_check_drains_transaction_before_socket_close(monkeypatch):
    from aioice import Candidate, Connection

    async def scenario():
        received = asyncio.Event()
        errors = []
        loop = asyncio.get_running_loop()
        loop.set_exception_handler(lambda _loop, context: errors.append(context))

        class Blackhole(asyncio.DatagramProtocol):
            def datagram_received(self, *_):
                received.set()

        transport, _ = await loop.create_datagram_endpoint(Blackhole, local_addr=("127.0.0.1", 0))
        connection = Connection(ice_controlling=True)
        monkeypatch.setattr(webrtc_ice, "physical_host_addresses", lambda: ["127.0.0.1"])
        webrtc_ice.configure_connection(connection)
        connection.remote_username, connection.remote_password = "peer", "password"
        await connection.gather_candidates()
        await connection.add_remote_candidate(Candidate(
            foundation="test", component=1, transport="udp", priority=1,
            host="127.0.0.1", port=transport.get_extra_info("sockname")[1], type="host",
        ))
        await connection.add_remote_candidate(None)
        connecting = asyncio.create_task(connection.connect())
        try:
            await asyncio.wait_for(received.wait(), 5)
            protocols = list(connection._protocols)
            assert any(p.transactions for p in protocols)
            transactions = [t for p in protocols for t in p.transactions.values()]
            timers = [t._Transaction__timeout_handle for t in transactions]
            futures = [t._Transaction__future for t in transactions]
            await webrtc_ice.stop_connection_checks(connection)
            assert all(not p.transactions for p in protocols)
            assert all(timer.cancelled() for timer in timers)
            assert all(future.done() for future in futures)
            await connection.close()
            await asyncio.gather(connecting, return_exceptions=True)
            assert not errors
        finally:
            connecting.cancel()
            await asyncio.gather(connecting, return_exceptions=True)
            await connection.close()
            transport.close()

    asyncio.run(scenario())


def test_unknown_transaction_layout_warns_without_mutation(caplog):
    transaction = SimpleNamespace()
    asyncio.run(webrtc_ice._drain_protocol_transactions([SimpleNamespace(transactions={b"id": transaction})]))
    assert "transaction adapter unsupported" in caplog.text


def test_allocated_turn_disconnect_drains_bind_refresh_and_bounded_delete(monkeypatch):
    from aioice import Connection, ice, stun, turn

    async def scenario():
        bind_seen, refresh_seen = asyncio.Event(), asyncio.Event()
        errors = []
        loop = asyncio.get_running_loop()
        loop.set_exception_handler(lambda _loop, context: errors.append(context))

        class TurnServer(asyncio.DatagramProtocol):
            def connection_made(self, transport):
                self.transport = transport

            def datagram_received(self, data, address):
                message = stun.parse_message(data)
                if message.message_method == stun.Method.ALLOCATE:
                    response = stun.Message(message_method=stun.Method.ALLOCATE, message_class=stun.Class.RESPONSE,
                                            transaction_id=message.transaction_id)
                    response.attributes.update({"LIFETIME": 600, "XOR-RELAYED-ADDRESS": ("127.0.0.1", 45678),
                                                "XOR-MAPPED-ADDRESS": address})
                    self.transport.sendto(bytes(response), address)
                elif message.message_method == stun.Method.CHANNEL_BIND:
                    bind_seen.set()
                elif message.message_method == stun.Method.REFRESH:
                    refresh_seen.set()

        server, _ = await loop.create_datagram_endpoint(TurnServer, local_addr=("127.0.0.1", 0))
        connection = Connection(ice_controlling=False)
        relay, protocol = await turn.create_turn_endpoint(
            lambda: ice.StunProtocol(connection), server.get_extra_info("sockname"), username=None, password=None,
        )
        connection._protocols.append(protocol)
        webrtc_ice.configure_turn_transport(relay)
        monkeypatch.setattr(webrtc_ice, "TURN_DELETE_MAX_SECONDS", 0.05)
        inner = relay._TurnTransport__inner_protocol
        inner.refresh_task.cancel()
        await asyncio.gather(inner.refresh_task, return_exceptions=True)
        inner.refresh_task = asyncio.create_task(inner.refresh(0))
        relay.sendto(b"media", ("127.0.0.1", 45679))
        try:
            await asyncio.wait_for(bind_seen.wait(), 5)
            await asyncio.wait_for(refresh_seen.wait(), 5)
            assert len(inner.transactions) == 2
            await webrtc_ice.stop_connection_checks(connection)
            await asyncio.wait_for(connection.close(), 5)
            assert not inner.transactions and not relay._TurnTransport__send_tasks
            assert inner.refresh_task is None
            assert not errors
        finally:
            await connection.close()
            server.close()

    asyncio.run(scenario())


def test_turn_cleanup_closes_socket_even_if_cancelled_while_draining():
    from aioice import turn

    async def scenario():
        started, draining, closed = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def sender():
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                draining.set()
                await asyncio.Event().wait()

        inner = SimpleNamespace(refresh_task=None, transport=SimpleNamespace(close=closed.set))
        relay = turn.TurnTransport(inner)
        task = asyncio.create_task(sender())
        relay._TurnTransport__send_tasks.add(task)
        await started.wait()
        webrtc_ice.configure_turn_transport(relay)
        relay.close()
        await asyncio.wait_for(draining.wait(), 5)
        closing = relay._TurnTransport__close_task
        closing.cancel()
        await asyncio.gather(closing, return_exceptions=True)
        assert closed.is_set() and task.done()

    asyncio.run(scenario())


def test_turn_delete_socket_error_is_expected_cleanup():
    from aioice import turn

    async def scenario():
        closed = asyncio.Event()

        async def delete():
            raise OSError("network gone")

        inner = SimpleNamespace(refresh_task=None, transport=SimpleNamespace(close=closed.set), delete=delete)
        relay = turn.TurnTransport(inner)
        webrtc_ice.configure_turn_transport(relay)
        relay.close()
        await relay._TurnTransport__close_task
        assert closed.is_set()

    asyncio.run(scenario())


def test_orphaned_upstream_srflx_transaction_is_drained_before_socket_close(monkeypatch):
    """Upstream gather cancellation leaves its separately created STUN task alive."""
    from aioice import Connection, ice

    async def scenario():
        received = asyncio.Event()
        errors = []
        loop = asyncio.get_running_loop()
        loop.set_exception_handler(lambda _loop, context: errors.append(context))

        class Blackhole(asyncio.DatagramProtocol):
            def datagram_received(self, _data, _address):
                received.set()

        server, _ = await loop.create_datagram_endpoint(Blackhole, local_addr=("127.0.0.1", 0))
        connection = Connection(ice_controlling=False, stun_server=server.get_extra_info("sockname"))
        monkeypatch.setattr(ice, "get_host_addresses", lambda **_: ["127.0.0.1"])
        gathering = asyncio.create_task(connection.gather_candidates())
        orphan = None
        try:
            await asyncio.wait_for(received.wait(), 5)
            protocols = list(connection._protocols)
            transactions = [t for p in protocols for t in p.transactions.values()]
            assert len(transactions) == 1
            timer = transactions[0]._Transaction__timeout_handle
            future = transactions[0]._Transaction__future
            orphan = next(t for t in asyncio.all_tasks()
                          if t.get_coro().__qualname__ == "server_reflexive_candidate")
            gathering.cancel()
            await asyncio.gather(gathering, return_exceptions=True)
            assert not orphan.done() and not timer.cancelled()
            await webrtc_ice.stop_connection_checks(connection)
            assert orphan.done() and timer.cancelled() and future.done()
            assert all(not p.transactions for p in protocols)
            await connection.close()
            assert not errors
        finally:
            if orphan is not None:
                orphan.cancel()
                await asyncio.gather(orphan, return_exceptions=True)
            await connection.close()
            server.close()

    asyncio.run(scenario())


NOMINATION_DELAY = 0.8


def test_early_complete_hook_nominates_first_succeeded_pair_only_for_controlled(caplog):
    from aioice import ice

    pair = SimpleNamespace(state=ice.CandidatePair.State.SUCCEEDED, component=1, nominated=False,
                           local_addr=("192.168.1.12", 5000), remote_addr=("192.168.1.10", 5001))
    controlled = SimpleNamespace(ice_controlling=False, _agentcall_first_pair_success_at=None,
                                 _agentcall_early_nominated_components=set(), _agentcall_early_complete_logged=False,
                                 _agentcall_connect_started_at=1000.0)
    with caplog.at_level("INFO", logger="agentcall.webrtc_ice"):
        webrtc_ice._early_complete_hook(controlled, pair)
        assert pair.nominated and controlled._agentcall_early_nominated_components == {1}
        assert "first-pair-success -> DTLS" in caplog.text
        webrtc_ice._early_complete_hook(controlled, pair)
        assert controlled._agentcall_early_nominated_components == {1}

    controlling = SimpleNamespace(ice_controlling=True, _agentcall_first_pair_success_at=None,
                                  _agentcall_early_nominated_components=set(), _agentcall_early_complete_logged=False,
                                  _agentcall_connect_started_at=1000.0)
    other = SimpleNamespace(state=ice.CandidatePair.State.SUCCEEDED, component=1, nominated=False,
                            local_addr=("a", 1), remote_addr=("b", 2))
    webrtc_ice._early_complete_hook(controlling, other)
    assert not other.nominated and not controlling._agentcall_early_nominated_components


async def _run_controlled_pair(enabled: bool) -> float:
    import os

    from aioice import Candidate, Connection, ice

    os.environ["DIRECT_ICE_EARLY_COMPLETE_ENABLED"] = "true" if enabled else "false"
    webrtc_ice.physical_host_addresses = lambda: ["127.0.0.1"]
    ice.get_host_addresses = lambda **_: ["127.0.0.1"]

    class Blackhole(asyncio.DatagramProtocol):
        def datagram_received(self, _data, _addr):
            pass

    loop = asyncio.get_running_loop()
    blackhole, _ = await loop.create_datagram_endpoint(Blackhole, local_addr=("127.0.0.1", 0))
    blackhole_addr = blackhole.get_extra_info("sockname")
    controlled = Connection(ice_controlling=False)
    peer = Connection(ice_controlling=True)
    try:
        webrtc_ice.configure_connection(controlled)
        await controlled.gather_candidates()
        await peer.gather_candidates()
        controlled.remote_username = peer.local_username
        controlled.remote_password = peer.local_password
        peer.remote_username = controlled.local_username
        peer.remote_password = controlled.local_password
        await controlled.add_remote_candidate(peer.local_candidates[0])
        await peer.add_remote_candidate(controlled.local_candidates[0])
        for i in range(4):
            await controlled.add_remote_candidate(Candidate(
                foundation=f"blackhole-{i}", component=1, transport="udp", priority=1,
                host=blackhole_addr[0], port=blackhole_addr[1], type="host"))
        await controlled.add_remote_candidate(None)
        await peer.add_remote_candidate(None)

        original_check_start = peer.check_start

        async def delayed_check_start(pair):
            await asyncio.sleep(NOMINATION_DELAY)
            await original_check_start(pair)

        peer.check_start = delayed_check_start

        started = time.monotonic()
        await asyncio.wait_for(controlled.connect(), timeout=6)
        return time.monotonic() - started
    finally:
        for connection in (controlled, peer):
            try:
                await webrtc_ice.stop_connection_checks(connection)
            finally:
                await connection.close()
        blackhole.close()


def test_early_complete_flag_cuts_controlled_connect_wait(monkeypatch):
    off_elapsed = asyncio.run(_run_controlled_pair(enabled=False))
    on_elapsed = asyncio.run(_run_controlled_pair(enabled=True))
    assert on_elapsed < 0.4
    assert off_elapsed > 0.5
    assert on_elapsed < off_elapsed


def test_early_complete_real_aiortc_loopback_media_and_data_channel(monkeypatch):
    import math
    import os
    import struct

    from aioice import ice
    from aiortc import RTCConfiguration, RTCPeerConnection
    from aiortc.mediastreams import AudioStreamTrack

    os.environ["DIRECT_ICE_EARLY_COMPLETE_ENABLED"] = "true"
    monkeypatch.setattr(ice, "get_host_addresses", lambda **_: ["127.0.0.1"])
    monkeypatch.setattr(webrtc_ice, "physical_host_addresses", lambda: ["127.0.0.1"])

    class ToneTrack(AudioStreamTrack):
        async def recv(self):
            frame = await super().recv()
            pcm = struct.pack("<160h", *(int(6000 * math.sin(2 * math.pi * 440 * n / 8000)) for n in range(160)))
            frame.planes[0].update(pcm)
            return frame

    async def scenario():
        phone = RTCPeerConnection(RTCConfiguration(iceServers=[]))
        answer = RTCPeerConnection(RTCConfiguration(iceServers=[]))
        channel = phone.createDataChannel("control")
        phone.addTrack(ToneTrack())
        channel_open, frame_received = asyncio.Event(), asyncio.Event()
        frame_peak = []

        @channel.on("open")
        def on_open():
            channel_open.set()

        @answer.on("track")
        def on_track(track):
            async def pump():
                try:
                    frame = await asyncio.wait_for(track.recv(), 8)
                    frame_peak.append(abs(frame.to_ndarray()).max())
                    frame_received.set()
                except Exception as error:
                    raise AssertionError(f"remote audio frame timed out: {type(error).__name__}") from error

            asyncio.create_task(pump())

        try:
            await phone.setLocalDescription(await phone.createOffer())
            await answer.setRemoteDescription(phone.localDescription)
            webrtc_ice.configure_peer(answer)
            # Delay the controlling peer's USE-CANDIDATE so the controlled answerer
            # self-nominates on its first successful check, exercising the flag path.
            phone_connection = phone.getTransceivers()[0].receiver.transport.transport._connection
            original_check_start = phone_connection.check_start

            async def delayed_check_start(pair):
                await asyncio.sleep(0.3)
                await original_check_start(pair)

            phone_connection.check_start = delayed_check_start
            await answer.setLocalDescription(await answer.createAnswer())
            await phone.setRemoteDescription(answer.localDescription)
            answer_connection = answer.getTransceivers()[0].receiver.transport.transport._connection
            assert getattr(answer_connection, "_agentcall_early_complete_logged", False)
            await asyncio.wait_for(channel_open.wait(), 10)
            await asyncio.wait_for(frame_received.wait(), 10)
            assert frame_peak and frame_peak[0] > 500
            assert channel.readyState == "open"
        finally:
            await phone.close()
            await answer.close()

    asyncio.run(scenario())
