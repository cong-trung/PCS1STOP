"""The ``check_path`` task: ask a PC2 worker whether a recipe's workflow file can be reached from that machine.

* the task payload is ``{"path": <Workflow Path>, "product": "SST" | "HST"}``
* the worker's result is ``{"path", "product", "exists", "is_file", "size", "modified", "detail"?, "checked_path"?}``; a file
  that is simply not there is a *result* (``exists: false``), not a failure - FAILED is for a worker that could not look.
  The worker maps the recipe's drive M: before it looks (SST -> Y:, HST -> X:): ``path`` is what the recipe says and
  ``checked_path`` the one it looked at, present only when they differ

Only plain data and text in here: ``describe_check`` turns a task into the text of the recipe page's Check column and
``PathCheckTracker`` follows the tasks of one open recipe.  The first word of that text is what ``pcs.checkKind`` in
static/pcs_grid.js colours.
"""
from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from database import FINAL_STATUSES, TaskStatus

from .task_service import TaskService
from .views import TaskView

CHECK_PATH = "check_path"

OK = "OK"
NOT_FOUND = "Not found"
NOT_A_FILE = "Not a file"
FAILED = "Failed"
TIMEOUT = "Timeout"
QUEUED = "Queued"
ASSIGNED = "Assigned"
CHECKING = "Checking"


def check_path_payload(path: str, product: str) -> dict[str, str]:
    return {"path": path.strip(), "product": product.strip().upper()}


def format_size(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    value = size / 1024
    for unit in ("KB", "MB"):
        if value < 1024:
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def _shorten(message: str, limit: int = 90) -> str:
    one_line = " ".join(message.split())
    return one_line if len(one_line) <= limit else one_line[: limit - 1] + "…"


def _describe_result(result: dict[str, Any] | None) -> str:
    if not isinstance(result, dict) or "exists" not in result:
        return f"{FAILED}: the worker sent no usable result"
    if not result["exists"]:
        detail = result.get("detail")
        return f"{NOT_FOUND} ({_shorten(str(detail))})" if detail else NOT_FOUND
    if result.get("is_file") is False:
        return NOT_A_FILE
    size = result.get("size")
    return f"{OK} ({format_size(size)})" if isinstance(size, int) and not isinstance(size, bool) and size >= 0 else OK


def describe_check(task: TaskView) -> str:
    """Text of the Check column for one path, from the task that looks at it."""
    worker = task.assigned_worker or "worker"
    match task.status:
        case TaskStatus.PENDING:
            return QUEUED
        case TaskStatus.ASSIGNED:
            return f"{ASSIGNED}: {worker}"
        case TaskStatus.RUNNING:
            return f"{CHECKING}: {worker}"
        case TaskStatus.COMPLETED:
            return _describe_result(task.result)
        case TaskStatus.FAILED:
            return f"{FAILED}: {_shorten(task.error_message or 'unknown error')}"
        case _:
            return f"{TIMEOUT}: {worker} did not answer in time" if task.assigned_worker else f"{TIMEOUT}: no worker took it"


def check_kind(text: str) -> str:
    """``ok`` | ``bad`` | ``warn`` | ``wait`` | ``''`` - the same sorting as ``pcs.checkKind`` in the grid script."""
    if not text:
        return ""
    if text.startswith(OK):
        return "ok"
    if text.startswith((NOT_FOUND, NOT_A_FILE)):
        return "bad"
    if text.startswith((FAILED, TIMEOUT)):
        return "warn"
    return "wait"


def summarize(texts: Iterable[str]) -> str:
    """``12 OK, 2 not found, 1 failed`` for the message shown when all checks are in."""
    counts = {"ok": 0, "bad": 0, "warn": 0, "wait": 0}
    for text in texts:
        kind = check_kind(text)
        if kind:
            counts[kind] += 1
    parts = [f"{counts['ok']} OK"]
    if counts["bad"]:
        parts.append(f"{counts['bad']} not found")
    if counts["warn"]:
        parts.append(f"{counts['warn']} failed / timed out")
    if counts["wait"]:
        parts.append(f"{counts['wait']} waiting")
    return ", ".join(parts)


class PathCheckTracker:
    """Follows the ``check_path`` tasks of one open recipe for one product.

    ``sync`` makes sure every path has a task - it queues the ones that are new and leaves the rest alone, so a path is
    asked about once however often the recipe is edited - and ``poll`` reads how far the tasks are.  Both talk to the
    database, so the UI runs them in a worker thread, one at a time (the tracker itself is not thread-safe).  A new
    task is not queued when an identical one is still waiting for a worker: several tabs, or the same recipe
    uploaded again while no worker is there, share it instead of piling up more."""

    def __init__(self, tasks: TaskService, product: str) -> None:
        self.tasks = tasks
        self.product = product.strip().upper()
        self._ids: dict[str, int] = {}  # path -> the task that looks at it
        self._texts: dict[str, str] = {}  # path -> what the Check column shows
        self._open: set[str] = set()  # paths whose task has not reached a final status yet

    def follows(self, path: str) -> bool:
        """Has a task been queued for this path?"""
        return path.strip() in self._ids

    @property
    def unfinished(self) -> bool:
        """Is any of the tasks still waiting for a worker or being worked on?"""
        return bool(self._open)

    @property
    def texts(self) -> dict[str, str]:
        """path -> text of the Check column, for every path that is followed."""
        return dict(self._texts)

    def sync(self, paths: Iterable[str]) -> list[str]:
        """Queue a task for every path that is not followed yet; returns those paths."""
        new = [path for path in dict.fromkeys(p.strip() for p in paths) if path and path not in self._ids]
        if new:
            ids = self.tasks.create_tasks(
                [(CHECK_PATH, check_path_payload(path, self.product)) for path in new], reuse_pending=True
            )
            for path, task_id in zip(new, ids):
                self._ids[path] = task_id
                self._texts[path] = QUEUED
                self._open.add(path)
        return new

    def poll(self) -> dict[str, str]:
        """Read the unfinished tasks again; returns ``texts``."""
        if self._open:
            views = {view.id: view for view in self.tasks.get_tasks(self._ids[path] for path in self._open)}
            for path in sorted(self._open):
                view = views.get(self._ids[path])
                if view is None:
                    self._texts[path] = f"{FAILED}: the task is gone"
                    self._open.discard(path)
                    continue
                self._texts[path] = describe_check(view)
                if view.status in FINAL_STATUSES:
                    self._open.discard(path)
        return self.texts
