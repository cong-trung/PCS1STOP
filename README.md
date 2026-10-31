# PCS 1-Stop (v2)

The PCS recipe editor (NiceGUI) with the PC1 / PC2 worker architecture of `architect.md`.

```
   PC1 (this app)                                          PC2 (worker/worker_agent.py)
+---------------------------+      HTTP, every 10 s      +---------------------------+
| NiceGUI   recipe editor   | <------------------------- | Worker agent              |
|           Tasks / Workers |   GET  /api/task/next      |  - asks for work          |
| FastAPI   /api/...        |   POST /api/task/start     |  - looks at the share     |
| SQLite    data/queue.db   |   POST /api/task/complete  |  - reports the result     |
| Timeout monitor (60 s)    |   POST /api/worker/heartbeat                           |
+---------------------------+                            +---------------------------+
```

PC2 never has to accept a connection - it only calls PC1. PC1 is the single source of truth for tasks and workers.

## Run PC1

```powershell
uv sync
uv run app.py                    # or: uv run app.py --config other.json
```

Open <http://localhost:8007> on that PC, or <http://ppv-pcs1stop-ss.intel.com> from any PC (the app never opens a browser
by itself). Everything is configured in `config.json` (see the top of `config.py` for every key); every path in it is
relative to the folder of `config.json`:

| key | meaning |
| --- | --- |
| `paths.alarm_map` | the `AlarmIdMapping.xml` that `recipe_checker` uses. The copy that ships with the app is `DpcConfig/V1.00/AlarmIdMapping.xml` - nothing outside this folder (no `C:\DpcConfig`) is needed. For another version put the file there and change the path |
| `checker.skip_workflow_check` | `true` = `recipe_checker` does not require the Workflow files to exist on PC1 (the workers check them) |
| `server.host` / `server.port` | where the app listens (restart needed). As shipped `127.0.0.1:8007` = this PC only, which is right behind IIS. `0.0.0.0` = the whole network - only for workers that connect to the port directly (that needs a firewall rule) |
| `server.public_url` | the address the workers and the users reach the app at: the IIS site, `http://ppv-pcs1stop-ss.intel.com`. `build_worker_package.py` puts it into the PC2 package. Empty = `http://<this PC>:<port>` (a direct connection). Read *Security* below |
| `ui.product` | `SST` or `HST` - the Product box (remembered here) |
| `database.path` | the SQLite task store, default `data/queue.db` (keep it on a local disk) |
| `tasks.monitor_interval_s` / `worker_offline_after_s` / `assigned_timeout_s` / `running_timeout_s` | 60 / 60 / 300 / 3600 seconds |
| `tasks.ui_refresh_s` | refresh of the Tasks and Workers pages (5) |
| `tasks.api_token` | the secret the workers send as `X-API-Token` (every `/api` call except `/api/health` needs it). Set in the shipped file; plain ASCII |

Pages: `/` recipe editor, `/tasks` the queue (filters, click a row for payload / result / error), `/workers` the PC2 machines.
Logs go to the console and `data/pcs1stop.log`; at start-up the log says which alarm map is in use.
Settings are read once at start-up, except `paths.*` and `checker.*`, which are picked up by the next validation.
Changing the Product box saves `config.json` again, but reads it first and changes only `ui.product`: a hand edit - or a
token that `build_worker_package.py --rotate-token` wrote meanwhile - is kept. (A `config.json` that is broken at that
moment is left alone, and the page says so.)

**Published through IIS.** The app listens on `127.0.0.1:8007` only; IIS (URL Rewrite + ARR) publishes it at
`http://ppv-pcs1stop-ss.intel.com` on port 80, and that address is what the users and the workers use. The port itself is
not reachable from other PCs, so **no firewall rule is needed**. Workers never follow a redirect (their token would
travel with it), so `server_url` must be the final address: do not redirect `/api` from HTTP to HTTPS - or put the
`https://` address into `server.public_url` and rebuild the package.

