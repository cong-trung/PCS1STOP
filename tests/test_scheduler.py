from __future__ import annotations

import asyncio
import threading

from scheduler import MonitorScheduler, install_scheduler
from services import MonitorReport


class CountingMonitor:
    def __init__(self, fail_first: int = 0) -> None:
        self.calls = 0
        self.fail_first = fail_first
        self.threads: set[int] = set()

    def run_once(self) -> MonitorReport:
        self.calls += 1
        self.threads.add(threading.get_ident())
        if self.calls <= self.fail_first:
            raise RuntimeError("database is locked")
        return MonitorReport()


def test_runs_immediately_and_then_every_interval():
    async def scenario() -> CountingMonitor:
        monitor = CountingMonitor()
        scheduler = MonitorScheduler(monitor, interval=0.05)
        scheduler.start()
        assert scheduler.running
        await asyncio.sleep(0.4)
        await scheduler.stop()
        assert not scheduler.running
        return monitor

    monitor = asyncio.run(scenario())
    assert monitor.calls >= 4
    assert threading.get_ident() not in monitor.threads  # blocking work stays off the event loop


def test_a_failing_pass_does_not_stop_the_loop(caplog):
    async def scenario() -> CountingMonitor:
        monitor = CountingMonitor(fail_first=2)
        scheduler = MonitorScheduler(monitor, interval=0.02)
        scheduler.start()
        await asyncio.sleep(0.3)
        await scheduler.stop()
        return monitor

    monitor = asyncio.run(scenario())
    assert monitor.calls > 3
    assert "timeout monitor pass failed" in caplog.text


def test_start_twice_and_stop_twice():
    async def scenario() -> int:
        monitor = CountingMonitor()
        scheduler = MonitorScheduler(monitor, interval=10)
        scheduler.start()
        scheduler.start()
        await asyncio.sleep(0.1)
        await scheduler.stop()
        await scheduler.stop()
        return monitor.calls

    assert asyncio.run(scenario()) == 1  # one loop only, one pass (the next is 10 s away)


def test_hooks_are_registered():
    registered: dict[str, object] = {}
    scheduler = MonitorScheduler(CountingMonitor(), interval=0.05)
    install_scheduler(lambda fn: registered.update(start=fn), lambda fn: registered.update(stop=fn), scheduler)

    async def scenario() -> bool:
        await registered["start"]()
        running = scheduler.running
        await registered["stop"]()
        return running and not scheduler.running

    assert asyncio.run(scenario())
