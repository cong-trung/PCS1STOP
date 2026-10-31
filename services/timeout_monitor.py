"""Housekeeping PC1 runs once a minute: workers that went quiet and tasks that stopped making progress.

* a worker not heard of for ``WorkerService.offline_after`` (60 s)          -> worker OFFLINE
* a task ASSIGNED for more than ``assigned_timeout`` (5 min), never started -> task TIMEOUT, worker released
* a task RUNNING for more than ``running_timeout`` (60 min), never finished -> task TIMEOUT, worker released

A timed-out task keeps ``assigned_worker`` (the history shows who had it); only the worker's ``current_task_id``
is cleared.  Everything happens in one write transaction.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import InstrumentedAttribute, Session

from database import Database, Task, TaskStatus

from .clock import Clock, utcnow
from .formatting import format_duration
from .worker_service import WorkerService

log = logging.getLogger("pcs.monitor")


@dataclass(frozen=True)
class MonitorLimits:
    assigned_timeout: timedelta = timedelta(minutes=5)
    running_timeout: timedelta = timedelta(minutes=60)


@dataclass
class MonitorReport:
    offline_workers: list[str] = field(default_factory=list)
    assigned_timeouts: list[int] = field(default_factory=list)
    running_timeouts: list[int] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.offline_workers or self.assigned_timeouts or self.running_timeouts)


class TimeoutMonitor:
    def __init__(
        self,
        db: Database,
        workers: WorkerService,
        limits: MonitorLimits | None = None,
        *,
        clock: Clock = utcnow,
    ) -> None:
        self.db = db
        self.workers = workers
        self.limits = limits or MonitorLimits()
        self.clock = clock

    def run_once(self) -> MonitorReport:
        """One pass over workers and tasks; safe to call at any time and as often as wanted."""
        with self.db.write_session() as session:
            now = self.clock()
            report = MonitorReport(
                offline_workers=self.workers.mark_stale_offline(session, now),
                assigned_timeouts=self._expire(
                    session, now, TaskStatus.ASSIGNED, Task.assigned_time, self.limits.assigned_timeout,
                    "was assigned to {worker} but not started within {limit}",
                ),
                running_timeouts=self._expire(
                    session, now, TaskStatus.RUNNING, Task.started_time, self.limits.running_timeout,
                    "was running on {worker} for more than {limit} without a result",
                ),
            )
        if report.changed:
            log.warning(
                "monitor: offline workers %s, assigned timeouts %s, running timeouts %s",
                report.offline_workers, report.assigned_timeouts, report.running_timeouts,
            )
        return report

    def _expire(
        self,
        session: Session,
        now: datetime,
        status: TaskStatus,
        since: InstrumentedAttribute,
        limit: timedelta,
        reason: str,
    ) -> list[int]:
        tasks = session.scalars(
            select(Task).where(Task.status == status, since < now - limit).order_by(Task.id)
        ).all()
        for task in tasks:
            task.status = TaskStatus.TIMEOUT
            task.completed_time = now
            task.error_message = "Task " + reason.format(worker=task.assigned_worker, limit=format_duration(limit))
            self.workers.release(session, task.assigned_worker, task.id)
        return [task.id for task in tasks]