**After a restart.** The token does not change (it is in `config.json` on PC1 and in `worker_config.json` on PC2). PC1:
the app does not start by itself - run `uv run app.py` again; the queue survives and the workers reconnect on their own.
PC2: the worker starts again when the user who installed it logs on.

## Deploy a worker on PC2

The worker is one stand-alone Python file (Python 3.10+, no packages). `deploy/build_worker_package.py` makes the folder
and zip to copy to PC2, with PC1's address and the API token already filled in.

**1. On PC1, once**

Start the app (`uv run app.py`) and keep it running, then open <http://ppv-pcs1stop-ss.intel.com/api/health> from another
PC: it must show `{"status": "ok", ...}`. That is all - the app is reached through IIS, so there is nothing to open in
the firewall.

**2. Build the package** (again whenever the address or the token changes)

```powershell
uv run python deploy/build_worker_package.py
```

writes `dist\PCS1Stop_worker\` and `dist\PCS1Stop_worker.zip`. The address in the package is `server.public_url` of
`config.json`; `--server http://other-address` overrides it. If the app listens on this PC only and there is no
`public_url`, there is no address PC2 could use and the build stops and says so.
Other options: `--rotate-token` (make a new token, save it in `config.json` *and* put it in the package - restart PC1 and
redeploy the workers afterwards), `--allowed-root FOLDER` (repeatable: the folders and shares a check may look at),
`--out DIR`. The package holds the API token: treat the zip like a password.

The package is built in a staging folder and moved into place only when it is complete and checked, so a failed build
(disk full, the old folder still open in a window or Explorer) leaves the previous package untouched; a
`PCS1Stop_worker` folder that the script did not make is never replaced. The token is saved to `config.json` only
after the build worked.

**3. On PC2**

Copy the zip, unzip it somewhere permanent (for example `D:\PCS1StopWorker`) and double-click **`install.bat`**. It

1. looks for Python 3.10+ (and says where to get it if there is none),
2. tests the connection to PC1 with the settings of the package (`worker_agent.py --check`) and stops with a clear
   message if PC1 cannot be reached or refuses the token,
3. creates the scheduled task **PCS1Stop Worker** - starts at every logon, runs without a window, restarted every minute
   if it stops - and starts it.

Then <http://ppv-pcs1stop-ss.intel.com/workers> lists the PC as ONLINE. There is one worker per PC: installing again (from a new
copy of the package, say) replaces the installed one - the script says which one it replaces. Also in the folder:
`uninstall.bat` (removes the task and stops the worker - only when it was installed from *that* folder, so an old copy
cannot remove the worker of a newer one; `uninstall.bat -Force` removes it anyway), `run.bat` (runs the worker in a window
with the first Python 3.10+ it finds; `run.bat --check` only tests the connection), `README.txt`.

