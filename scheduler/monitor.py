"""Runs the timeout monitor in the background of the PC1 process (architect.md: every 60 seconds)."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Protocol

from services import MonitorReport

log = logging.getLogger("pcs.scheduler")


class Monitor(Protocol):
    def run_once(self) -> MonitorReport: ...


class Scheduler(Protocol):
    """What ``install_scheduler`` hooks up: ``MonitorScheduler``, ``DailyScheduler``."""

    def start(self) -> None: ...

    async def stop(self) -> None: ...


class MonitorScheduler:
    """``monitor.run_once()`` right away and then every ``interval`` seconds, until ``stop()``.

    The monitor does blocking database work, so it runs in a worker thread and never stalls the web server.
    A failing pass is logged and retried at the next tick; it never stops the loop."""

    def __init__(self, monitor: Monitor, interval: float = 60.0) -> None:
        self.monitor = monitor
        self.interval = interval
        self._task: asyncio.Task[None] | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        """Begin looping on the running event loop (does nothing when already started)."""
        if not self.running:
            self._task = asyncio.get_running_loop().create_task(self._loop(), name="pcs-timeout-monitor")
            log.info("timeout monitor started (every %g s)", self.interval)

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        log.info("timeout monitor stopped")

    async def _loop(self) -> None:
        while True:
            try:
                await asyncio.to_thread(self.monitor.run_once)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a locked database or a bug must not end the housekeeping for good
                log.exception("timeout monitor pass failed")
            await asyncio.sleep(self.interval)


def install_scheduler(
    on_startup: Callable[[Callable[[], Awaitable[None] | None]], object],
    on_shutdown: Callable[[Callable[[], Awaitable[None] | None]], object],
    scheduler: Scheduler,
) -> None:
    """Hook ``scheduler`` to an application's startup / shutdown (``app.on_startup`` / ``app.on_shutdown``)."""

    async def _start() -> None:
        scheduler.start()

    async def _stop() -> None:
        await scheduler.stop()

    on_startup(_start)
    on_shutdown(_stop)
