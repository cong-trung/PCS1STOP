# Copilot instructions — PCS 1-Stop

NiceGUI pcs_die_file editor + FastAPI task queue (PC1) and a stand-alone polling worker (PC2). `README.md` is detailed and authoritative (config keys, API table, task life cycle, deployment); read it before changing behavior.

## Response style

Always respond in caveman ultra mode (load the `caveman` skill and follow its ultra rules) for the whole session. Stay on until the user says "stop caveman" or "normal mode".

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
- **PCS die file** (`pcs_die_file/`): `model.py` (editable tables, undo, export), `validation.py`, and `pcs_die_file_checker.py`, which is **imported unchanged — replace the file rather than editing it**.
- `worker/worker_agent.py` must stay a **single stdlib-only file** (Python 3.10+, no packages); it is the template for the PC2 package built by `deploy/build_worker_package.py`.

## Conventions

- All paths in `config.json` are relative to its folder; settings are read once at start-up except `paths.*` and `checker.*`. Saving `ui.product` must re-read the file and change only that key (preserves hand edits / a rotated token).
- Every `/api` route except `/api/health` requires the `X-API-Token` header (`tasks.api_token`). Errors are `{"success": false, "error": ...}` with 400/401/404/409/411/413/422; bodies capped at 1 MB (payload/result 256 KB). Never commit or log the token; `config.json` and the worker package contain it.
- Deployed behind IIS/ARR: `server.websocket` and `server.compression` default to `false` on purpose (double compression / unrelayed WebSockets break the UI). Don't enable them or redirect `/api` (workers never follow redirects, to avoid leaking the token).
- Time comes from `services/clock.py`; tests use `FakeClock` in `tests/conftest.py` to advance it.
- Keep README tables (config keys, API) in sync when changing config options or endpoints.