Good to know: the worker runs *as the user who installed it*, and a mapped drive such as `Y:\` or `X:\` exists only for a
logged-on user - so install it as the user who has the drives, or use `\\server\share` paths. Checks that say
`Not found` for paths that do exist almost always mean the worker's user cannot see that drive or share
(`run.bat --check` and `install.bat` warn when `Y:` or `X:` is missing).

`worker_config.json` (PC2):

| key | meaning |
| --- | --- |
| `server_url`, `api_token` | where PC1 is (the IIS address), and its `tasks.api_token` |
| `hostname` | name on the Workers page (empty = this machine's name). Run **one agent per hostname** |
| `poll_interval_s` / `heartbeat_interval_s` | 10 / 20 seconds |
| `request_timeout_s` / `task_timeout_s` | one HTTP call (15) / one task (120) |
| `max_backoff_s` / `max_delivery_s` | longest wait while PC1 is down (60) / how long a finished result is retried (3600) |
| `allowed_roots` | folders and shares a check may look at, e.g. `["Y:\\pcs", "X:\\pcs", "\\\\fileserver\\recipes"]`; empty = anywhere. It is judged on the path *after* the drive mapping (so list `Y:` / `X:`, not `M:`). Anything else fails with `not allowed` |
| `drive_map` | per product, the drive a recipe names -> the drive this PC has it as. Shipped: `{"SST": {"M": "Y"}, "HST": {"M": "X"}}` (see *Workflow path checks*); `{}` = every path is looked up as written |
| `max_stuck_tasks` | a check on a share that does not answer cannot be interrupted; after this many such stuck checks (8) the agent takes no new tasks until some are done |
| `log_file` | `worker.log` next to the agent (rotating), empty = console only. A wrong setting is also written there |

The agent polls every 10 s and works the queue off without pausing between tasks, sends a heartbeat every 20 s even
while a task runs, retries with back-off while PC1 is down and carries on by itself when it is back (a result it
could not deliver is delivered then). Command line: `python worker_agent.py [--config FILE] [--server URL]
[--token TOKEN] [--hostname NAME] [--check] [-v]`.

## Workflow path checks (recipe page -> worker)

The paths in the *List Workflow* table are **always checked, nobody has to ask**: when a recipe is uploaded, when a path
is added or changed, and when the Product box changes, every path that has not been checked yet goes to a worker as one
`check_path` task. The payload is

```json
{"path": "M:\\pcs\\prod\\workflows\\SST_PowerCycle.xml", "product": "SST"}
```

where `product` is the value of the Product box (`SST` / `HST`). The recipe names the file on the drive `M:`, but PC2 has
that share under another letter depending on the product, so **the worker maps the drive before it looks**:

| product | the recipe says | the worker looks at |
| --- | --- | --- |
| `SST` | `M:\pcs\prod\workflows\SST_PowerCycle.xml` | `Y:\pcs\prod\workflows\SST_PowerCycle.xml` |
| `HST` | `M:\pcs\prod\workflows\HST_PowerCycle.xml` | `X:\pcs\prod\workflows\HST_PowerCycle.xml` |

Only the drive letter changes (`m:` / `M:/...` count too); a path on another drive, a `\\server\share\...` path, and
`M:file` without a slash stay as written. The map is `drive_map` in `worker_config.json`, so another letter is a one-line
edit on PC2. The worker answers with `{"path", "product", "exists", "is_file", "size", "modified"}` - `path` is what the
recipe says, and `checked_path` (only when the drive was mapped) is the path that was really looked at; the Tasks page
shows both. The read-only **Check** column shows
`OK (24.1 KB)`, `Not found`, `Not a file`, `Failed: ...` or `Timeout: ...`, and `Queued`, `Assigned: PC2` or
`Checking: PC2` while a task is on its way. The two icons in the toolbar open the Tasks and Workers pages in a new tab.

Things to know:

* The answers are information only: they never block Save. A file that is not there is a *result* (`Not found`);
  `Failed` means the worker could not look (access denied, path not allowed, ...).
* A path is asked about once however often the recipe is edited; a new path costs one task, nothing else is re-sent.
  To check everything again, upload the file again (or change the product and back).
* With no worker online the cells say `Queued` and the page says so; the checks wait in the queue until a worker
  connects. Tasks that are still waiting are shared: uploading the same recipe again (or opening it in a second tab)
  does not queue them twice.
* If the queue (database) cannot be reached when the paths are sent, the page says so once and tries again by itself
  after 5, 10, 20, 40, then every 60 seconds - nobody has to upload the recipe again. While a recipe is being opened the
  page ignores edits and a second upload ("A recipe is being opened - one moment"), so the grids can never show one
  recipe while the edits go to another.
* The Product box applies to the checks: after it changes, every path is checked again for the new product.

## Task life cycle

`PENDING -> ASSIGNED -> RUNNING -> COMPLETED | FAILED | TIMEOUT`

* a worker not heard of for 60 s is shown OFFLINE
* ASSIGNED for more than 5 min (never started) -> TIMEOUT, worker released. The 5 minutes run from the *first*
  hand-out. A worker that asks again while it still holds such a task (its last answer got lost, or it was restarted)
  is handed that same task again - a new *delivery*, counted in `retry_count` and shown on the task's details
* RUNNING for more than 60 min -> TIMEOUT, worker released (this is how a crashed worker's task is cleaned up; the
  checks here take seconds, so `tasks.running_timeout_s` can safely be lowered, e.g. to 300)

Assignment is one `BEGIN IMMEDIATE` transaction on a WAL database, so a task goes to exactly one worker and nothing is lost
across restarts. `start` / `complete` / `fail` are idempotent for the owning worker, so a repeated request is harmless.
The worker passes the `delivery` it was given back to `start`; only the newest delivery may start the task, so two
agents that share a name can never both run it.

## API

All answers are JSON; errors are `{"success": false, "error": "..."}` with 400 / 401 / 404 / 409 (wrong state, worker or
delivery) / 411 / 413 / 422. Request bodies are limited to 1 MB (a task's payload and a result to 256 KB each).

| call | body / query | answer |
| --- | --- | --- |
| `POST /api/task/create` | `{"task_type", "payload": {...}}` | `{"success": true, "task_id"}` |
| `GET /api/task/next?worker=PC2` | (also the heartbeat) | `{"task_id", "task_type", "payload", "delivery"}` or `{"task": null}` |
| `POST /api/task/start` | `{"task_id", "worker", "delivery"?}` | `{"success": true}` |
| `POST /api/task/complete` | `{"task_id", "worker", "result": {...}}` | `{"success": true}` |
| `POST /api/task/fail` | `{"task_id", "worker", "error"}` | `{"success": true}` |
| `POST /api/worker/heartbeat` | `{"hostname", "ip", "version"}` | `{"success": true}` |
| `GET /api/task/{id}`, `GET /api/health` | | task details / `{"status": "ok"}` |

(`delivery` is the one addition to the messages of `architect.md`; clients that leave it out still work.)

## Security

* **The web pages have no login.** Whoever can reach the site (`http://ppv-pcs1stop-ss.intel.com`) can upload recipes,
  queue path checks and read the queue. The app listens on `127.0.0.1` only, so IIS is the one way in: restrict there who
  may use the site (IP restrictions, authentication) if the whole network should not. Leave `/api` reachable for the
  workers - they identify themselves with the API token only, not with a Windows login.
