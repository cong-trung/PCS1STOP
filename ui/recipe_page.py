"""The recipe page - NiceGUI edition of the old WinForms window (same look, same behaviour as PCS1Stop).

Flow: Browse (upload a recipe XML) -> tables are parsed out and checked with recipe_checker -> turn Edit on, change
cells (Ctrl+C / Ctrl+X / Ctrl+V / Ctrl+Z / Ctrl+Y all work), add rows to the XOFYBIN / List Workflow tables (Add row)
and delete rows (trash icon) -> Save downloads the edited XML.

The recipe is checked again a moment after every change; Save stays disabled while the check reports errors.

The workflow paths are always checked, nobody has to ask: when a recipe is opened, when a path is added or changed,
and when the Product box changes, every path that has not been checked yet goes to a PC2 worker as one ``check_path``
task.  The task payload is the Path plus the Product box value (SST / HST); the worker's answer shows in the read-only
Check column.  The answers are information only - they never block Save.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time

from nicegui import events, run, ui

from config import PRODUCTS, ConfigError, Settings
from recipe import validation as rv
from recipe.model import (
    ADD_ROW_TABLES,
    COLUMNS,
    DELETE_ROW_TABLES,
    TABLES,
    CellEdit,
    CellUpdate,
    EditResult,
    RecipeDoc,
    RecipeError,
)
from services import Services
from services import path_checks as pc

from .common import TEAL, add_assets, notify

log = logging.getLogger("pcs.ui")

#: seconds the recipe has to stay untouched before it is checked again
RECHECK_DELAY = 0.8
#: seconds between two looks at the path-check tasks while some are unfinished
PATH_POLL_SECONDS = 1.5
#: seconds to wait before the paths are sent again after the queue (database) could not be reached: grows with every
#: failure in a row up to the last value, and goes on trying at that pace
PATH_RETRY_SECONDS = (5, 10, 20, 40, 60)

GRID_TITLES = {
    "workflow": "List Workflow",
    "parametric": "PARAMETRIC Rule",
    "xofy": "XOFYBIN Rule",
}
#: the cell that gets the cursor in a freshly added row
FIRST_CELL = {"xofy": "bins", "workflow": "key"}
#: id of the trash-icon column (it has no data field)
TRASH_COL = "_del"
#: the Check column colours its text by what the worker found (pcs.checkKind in pcs_grid.js decides ok / bad / ...)
CHECK_CLASS_RULES = (
    '{"pcs-sel": p => window.pcs.inRange(p), '
    '"pcs-chk-ok": p => window.pcs.checkKind(p.value) === "ok", '
    '"pcs-chk-bad": p => window.pcs.checkKind(p.value) === "bad", '
    '"pcs-chk-warn": p => window.pcs.checkKind(p.value) === "warn", '
    '"pcs-chk-wait": p => window.pcs.checkKind(p.value) === "wait"}'
)
#: per-column AG Grid options (flex = share of the free width)
COLUMN_OPTIONS: dict[str, dict[str, dict]] = {
    "workflow": {
        "key": {"flex": 2, "minWidth": 140},
        "path": {"flex": 5, "minWidth": 220},
        "check": {"flex": 2.5, "minWidth": 150, ":cellClassRules": CHECK_CLASS_RULES},
    },
    "parametric": {
        "group": {"flex": 2, "minWidth": 110},
        "parameter_name": {"flex": 3, "minWidth": 130},
        "upper_limit": {"flex": 1.5, "minWidth": 90},
        "action": {"flex": 2, "minWidth": 110},
        "config_key": {"flex": 2.5, "minWidth": 130},
    },
    "xofy": {
        "valid_operations": {"width": 135, "minWidth": 100},
        "bins": {"flex": 4, "minWidth": 240, "wrapText": True, "autoHeight": True},
        "x": {"width": 70, "minWidth": 50},
        "y": {"width": 70, "minWidth": 50},
        "rule_name": {"flex": 2.5, "minWidth": 170},
        "clear_scope": {"flex": 1.5, "minWidth": 110},
        "action": {"flex": 1.5, "minWidth": 110},
        "alarm_config_key": {"flex": 2.5, "minWidth": 160},
        "comment": {"flex": 2, "minWidth": 140},
        "bin_or_token": {"flex": 2, "minWidth": 130},
        "error": {"flex": 3.5, "minWidth": 220},
    },
}


def column_defs(table: str) -> list[dict]:
    defs = [
        {
            "field": f,
            "headerName": h,
            "tooltipField": f,
            **COLUMN_OPTIONS[table].get(f, {}),
        }
        for f, h in COLUMNS[table]
    ]
    if (
        table in DELETE_ROW_TABLES
    ):  # the trash icon of every row (faded until Edit mode is on, see pcs_grid.js / pcs.css)
        defs.append(
            {
                "colId": TRASH_COL,
                "headerName": "",
                "width": 44,
                "minWidth": 44,
                "maxWidth": 44,
                "pinned": "right",
                "sortable": False,
                "resizable": False,
                "suppressMovable": True,
                "editable": False,
                "suppressNavigable": True,
                "cellClass": "pcs-trashcell",
                ":cellRenderer": "p => window.pcs.trashCell(p)",
            }
        )
    return defs


class RecipePage:
    """One browser tab = one instance (own document, own undo stack)."""

    def __init__(self, services: Services, settings: Settings) -> None:
        self.services = services
        self.cfg = settings
        self.doc: RecipeDoc | None = None
        self.edit_mode = False
        self.busy = False
        self.product = settings.product
        self.check: rv.CheckResult | None = None  # the last finished check ...
        self.checked_doc: RecipeDoc | None = None  # ... of this document ...
        self.checked_rev = -1  # ... as it was in this revision
        self.recheck_at: float | None = (
            None  # monotonic time at which the automatic re-check is due
        )
        self.check_lock = asyncio.Lock()
        # the workflow path checks (see the module docstring)
        self.tracker: pc.PathCheckTracker | None = None  # follows the tasks of the open recipe for the current product
        self.paths_at: float | None = None  # monotonic time at which new paths are due to be sent to a worker
        self.next_poll = 0.0  # monotonic time of the next look at the unfinished tasks
        self.path_lock = asyncio.Lock()  # one database round trip of the tracker at a time
        self.path_failures = 0  # times in a row the queue could not be reached when the paths were sent
        self.report_paths = False  # True until the checks of an upload / product change have been reported
        self.grids: dict[str, ui.aggrid] = {}
        self.needs_doc: list = []

    # ------------------------------------------------------------------------------------- layout
    def build(self) -> None:
        ui.page_title("PCS Recipe Checker")
        ui.colors(primary=TEAL)
        add_assets("pcs.css", "pcs_grid.js")
        with ui.column().classes("pcs-root"):
            self._toolbar()
            self._path_row()
            self._action_row()
            self._grid_row()
            self._rule_tabs()
            self._status_bar()
        ui.on("pcs_bulk", self.on_bulk)
        ui.on("pcs_add_row", self.on_add_row)
        ui.on("pcs_delete_row", self.on_delete_row)
        ui.on("pcs_undo", lambda: self.undo())
        ui.on("pcs_redo", lambda: self.redo())
        ui.on("pcs_export", lambda: self.export())
        ui.on(
            "pcs_notice",
            lambda e: notify(e.args.get("message", ""), e.args.get("type", "info")),
        )
        ui.timer(0.3, self.recheck_tick)
        ui.timer(0.5, self.path_tick)
        self.refresh_buttons()

    def _toolbar(self) -> None:
        with ui.row().classes("pcs-toolbar w-full items-center no-wrap gap-1"):

            def tool(icon, tip, color, **kw):
                b = (
                    ui.button(icon=icon, **kw)
                    .props(f"flat dense round color={color}")
                    .tooltip(tip)
                )
                return b

            tool("folder_open", "Open recipe (Ctrl+O)", "amber-9").on(
                "click", js_handler="() => pcs.pickFile()"
            )
            self.tb_edit = tool(
                "edit",
                "Edit mode on / off",
                "blue-grey-8",
                on_click=lambda: self.toggle_edit(),
            )
            self.tb_save = tool(
                "save_alt",
                "Save - download the edited XML (Ctrl+S)",
                "green-8",
                on_click=lambda: self.export(),
            )
            self.tb_validate = tool(
                "fact_check",
                "Validate with recipe_checker",
                "teal-8",
                on_click=lambda: self.validate(),
            )
            self.tb_error = tool(
                "cancel",
                "Show the Error tab",
                "red-7",
                on_click=lambda: self.tabs.set_value("Error"),
            )
            self.needs_doc += [self.tb_edit, self.tb_validate, self.tb_error]
            ui.space()
            tool(
                "list_alt",
                "Tasks - the worker queue (opens in a new tab)",
                "blue-grey-8",
            ).props("href=/tasks target=_blank")
            tool(
                "dns",
                "Workers - the PC2 machines (opens in a new tab)",
                "blue-grey-8",
            ).props("href=/workers target=_blank")
            (
                ui.select(
                    list(PRODUCTS),
                    value=self.product,
                    label="Product",
                    on_change=self.on_product,
                )
                .props("dense outlined options-dense")
                .classes("pcs-product")
                .tooltip("Product: SST or HST - sent to the workers with every path check")
            )

    def _path_row(self) -> None:
        self.upload = (
            ui.upload(
                on_upload=self.on_upload,
                auto_upload=True,
                max_files=1,
                max_file_size=200 * 1024 * 1024,
            )
            .props('accept=".xml,application/xml,text/xml" flat')
            .classes("hidden")
        )
        ui.add_body_html(f"<script>window.pcs.uploadId = {self.upload.id};</script>")
        with ui.row().classes("pcs-pathrow w-full items-center no-wrap gap-2"):
            ui.label("File Path").classes("font-bold")
            self.path_input = (
                ui.input().props("readonly dense outlined").classes("flex-grow")
            )
            ui.button("Browse").props("unelevated no-caps").on(
                "click", js_handler="() => pcs.pickFile()"
            )

    def _action_row(self) -> None:
        with ui.row().classes("pcs-actions w-full items-center no-wrap"):
            self.info_label = ui.label("No recipe loaded - click Browse.").classes(
                "pcs-info"
            )
            ui.space()
            self.save_hint = ui.label().classes("pcs-savehint")
            self.save_hint.set_visibility(False)
            self.edit_btn = (
                ui.button("Edit", on_click=lambda: self.toggle_edit())
                .props("outline no-caps dense")
                .classes("w-28")
            )
            self.save_btn = (
                ui.button("Save", on_click=lambda: self.export())
                .props("outline no-caps dense")
                .classes("w-20")
            )
            self.needs_doc += [self.edit_btn]

    def make_grid(self, table: str) -> ui.aggrid:
        options = {
            "columnDefs": column_defs(table),
            "rowData": [],
            "context": {"key": table},
            ":getRowId": "p => p.data.id",
            "defaultColDef": {
                "sortable": True,
                "resizable": True,
                ":editable": "p => window.pcs.isEditable(p)",
                ":cellClassRules": '{"pcs-sel": p => window.pcs.inRange(p), "pcs-editable": p => window.pcs.isEditable(p)}',
            },
            ":rowClassRules": '{"pcs-row-err": p => !!p.data && !!p.data.error && p.data.error !== "OK", '
            '"pcs-row-ok": p => !!p.data && p.data.error === "OK"}',
            "stopEditingWhenCellsLoseFocus": True,
            "rowHeight": 28,
            "headerHeight": 30,
            "tooltipShowDelay": 500,
            "suppressFieldDotNotation": True,
            "animateRows": False,
            "overlayNoRowsTemplate": '<span class="pcs-empty">Click Browse and pick a recipe XML to load it here.</span>',
        }
        grid = (
            ui.aggrid(options, theme="balham")
            .classes("pcs-grid")
            .props(f"data-pcs={table}")
        )
        grid.on(
            "cellValueChanged",
            lambda e, t=table: self.on_cell_changed(t, e),
            args=["rowId", "colId", "newValue", "oldValue", "source"],
        )
        self.grids[table] = grid
        return grid

    def _section_title(self, table: str) -> None:
        """Table caption; the tables that accept new rows get an Add row button next to it."""
        with ui.row().classes("pcs-sectionrow items-center no-wrap gap-2"):
            ui.label(GRID_TITLES[table]).classes("pcs-section")
            if table in ADD_ROW_TABLES:
                btn = (
                    ui.button("Add row", icon="add")
                    .props("flat dense no-caps size=sm color=primary")
                    .classes("pcs-addrow")
                    .tooltip(
                        "Insert a new row below the selected row (Ctrl+Z removes it)"
                    )
                    .on("click", js_handler=f"() => pcs.addRow('{table}')")
                )
                self.needs_doc.append(btn)

    def _grid_row(self) -> None:
        with ui.row().classes("pcs-grids w-full"):
            for table in ("workflow", "parametric"):
                with ui.column().classes("pcs-gridbox"):
                    self._section_title(table)
                    with ui.element("div").classes("pcs-gridwrap w-full"):
                        self.make_grid(table)

    def _rule_tabs(self) -> None:
        with ui.column().classes("pcs-rule w-full"):
            self._section_title("xofy")
            with ui.tabs(
                value="Rule", on_change=lambda e: self.show_tab(e.value)
            ).props("dense align=left no-caps") as self.tabs:
                ui.tab("Rule")
                with ui.tab("Error"):
                    self.err_badge = ui.badge("0", color="negative").props("floating")
                    self.err_badge.set_visibility(False)
            # Both panes stay mounted (a Quasar tab panel would detach the grid from the page while hidden,
            # and the server's updates - undo, validation - would then miss it).
            with ui.element("div").classes("pcs-panels w-full"):
                self.rule_pane = ui.element("div").classes("pcs-gridwrap w-full h-full")
                with self.rule_pane:
                    self.make_grid("xofy")
                self.err_pane = ui.element("div").classes("w-full h-full")
                with self.err_pane:
                    self.err_text = ui.label(
                        "Open a recipe to see the recipe_checker output here."
                    ).classes("pcs-errtext")
                self.err_pane.set_visibility(False)

    def show_tab(self, name: str) -> None:
        self.rule_pane.set_visibility(name == "Rule")
        self.err_pane.set_visibility(name == "Error")

    def _status_bar(self) -> None:
        bar = {"value": 0, "show_value": False}
        with ui.row().classes("pcs-status w-full items-center no-wrap gap-2"):
            self.parse_label = ui.label("Parse Status")
            self.parse_bar = (
                ui.linear_progress(**bar)
                .props("size=10px rounded color=white track-color=teal-9")
                .classes("w-24")
            )
            self.val_label = ui.label("Validation Status")
            self.val_bar = (
                ui.linear_progress(**bar)
                .props("size=10px rounded color=white track-color=teal-9")
                .classes("w-24")
            )
            ui.space()
            self.mode_label = ui.label("").classes("font-bold")

    # ------------------------------------------------------------------------------------- state helpers
    @property
    def can_save(self) -> bool:
        """Saving is allowed while the last check of the open recipe found no errors."""
        return self.doc is not None and self.check is not None and self.check.ok

    def refresh_buttons(self) -> None:
        loaded = self.doc is not None
        for el in self.needs_doc:
            el.set_enabled(loaded)
        for el in (self.tb_save, self.save_btn):
            el.set_enabled(self.can_save)
        blocked = loaded and self.check is not None and not self.check.ok
        self.save_hint.set_visibility(blocked)
        if blocked:
            why = (
                " (see the [warn] line in the Error tab)" if self.check.warnings else ""
            )
            self.save_hint.set_text(
                f"Save is off: {self.check.n_errors} validation error(s) - fix them first{why}"
            )
        on = self.edit_mode
        self.edit_btn.set_text("Stop Editing" if on else "Edit")
        if on:
            self.edit_btn.props(remove="outline")
            self.tb_edit.props(remove="flat")
        else:
            self.edit_btn.props("outline")
            self.tb_edit.props("flat")
        self.mode_label.set_text("EDIT MODE" if on else "")

    def on_product(self, e: events.ValueChangeEventArguments) -> None:
        """Remember the chosen product (SST / HST).  A check is about one product, so the open recipe's paths are
        checked again for the new one."""
        self.product = e.value
        try:
            self.cfg.update(product=e.value)
        except (OSError, ConfigError) as ex:
            notify(f"Could not remember the product in config.json: {ex}", "warning")
        if self.doc is None:
            return
        self.tracker = pc.PathCheckTracker(self.services.tasks, self.product)
        self.path_failures = 0
        self.push(self.doc.set_path_checks({}))
        self.report_paths = True
        self.schedule_path_checks(0)

    def set_progress(self, bar: ui.linear_progress, running: bool) -> None:
        if running:
            bar.props("indeterminate")
        else:
            bar.props(remove="indeterminate")
            bar.set_value(1)

    def push(self, updates: list[CellUpdate]) -> None:
        if updates:
            ui.run_javascript(
                "pcs.apply({})".format(
                    json.dumps(
                        [[u.table, u.row_id, u.field, u.value] for u in updates],
                        ensure_ascii=False,
                    )
                )
            )

    def push_rows(self, res: EditResult, *, focus: bool = False) -> None:
        """Rows the model added / took out (Add row, Delete, and Undo / Redo of them) -> the grids."""
        ops = [{"op": "remove", "table": r.table, "id": r.row_id} for r in res.removed]
        ops += [
            {
                "op": "add",
                "table": a.table,
                "row": a.row,
                "index": a.index,
                "col": FIRST_CELL.get(a.table) if focus else None,
            }
            for a in res.added
        ]
        if ops:
            ui.run_javascript(f"pcs.rows({json.dumps(ops, ensure_ascii=False)})")

    def update_info(self) -> None:
        assert self.doc is not None
        info = self.doc.info()
        self.info_label.set_text(
            f"{info['datagroup'] or '?'} - {info['name'] or '?'} v{info['version'] or '?'}   |   "
            f"{info['xofy']} XOFYBIN, {info['parametric']} PARAMETRIC, {info['workflow']} workflows"
        )

    def changed(self) -> None:
        """Call after the document content changed: the last check is out of date, the next one starts in a moment
        (and so does the look for workflow paths that nobody has checked yet)."""
        assert self.doc is not None
        ui.run_javascript(f"pcs.setDirty({'true' if self.doc.dirty else 'false'})")
        self.val_label.set_text("Validation: pending ...")
        self.val_bar.set_value(0)
        self.recheck_at = time.monotonic() + RECHECK_DELAY
        self.schedule_path_checks()

    def current_settings(self) -> Settings:
        try:
            self.cfg.reload_if_changed()
        except ConfigError as e:
            notify(f"config.json not reloaded (old values kept): {e}", "warning")
        return self.cfg

    def loading(self) -> bool:
        """True while a recipe is being opened (the user is told): the grids still show the previous one, so nothing
        may be applied to the document until the new rows are in."""
        if self.busy:
            notify("A recipe is being opened - one moment.", "info")
        return self.busy

    # ------------------------------------------------------------------------------------- loading
    async def confirm(self, message: str, yes: str) -> bool:
        with ui.dialog() as dlg, ui.card():
            ui.label(message)
            with ui.row().classes("w-full justify-end"):
                ui.button("Cancel", on_click=lambda: dlg.submit(False)).props("flat")
                ui.button(yes, on_click=lambda: dlg.submit(True)).props(
                    "unelevated color=negative"
                )
        answer = await dlg
        dlg.delete()
        return bool(answer)

    async def on_upload(self, e: events.UploadEventArguments) -> None:
        name, data = e.file.name, await e.file.read()
        self.upload.reset()
        if self.busy:
            notify(f"A recipe is still being opened - open {name} again in a moment.", "warning")
            return
        if (
            self.doc is not None
            and self.doc.dirty
            and not await self.confirm(
                f"Discard the unsaved edits in {self.doc.filename} and open {name}?",
                "Discard",
            )
        ):
            return
        await self.load(data, name)

    async def load(self, data: bytes, name: str) -> None:
        """Open a recipe, one at a time: until its rows are in the grids they still show the previous recipe, so a second
        upload (or an edit) in between would mix the two up - ``busy`` stays on until everything is in place."""
        if self.busy:
            notify(f"A recipe is still being opened - open {name} again in a moment.", "warning")
            return
        self.busy = True
        try:
            await self._open(data, name)
        finally:
            self.busy = False

    async def _open(self, data: bytes, name: str) -> None:
        self.set_progress(self.parse_bar, True)
        self.parse_label.set_text(f"Parsing {name} ...")
        try:
            doc = await run.io_bound(RecipeDoc, data, name)
        except RecipeError as ex:
            self.parse_label.set_text("Parse failed")
            notify(f"{name}: {ex}", "negative")
            return
        except Exception as ex:  # noqa: BLE001
            self.parse_label.set_text("Parse failed")
            notify(f"{name}: {type(ex).__name__}: {ex}", "negative")
            return
        finally:
            self.set_progress(self.parse_bar, False)
        self.doc, self.edit_mode = doc, False
        self.tracker = pc.PathCheckTracker(self.services.tasks, self.product)  # (the old file's checks are let go)
        self.paths_at, self.report_paths, self.path_failures = None, True, 0
        self.check, self.checked_doc, self.checked_rev, self.recheck_at = (
            None,
            None,
            -1,
            None,
        )
        ui.run_javascript("pcs.setEditMode(false); pcs.setDirty(false); pcs.reset()")
        self.path_input.set_value(name)
        await self.run_validation(initial=True)
        await self.sync_path_checks(initial=True)  # before the rows are built: they start out showing "Queued"
        for table in TABLES:
            self.grids[table].run_grid_method(
                "setGridOption", "rowData", doc.rows(table)
            )
        self.update_info()
        self.parse_label.set_text(f"Parsed {name} at {time.strftime('%H:%M:%S')}")
        self.refresh_buttons()
        notify(f"Loaded {name}", "positive")

    # ------------------------------------------------------------------------------------- validation
    def is_stale(self) -> bool:
        """True when the open recipe has changed since the last finished check."""
        return self.doc is not None and (
            self.checked_doc is not self.doc or self.checked_rev != self.doc.revision
        )

    async def recheck_tick(self) -> None:
        """Timer: check the recipe again once it has been left alone for RECHECK_DELAY seconds."""
        if (
            self.recheck_at is None
            or time.monotonic() < self.recheck_at
            or self.check_lock.locked()
        ):
            return
        self.recheck_at = None
        if self.is_stale():
            await self.run_validation()

    async def run_validation(self, *, initial: bool = False) -> None:
        async with self.check_lock:
            doc = self.doc
            if doc is None:
                return
            cfg = self.current_settings()
            rev = doc.revision
            self.set_progress(self.val_bar, True)
            self.val_label.set_text("Validating ...")
            try:
                res = await run.io_bound(
                    rv.run_check,
                    doc.current_text(),
                    doc.filename,
                    cfg.alarm_map,
                    cfg.skip_workflow_check,
                )
            finally:
                self.set_progress(self.val_bar, False)
            if self.doc is not doc:  # another file was opened meanwhile
                return
            if (
                doc.revision != rev
            ):  # edited while the checker ran: its answer is about an older state
                self.val_label.set_text("Validation: pending ...")
                self.val_bar.set_value(0)
                self.recheck_at = time.monotonic() + 0.2
                return
            self.check, self.checked_doc, self.checked_rev = res, doc, rev
            updates = doc.set_validation(res.per_rule)
            if not initial:
                self.push(updates)
            self.err_text.set_text(res.text)
            self.err_badge.set_text(str(res.n_errors))
            self.err_badge.set_visibility(not res.ok)
            self.val_label.set_text(
                ("Validation: OK" if res.ok else f"Validation: {res.n_errors} error(s)")
                + f"  ({res.seconds:.1f}s)"
            )
            self.refresh_buttons()

    async def validate(self) -> None:
        if self.loading():
            return
        if self.doc is None:
            notify("Load a recipe first.", "warning")
            return
        self.recheck_at = None
        await self.run_validation()
        if self.check is None or self.is_stale():
            return  # edited again while it ran: the automatic re-check will report
        notify(
            "No errors found."
            if self.check.ok
            else f"{self.check.n_errors} error(s) - see the Error tab.",
            "positive" if self.check.ok else "warning",
        )

    # ------------------------------------------------------------------------------------- path checks
    def schedule_path_checks(self, delay: float = RECHECK_DELAY) -> None:
        """Have a look at the workflow paths soon: every path nobody has checked yet goes to a worker."""
        self.paths_at = time.monotonic() + delay

    async def path_tick(self) -> None:
        """Timer: send the paths that are due to a worker, otherwise copy what the workers did so far."""
        if self.path_lock.locked():
            return
        now = time.monotonic()
        if self.paths_at is not None and now >= self.paths_at:
            self.paths_at = None
            await self.sync_path_checks()
        elif self.tracker is not None and self.tracker.unfinished and now >= self.next_poll:
            self.next_poll = now + PATH_POLL_SECONDS
            await self.poll_path_checks()

    async def sync_path_checks(self, *, initial: bool = False) -> None:
        """Queue one ``check_path`` task (payload: the Path and the Product box value) for every workflow path that
        has none yet.  ``initial``: the rows are not in the grids yet, so nothing has to be pushed to them."""
        async with self.path_lock:
            doc, tracker = self.doc, self.tracker
            if doc is None or tracker is None:
                return
            new = [path for path in doc.workflow_paths() if not tracker.follows(path)]
            if not new:
                return
            announce = self.report_paths
            try:
                online = await run.io_bound(self.services.workers.count_online) if announce else 1
                await run.io_bound(tracker.sync, new)
            except Exception as ex:  # noqa: BLE001 - the queue (database) is unavailable; editing must go on
                if self.doc is doc and self.tracker is tracker:  # (not when another file was opened / the product changed)
                    self.path_failures += 1
                    delay = PATH_RETRY_SECONDS[min(self.path_failures, len(PATH_RETRY_SECONDS)) - 1]
                    log.warning(
                        "could not queue the path checks (%d in a row) - trying again in %d s: %s",
                        self.path_failures, delay, ex, exc_info=self.path_failures == 1,
                    )
                    if self.paths_at is None:  # (an edit that came meanwhile has set a sooner time already)
                        self.schedule_path_checks(delay)
                    if self.path_failures == 1:  # say it once, not at every retry
                        notify(f"Could not queue the path checks: {ex} - trying again in a few seconds.", "negative")
                return
            if self.doc is not doc or self.tracker is not tracker:  # another file was opened / the product changed
                return
            self.path_failures = 0
            updates = doc.set_path_checks(tracker.texts)
            if not initial:
                self.push(updates)
            if announce and not online:
                notify(
                    f"No worker is online - the {len(new)} path check(s) wait in the queue until one connects.",
                    "warning",
                )

    async def poll_path_checks(self) -> None:
        """Copy what the workers have done so far into the Check column; say so when everything is in."""
        async with self.path_lock:
            doc, tracker = self.doc, self.tracker
            if doc is None or tracker is None or not tracker.unfinished:
                return
            try:
                texts = await run.io_bound(tracker.poll)
            except Exception as ex:  # noqa: BLE001 - try again at the next tick
                log.warning("could not read the path-check tasks: %s", ex)
                return
            if self.doc is not doc or self.tracker is not tracker:  # another file was opened / the product changed
                return
            self.push(doc.set_path_checks(texts))
            if tracker.unfinished or not self.report_paths:
                return
            self.report_paths = False
            shown = [texts[path] for path in doc.workflow_paths() if path in texts]
            notify(
                f"Paths checked for {tracker.product}: {pc.summarize(shown)}",
                "positive" if {pc.check_kind(text) for text in shown} <= {"ok"} else "warning",
            )

    # ------------------------------------------------------------------------------------- editing
    def toggle_edit(self) -> None:
        if self.doc is None or self.loading():
            return
        self.edit_mode = not self.edit_mode
        ui.run_javascript(f"pcs.setEditMode({'true' if self.edit_mode else 'false'})")
        self.refresh_buttons()

    def on_cell_changed(self, table: str, e: events.GenericEventArguments) -> None:
        a = e.args
        if self.doc is None or self.busy or a.get("source") == "pcs":  # (while busy the grid shows the old recipe)
            return
        value = "" if a.get("newValue") is None else str(a["newValue"])
        res = self.doc.apply_edits([CellEdit(table, a["rowId"], a["colId"], value)])
        echo = (table, a["rowId"], a["colId"], value)  # what the grid already shows
        self.push(
            [u for u in res.updates if (u.table, u.row_id, u.field, u.value) != echo]
        )
        if res.skipped_readonly:
            notify("That cell is read-only.", "warning")
        for msg in res.errors:
            notify(msg, "negative")
        if res.applied:
            self.changed()

    def on_bulk(self, e: events.GenericEventArguments) -> None:
        a = e.args
        if self.doc is None or self.loading():
            return
        if not self.edit_mode:
            notify("Edit mode is off - press Edit to change cells.", "warning")
            return
        table = a.get("table")
        if table not in TABLES:
            return
        res = self.doc.apply_edits(
            [
                CellEdit(
                    table,
                    str(c["id"]),
                    str(c["field"]),
                    "" if c.get("value") is None else str(c["value"]),
                )
                for c in a.get("cells", [])
            ]
        )
        self.push(res.updates)
        parts = (
            [f"{res.applied} cell(s) changed"] if res.applied else ["Nothing changed"]
        )
        if res.skipped_readonly:
            parts.append(f"{res.skipped_readonly} read-only skipped")
        notify(", ".join(parts), "positive" if res.applied else "info")
        if res.applied:
            self.changed()

    def on_add_row(self, e: events.GenericEventArguments) -> None:
        if self.doc is None or self.loading():
            return
        table, after = e.args.get("table"), e.args.get("after")
        try:
            res = self.doc.add_row(table, after)
        except RecipeError as ex:
            notify(str(ex), "warning")
            return
        if not self.edit_mode:  # adding a row means editing: switch Edit on instead of making the user find it
            self.edit_mode = True
            ui.run_javascript("pcs.setEditMode(true)")
            self.refresh_buttons()
        self.push_rows(res, focus=True)
        self.update_info()
        self.changed()
        notify("Row added - fill it in (Ctrl+Z takes it out again).", "positive")

    def on_delete_row(self, e: events.GenericEventArguments) -> None:
        if self.doc is None or self.loading():
            return
        if not self.edit_mode:  # (the grid already says so; this is for a stale click)
            notify("Edit mode is off - press Edit to delete rows.", "warning")
            return
        try:
            res = self.doc.delete_row(str(e.args.get("table")), str(e.args.get("id")))
        except RecipeError as ex:
            notify(str(ex), "warning")
            return
        self.push_rows(res)
        self.update_info()
        self.changed()
        notify("Row deleted - Ctrl+Z brings it back.", "positive")

    def undo(self) -> None:
        if not self.loading():
            self._replay(self.doc.undo() if self.doc else None, "undo")

    def redo(self) -> None:
        if not self.loading():
            self._replay(self.doc.redo() if self.doc else None, "redo")

    def _replay(self, res: EditResult | None, what: str) -> None:
        if res is None:
            notify(f"Nothing to {what}.", "info")
            return
        self.push_rows(res)
        self.push(res.updates)
        if res.added or res.removed:
            self.update_info()
        self.changed()

    # ------------------------------------------------------------------------------------- export
    async def export(self) -> None:
        if self.loading():
            return
        doc = self.doc
        if doc is None:
            notify("Nothing to save - load a recipe first.", "warning")
            return
        if (
            self.is_stale()
        ):  # changed since the last check: what is saved must be what was checked
            self.recheck_at = None
            await self.run_validation()
            if self.doc is not doc or self.is_stale():
                notify(
                    "The recipe changed while it was being checked - press Save again.",
                    "warning",
                )
                return
        if not self.check.ok:
            notify(
                f"Not saved: {self.check.n_errors} validation error(s) - fix them first (see the Error tab).",
                "negative",
            )
            return
        data = doc.export_bytes()
        ui.download.content(data, doc.export_filename(), "application/xml")
        ui.run_javascript("pcs.setDirty(false)")
        note = (
            " (re-serialized, formatting may differ)"
            if doc.export_mode == "serialize"
            else ""
        )
        notify(f"Saved {doc.export_filename()} - {len(data):,} bytes{note}", "positive")


def register(services: Services, settings: Settings) -> None:
    @ui.page("/")
    def index() -> None:
        RecipePage(services, settings).build()
