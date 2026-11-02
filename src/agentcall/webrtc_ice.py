"""Instance-local aioice adapter. Never patches LiveKit or global aioice behavior.

The private adapter is audited against aioice 0.11.x; unknown layouts keep upstream
gathering. The cap bounds caller silence, not a choice between network paths.
"""

from __future__ import annotations

import asyncio
import inspect
import ipaddress
import logging
import socket
import time
from types import MethodType
from typing import Any

import aioice
import ifaddr
from aioice import ice, stun

logger = logging.getLogger(__name__)
GATHER_MAX_SECONDS = 1.5
TURN_DELETE_MAX_SECONDS = 0.25  # Local teardown cannot wait for a disappeared TURN server.
_VIRTUAL_PREFIXES = ("utun", "ipsec", "tun", "tap", "awdl", "llw", "bridge", "ppp", "wg", "gif", "stf", "lo",
                     "tailscale", "wireguard", "wintun")
_CGNAT = ipaddress.ip_network("100.64.0.0/10")


def physical_host_addresses(adapters: Any = None) -> list[str]:
    addresses: list[str] = []
    for adapter in ifaddr.get_adapters() if adapters is None else adapters:
        names = (str(adapter.name).lower(), str(adapter.nice_name).lower())
        if any(name.startswith(_VIRTUAL_PREFIXES) for name in names):
            continue
        for entry in adapter.ips:
            value = entry.ip if isinstance(entry.ip, str) else entry.ip[0]
            address = ipaddress.ip_address(value)
            if (address.is_loopback or address.is_link_local or address.is_unspecified or address.is_multicast
                    or (address.version == 4 and address in _CGNAT)):
                continue
            if value not in addresses:
                addresses.append(value)
    return addresses


def default_stun_sources(server: tuple[str, int]) -> dict[str, tuple[str, int]]:
    """UDP connect selects a route without sending a packet. Run DNS off-loop."""
    sources: dict[str, tuple[str, int]] = {}
    try:
        resolved = socket.getaddrinfo(server[0], server[1], socket.AF_UNSPEC, socket.SOCK_DGRAM)
    except OSError:
        return sources
    families: set[int] = set()
    for family, _, _, _, target in resolved:
        if family not in (socket.AF_INET, socket.AF_INET6) or family in families:
            continue
        try:
            with socket.socket(family, socket.SOCK_DGRAM) as probe:
                probe.connect(target)
                source = probe.getsockname()[0]
            address = ipaddress.ip_address(source)
            if address.is_unspecified or address.is_loopback or address.is_link_local:
                continue
            sources[source] = (str(target[0]), int(target[1]))
            families.add(family)
        except OSError:
            continue
    return sources


async def _server_reflexive(protocol: Any, target: tuple[str, int]) -> tuple[Any, None]:
    # Unlike aioice's IPv4-only helper, this accepts the numeric v4/v6 route target.
    request = stun.Message(message_method=stun.Method.BINDING, message_class=stun.Class.REQUEST)
    response, _ = await protocol.request(request, target)
    local = protocol.local_candidate
    host, port = response.attributes["XOR-MAPPED-ADDRESS"]
    return ice.Candidate(
        foundation=ice.candidate_foundation("srflx", "udp", local.host), component=local.component,
        transport="udp", priority=ice.candidate_priority(local.component, "srflx"),
        host=host, port=port, type="srflx", related_address=local.host, related_port=local.port,
    ), None


def _supported(connection: Any) -> bool:
    return (aioice.__version__.startswith("0.11.")
            and list(inspect.signature(ice.Connection.get_component_candidates).parameters)
            == ["self", "component", "addresses", "timeout"]
            and all(hasattr(connection, name) for name in (
                "_protocols", "_check_list", "_check_list_state", "_query_consent_task", "_transport_policy",
                "_use_ipv4", "_use_ipv6")))


def configure_connection(connection: Any) -> bool:
    if not _supported(connection):
        logger.warning("Direct ICE optimization unsupported aioice layout/version; using upstream gathering")
        return False
    if getattr(connection, "_agentcall_gathers", None) is None:
        connection._agentcall_gathers = set()
        connection._agentcall_stopping = False
        connection.get_component_candidates = MethodType(_gather_component, connection)
    return True


