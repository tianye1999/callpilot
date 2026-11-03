"""Instance-local aioice adapter. Never patches LiveKit or global aioice behavior.

The private adapter is audited against aioice 0.11.x; unknown layouts keep upstream
behavior. The gathering cap bounds caller silence, not a choice between paths.
"""

from __future__ import annotations

import asyncio
import inspect
import ipaddress
import logging
import random
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
CONSENT_MAX_AGE = 30.0
CONSENT_RESPONSE_SECONDS = 4.0
_VIRTUAL_PREFIXES = ("utun", "ipsec", "tun", "tap", "awdl", "llw", "bridge", "ppp", "wg", "gif", "stf", "lo",
                     "tailscale", "wireguard", "wintun")


def physical_host_addresses(adapters: Any = None) -> list[str]:
    """Host candidates come from physical interfaces; a host with none keeps every routable address.

    VPN / Tailscale interfaces are excluded by name only (utun*, ipsec*, tailscale*, wg*, ...).
    If the name filter leaves nothing (unusual interface naming, cloud VMs, CI runners), fall back to
    the upstream-equivalent list so gathering never starts with zero host candidates.
    """
    adapters = list(ifaddr.get_adapters() if adapters is None else adapters)
    physical: list[str] = []
    everything: list[str] = []
    for adapter in adapters:
        names = (str(adapter.name).lower(), str(adapter.nice_name).lower())
        virtual = any(name.startswith(_VIRTUAL_PREFIXES) for name in names)
        for entry in adapter.ips:
            value = entry.ip if isinstance(entry.ip, str) else entry.ip[0]
            address = ipaddress.ip_address(value)
            if address.is_loopback or address.is_link_local or address.is_unspecified or address.is_multicast:
                continue
            if value not in everything:
                everything.append(value)
            if not virtual and value not in physical:
                physical.append(value)
    if not physical and everything:
        logger.warning("Direct ICE: no physical interface address found; using all %d routable addresses",
                       len(everything))
        return everything
    return physical


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
            and list(inspect.signature(ice.Connection.query_consent).parameters) == ["self"]
            and list(inspect.signature(stun.Transaction.__init__).parameters)
            == ["self", "request", "addr", "protocol", "retransmissions"]
            and all(hasattr(connection, name) for name in (
                "_protocols", "_check_list", "_check_list_state", "_query_consent_task", "_transport_policy",
                "_use_ipv4", "_use_ipv6", "_nominated", "remote_password")))


def configure_connection(connection: Any) -> bool:
    if not _supported(connection):
        logger.warning("Direct ICE optimization unsupported aioice layout/version; using upstream behavior")
        return False
    if getattr(connection, "_agentcall_gathers", None) is None:
        connection._agentcall_gathers = set()
        connection._agentcall_stopping = False
        connection.get_component_candidates = MethodType(_gather_component, connection)
        connection.query_consent = MethodType(_query_consent, connection)
    return True


class _ConsentTransaction(stun.Transaction):
    """One transmission, but retain the transaction beyond aioice's 500 ms RTO.

    RFC 7675 section 5.1 forbids retransmission and requires an independent
    30 s expiry. Four seconds allows delayed cellular replies without overlapping
    requests (the randomized request interval is at least four seconds).
    """

    def __init__(self, request: Any, addr: Any, protocol: Any, key: bytes) -> None:
        request.add_message_integrity(key)
        super().__init__(request, addr, protocol, retransmissions=0)
        self._Transaction__timeout_delay = CONSENT_RESPONSE_SECONDS
        self._consent_addr, self._consent_key = addr, key

    def receive_bytes(self, data: bytes, addr: Any) -> None:
        if addr != self._consent_addr:
            return
        try:
            message = stun.parse_message(data, integrity_key=self._consent_key)
            if message.message_method != stun.Method.BINDING or "MESSAGE-INTEGRITY" not in message.attributes:
                return
        except ValueError:
            return
        super().response_received(message, addr)


