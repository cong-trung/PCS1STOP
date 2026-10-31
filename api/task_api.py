"""Task endpoints.  Workers poll these every few seconds; the UI uses the services directly."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query

from services import Services, TaskView

from .deps import get_services, require_token
from .schemas import CompleteTaskRequest, CreateTaskRequest, FailTaskRequest, StartTaskRequest

router = APIRouter(prefix="/api/task", tags=["tasks"], dependencies=[Depends(require_token)])


def _iso(value: Any) -> str | None:
    return None if value is None else value.isoformat()


def _describe(task: TaskView) -> dict[str, Any]:
    return {
        "task_id": task.id,
        "task_type": task.task_type,
        "status": task.status.value,
        "assigned_worker": task.assigned_worker,
        "created_time": _iso(task.created_time),
        "assigned_time": _iso(task.assigned_time),
        "started_time": _iso(task.started_time),
        "completed_time": _iso(task.completed_time),
        "payload": task.payload,
        "result": task.result,
        "error": task.error_message,
        "retry_count": task.retry_count,
    }


@router.post("/create")
def create_task(body: CreateTaskRequest, services: Services = Depends(get_services)) -> dict[str, Any]:
    """Queue a task (status PENDING)."""
    return {"success": True, "task_id": services.tasks.create_task(body.task_type, body.payload)}


@router.get("/next")
def next_task(
    worker: str = Query(min_length=1, max_length=255),
    services: Services = Depends(get_services),
) -> dict[str, Any]:
    """Heartbeat of ``worker`` + the oldest PENDING task, now ASSIGNED to it (``{"task": null}`` when idle).

    ``delivery`` counts how often this very task was handed out; the worker sends it back with /start."""
    task = services.tasks.claim_next(worker)
    if task is None:
        return {"task": None}
    return {"task_id": task.id, "task_type": task.task_type, "payload": task.payload, "delivery": task.retry_count}


@router.post("/start")
def start_task(body: StartTaskRequest, services: Services = Depends(get_services)) -> dict[str, Any]:
    """ASSIGNED -> RUNNING."""
    services.tasks.start_task(body.task_id, body.worker, body.delivery)
    return {"success": True}


@router.post("/complete")
def complete_task(body: CompleteTaskRequest, services: Services = Depends(get_services)) -> dict[str, Any]:
    """RUNNING -> COMPLETED, storing the result."""
    services.tasks.complete_task(body.task_id, body.worker, body.result)
    return {"success": True}


@router.post("/fail")
def fail_task(body: FailTaskRequest, services: Services = Depends(get_services)) -> dict[str, Any]:
    """RUNNING -> FAILED, storing the error message."""
    services.tasks.fail_task(body.task_id, body.worker, body.error)
    return {"success": True}


@router.get("/{task_id}")
def get_task(task_id: int, services: Services = Depends(get_services)) -> dict[str, Any]:
    """Where a task stands (for whoever created it)."""
    return _describe(services.tasks.get_task(task_id))
