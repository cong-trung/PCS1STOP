"""Request bodies of the HTTP API (the JSON documents of architect.md)."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from services.task_service import MAX_ERROR_LENGTH, MAX_TASK_TYPE_LENGTH
from services.worker_service import MAX_HOSTNAME_LENGTH


class CreateTaskRequest(BaseModel):
    task_type: str = Field(min_length=1, max_length=MAX_TASK_TYPE_LENGTH)
    payload: dict[str, Any] = Field(default_factory=dict)


class TaskStepRequest(BaseModel):
    """Common part of start / complete / fail: which task, and who says so."""

    task_id: int = Field(ge=1)
    worker: str = Field(min_length=1, max_length=MAX_HOSTNAME_LENGTH)


class StartTaskRequest(TaskStepRequest):
    #: the ``delivery`` number /api/task/next returned with the task (optional: without it nothing is checked)
    delivery: int | None = Field(default=None, ge=0)


class CompleteTaskRequest(TaskStepRequest):
    result: dict[str, Any] | None = None


class FailTaskRequest(TaskStepRequest):
    error: str = Field(default="unknown error", max_length=MAX_ERROR_LENGTH * 4)


class HeartbeatRequest(BaseModel):
    hostname: str = Field(min_length=1, max_length=MAX_HOSTNAME_LENGTH)
    ip: str | None = Field(default=None, max_length=64)
    version: str | None = Field(default=None, max_length=64)