async def _gather_component(connection: Any, component: int, addresses: list[str], timeout: int = 5) -> list[Any]:
    del addresses, timeout  # Upstream's unfiltered interfaces and 5s timeout are not used.
    candidates: list[Any] = []
    jobs: set[asyncio.Task[Any]] = set()
    current = asyncio.current_task()
    connection._agentcall_gathers.add(current)
    started = time.monotonic()

    async def collect(awaitable: Any) -> None:
        try:
            candidate, protocol = await awaitable
            if protocol is not None:
                configure_turn_transport(protocol.transport)
                connection._protocols.append(protocol)
            candidates.append(candidate)
        except (OSError, stun.TransactionError) as error:
            logger.debug("Direct ICE candidate unavailable: %s", type(error).__name__)

    def launch(awaitable: Any) -> None:
        jobs.add(asyncio.create_task(collect(awaitable)))

    protocols: dict[str, Any] = {}

    async def bind_host(host: str, advertise: bool) -> None:
        if ((ipaddress.ip_address(host).version == 4 and not connection._use_ipv4)
                or (ipaddress.ip_address(host).version == 6 and not connection._use_ipv6)):
            return
        try:
            transport, protocol = await asyncio.get_running_loop().create_datagram_endpoint(
                lambda: ice.StunProtocol(connection), local_addr=(host, 0),
            )
        except OSError:
            return
        connection._protocols.append(protocol)
        sock = transport.get_extra_info("socket")
        if sock is not None:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, ice.turn.UDP_SOCKET_BUFFER_SIZE)
        bound = transport.get_extra_info("sockname")
        protocol.local_candidate = ice.Candidate(
            foundation=ice.candidate_foundation("host", "udp", bound[0]), component=component,
            transport="udp", priority=ice.candidate_priority(component, "host"),
            host=bound[0], port=bound[1], type="host",
        )
        protocols[host] = protocol
        if advertise and connection._transport_policy == ice.TransportPolicy.ALL:
            candidates.append(protocol.local_candidate)

    try:
        async with asyncio.timeout(GATHER_MAX_SECONDS):
            if connection._agentcall_stopping:
                return candidates
            if connection.turn_server:
                launch(ice.relayed_candidate(
                    component=component, protocol_factory=lambda: ice.StunProtocol(connection),
                    turn_server=connection.turn_server, turn_username=connection.turn_username,
                    turn_password=connection.turn_password, turn_ssl=connection.turn_ssl,
                    turn_transport=connection.turn_transport,
                ))
            hosts = physical_host_addresses()
            for host in hosts:
                await bind_host(host, advertise=True)
            # Host sockets already exist if DNS stalls until the gathering ceiling.
            sources = (await asyncio.to_thread(default_stun_sources, connection.stun_server)
                       if connection.stun_server else {})
            for source, target in sources.items():
                # A private VPN default source must remain usable on VPN-only hosts.
                if source not in protocols:
                    await bind_host(source, advertise=ipaddress.ip_address(source).is_private)
                if source in protocols:
                    launch(_server_reflexive(protocols[source], target))
            if jobs:
                await asyncio.gather(*jobs)
    except TimeoutError:
        logger.info("Direct ICE gather reached caller budget; retaining completed candidates")
    finally:
        for task in jobs:
            if not task.done():
                task.cancel()
        if jobs:
            await asyncio.gather(*jobs, return_exceptions=True)
        connection._agentcall_gathers.discard(current)
        connection._agentcall_gather_ms = (time.monotonic() - started) * 1000
    return candidates


def peer_connections(pc: Any) -> list[Any]:
    if not callable(getattr(pc, "getTransceivers", None)):
        return []  # Test transport or an unsupported peer implementation.
    try:
        transports = [t.receiver.transport.transport for t in pc.getTransceivers()]
        if pc.sctp is not None:
            transports.append(pc.sctp.transport.transport)
        return list({id(t._connection): t._connection for t in transports}.values())
    except AttributeError:
        logger.warning("Direct ICE optimization unsupported peer layout; using upstream behavior")
        return []


def configure_peer(pc: Any) -> None:
    for connection in peer_connections(pc):
        configure_connection(connection)


async def stop_connection_checks(connection: Any) -> None:
    if not _supported(connection):
        logger.warning("Direct ICE shutdown adapter unsupported; using upstream close")
        return
    connection._agentcall_stopping = True
    # Fence new periodic and triggered checks before yielding to drain old tasks.
    connection.check_start_task = lambda _pair: None
    connection.check_periodic = lambda: False
    connection._check_list_done = True
    connection._check_list_state.put_nowait(ice.ICE_FAILED)
    tasks = {p.task for p in connection._check_list if p.task is not None}
    tasks.update(getattr(connection, "_agentcall_gathers", set()))
    if connection._query_consent_task is not None:
        tasks.add(connection._query_consent_task)
    tasks.discard(asyncio.current_task())
    for task in tasks:
        task.cancel()
    if tasks:
        # Transaction.run finally cancels its retry timer before socket close.
        await asyncio.gather(*tasks, return_exceptions=True)


async def stop_peer_checks(pc: Any) -> None:
    for connection in peer_connections(pc):
        await stop_connection_checks(connection)


def configure_turn_transport(transport: Any) -> None:
    """Drain channel-bind/refresh transactions before releasing the TURN socket."""
    if not isinstance(transport, ice.turn.TurnTransport):
        return
    inner = getattr(transport, "_TurnTransport__inner_protocol", None)
    sends = getattr(transport, "_TurnTransport__send_tasks", None)
    if (not aioice.__version__.startswith("0.11.") or inner is None or sends is None
            or not hasattr(inner, "refresh_task")):
        logger.warning("Direct TURN shutdown adapter unsupported; using upstream close")
        return

    async def close_turn() -> None:
        # TurnTransport.close has already fenced sendto by setting __close_task.
        try:
            tasks = set(sends)
            if inner.refresh_task is not None:
                tasks.add(inner.refresh_task)
                inner.refresh_task = None
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            try:
                async with asyncio.timeout(TURN_DELETE_MAX_SECONDS):
                    await inner.delete()
            except (TimeoutError, OSError):
                logger.debug("Direct TURN delete unavailable; closing local allocation socket")
        finally:
            inner.transport.close()

    setattr(transport, "_close", close_turn)
