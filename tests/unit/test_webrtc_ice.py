"""Route-aware gathering and cancellation contracts, independent of real networks."""

import asyncio
import socket
import time
from types import SimpleNamespace

from agentcall import webrtc_ice


def adapter(name, *addresses):
    return SimpleNamespace(name=name, nice_name=name, ips=[SimpleNamespace(ip=ip) for ip in addresses])


def test_only_physical_routable_host_addresses():
    interfaces = [adapter("en0", "192.168.1.2", ("240e::1", 0, 0), ("fe80::1", 0, 1)),
                  adapter("utun12", "100.100.1.1"), adapter("ipsec0", "10.0.0.3"),
                  adapter("en1", "169.254.1.1", "100.64.0.1"), adapter("lo0", "127.0.0.1"),
                  adapter("bridge0", "192.168.2.1"), adapter("Tailscale", ("fd7a::1", 0, 0))]
    # VPN/Tailscale only by interface name; CGNAT ranges stay (cloud VMs and some LANs use them).
    assert webrtc_ice.physical_host_addresses(interfaces) == ["192.168.1.2", "240e::1", "100.64.0.1"]


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
        monkeypatch.setattr(webrtc_ice.aioice, "__version__", "99.0.0")
        assert not webrtc_ice.configure_connection(connection)
        assert connection.get_component_candidates == original
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
