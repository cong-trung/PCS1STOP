# Repository Guidelines

## Project Structure & Module Organization

- `app.py` - PC1 entry point: wires config to SQLite, services, FastAPI `/api`, NiceGUI pages and the scheduler.
- `api/` HTTP routes; `services/` domain logic (tasks, workers, path checks, program bins); `database/` SQLAlchemy models and SQLite session; `scheduler/` periodic jobs; `pcs_die_file/` XML model, validation and checker; `queries/` SQL templates; `ui/` and `static/` NiceGUI pages plus CSS/JS.
- `worker/worker_agent.py` - PC2 poller; must stay a single stdlib-only file (Python 3.10+). `deploy/` builds the PC2 package and holds the IIS `web.config`.
- `tests/` pytest suite; `data/` runtime queue DB (git-ignored); `DpcConfig/` alarm-map XML.

## Build, Test, and Development Commands

```powershell
uv sync                                         # install dependencies (incl. dev group)
uv run app.py [--config other.json]             # run PC1 at http://localhost:8007
uv run pytest                                   # full suite
uv run pytest tests/test_api.py::test_name -x   # single test
uv run pyflakes .                               # lint
uv run python deploy/build_worker_package.py    # build dist/PCS1Stop_worker(.zip)
```

## Coding Style & Naming Conventions

- Python 3.10+; 4-space indent; `from __future__ import annotations` and type hints in every module; `snake_case` modules/functions, `PascalCase` classes; module docstrings; `log = logging.getLogger("pcs.<area>")`.
- `pyflakes` is the only linter; no formatter is configured.

## Testing Guidelines

- pytest (`testpaths = tests`), files `tests/test_*.py`, descriptive behavior names, e.g. `test_a_missing_file_is_an_answer_not_a_failure`.
- Reuse the fixtures in `tests/conftest.py` (`FakeClock`, throw-away SQLite DB) and the rig in `tests/helpers.py`; never touch the real `data/queue.db`.
- UI changes (`ui/`, `static/`) need no tests - verify in the browser; a quick `uv run pyflakes <file>` is enough.

## Commit & Pull Request Guidelines

- Commit subjects are imperative sentences ("Add ...", "Refactor ..."), occasionally with a `feat:` / `fix:` prefix.
- PRs: describe the behavior change; keep the README config-key and API tables in sync; there is no CI, so run `uv run pytest` and `uv run pyflakes .` locally.

## Repository Rules

- `README.md` is authoritative for config keys, the API and the task life cycle; read it before changing behavior.
- `pcs_die_file/pcs_die_file_checker.py` is imported unchanged - replace the file rather than editing it.
- Never commit or log the API token (`config.json`); keep `server.websocket` and `server.compression` `false` (IIS/ARR deployment).
