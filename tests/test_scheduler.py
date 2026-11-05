from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable
from datetime import datetime
from datetime import time as time_of_day
from datetime import timedelta

import pytest

from scheduler import DailyScheduler, MonitorScheduler, install_scheduler
from scheduler.daily import next_run
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


MIDNIGHT = time_of_day(0, 0)


@pytest.mark.parametrize(
    ("now", "at", "expected"),
    [
        (datetime(2025, 1, 6, 8, 0), MIDNIGHT, datetime(2025, 1, 7, 0, 0)),  # today's midnight is gone
        (datetime(2025, 1, 6, 0, 0), MIDNIGHT, datetime(2025, 1, 7, 0, 0)),  # exactly at the time: the next one
        (datetime(2025, 1, 5, 23, 59, 59), MIDNIGHT, datetime(2025, 1, 6, 0, 0)),
        (datetime(2024, 12, 31, 12, 0), MIDNIGHT, datetime(2025, 1, 1, 0, 0)),
        (datetime(2024, 2, 28, 12, 0), MIDNIGHT, datetime(2024, 2, 29, 0, 0)),
        (datetime(2024, 2, 29, 12, 0), MIDNIGHT, datetime(2024, 3, 1, 0, 0)),
        (datetime(2025, 1, 6, 3, 4), time_of_day(3, 5), datetime(2025, 1, 6, 3, 5)),  # still ahead today
        (datetime(2025, 1, 6, 3, 6), time_of_day(3, 5), datetime(2025, 1, 7, 3, 5)),
    ],
)
def test_next_run_is_the_first_time_the_clock_shows_the_time_after_now(now, at, expected):
    assert next_run(now, at) == expected


class CountingJob:
    def __init__(self, fail_first: int = 0) -> None:
        self.calls = 0
        self.fail_first = fail_first
        self.threads: set[int] = set()

    def __call__(self) -> None:
        self.calls += 1
        self.threads.add(threading.get_ident())
        if self.calls <= self.fail_first:
            raise RuntimeError("folder is locked")


def wall_clock_at(moment: datetime) -> time_of_day:
    """What the PC's wall clock shows at ``moment`` (the scheduler's time of day is meant in it)."""
    return moment.astimezone().time()


async def until(condition: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not condition():
        assert asyncio.get_running_loop().time() < deadline, "gave up waiting"
        await asyncio.sleep(0.005)


async def idle() -> None:
    """Long enough for a scheduler that polls every 5 ms to look at the (fake) clock many times."""
    await asyncio.sleep(0.05)


def test_the_job_runs_once_a_day_when_the_clock_shows_the_time(clock):
    async def scenario() -> list[int]:
        job = CountingJob()
        scheduler = DailyScheduler(job, wall_clock_at(clock() + timedelta(minutes=10)), clock=clock, poll=0.005)
        scheduler.start()
        assert scheduler.running
        calls = []
        await idle()
        calls.append(job.calls)  # not yet: 10 minutes to go
        clock.advance(minutes=9, seconds=59)
        await idle()
        calls.append(job.calls)  # one second to go
        clock.advance(seconds=1)
        await until(lambda: job.calls == 1)
        await idle()
        calls.append(job.calls)  # once, not again the same day
        clock.advance(hours=23, minutes=59)
        await idle()
        calls.append(job.calls)  # one minute before tomorrow's run
        clock.advance(minutes=1)
        await until(lambda: job.calls == 2)
        await scheduler.stop()
        assert not scheduler.running
        calls.append(job.calls)
        return calls

    assert asyncio.run(scenario()) == [0, 0, 1, 1, 2]


def test_a_pc_that_slept_through_the_time_runs_the_job_once_when_it_wakes(clock):
    async def scenario() -> int:
        job = CountingJob()
        scheduler = DailyScheduler(job, wall_clock_at(clock() + timedelta(hours=1)), clock=clock, poll=0.005)
        scheduler.start()
        await idle()
        clock.advance(hours=50)  # asleep for two nights
        await until(lambda: job.calls == 1)
        await idle()
        await scheduler.stop()
        return job.calls

    assert asyncio.run(scenario()) == 1  # once, not once for every night that was missed


def test_the_job_runs_off_the_event_loop_thread(clock):
    async def scenario() -> CountingJob:
        job = CountingJob()
        scheduler = DailyScheduler(job, wall_clock_at(clock() + timedelta(minutes=1)), clock=clock, poll=0.005)
        scheduler.start()
        clock.advance(minutes=1)
        await until(lambda: job.calls == 1)
        await scheduler.stop()
        return job

    job = asyncio.run(scenario())
    assert threading.get_ident() not in job.threads  # blocking file work stays off the event loop


def test_a_failing_run_is_logged_and_tomorrow_runs_again(clock, caplog):
    async def scenario() -> int:
        job = CountingJob(fail_first=1)
        scheduler = DailyScheduler(
            job, wall_clock_at(clock() + timedelta(minutes=1)), name="TP-bin cleanup", clock=clock, poll=0.005
        )
        scheduler.start()
        clock.advance(minutes=1)
        await until(lambda: job.calls == 1)
        await idle()
        assert scheduler.running  # the failure did not end the loop
        clock.advance(days=1)
        await until(lambda: job.calls == 2)
        await scheduler.stop()
        return job.calls

    assert asyncio.run(scenario()) == 2
    assert "TP-bin cleanup failed" in caplog.text and "folder is locked" in caplog.text


def test_daily_start_twice_and_stop_twice(clock, caplog):
    caplog.set_level(logging.INFO, logger="pcs.scheduler")

    async def scenario() -> int:
        job = CountingJob()
        scheduler = DailyScheduler(job, wall_clock_at(clock() + timedelta(hours=1)), name="TP-bin cleanup", clock=clock, poll=0.005)
        scheduler.start()
        scheduler.start()
        await idle()
        await scheduler.stop()
        await scheduler.stop()
        assert not scheduler.running
        return job.calls

    assert asyncio.run(scenario()) == 0
    assert caplog.text.count("TP-bin cleanup scheduled every day at") == 1  # one loop only


def test_the_start_says_when_the_job_runs_next(clock, caplog):
    caplog.set_level(logging.INFO, logger="pcs.scheduler")
    at = wall_clock_at(clock() + timedelta(minutes=10))

    async def scenario() -> None:
        scheduler = DailyScheduler(CountingJob(), at, name="TP-bin cleanup", clock=clock)
        scheduler.start()
        await scheduler.stop()

    asyncio.run(scenario())
    expected = (clock() + timedelta(minutes=10)).astimezone().strftime("%Y-%m-%d %H:%M")
    assert f"every day at {at:%H:%M}, next run {expected}" in caplog.text


def test_a_daily_scheduler_is_hooked_to_the_startup_and_shutdown(clock):
    registered: dict[str, object] = {}
    scheduler = DailyScheduler(CountingJob(), MIDNIGHT, clock=clock)
    install_scheduler(lambda fn: registered.update(start=fn), lambda fn: registered.update(stop=fn), scheduler)

    async def scenario() -> bool:
        await registered["start"]()
        running = scheduler.running
        await registered["stop"]()
        return running and not scheduler.running

    assert asyncio.run(scenario())
