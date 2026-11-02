"""Call-local silent transport ownership; never touches modem or audio bridges."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from .call_timing import CallTiming, attach_endpoint_timing
from .remote_dialer import IssuedLiveKitSession

logger = logging.getLogger(__name__)

class InboundPreconnect:
    def __init__(self) -> None:
        self.endpoint: Any = None
        self.issued: IssuedLiveKitSession | None = None
        self.task: asyncio.Task[None] | None = None
        self.timing = CallTiming()
        self.reported = False

    def start(self, issued: IssuedLiveKitSession, factory: Any) -> None:
        if self.task is not None:
            return
        self.issued = issued
        self.timing.mark("prepare_received")
        self.task = asyncio.create_task(self._prepare(issued, factory))

    async def _prepare(self, issued: IssuedLiveKitSession, factory: Any) -> None:
        self.endpoint = factory(issued)
        attach_endpoint_timing(self.endpoint, self.timing)
        await self.endpoint.preconnect()

    async def adopt(self, issued: IssuedLiveKitSession, timing: CallTiming) -> Any:
        task, endpoint = self.task, self.endpoint
        if (task is not None and task.done() and not task.cancelled()
                and task.exception() is None and self.issued is not None
                and issued.room_name == self.issued.room_name
                and issued.edge_identity == self.issued.edge_identity):
            attach_endpoint_timing(endpoint, timing)
            try:
                await endpoint.bind_winner(issued)
            except Exception:
                await self.close()
                timing.include_preconnect(self.timing)
                self.reported = True
                return None
            timing.include_preconnect(self.timing)
            self.reported = True
            self.endpoint = None
            self.task = None
            return endpoint
        await self.close()
        timing.include_preconnect(self.timing)
        self.reported = True
        return None

    async def close(self) -> None:
        task, endpoint = self.task, self.endpoint
        self.task, self.endpoint = None, None
        if task is not None:
            task.cancel()
        if endpoint is not None:
            try:
                await asyncio.wait_for(endpoint.close(), timeout=2.0)
            except Exception:
                logger.warning("Silent inbound transport cleanup failed")
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
