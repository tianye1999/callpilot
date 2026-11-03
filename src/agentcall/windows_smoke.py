"""CI-only backend mode: real HTTP routes, no service/modem construction or dotenv."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from aiohttp import web

from . import config
from .events import EventHub
from .web.server import build_app


def run(stop_requested: Callable[[], bool] | None) -> None:
    async def serve():
        hub = EventHub(asyncio.get_running_loop())
        meta = config.runtime_meta(provider='qwen', model='', port='disabled-for-selftest')
        application = build_app(hub, None, meta=meta)
        application['hardware_selftest'] = True
        runner = web.AppRunner(application)
        await runner.setup()
        try:
            await web.TCPSite(runner, '127.0.0.1', 47100).start()
            while stop_requested is None or not stop_requested():
                await asyncio.sleep(0.1)
        finally:
            await runner.cleanup()

    asyncio.run(serve())
