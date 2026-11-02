#!/usr/bin/env python3
"""Probe current routes without printing SDP or TURN credentials.

Optional WEBRTC_ICE_SERVERS_JSON contains the Cloudflare iceServers array (or
the full {iceServers: ...} response). Otherwise only public Cloudflare STUN is used.
No secrets are fetched and no phone or modem is contacted.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentcall.webrtc_media import WebRtcRemoteMediaEndpoint, warm_webrtc_stack  # noqa: E402


async def probe() -> None:
    stack = await asyncio.wrap_future(warm_webrtc_stack())
    servers = json.loads(os.environ.get("WEBRTC_ICE_SERVERS_JSON", '[{"urls":["stun:stun.cloudflare.com:3478"]}]'))
    if isinstance(servers, dict):
        servers = servers["iceServers"]
    phone = stack.rtc.RTCPeerConnection(stack.rtc.RTCConfiguration(iceServers=[]))
    endpoint = None
    try:
        phone.addTransceiver("audio")
        phone.createDataChannel("callpilot.control")
        await phone.setLocalDescription(await phone.createOffer())
        endpoint = WebRtcRemoteMediaEndpoint(
            {"type": "offer", "sdp": phone.localDescription.sdp}, servers, call_id="gather-probe",
        )
        print("mode:", "TURN supplied via environment" if "WEBRTC_ICE_SERVERS_JSON" in os.environ else "STUN only")
        print("host_before:", json.dumps(stack.ice.ice.get_host_addresses(True, True)))
        print("host_after:", json.dumps(stack.ice.physical_host_addresses()))
        started = time.monotonic()
        await endpoint.connect()
        elapsed = (time.monotonic() - started) * 1000
        connections = stack.ice.peer_connections(endpoint._pc)
        for index, connection in enumerate(connections):
            sources = (await asyncio.to_thread(stack.ice.default_stun_sources, connection.stun_server)
                       if connection.stun_server else {})
            print(f"srflx_sources[{index}]:", json.dumps(list(sources)))
            print(f"gather_ms[{index}]:", round(getattr(connection, "_agentcall_gather_ms", -1), 3))
            print(f"candidate_types[{index}]:", json.dumps([c.type for c in connection.local_candidates]))
        print("answer_ready_ms:", round(elapsed, 3))
        print("acceptance_under_1000ms:", elapsed < 1000)
    finally:
        if endpoint is not None:
            await endpoint.close()
        await stack.ice.stop_peer_checks(phone)
        await phone.close()


if __name__ == "__main__":
    asyncio.run(probe())
