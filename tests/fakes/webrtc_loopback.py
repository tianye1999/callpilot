"""Real loopback diagnostics; never substitute ICE, DTLS, RTP or SCTP."""

import asyncio
from contextlib import asynccontextmanager


async def peer_diagnostics(peers):
    result = []
    for peer in peers:
        transports = {t.receiver.transport for t in peer.getTransceivers()}
        if peer.sctp is not None:
            transports.add(peer.sctp.transport)
        states = []
        for dtls in transports:
            ice = dtls.transport
            states.append({
                "ice": ice.state, "gathering": ice.iceGatherer.state, "dtls": dtls.state,
                "localCandidates": [(c.ip, c.type) for c in ice.iceGatherer.getLocalCandidates()],
                "remoteCandidates": [(c.ip, c.type) for c in ice.getRemoteCandidates()],
                "checks": [{"state": pair.state.name,
                            "taskError": (type(pair.task.exception()).__name__
                                          if pair.task is not None and pair.task.done()
                                          and not pair.task.cancelled() and pair.task.exception() else None)}
                           for pair in ice._connection._check_list],
            })
        try:
            stats = await asyncio.wait_for(peer.getStats(), 1)
            counters = [{key: value for key, value in vars(stat).items()
                         if key in {"type", "packetsSent", "packetsReceived", "packetsLost",
                                    "bytesSent", "bytesReceived", "dtlsState"}}
                        for stat in stats.values()]
        except Exception as error:
            counters = [{"statsError": type(error).__name__}]
        result.append({"connection": peer.connectionState, "ice": peer.iceConnectionState,
                       "signaling": peer.signalingState, "transports": states, "stats": counters})
    return result


@asynccontextmanager
async def loopback_phase(name, peers, timeout=30):
    try:
        async with asyncio.timeout(timeout):
            yield
    except TimeoutError as error:
        pending = sorted(t.get_coro().__qualname__ for t in asyncio.all_tasks()
                         if t is not asyncio.current_task())
        raise AssertionError(f"loopback phase={name!r} timed out after {timeout}s; "
                             f"peers={await peer_diagnostics(peers)}; pending={pending}") from error
