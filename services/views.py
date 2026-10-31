"""Plain read-only snapshots of database rows, safe to hand to the UI and to other threads."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from database import Task, TaskStatus, Worker, WorkerStatus


@dataclass(frozen=True)
class TaskView:
    id: int
    task_type: str
    status: TaskStatus
    assigned_worker: str | None
    created_time: datetime
    assigned_time: datetime | None
    started_time: datetime | None
    completed_time: datetime | None
    payload: dict[str, Any]
    result: dict[str, Any] | None
    error_message: str | None
    retry_count: int

    @classmethod
    def from_row(cls, task: Task) -> TaskView:
        return cls(
            id=task.id,
            task_type=task.task_type,
            status=task.status,
            assigned_worker=task.assigned_worker,
            created_time=task.created_time,
            assigned_time=task.assigned_time,
            started_time=task.started_time,
            completed_time=task.completed_time,
            payload=task.payload_data,
            result=task.result_data,
            error_message=task.error_message,
            retry_count=task.retry_count,
        )

    def duration(self, now: datetime) -> timedelta | None:
        """Completed time - started time for a finished task, current time - started time for one in progress.

        ``None`` while the task has not started (PENDING / ASSIGNED, or a TIMEOUT that never started)."""
        if self.started_time is None:
            return None
        return max(timedelta(0), (self.completed_time or now) - self.started_time)


@dataclass(frozen=True)
class WorkerView:
    hostname: str
    status: WorkerStatus  # what the UI shows: ONLINE only while the worker has really been heard of recently
    stored_status: WorkerStatus  # what the table says (the monitor refreshes it once a minute)
    last_seen: datetime
    current_task_id: int | None
    ip_address: str | None
    version: str | None

    @classmethod
    def from_row(cls, worker: Worker, now: datetime, offline_after: timedelta) -> WorkerView:
        fresh = now - worker.last_seen <= offline_after
        status = WorkerStatus.ONLINE if worker.status is WorkerStatus.ONLINE and fresh else WorkerStatus.OFFLINE
        return cls(
            hostname=worker.hostname,
            status=status,
            stored_status=worker.status,
            last_seen=worker.last_seen,
            current_task_id=worker.current_task_id,
            ip_address=worker.ip_address,
            version=worker.version,
        )
