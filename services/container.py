"""The services wired together (what the API, the pages and the scheduler share)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from database import Database

from .clock import Clock, utcnow
from .task_service import TaskService
from .timeout_monitor import MonitorLimits, TimeoutMonitor
from .worker_service import WorkerService


@dataclass
class Services:
    db: Database
    workers: WorkerService
    tasks: TaskService
    monitor: TimeoutMonitor
    api_token: str = ""  # when set, API callers must send it in the X-API-Token header

    @classmethod
    def create(
        cls,
        db: Database,
        *,
        offline_after: timedelta = timedelta(seconds=60),
        assigned_timeout: timedelta = timedelta(minutes=5),
        running_timeout: timedelta = timedelta(minutes=60),
        api_token: str = "",
        clock: Clock = utcnow,
    ) -> Services:
        workers = WorkerService(db, offline_after=offline_after, clock=clock)
        return cls(
            db=db,
            workers=workers,
            tasks=TaskService(db, workers, clock=clock),
            monitor=TimeoutMonitor(db, workers, MonitorLimits(assigned_timeout, running_timeout), clock=clock),
            api_token=api_token,
        )