* `tasks.api_token` protects the **worker API** (`/api`), not the pages. Workers prove themselves with that shared
  token and their hostname; it is not a per-worker credential. `config.json` and the worker package hold it:
  keep both out of source control and chat. Rotate it with `build_worker_package.py --rotate-token`.
  The worker sends the token only to `server_url` itself: the connection test asks `/api/health` *without* it first, and
  a redirect is never followed (it would carry the token on to wherever it points).
* A recipe can name any path, including `\\any-server\share\...` - a check makes the worker PC contact that server.
  Set `allowed_roots` (`--allowed-root`) to the folders and shares your recipes really use.
* Request bodies are size-checked before they are read, so a huge request is refused (413) with or without a token.

## Layout

```
app.py                  start-up: config, database, services, API, pages, scheduler
config.py, config.json  every path and knob (paths are relative to config.json)
DpcConfig/V1.00/        AlarmIdMapping.xml for the recipe checker
database/               SQLAlchemy models, SQLite engine (WAL, BEGIN IMMEDIATE), init_db
services/               task_service, worker_service, timeout_monitor (+ path_checks: the check_path task, its tracker)
api/                    task_api, worker_api, schemas, token check, request-size limit
scheduler/monitor.py    runs the timeout monitor every 60 s
ui/                     recipe_page (the editor), tasks_page, workers_page
recipe/                 model.py (editable tables, undo, export), validation.py, recipe_checker.py
worker/                 worker_agent.py + worker_config.json (stand-alone; the template of the PC2 package)
deploy/                 build_worker_package.py, pc2/ (install / uninstall / run scripts)
dist/                   the built PC2 package (git-ignored: it holds the API token)
static/                 pcs_grid.js, pcs.css, ops.css
tests/                  uv run pytest   (Python 3.10+)
```

`recipe/recipe_checker.py` is imported unchanged - replace the file to update the checker (its own command-line default
`C:\DpcConfig\...` is not used by the app).
