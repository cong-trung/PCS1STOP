"""Runs a job once a day, at a fixed time of day, in the background of the PC1 process (the TP-bin CSV cleanup)."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime, time, timedelta

from services import Clock, utcnow

log = logging.getLogger("pcs.scheduler")


def next_run(now: datetime, at: time) -> datetime:
    """The first moment after ``now`` at which a wall clock shows ``at`` (both are naive local times)."""
    today = datetime.combine(now.date(), at)
    return today if today > now else today + timedelta(days=1)


class DailyScheduler:
    """``job()`` once a day when the PC's clock shows ``at``, until ``stop()`` (starts and stops like ``MonitorScheduler``).

    The job does blocking file work, so it runs in a worker thread and never stalls the web server.  The clock is read
    again at least every ``poll`` seconds instead of sleeping all the way to the next run: a PC that slept through the
    time, or whose clock was set forward, runs the job once as soon as it is awake - not a day late.  A failing run is
    logged; the next one is tomorrow."""

    def __init__(
        self,
        job: Callable[[], object],
        at: time,
        *,
        name: str = "daily job",
        clock: Clock = utcnow,
        poll: float = 30.0,
    ) -> None:
        self.job = job
        self.at = at
        self.name = name
        self.clock = clock
        self.poll = poll
        self._task: asyncio.Task[None] | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        """Begin looping on the running event loop (does nothing when already started)."""
        if not self.running:
            due = next_run(self._local_now(), self.at)
            self._task = asyncio.get_running_loop().create_task(self._loop(due), name=f"pcs-{self.name}")
            log.info("%s scheduled every day at %s, next run %s", self.name, self.at.strftime("%H:%M"), due.strftime("%Y-%m-%d %H:%M"))

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        log.info("%s stopped", self.name)

    def _local_now(self) -> datetime:
        """The clock as the PC's wall clock shows it (the time of day ``at`` is meant in)."""
        return self.clock().astimezone().replace(tzinfo=None)

    async def _loop(self, due: datetime) -> None:
        while True:
            wait = (due - self._local_now()).total_seconds()
            if wait > 0:
                await asyncio.sleep(min(wait, self.poll))
                continue
            try:
                await asyncio.to_thread(self.job)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a bug or a locked folder must not end the job for good
                log.exception("%s failed", self.name)
            due = next_run(self._local_now(), self.at)
