# Copilot instructions — PCS 1-Stop

NiceGUI pcs_die_file editor + FastAPI task queue (PC1) and a stand-alone polling worker (PC2). `README.md` is detailed and authoritative (config keys, API table, task life cycle, deployment); read it before changing behavior.

## Response style

Always respond in ultracave mode. At session start, read C:\Users\congtrun\.agents\skills\ultracave\SKILL.md and follow it. Stay on until the user says "stop caveman" or "normal mode".

## Commands

Python 3.10+, managed with `uv`.

```powershell
uv sync                                   # install (incl. dev group)
uv run app.py [--config other.json]       # run PC1 on http://localhost:8007
uv run pytest                             # all tests (testpaths=tests, pythonpath=.)
uv run pytest tests/test_api.py::test_name -x   # single test
uv run pyflakes .                         # lint (pyflakes is the only linter configured)
uv run python deploy/build_worker_package.py    # build dist\PCS1Stop_worker(.zip)
```

## Architecture

- **PC1 (`app.py`)** wires config → SQLite DB → services → FastAPI `/api` → NiceGUI pages (`ui/`) → scheduler. `services/container.py` holds the shared service instances.
- **PC2 never accepts connections**: `worker/worker_agent.py` polls PC1 (`GET /api/task/next` doubles as heartbeat, then `start` / `complete` / `fail`). PC1 is the single source of truth for tasks and workers.
- **Task queue** (`database/`, `services/task_service.py`): SQLAlchemy on SQLite (WAL). Assignment is one `BEGIN IMMEDIATE` transaction so a task goes to exactly one worker. Life cycle `PENDING → ASSIGNED → RUNNING → COMPLETED|FAILED|TIMEOUT`; `services/timeout_monitor.py` (run every 60 s by `scheduler/monitor.py`) times out stale tasks and marks workers offline. A `delivery` counter ensures only the newest delivery can start a task; start/complete/fail are idempotent for the owning worker.
- **Path checks** (`services/path_checks.py`): the pcs_die_file page auto-queues one `check_path` task per unchecked Workflow path; the worker maps the drive letter via `drive_map` per product (SST `M→Y`, HST `M→X`) and enforces `allowed_roots`. Results are informational and never block Save.
- **PCS die file** (`pcs_die_file/`): `model.py` (editable tables, undo, export), `validation.py`, and `pcs_die_file_checker.py`, which is **imported unchanged — replace the file rather than editing it**. After the checker, `validation.run_check` runs two more steps that are **information only** (they never turn `CheckResult.ok` off, so Save stays on): the duplicate bins (`duplicates.py`, logic of `dupilcate.md`; only rules of the same Valid Operations are compared: an XOFYBIN rule whose effective bin ranges lie inside another rule's gets a yellow row, a read-only `note` column and an entry in the Warning tab) and the missing bins (`missing_bins.py`: the bins in the TP's CSV — PROGRAM_NAME + BIN_NUMBER, scanned with Polars `scan_csv` — that no rule has, ignoring the `[ ]` parts of a rule's Bins and all LowYield rules, go to the Missing Bins tab; the CSV of the bins its units had goes through the same check into the tab next to it).
- **Action / Alarm Config Key pickers** (XOFYBIN grid): both cells open `PickerEditor` (`static/pcs_grid.js`, an AG Grid popup cell editor, grid option `popupParent` = body) instead of a text box. It never writes the cell: it sends the picked list as `pcs_flow`, and `PcsDieFilePage.on_flow` turns it into the rule's alarms with `pcs_die_file/flow.py::apply_pick` (the Product box decides: **HST** picks the actions, and the keys go to the ConfigKey actions in order; **SST** has no Action pick - every alarm is ConfigKey, one per key) and `PcsDieFileDoc.set_flow` (one structured, atomic, undoable edit - never a `;` text). Rows carry the hidden `_flow` (`[[Action, ConfigKey], ...]`, one per alarm) because the alarm cells collapse equal values; `PcsDieFilePage.push` keeps it in step. The text path (`apply_edits`, paste / Delete, `a; b; c`) still works, except on the SST Action cell (`PcsDieFilePage.locked`). Actions come from the alarm map (`flow.actions_of`), keys from the List Workflow grid.
- **TP bins** (`services/program_bins_pyuber.py`): the TP Name box records its value 10 s after the last keystroke, then the active PyUber service reads `queries/program_bins.sql` and `queries/unit_tp_bins.sql` on each lookup. Polars reads results; CSV output stays `PROGRAM_NAME,BIN_NUMBER`. The two lookups run independently through `run.io_bound`; datasource order and pair dedup are preserved. Unit bins prefer data, then functional, then interface, and use `programname` / `min_ww` binds. `scheduler/daily.py` (`DailyScheduler`, installed by `app.py::cleanup_scheduler`) runs `ProgramBinsFiles.clean_out_dir` every day at `program_bins.cleanup_at` (default `00:00` on PC1's clock, `""` = off, no run at start-up): it deletes every file in `%TEMP%\pcs1stop_bins` (the same folder the lookups save to, never a hard-coded path); a file another program has open waits for the next run. `services/program_bins.py` and `pcs_oracle_portable/` remain dormant Oracle code, unchanged and not imported by the app.
- `worker/worker_agent.py` must stay a **single stdlib-only file** (Python 3.10+, no packages); it is the template for the PC2 package built by `deploy/build_worker_package.py`.

## Conventions

- All paths in `config.json` are relative to its folder; settings are read once at start-up except `paths.*` and `checker.*`. Saving `ui.product` must re-read the file and change only that key (preserves hand edits / a rotated token).
- Every `/api` route except `/api/health` requires the `X-API-Token` header (`tasks.api_token`). Errors are `{"success": false, "error": ...}` with 400/401/404/409/411/413/422; bodies capped at 1 MB (payload/result 256 KB). Never commit or log the token; `config.json` and the worker package contain it.
- Deployed behind IIS/ARR: `server.websocket` and `server.compression` default to `false` on purpose (double compression / unrelayed WebSockets break the UI). Don't enable them or redirect `/api` (workers never follow redirects, to avoid leaking the token).
- Time comes from `services/clock.py`; tests use `FakeClock` in `tests/conftest.py` to advance it.
- Keep README tables (config keys, API) in sync when changing config options or endpoints.
- **UI changes need no tests.** When changing the pages (`ui/`, `static/`), do not write or run tests for it — the user checks the page in the browser (a quick `uv run pyflakes <file>` is fine). The rest (services, API, database, `pcs_die_file/`, the worker) keeps its tests.
