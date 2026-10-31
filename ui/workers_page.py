"""Workers page: the PC2 machines that talk to this server and what each is doing (architect.md, "Workers page")."""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from nicegui import run, ui

from config import Settings
from database import WorkerStatus
from services import Services, TaskView, WorkerView, format_duration, local_time, utcnow

from .common import TEAL, add_assets, nav_bar

log = logging.getLogger("pcs.ui")

COLUMNS = [
    {"name": "hostname", "label": "Hostname", "field": "hostname", "align": "left", "sortable": True},
    {"name": "status", "label": "Status", "field": "status", "align": "left", "sortable": True},
    {"name": "task", "label": "Current Task", "field": "task", "align": "left"},
    {"name": "last_seen", "label": "Last Seen", "field": "last_seen", "align": "left"},
    {"name": "version", "label": "Version", "field": "version", "align": "left"},
    {"name": "ip", "label": "IP", "field": "ip", "align": "left"},
]

STATUS_SLOT = """
<q-td :props="props">
  <q-badge :color="props.value === 'ONLINE' ? 'green-7' : 'red-7'">{{ props.value }}</q-badge>
</q-td>
"""


def worker_rows(workers: list[WorkerView], tasks: dict[int, TaskView], now: datetime) -> list[dict[str, Any]]:
    rows = []
    for worker in workers:
        task = tasks.get(worker.current_task_id) if worker.current_task_id is not None else None
        if task is not None:
            current = f"#{task.id}  {task.task_type}"
        else:
            current = "-" if worker.current_task_id is None else f"#{worker.current_task_id}"
        rows.append(
            {
                "hostname": worker.hostname,
                "status": worker.status.value,
                "task": current,
                "last_seen": f"{local_time(worker.last_seen)}  ({format_duration(now - worker.last_seen)} ago)",
                "version": worker.version or "-",
                "ip": worker.ip_address or "-",
            }
        )
    return rows


def summary_text(workers: list[WorkerView]) -> str:
    online = sum(1 for worker in workers if worker.status is WorkerStatus.ONLINE)
    return f"{online} online of {len(workers)} worker(s)" if workers else "No worker has connected yet."


class WorkersPage:
    def __init__(self, services: Services, settings: Settings) -> None:
        self.services = services
        self.settings = settings
        self.rows: list[dict[str, Any]] = []

    def build(self) -> None:
        ui.page_title("PCS 1-Stop - Workers")
        ui.colors(primary=TEAL)
        add_assets("pcs.css", "ops.css")
        with ui.column().classes("ops-root"):
            nav_bar("Workers")
            with ui.column().classes("ops-body"):
                with ui.row().classes("ops-bar items-center"):
                    self.summary = ui.label().classes("ops-summary")
                    ui.space()
                    self.updated = ui.label("Loading ...").classes("ops-updated")
                self.table = (
                    ui.table(
                        columns=COLUMNS,
                        rows=[],
                        row_key="hostname",
                        pagination={"rowsPerPage": 25, "sortBy": "hostname"},
                    )
                    .classes("ops-table")
                    .props("flat bordered dense")
                )
                self.table.add_slot("body-cell-status", STATUS_SLOT)
                ui.label(
                    f"A worker is OFFLINE when it has not been heard of for "
                    f"{format_duration(self.services.workers.offline_after)}."
                ).classes("ops-updated")
        ui.timer(self.settings.ui_refresh, self.refresh)

    def _load(self) -> tuple[list[WorkerView], dict[int, TaskView]]:
        workers = self.services.workers.list_workers()
        ids = [worker.current_task_id for worker in workers if worker.current_task_id is not None]
        return workers, {task.id: task for task in self.services.tasks.get_tasks(ids)}

    async def refresh(self) -> None:
        try:
            workers, tasks = await run.io_bound(self._load)
        except Exception as exc:  # noqa: BLE001 - the next tick retries
            log.warning("Workers page: could not read the workers: %s", exc)
            self.updated.set_text(f"Could not read the workers: {exc}")
            self.updated.classes(add="ops-error")
            return
        now = utcnow()
        rows = worker_rows(workers, tasks, now)
        if rows != self.rows:
            self.rows = rows
            self.table.rows = rows
            self.table.update()
        self.summary.set_text(summary_text(workers))
        self.updated.classes(remove="ops-error")
        self.updated.set_text(f"Updated {local_time(now)}")


def register(services: Services, settings: Settings) -> None:
    @ui.page("/workers")
    def workers_page() -> None:
        WorkersPage(services, settings).build()
