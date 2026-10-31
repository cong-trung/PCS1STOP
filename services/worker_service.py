"""Worker registry: heartbeats, offline detection and the worker list shown on the Workers page."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from database import Database, Worker, WorkerStatus

from .clock import Clock, utcnow
from .errors import InvalidRequest
from .views import WorkerView

log = logging.getLogger("pcs.workers")

MAX_HOSTNAME_LENGTH = 255


def clean_hostname(value: object) -> str:
    """A worker name as stored (trimmed); empty or oversized names are a client error."""
    name = value.strip() if isinstance(value, str) else ""
    if not name:
        raise InvalidRequest("worker name must not be empty")
    if len(name) > MAX_HOSTNAME_LENGTH:
        raise InvalidRequest(f"worker name is longer than {MAX_HOSTNAME_LENGTH} characters")
    return name


class WorkerService:
    """The workers table.  PC1 is its only writer, so it is the single source of truth for worker state."""

    def __init__(
        self,
        db: Database,
        *,
        offline_after: timedelta = timedelta(seconds=60),
        clock: Clock = utcnow,
    ) -> None:
        self.db = db
        self.offline_after = offline_after
        self.clock = clock

    # ------------------------------------------------- building blocks (run inside the caller's transaction)
    def touch(
        self,
        session: Session,
        hostname: str,
        *,
        ip: str | None = None,
        version: str | None = None,
        now: datetime | None = None,
    ) -> Worker:
        """Record that ``hostname`` has just been heard of: create or refresh its row and mark it ONLINE."""
        now = now or self.clock()
        worker = session.get(Worker, hostname)
        if worker is None:
            worker = Worker(hostname=hostname, last_seen=now, status=WorkerStatus.ONLINE, ip_address=ip, version=version)
            session.add(worker)
            log.info("worker %s registered (%s, version %s)", hostname, ip or "ip unknown", version or "unknown")
            return worker
        if worker.status is not WorkerStatus.ONLINE:
            log.info("worker %s is back online", hostname)
        worker.last_seen = now
        worker.status = WorkerStatus.ONLINE
        if ip:
            worker.ip_address = ip
        if version:
            worker.version = version
        return worker

    def assign(self, session: Session, hostname: str, task_id: int) -> None:
        """Remember the task the worker is holding."""
        worker = session.get(Worker, hostname)
        if worker is not None:
            worker.current_task_id = task_id

    def release(self, session: Session, hostname: str | None, task_id: int) -> None:
        """The worker no longer holds ``task_id`` (finished, failed or timed out)."""
        worker = session.get(Worker, hostname) if hostname else None
        if worker is not None and worker.current_task_id == task_id:
            worker.current_task_id = None

    def mark_stale_offline(self, session: Session, now: datetime) -> list[str]:
        """ONLINE workers that have not been heard of for ``offline_after`` become OFFLINE; returns their names."""
        stale = [
            Worker.status == WorkerStatus.ONLINE,
            Worker.last_seen < now - self.offline_after,
        ]
        names = list(session.scalars(select(Worker.hostname).where(*stale)))
        if names:
            session.execute(update(Worker).where(*stale).values(status=WorkerStatus.OFFLINE))
        return names

    # ------------------------------------------------------------------------------------------- public
    def heartbeat(self, hostname: str, ip: str | None = None, version: str | None = None) -> None:
        name = clean_hostname(hostname)
        with self.db.write_session() as session:
            self.touch(session, name, ip=ip, version=version)

    def list_workers(self) -> list[WorkerView]:
        now = self.clock()
        with self.db.read_session() as session:
            rows = list(session.scalars(select(Worker)))
        views = [WorkerView.from_row(row, now, self.offline_after) for row in rows]
        return sorted(views, key=lambda view: view.hostname.lower())

    def count_online(self) -> int:
        cutoff = self.clock() - self.offline_after
        with self.db.read_session() as session:
            return session.scalar(
                select(func.count()).select_from(Worker).where(Worker.status == WorkerStatus.ONLINE, Worker.last_seen >= cutoff)
            ) or 0