def _configure_consent_protocol(protocol: Any) -> None:
    if getattr(protocol, "_agentcall_consent_datagrams", False):
        return
    original = protocol.datagram_received

    def receive(data: bytes, addr: Any) -> None:
        # Authenticate the original wire bytes, not a reserialized Message:
        # aioice drops unknown attributes while parsing, changing the HMAC input.
        try:
            message = stun.parse_message(data)
        except ValueError:
            original(data, addr)
            return
        transaction = protocol.transactions.get(message.transaction_id)
        if (isinstance(transaction, _ConsentTransaction)
                and message.message_class in (stun.Class.RESPONSE, stun.Class.ERROR)):
            transaction.receive_bytes(data, (addr[0], addr[1]))
        else:
            original(data, addr)

    protocol.datagram_received = receive
    protocol._agentcall_consent_datagrams = True


async def _consent_request(connection: Any, pair: Any) -> None:
    request = connection.build_request(pair, nominate=False)
    protocol = pair.protocol
    _configure_consent_protocol(protocol)
    transaction = _ConsentTransaction(request, pair.remote_addr, protocol, connection.remote_password.encode("utf8"))
    protocol.transactions[request.transaction_id] = transaction
    try:
        await transaction.run()
    finally:
        del protocol.transactions[request.transaction_id]


async def _consent_component(connection: Any, component: int) -> None:
    pair = connection._nominated[component]
    last_success = time.monotonic()
    next_check = last_success + 5 * (0.8 + 0.4 * random.random())
    while True:
        await asyncio.sleep(max(0, min(next_check, last_success + CONSENT_MAX_AGE) - time.monotonic()))
        # Upstream follows authenticated USE-CANDIDATE / successful triggered
        # checks, including newly learned prflx addresses. Never retarget on RTP.
        current = connection._nominated[component]
        if current is not pair:
            pair, last_success = current, time.monotonic()
        remaining = last_success + CONSENT_MAX_AGE - time.monotonic()
        if remaining <= 0:
            logger.warning("Direct ICE consent expired connection=%s component=%s age=%.1fs local=%s remote=%s",
                           connection, component, time.monotonic() - last_success, pair.local_addr, pair.remote_addr)
            return
        started = time.monotonic()
        next_check = started + 5 * (0.8 + 0.4 * random.random())
        logger.info("Direct ICE consent request connection=%s component=%s local=%s remote=%s",
                    connection, component, pair.local_addr, pair.remote_addr)
        try:
            async with asyncio.timeout(remaining):
                await _consent_request(connection, pair)
            if time.monotonic() >= last_success + CONSENT_MAX_AGE:
                continue  # A response processed after expiry cannot restore consent.
            last_success = time.monotonic()
            logger.info("Direct ICE consent ok connection=%s component=%s local=%s remote=%s elapsedMs=%.0f",
                        connection, component, pair.local_addr, pair.remote_addr, (last_success - started) * 1000)
        except (stun.TransactionError, TimeoutError) as error:
            logger.warning("Direct ICE consent failed connection=%s component=%s local=%s remote=%s "
                           "age=%.1fs error=%s", connection, component, pair.local_addr, pair.remote_addr,
                           time.monotonic() - last_success, type(error).__name__)
            if (isinstance(error, stun.TransactionFailed)
                    and error.response is not None
                    and error.response.attributes.get("ERROR-CODE", (0,))[0] == 403):
                return  # RFC 7675 section 5.2: authenticated immediate revocation.


async def _query_consent(connection: Any) -> None:
    tasks = [asyncio.create_task(_consent_component(connection, component)) for component in connection._nominated]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    connection._query_consent_task = None
    await connection.close()


