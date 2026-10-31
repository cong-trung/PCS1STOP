"""The task queue: create tasks, hand them to workers atomically, follow them through their life cycle.

    PENDING -> ASSIGNED -> RUNNING -> COMPLETED | FAILED | TIMEOUT

PC1 is the single source of truth: every step is one short ``BEGIN IMMEDIATE`` transaction, so two workers asking
for work at the same moment can never receive the same task.  The steps a worker repeats after a network error
(``start``, ``complete``, ``fail``) are idempotent for the worker that owns the task.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from database import Database, Task, TaskStatus

from .clock import Clock, utcnow
from .errors import InvalidRequest, InvalidTransition, TaskNotFound, WorkerMismatch
from .views import TaskView
from .worker_service import WorkerService, clean_hostname

log = logging.getLogger("pcs.tasks")

MAX_TASK_TYPE_LENGTH = 64
MAX_JSON_BYTES = 256 * 1024
MAX_ERROR_LENGTH = 4000
#: ids / payloads per ``IN (...)`` query (older SQLite builds allow only 999 variables in one statement)
_CHUNK = 500


def _dump(data: Mapping[str, Any] | None, what: str) -> str | None:
    """A JSON object as compact text; anything else is a client error."""
    if data is None:
        return None
    if not isinstance(data, Mapping):
        raise InvalidRequest(f"{what} must be a JSON object")
    try:
        text = json.dumps(data, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise InvalidRequest(f"{what} is not valid JSON: {exc}") from exc
    if len(text.encode("utf-8")) > MAX_JSON_BYTES:
        raise InvalidRequest(f"{what} is larger than {MAX_JSON_BYTES // 1024} KB")
    return text


def _clean_type(task_type: object) -> str:
    name = task_type.strip() if isinstance(task_type, str) else ""
    if not name:
        raise InvalidRequest("task_type must not be empty")
    if len(name) > MAX_TASK_TYPE_LENGTH:
        raise InvalidRequest(f"task_type is longer than {MAX_TASK_TYPE_LENGTH} characters")
    return name


class TaskService:
    def __init__(self, db: Database, workers: WorkerService, *, clock: Clock = utcnow) -> None:
        self.db = db
        self.workers = workers
        self.clock = clock

    # ------------------------------------------------------------------------------------------ creating
    def create_task(self, task_type: str, payload: Mapping[str, Any]) -> int:
        """Queue one task (status PENDING); returns its id."""
        return self.create_tasks([(task_type, payload)])[0]

    def create_tasks(
        self, items: Sequence[tuple[str, Mapping[str, Any]]], *, reuse_pending: bool = False
    ) -> list[int]:
        """Queue several tasks in one transaction (all or none); returns their ids in the order given.

        With ``reuse_pending`` an item that is exactly like a task still waiting in the queue (same type, same payload,
        status PENDING) does not get a second task: it gets the id of the waiting one, and so do equal items of this
        call.  That keeps the queue short when the same checks are asked for again and again while no worker is
        there to answer.  Tasks a worker already holds are never reused - their outcome may be old or never come."""
        prepared = [(_clean_type(task_type), _dump(payload, "payload") or "{}") for task_type, payload in items]
        with self.db.write_session() as session:
            now = self.clock()
            known: dict[tuple[str, str], int | Task] = self._pending_ids(session, prepared) if reuse_pending else {}
            slots: list[int | Task] = []
            created: list[Task] = []
            for key in prepared:
                slot = known.get(key)
                if slot is None:
                    slot = Task(task_type=key[0], payload=key[1], status=TaskStatus.PENDING, created_time=now)
                    created.append(slot)
                    if reuse_pending:
                        known[key] = slot
                slots.append(slot)
            session.add_all(created)
            session.flush()
            ids = [slot if isinstance(slot, int) else slot.id for slot in slots]
        log.info("queued task(s) %s%s", [task.id for task in created], " (the rest reused)" if len(created) < len(ids) else "")
        return ids

    @staticmethod
    def _pending_ids(session: Session, keys: Sequence[tuple[str, str]]) -> dict[tuple[str, str], int | Task]:
        """The oldest PENDING task of every ``(type, payload)`` in ``keys`` that has one."""
        found: dict[tuple[str, str], int | Task] = {}
        payloads = sorted({payload for _, payload in keys})
        for start in range(0, len(payloads), _CHUNK):
            query = (
                select(Task.id, Task.task_type, Task.payload)
                .where(Task.status == TaskStatus.PENDING, Task.payload.in_(payloads[start : start + _CHUNK]))
                .order_by(Task.id)
            )
            for task_id, task_type, payload in session.execute(query):
                found.setdefault((task_type, payload), task_id)
        return found

    # ----------------------------------------------------------------------------------------- assigning
    def claim_next(self, worker: str, *, ip: str | None = None, version: str | None = None) -> TaskView | None:
        """Give ``worker`` the oldest PENDING task (ASSIGNED to it from now on), or ``None`` when the queue is empty.

        The worker's heartbeat is recorded in the same transaction.  A worker that asks again while it still holds
        an ASSIGNED task it never started (the answer to its last request got lost, or it was restarted) gets that very
        task back instead of a second one.  Such a repeated hand-out raises ``retry_count`` - the *delivery number*
        the worker passes to ``start_task`` - so only the newest delivery can start the task: two agents that share a
        name cannot both run it.  The ASSIGNED timeout keeps counting from the first assignment, so a worker that
        polls but never starts cannot hold a task for ever."""
        hostname = clean_hostname(worker)
        with self.db.write_session() as session:
            now = self.clock()
            self.workers.touch(session, hostname, ip=ip, version=version, now=now)
            task = session.scalars(
                select(Task)
                .where(Task.assigned_worker == hostname, Task.status == TaskStatus.ASSIGNED)
                .order_by(Task.id)
                .limit(1)
            ).first()
            if task is not None:
                task.retry_count += 1
            else:
                task = session.scalars(
                    select(Task).where(Task.status == TaskStatus.PENDING).order_by(Task.created_time, Task.id).limit(1)
                ).first()
                if task is None:
                    return None
                task.status = TaskStatus.ASSIGNED
                task.assigned_worker = hostname
                task.assigned_time = now
            self.workers.assign(session, hostname, task.id)
            view = TaskView.from_row(task)
        log.info("task %s (%s) assigned to %s (delivery %s)", view.id, view.task_type, hostname, view.retry_count)
        return view

    # ----------------------------------------------------------------------------------- life cycle steps
    def start_task(self, task_id: int, worker: str, delivery: int | None = None) -> None:
        """ASSIGNED -> RUNNING.

        ``delivery`` is the number ``claim_next`` handed out with the task; a start that carries an older number comes
        from an agent whose task was given out again, and is refused.  (``None`` skips the check.)"""
        hostname = clean_hostname(worker)
        with self.db.write_session() as session:
            now = self.clock()
            task = self._owned(session, task_id, hostname)
            if delivery is not None and delivery != task.retry_count:
                raise InvalidTransition(
                    f"task {task_id} was handed out again (delivery {delivery}, the current one is {task.retry_count})"
                )
            if task.status is TaskStatus.RUNNING:
                return  # the worker repeated a request whose answer got lost
            self._expect(task, TaskStatus.ASSIGNED, "start")
            self.workers.touch(session, hostname, now=now)
            task.status = TaskStatus.RUNNING
            task.started_time = now
            self.workers.assign(session, hostname, task.id)
        log.info("task %s started by %s", task_id, hostname)

    def complete_task(self, task_id: int, worker: str, result: Mapping[str, Any] | None = None) -> None:
        """RUNNING -> COMPLETED, storing the result."""
        hostname = clean_hostname(worker)
        result_json = _dump(result, "result")
        with self.db.write_session() as session:
            now = self.clock()
            task = self._owned(session, task_id, hostname)
            if task.status is TaskStatus.COMPLETED:
                return
            self._expect(task, TaskStatus.RUNNING, "complete")
            self.workers.touch(session, hostname, now=now)
            task.status = TaskStatus.COMPLETED
            task.completed_time = now
            task.result = result_json
            task.error_message = None
            self.workers.release(session, hostname, task.id)
        log.info("task %s completed by %s", task_id, hostname)

    def fail_task(self, task_id: int, worker: str, error: str) -> None:
        """RUNNING -> FAILED, storing the error message."""
        hostname = clean_hostname(worker)
        message = (error or "unknown error")[:MAX_ERROR_LENGTH]
        with self.db.write_session() as session:
            now = self.clock()
            task = self._owned(session, task_id, hostname)
            if task.status is TaskStatus.FAILED:
                return
            self._expect(task, TaskStatus.RUNNING, "fail")
            self.workers.touch(session, hostname, now=now)
            task.status = TaskStatus.FAILED
            task.completed_time = now
            task.error_message = message
            self.workers.release(session, hostname, task.id)
        log.warning("task %s failed on %s: %s", task_id, hostname, message)

    # -------------------------------------------------------------------------------------------- reading
    def get_task(self, task_id: int) -> TaskView:
        with self.db.read_session() as session:
            task = session.get(Task, task_id)
            if task is None:
                raise TaskNotFound(f"task {task_id} does not exist")
            return TaskView.from_row(task)

    def get_tasks(self, task_ids: Iterable[int]) -> list[TaskView]:
        """The tasks with these ids (unknown ids are left out), oldest id first."""
        ids = list(dict.fromkeys(task_ids))
        views: list[TaskView] = []
        if not ids:
            return views
        with self.db.read_session() as session:
            for start in range(0, len(ids), _CHUNK):
                rows = session.scalars(select(Task).where(Task.id.in_(ids[start : start + _CHUNK])))
                views.extend(TaskView.from_row(row) for row in rows)
        views.sort(key=lambda view: view.id)
        return views

    def list_tasks(self, status: TaskStatus | str | None = None, *, limit: int = 500) -> list[TaskView]:
        """The newest tasks first, optionally only those in one status."""
        query = select(Task).order_by(Task.id.desc()).limit(max(1, limit))
        if status is not None:
            query = query.where(Task.status == TaskStatus(status))
        with self.db.read_session() as session:
            return [TaskView.from_row(row) for row in session.scalars(query)]

    def counts_by_status(self) -> dict[TaskStatus, int]:
        with self.db.read_session() as session:
            rows = session.execute(select(Task.status, func.count()).group_by(Task.status)).all()
        counts = {status: 0 for status in TaskStatus}
        counts.update({status: total for status, total in rows})
        return counts

    # ----------------------------------------------------------------------------------------- internals
    @staticmethod
    def _owned(session: Session, task_id: int, hostname: str) -> Task:
        task = session.get(Task, task_id)
        if task is None:
            raise TaskNotFound(f"task {task_id} does not exist")
        if task.assigned_worker is None:
            raise InvalidTransition(f"task {task_id} is {task.status.value} and has not been assigned to a worker")
        if task.assigned_worker != hostname:
            raise WorkerMismatch(f"task {task_id} belongs to {task.assigned_worker}, not to {hostname}")
        return task

    @staticmethod
    def _expect(task: Task, wanted: TaskStatus, step: str) -> None:
        if task.status is not wanted:
            raise InvalidTransition(f"cannot {step} task {task.id}: it is {task.status.value}, expected {wanted.value}")
