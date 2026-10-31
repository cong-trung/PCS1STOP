"""SQLAlchemy models of the task store (the tables of architect.md)."""
from __future__ import annotations

import enum
import json
from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, Index, Integer, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from .types import EnumText, UTCDateTime


class TaskStatus(str, enum.Enum):
    PENDING = "PENDING"
    ASSIGNED = "ASSIGNED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    TIMEOUT = "TIMEOUT"


#: statuses a task never leaves
FINAL_STATUSES = frozenset({TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.TIMEOUT})
#: statuses in which a worker owns the task
ACTIVE_STATUSES = frozenset({TaskStatus.ASSIGNED, TaskStatus.RUNNING})


class WorkerStatus(str, enum.Enum):
    ONLINE = "ONLINE"
    OFFLINE = "OFFLINE"


def _in_list(enum_class: type[enum.Enum]) -> str:
    return ", ".join(f"'{member.value}'" for member in enum_class)


class Base(DeclarativeBase):
    pass


class Task(Base):
    """One unit of work for a worker.  ``payload`` / ``result`` are JSON documents kept as text."""

    __tablename__ = "tasks"
    __table_args__ = (
        CheckConstraint(f"status IN ({_in_list(TaskStatus)})", name="ck_tasks_status"),
        Index("ix_tasks_status_created", "status", "created_time"),
        Index("ix_tasks_assigned_worker", "assigned_worker"),
        {"sqlite_autoincrement": True},  # ids are never reused, so history stays unambiguous
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_type: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[TaskStatus] = mapped_column(EnumText(TaskStatus), nullable=False, default=TaskStatus.PENDING)
    assigned_worker: Mapped[str | None] = mapped_column(Text)
    created_time: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    assigned_time: Mapped[datetime | None] = mapped_column(UTCDateTime)
    started_time: Mapped[datetime | None] = mapped_column(UTCDateTime)
    completed_time: Mapped[datetime | None] = mapped_column(UTCDateTime)
    result: Mapped[str | None] = mapped_column(Text)
    error_message: Mapped[str | None] = mapped_column(Text)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")

    @property
    def payload_data(self) -> dict[str, Any]:
        return json.loads(self.payload) if self.payload else {}

    @property
    def result_data(self) -> dict[str, Any] | None:
        return json.loads(self.result) if self.result else None


class Worker(Base):
    """A PC2 agent as PC1 knows it.  PC1 is the only source of truth for this table."""

    __tablename__ = "workers"
    __table_args__ = (CheckConstraint(f"status IN ({_in_list(WorkerStatus)})", name="ck_workers_status"),)

    hostname: Mapped[str] = mapped_column(Text, primary_key=True)
    last_seen: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    status: Mapped[WorkerStatus] = mapped_column(EnumText(WorkerStatus), nullable=False, default=WorkerStatus.ONLINE)
    current_task_id: Mapped[int | None] = mapped_column(Integer)
    ip_address: Mapped[str | None] = mapped_column(Text)
    version: Mapped[str | None] = mapped_column(Text)
