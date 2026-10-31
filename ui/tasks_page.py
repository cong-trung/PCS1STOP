"""Tasks page: every task of the queue with its status, worker, age and duration (architect.md, "Tasks page")."""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from nicegui import run, ui

from config import Settings
from database import TaskStatus
from services import Services, TaskView, format_duration, local_time, utcnow

from .common import TEAL, add_assets, nav_bar

log = logging.getLogger("pcs.ui")

ALL = "All"
FILTERS = {ALL: ALL, **{status.value: status.value.title() for status in TaskStatus}}
MAX_ROWS = 500

COLUMNS = [
    {"name": "id", "label": "ID", "field": "id", "align": "left", "sortable": True},
    {"name": "type", "label": "Type", "field": "type", "align": "left", "sortable": True},
    {"name": "status", "label": "Status", "field": "status", "align": "left", "sortable": True},
    {"name": "worker", "label": "Worker", "field": "worker", "align": "left", "sortable": True},
    {"name": "created", "label": "Created", "field": "created", "align": "left", "sortable": True},
    {"name": "duration", "label": "Duration", "field": "duration", "align": "left"},
]

STATUS_SLOT = """
<q-td :props="props">
  <q-badge :color="{PENDING: 'grey-7', ASSIGNED: 'blue-7', RUNNING: 'orange-8', COMPLETED: 'green-7',
                    FAILED: 'red-7', TIMEOUT: 'deep-orange-9'}[props.value] || 'grey'">{{ props.value }}</q-badge>
</q-td>
"""


def task_rows(tasks: list[TaskView], now: datetime) -> list[dict[str, Any]]:
    return [
        {
            "id": task.id,
            "type": task.task_type,
            "status": task.status.value,
            "worker": task.assigned_worker or "-",
            "created": local_time(task.created_time),
            "duration": format_duration(task.duration(now)),
        }
        for task in tasks
    ]


def summary_text(counts: dict[TaskStatus, int]) -> str:
    return "   ·   ".join(f"{status.value.title()} {counts.get(status, 0)}" for status in TaskStatus)


def _json(data: Any) -> str:
    return "-" if data is None else json.dumps(data, indent=2, ensure_ascii=False)


def detail_lines(task: TaskView, now: datetime) -> list[tuple[str, str]]:
    """What the details dialog shows, as (label, text) pairs."""
    lines = [
        ("Task", f"#{task.id}  ({task.task_type})"),
        ("Status", task.status.value),
        ("Worker", task.assigned_worker or "-"),
        ("Created", local_time(task.created_time)),
        ("Assigned", local_time(task.assigned_time)),
        ("Started", local_time(task.started_time)),
        ("Completed", local_time(task.completed_time)),
        ("Duration", format_duration(task.duration(now))),
        ("Payload", _json(task.payload)),
        ("Result", _json(task.result)),
    ]
    if task.retry_count:
        lines.insert(3, ("Handed out", f"{task.retry_count + 1} times (the worker's earlier answer got lost, or it was restarted)"))
    if task.error_message:
        lines.append(("Error", task.error_message))
    return lines


class TasksPage:
    def __init__(self, services: Services, settings: Settings) -> None:
        self.services = services
        self.settings = settings
        self.rows: list[dict[str, Any]] = []
        self._request = 0

    def build(self) -> None:
        ui.page_title("PCS 1-Stop - Tasks")
        ui.colors(primary=TEAL)
        add_assets("pcs.css", "ops.css")
        with ui.column().classes("ops-root"):
            nav_bar("Tasks")
            with ui.column().classes("ops-body"):
                with ui.row().classes("ops-bar items-center"):
                    self.filter = ui.toggle(FILTERS, value=ALL, on_change=lambda: self.refresh()).props(
                        "unelevated no-caps dense toggle-color=primary color=white text-color=primary"
                    )
                    ui.space()
                    self.updated = ui.label("Loading ...").classes("ops-updated")
                self.summary = ui.label().classes("ops-summary")
                self.table = (
                    ui.table(
                        columns=COLUMNS,
                        rows=[],
                        row_key="id",
                        pagination={"rowsPerPage": 25, "sortBy": "id", "descending": True},
                    )
                    .classes("ops-table ops-tasks")
                    .props("flat bordered dense")
                )
                self.table.add_slot("body-cell-status", STATUS_SLOT)
                self.table.on("rowClick", self.show_details)
                ui.label(f"Newest {MAX_ROWS} tasks. Click a row for its payload and result.").classes("ops-updated")
        ui.timer(self.settings.ui_refresh, self.refresh)

    def _load(self, status: str | None) -> tuple[list[TaskView], dict[TaskStatus, int]]:
        return self.services.tasks.list_tasks(status, limit=MAX_ROWS), self.services.tasks.counts_by_status()

    async def refresh(self) -> None:
        self._request += 1
        request = self._request
        status = None if self.filter.value == ALL else self.filter.value
        try:
            tasks, counts = await run.io_bound(self._load, status)
        except Exception as exc:  # noqa: BLE001 - a busy database must not break the page, the next tick retries
            log.warning("Tasks page: could not read the queue: %s", exc)
            self.updated.set_text(f"Could not read the queue: {exc}")
            self.updated.classes(add="ops-error")
            return
        if request != self._request:  # the filter was changed while this was loading
            return
        now = utcnow()
        rows = task_rows(tasks, now)
        if rows != self.rows:
            self.rows = rows
            self.table.rows = rows
            self.table.update()
        self.summary.set_text(summary_text(counts))
        self.updated.classes(remove="ops-error")
        self.updated.set_text(f"Updated {local_time(now)}")

    async def show_details(self, event) -> None:
        try:
            task_id = int(event.args[1]["id"])
            task = await run.io_bound(self.services.tasks.get_task, task_id)
        except Exception as exc:  # noqa: BLE001
            ui.notify(f"Could not open the task: {exc}", type="negative")
            return
        with ui.dialog() as dialog, ui.card().classes("ops-detail"):
            for label, text in detail_lines(task, utcnow()):
                ui.label(label).classes("ops-key")
                if "\n" in text or label in ("Payload", "Result", "Error"):
                    ui.label(text).classes("ops-mono")
                else:
                    ui.label(text)
            with ui.row().classes("w-full justify-end"):
                ui.button("Close", on_click=dialog.close).props("flat")
        dialog.on("hide", dialog.delete)
        dialog.open()


def register(services: Services, settings: Settings) -> None:
    @ui.page("/tasks")
    def tasks_page() -> None:
        TasksPage(services, settings).build()