async def _gather_component(connection: Any, component: int, addresses: list[str], timeout: int = 5) -> list[Any]:
    del addresses, timeout  # Upstream's unfiltered interfaces and 5s timeout are not used.
    candidates: list[Any] = []
    jobs: set[asyncio.Task[Any]] = set()
    current = asyncio.current_task()
    connection._agentcall_gathers.add(current)
    started = time.monotonic()

    def milestone(kind: str, outcome: str, launched: float) -> None:
        now = time.monotonic()
        logger.info("Direct ICE candidate connection=%s component=%s type=%s outcome=%s "
                    "ready_ms=%.1f operation_ms=%.1f", id(connection), component, kind, outcome,
                    (now - started) * 1000, (now - launched) * 1000)

    async def collect(awaitable: Any, kind: str) -> None:
        launched = time.monotonic()
        try:
            candidate, protocol = await awaitable
            if protocol is not None:
                configure_turn_transport(protocol.transport)
                connection._protocols.append(protocol)
            candidates.append(candidate)
            milestone(kind, "ready", launched)
        except asyncio.CancelledError:
            milestone(kind, "cancelled", launched)
            raise
        except (OSError, stun.TransactionError) as error:
            milestone(kind, "unavailable", launched)
            logger.debug("Direct ICE candidate unavailable: %s", type(error).__name__)

    def launch(awaitable: Any, kind: str) -> None:
        jobs.add(asyncio.create_task(collect(awaitable, kind)))

    protocols: dict[str, Any] = {}

    async def bind_host(host: str, advertise: bool) -> None:
        launched = time.monotonic()
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
            milestone("host", "ready", launched)

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
                ), "relay")
            hosts = physical_host_addresses()
            for host in hosts:
                await bind_host(host, advertise=True)
            # Host sockets already exist if DNS stalls until the gathering ceiling.
            dns_started = time.monotonic()
            sources = (await asyncio.to_thread(default_stun_sources, connection.stun_server)
                       if connection.stun_server else {})
            milestone("stun_route", "ready", dns_started)
            for source, target in sources.items():
                # A private VPN default source must remain usable on VPN-only hosts.
                if source not in protocols:
                    await bind_host(source, advertise=ipaddress.ip_address(source).is_private)
                if source in protocols:
                    launch(_server_reflexive(protocols[source], target), "srflx")
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
        logger.info("Direct ICE gather connection=%s component=%s total_ms=%.1f host=%s srflx=%s relay=%s",
                    id(connection), component, connection._agentcall_gather_ms,
                    sum(c.type == "host" for c in candidates), sum(c.type == "srflx" for c in candidates),
                    sum(c.type == "relay" for c in candidates))
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
        logger.info("Direct ICE peer=%s connection=%s", id(pc), id(connection))
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
    # Upstream gather uses detached srflx tasks: cancelling gather while it is
    # awaiting asyncio.wait leaves them outside both _check_list and our gathers.
    # Their STUN futures, rather than the candidate/check lists, own the retries.
    await _drain_protocol_transactions(connection._protocols)


async def _drain_protocol_transactions(protocols: list[Any]) -> None:
    tasks: set[asyncio.Task[Any]] = set()
    for protocol in protocols:
        for transaction in list(getattr(protocol, "transactions", {}).values()):
            future = getattr(transaction, "_Transaction__future", None)
            timer = getattr(transaction, "_Transaction__timeout_handle", None)
            if (not aioice.__version__.startswith("0.11.") or not isinstance(future, asyncio.Future)
                    or not hasattr(future, "_callbacks") or (timer is not None
                    and not isinstance(timer, asyncio.TimerHandle))):
                logger.warning("Direct STUN transaction adapter unsupported; using upstream close")
                continue
            # CPython Task's wakeup callback identifies the coroutine awaiting
            # this exact transaction; never cancel unrelated loop tasks.
            for callback, _context in future._callbacks or ():
                owner = getattr(callback, "__self__", None)
                if isinstance(owner, asyncio.Task) and owner is not asyncio.current_task():
                    tasks.add(owner)
            if timer is not None:
                timer.cancel()
            future.cancel()
    for task in tasks:
        task.cancel()
    if tasks:
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
            try:
                await _drain_protocol_transactions([inner])
            finally:
                inner.transport.close()

    setattr(transport, "_close", close_turn)
