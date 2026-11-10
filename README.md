# PCS 1-Stop (v2)

The PCS pcs_die_file editor (NiceGUI) with the PC1 / PC2 worker architecture of `architect.md`.

```
   PC1 (this app)                                          PC2 (worker/worker_agent.py)
+---------------------------+      HTTP, every 10 s      +---------------------------+
| NiceGUI   pcs_die_file editor   | <------------------------- | Worker agent              |
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
| `paths.alarm_map` | the `AlarmIdMapping.xml` that `pcs_die_file_checker` uses. The copy that ships with the app is `DpcConfig/V1.00/AlarmIdMapping.xml` - nothing outside this folder (no `C:\DpcConfig`) is needed. For another version put the file there and change the path |
| `checker.bin_digit_exceptions` | Bin numbers allowed although they break the digit rule (`FB` needs 4 digits, `IB` 2, `DB` 8); default `IB: [1, 5, 6, 7, 8, 9]`, `FB: [100, 500, 600, 700, 800]`, `DB: [1000000, 5000000, 6000000, 7000000, 8000000]`. The defaults only fill in what `config.json` lacks: a list in `config.json` replaces the default list of that kind, so change the lists **in `config.json`** (the next check uses them, no restart) - editing the defaults in `config.py` does nothing for a `config.json` that already has them |
| `checker.skip_workflow_check` | `true` = `pcs_die_file_checker` does not require the Workflow files to exist on PC1 (the workers check them) |
| `server.host` / `server.port` | where the app listens (restart needed). As shipped `127.0.0.1:8007` = this PC only, which is right behind IIS. `0.0.0.0` = the whole network - only for workers that connect to the port directly (that needs a firewall rule) |
| `server.public_url` | the address the workers and the users reach the app at: the IIS site, `http://ppv-pcs1stop-ss.intel.com`. `build_worker_package.py` puts it into the PC2 package. Empty = `http://<this PC>:<port>` (a direct connection). Read *Security* below |
| `server.websocket` | how the browsers stay connected. `false` (default, as shipped) = plain HTTP polling, which works behind every reverse proxy at once. `true` = WebSocket first, polling as the fallback: lighter, but IIS + ARR often cannot relay it - the browser then waits about 20 s before it falls back, or keeps retrying the WebSocket - and the PCS die file, Tasks and Workers pages stay empty meanwhile |
| `server.compression` | `false` (default, as shipped) = the app compresses nothing: NiceGUI gzips its pages and the socket.io engine gzips every polling response of 1 KB or more, and behind IIS, which compresses by itself, that makes two layers. The browser undoes only one, the NiceGUI connection receives binary garbage, drops the session and reconnects for ever - small updates get through, every big one (a pcs_die_file's tables) is lost. `true` = the app compresses (a direct connection without a proxy) |
| `ui.product` | `SST` or `HST` - the Product box (remembered here): which drive mapping the path checks use, and what the Action / Alarm Config Key pickers of the rule grid offer (see *Action and Alarm Config Key pickers*) |
| `program_bins.datasources` | ordered PyUber source names (restart needed), default `["VN_PROD_ARIES", "SVC_PROD_ARIES"]`; each lookup queries every source and deduplicates results |
| `program_bins.cleanup_at` | the time of day (`HH:MM`, 24 hours, on PC1's clock; restart needed), default `00:00`: every day at that time PC1 deletes every file in `%TEMP%\pcs1stop_bins`, where the TP lookups save their CSVs (see *TP bins*). `""` = never delete |
| `oracle.portable_dir` | legacy Oracle service setting; does not affect active PyUber lookups |
| `oracle.alias` | legacy Oracle service setting; does not affect active PyUber lookups |
| `database.path` | the SQLite task store, default `data/queue.db` (keep it on a local disk) |
| `tasks.monitor_interval_s` / `worker_offline_after_s` / `assigned_timeout_s` / `running_timeout_s` | 60 / 60 / 300 / 3600 seconds |
| `tasks.ui_refresh_s` | refresh of the Tasks and Workers pages (5) |
| `tasks.api_token` | the secret the workers send as `X-API-Token` (every `/api` call except `/api/health` needs it). Set in the shipped file; plain ASCII |

Pages: `/` pcs_die_file editor (with the *TP Name* box, see *TP bins* below), `/tasks` the queue (filters, click a row for payload / result / error), `/workers` the PC2 machines.
Logs go to the console and `data/pcs1stop.log`; at start-up the log says which alarm map is in use.
Settings are read once at start-up, except `paths.*` and `checker.*`, which are picked up by the next validation.
Changing the Product box saves `config.json` again, but reads it first and changes only `ui.product`: a hand edit - or a
token that `build_worker_package.py --rotate-token` wrote meanwhile - is kept. (A `config.json` that is broken at that
moment is left alone, and the page says so.)

**Published through IIS.** The app listens on `127.0.0.1:8007` only; IIS (URL Rewrite + ARR) publishes it at
`http://ppv-pcs1stop-ss.intel.com` on port 80, and that address is what the users and the workers use. The port itself is
not reachable from other PCs, so **no firewall rule is needed**. The site's `web.config` is `deploy/iis/web.config` (one
rewrite rule to `http://127.0.0.1:8007` - change the port there if you change `server.port`): copy it into the site's
folder. The browsers use plain HTTP polling (`server.websocket` is `false`), so IIS needs no *WebSocket Protocol* feature; a
WebSocket that IIS cannot relay leaves the pages empty. The app sends everything uncompressed (`server.compression` is
`false`): IIS compresses by itself, and a response that is compressed twice cannot be read by the browser. Workers never
follow a redirect (their token would
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
| `allowed_roots` | folders and shares a check may look at, e.g. `["Y:\\pcs", "X:\\pcs", "\\\\fileserver\\pcs_die_files"]`; empty = anywhere. It is judged on the path *after* the drive mapping (so list `Y:` / `X:`, not `M:`). Anything else fails with `not allowed` |
| `drive_map` | per product, the drive a pcs_die_file names -> the drive this PC has it as. Shipped: `{"SST": {"M": "Y"}, "HST": {"M": "X"}}` (see *Workflow path checks*); `{}` = every path is looked up as written |
| `max_stuck_tasks` | a check on a share that does not answer cannot be interrupted; after this many such stuck checks (8) the agent takes no new tasks until some are done |
| `log_file` | `worker.log` next to the agent (rotating), empty = console only. A wrong setting is also written there |

The agent polls every 10 s and works the queue off without pausing between tasks, sends a heartbeat every 20 s even
while a task runs, retries with back-off while PC1 is down and carries on by itself when it is back (a result it
could not deliver is delivered then). Command line: `python worker_agent.py [--config FILE] [--server URL]
[--token TOKEN] [--hostname NAME] [--check] [-v]`.

## Workflow path checks (pcs_die_file page -> worker)

The paths in the *List Workflow* table are **always checked, nobody has to ask**: when a pcs_die_file is uploaded, when a path
is added or changed, and when the Product box changes, every path that has not been checked yet goes to a worker as one
`check_path` task. The payload is

```json
{"path": "M:\\pcs\\prod\\workflows\\SST_PowerCycle.xml", "product": "SST"}
```

where `product` is the value of the Product box (`SST` / `HST`). The pcs_die_file names the file on the drive `M:`, but PC2 has
that share under another letter depending on the product, so **the worker maps the drive before it looks**:

| product | the pcs_die_file says | the worker looks at |
| --- | --- | --- |
| `SST` | `M:\pcs\prod\workflows\SST_PowerCycle.xml` | `Y:\pcs\prod\workflows\SST_PowerCycle.xml` |
| `HST` | `M:\pcs\prod\workflows\HST_PowerCycle.xml` | `X:\pcs\prod\workflows\HST_PowerCycle.xml` |

Only the drive letter changes (`m:` / `M:/...` count too); a path on another drive, a `\\server\share\...` path, and
`M:file` without a slash stay as written. The map is `drive_map` in `worker_config.json`, so another letter is a one-line
edit on PC2. The worker answers with `{"path", "product", "exists", "is_file", "size", "modified"}` - `path` is what the
pcs_die_file says, and `checked_path` (only when the drive was mapped) is the path that was really looked at; the Tasks page
shows both. The read-only **Check** column shows
`OK (24.1 KB)`, `Not found`, `Not a file`, `Failed: ...` or `Timeout: ...`, and `Queued`, `Assigned: PC2` or
`Checking: PC2` while a task is on its way. The two icons in the toolbar open the Tasks and Workers pages in a new tab.

Things to know:

* The answers are information only: they never block Save. A file that is not there is a *result* (`Not found`);
  `Failed` means the worker could not look (access denied, path not allowed, ...).
* A path is asked about once however often the pcs_die_file is edited; a new path costs one task, nothing else is re-sent.
  To check everything again, upload the file again (or change the product and back).
* With no worker online the cells say `Queued` and the page says so; the checks wait in the queue until a worker
  connects. Tasks that are still waiting are shared: uploading the same pcs_die_file again (or opening it in a second tab)
  does not queue them twice.
* If the queue (database) cannot be reached when the paths are sent, the page says so once and tries again by itself
  after 5, 10, 20, 40, then every 60 seconds - nobody has to upload the pcs_die_file again. While a pcs_die_file is being opened the
  page ignores edits and a second upload ("A pcs_die_file is being opened - one moment"), so the grids can never show one
  pcs_die_file while the edits go to another.
* The Product box applies to the checks: after it changes, every path is checked again for the new product.

## TP bins (TP Name box -> PyUber)

The **TP Name** box in the toolbar takes the name of a test program (TP). It never changes the open pcs_die_file; the bins it
looks up are compared with the rules (see *Missing bins* and *Unit level bins* below).
Type or paste it and leave it alone: **10 seconds after the last keystroke** the value is recorded, PC1 looks the TP's
bins up through the configured backend (PyUber by default) and saves them - and, at the same moment, the bins its units had (see *Unit level bins*). Every keystroke
starts the 10 seconds again; emptying the box cancels the wait. The status bar at the bottom follows it: `TP: query in 12 s ...`,
`TP 'EMRR1X3B13K2634A': querying program bins ...`, then `TP 'EMRR1X3B13K2634A': 13978 bin(s) queried` - or why it failed (a toast says
so too). The CSV's folder is not shown there; it is in the log.

The two TP status labels in the status bar (the TP's bins, and the bins of its units) and their progress bars move together:
every keystroke empties both bars and starts both countdowns (`TP: query in 12 s ...` and `TP Unit: query in 12 s ...`), both
switch to `querying ...` when the 10 seconds are over, and emptying the box puts both back to `TP Status` / `TP Unit Status`.
A lookup of the name before that ends while you type again does not touch them (its bins still go to their CSV and a toast
says how it went), so the bar never shows the outcome for one name next to the countdown for another.

**Datasources.** `program_bins.datasources` lists PyUber/UNIQE datasource names (not TNS aliases); defaults:
`VN_PROD_ARIES` and `SVC_PROD_ARIES`. Both lookups (the TP's own bins and the bins of its units) query every source in
order. A `(PROGRAM_NAME, BIN_NUMBER)` found in more than one source is written once, with the first source's rows first.
If a source cannot be reached or queried, the whole lookup fails and names that source; no partial CSV is written.

The old Oracle service remains in `services/program_bins.py`; the app no longer imports or selects it. The
`pcs_oracle_portable/` files remain untouched. Re-enabling that path requires restoring the old service wiring manually.

**Where the SQL is.** Active lookups read SQL files from the **`queries/`** folder next to `app.py` and execute them through
PyUber; Polars reads the query results. One `.sql` file per query (a comment on top says what it is and which bind
variables it takes):

| file | what | bind variables |
| --- | --- | --- |
| `queries/program_bins.sql` | the Test Program query of the old WinForms app: the bins a TP defines | `programname` |
| `queries/unit_tp_bins.sql` | the unit level query of the old WinForms app, cut down to the bins of its units | `programname`, `min_ww` |
| `queries/who.sql` | how the login was done (`AUTH`) and which `SERVICE` it reached - for the self-test | - |

The files are read at every lookup: edit and save, the next lookup uses the new text - no restart. A `;` at the end (as SQL
Developer writes it) is dropped. A bind variable that is no longer in the text is not passed. The TP name is always a bind
variable, never part of the SQL text. Of the three columns `program_bins.sql` returns (`PROGRAM_NAME`, `BIN_DESCRIPTION`,
`BIN_NUMBER`) the CSV keeps two and deduplicates each pair across all datasources, in configured order:

```
%TEMP%\pcs1stop_bins\bins_EMRR1X3B13K2634A.csv
PROGRAM_NAME,BIN_NUMBER
EMRR1X3B13K2634A,9103
EMRR1X3B13K2634A,91030000
...
```

`%TEMP%` is that of the user who runs PC1. The next lookup of the same TP replaces the file - also when the TP has no bins
(a toast says so; only the header is left). If the file is open in another program (Excel locks it) the bins go to
`bins_<TP>_1.csv` (`_2`, ...) instead and a toast says so: a lookup never fails for that. In the file name every character
other than letters, digits, `.`, `_` and `-` becomes `_`.

**Cleanup.** The CSVs are working files, so PC1 empties the folder every day at `program_bins.cleanup_at` (default `00:00`
on PC1's clock; `""` turns it off): every regular file in `%TEMP%\pcs1stop_bins` is deleted (`bins_<TP>.csv`,
`bins_<TP>_1.csv`, `unit_bins_<TP>.csv`, ...); the folder itself and any subfolder stay. It is a background job of the app,
not a task for a worker - a PC2 worker cannot reach PC1's `%TEMP%`. It does not run when the app starts, and a night the app
was down is not made up. A file that another program has open (Excel) cannot be deleted: it stays, `pcs1stop.log` names it
in a warning, and the next cleanup takes it. The log also says at start-up when the next cleanup is due, and after each one
how many files it deleted. A page left open over the night, with a TP looked up, then reports `the file is gone - query the
TP again` in its Missing Bins tabs until the TP is queried again.

PyUber runs through Intel's Uber/UNIQE client and requires its .NET runtime; PC1's user must have access to every configured
datasource. `queries/who.sql` remains the dormant Oracle module's self-test query; it is not part of active TP-bin lookups.

## Unit level bins (second Missing Bins tab)

Next to the **Missing Bins** tab is the tab **Missing bins based unit level data lasted 60 days**. It does the same with
other bins: not the ones the TP *defines*, but the ones its *units had* in the last 60 days, so a bin that no unit ever hits
does not show up. The lookup is part of the TP Name box: the same 10 seconds, then `queries/unit_tp_bins.sql` runs - in a
timer of its own, because it takes far longer than the TP's own bins and must not hold up the countdown. Its status has a
place of its own in the status bar: `TP 'EMRR1X3B13K2634A': querying unit level data ...`, then
`TP 'EMRR1X3B13K2634A': 412 unit level bin(s) in the last 60 days`.

* The SQL is the unit level query of the old WinForms app (`unit_tp_query`) cut down to what the bins need: `SELECT DISTINCT
  program_name, interface_bin, functional_bin, data_bin` over `A_Testing_Session` joined to `A_Device_Testing`, so the database returns
  a few thousand rows however many units the TP has. The join of those two tables and the list of operations are the old
  query's, unchanged. What the old query had besides - the joins to `A_Device_Item` and `A_Device_Item_ULT_Log`, 37 columns
  in an inline view with its own `SELECT DISTINCT` - never reached the bins and is gone, and `program_name` is compared as it
  is (`= UPPER(:programname)`; every TP name is upper case) so that the index `TS_TP_FK_I` is used. A TP with 49 000
  sessions in 30 days went from 171 s to under 1 s, with the same bins; the comment on top of the file says what must stay.
* **60 days**: the old query started at a fixed work week (`lao_start_ww >= 202521`). Here it is the bind variable `min_ww`,
  the Intel work week of *today minus 60 days* (`UNIT_DAYS` and `work_week` in `services/program_bins_common.py`; Sunday to
  Saturday, week 1 holds January 1, written YYYYWW). It is a work week, not a day: the first week is taken whole.
* **A unit counts with its most detailed bin**: `data_bin`, else `functional_bin`, else `interface_bin`, written as a whole
  number (a NUMBER that arrives as `91030000.0` is `91030000`). The bins nest - `91030000` is in `9103`, which is in `91` - so
  a unit whose bin is in a rule is covered at every level.
* The bins are saved as `%TEMP%\pcs1stop_bins\unit_bins_<TP>.csv` (`PROGRAM_NAME,BIN_NUMBER`, like `bins_<TP>.csv`; the same
  rules for a file that is open in Excel) and go through **the same check as the Missing Bins tab** - see below. The tab has
  its own badge, the status bar says `Validation: OK, 120 bin(s) missing, 15 unit level bin(s) missing`, and it never blocks
  Save either.

## Duplicate bins (Warning tab)

Validation has one more step after `pcs_die_file_checker`: it looks for XOFYBIN rules whose bins lie completely inside the
bins of another rule of the **same `Valid Operations`** (`pcs_die_file/duplicates.py`, the logic of `dupilcate.md`). It is
**only a warning**: the pcs_die_file stays valid and **Save stays on**.

* The `Bins` of every rule become ranges of the 8 digits of a DB bin: `DB88050218` = `88050218-88050218`, `FB7006` =
  `70060000-70069999` (padded with `0` for the start and `9` for the end), `IB70` = `70000000-70999999`, `FB8090-8099` =
  `80900000-80999999`, `IB9-99` = `90000000-99999999`.
* What stands in `[ ]` is a deliberate carve-out and is cut out: `IB70,[FB7056]` = `70000000-70559999` and
  `70570000-70999999`. These *effective ranges* - never the text - are what rules are compared by.
* Rules are only compared inside the same `Valid Operations` (the value of their `<RuleGroup>`): two different values -
  and a rule outside any group (`""`) against a rule of a group - never pair up, so the same bins under two operations are
  no duplicates.
* A rule whose effective ranges lie completely inside those of another rule is a duplicate of it (two rules with the same
  ranges are duplicates of each other). **Not** reported: a rule that only overlaps another one, and a rule that sits in a
  carve-out (`FB7056` next to `IB70,[FB7056]`, `DB80905871` next to `FB8090-8099,[DB80905871]`) - that is on purpose.
* Rules with `LowYield` in their name (any case) are skipped, and so is a rule whose Bins cannot be read (the checker
  reports those).

On the page the row of the duplicate turns **yellow** (red when the rule has an error as well), its **Note** cell says
`Duplicate of <RuleName>` (`+N more` after three rules), the **Warning** tab - with a badge that counts them - lists every
duplicate with its bins, its ranges and the rule it lies inside, and the status bar says `Validation: OK, 8 warning(s)`.
The Note is information like Validation Error: it is not part of the file and cannot be edited.

## Missing bins (Missing Bins tab)

When a TP has been looked up (TP Name box) the validation also compares its bins with the rules
(`pcs_die_file/missing_bins.py`): the bins that **no rule has** are listed in the **Missing Bins** tab of the XOFYBIN table,
one bin number per line (a badge counts them, the status bar says `Validation: OK, 120 bin(s) missing`). It is information
only: it never makes the check fail and **Save stays on**. The check runs again whenever the rules change and whenever
another TP is looked up; with no TP the tab says what to do. The tab **Missing bins based unit level data lasted 60 days**
(see *Unit level bins* above) runs exactly this check on the CSV of the bins the TP's units had.

* The bins are read from the CSV the lookup saved (`PROGRAM_NAME` and `BIN_NUMBER`, see above) with Polars: the file is
  scanned lazily (`polars.scan_csv`), only those two columns are read, and a bin counts once however often it is listed.
  Rows whose `BIN_NUMBER` is no number of 1 to 8 digits are left out (the tab says how many).
* A bin number stands for a range of the 8 digits of a DB bin, like the bins of a rule: `91030000` is itself (a DB), `9103`
  is `91030000-91039999` (an FB), `91` is `91000000-91999999` (an IB). It is **in a rule** when that whole range lies inside
  what the rules cover - so a rule with only `DB91030000` has that DB but not the FB `9103`.
* A rule covers its `Bins` **without the `[ ]` parts**: they are not subtracted, so `IB70,[FB7056]` covers IB70 as a whole.
* Rules with `LowYield` in their name (any case) are **not used**: they cover nearly everything and would hide every gap.
  A token of a rule that is no bin is skipped (the checker reports it).
* The list is sorted by bin, an FB before its DBs: `9103`, `91030000`, `91030001`, ..., `9104`, ...

## Quality bin tab (XOFYBIN Rule)

Next to the **Rule** tab of the XOFYBIN table is the tab **Quality bin**: the same table, with the same columns and the
same editing, but only the rules whose **X** and **Y** are both `1`. It is the same data - an edit in either tab changes
the file - and the filter follows the X / Y cells as they are edited (a rule that no longer matches leaves the tab).

## Action and Alarm Config Key pickers (XOFYBIN Rule grid)

A rule runs its `<Alarm>`s in order, each with an `Action` from `AlarmIdMapping.xml`. The Action `ConfigKey` runs the
workflows of a ConfigGroup of the **List Workflow** table - the one the alarm's `ConfigKey` attribute names - so the keys
belong to the ConfigKey actions, in the same order (`tests/EMR_MCC_Die.xml`: `Socket` -> `ConfigKey` (`Check_DUT_Orientation`)
-> `Escalate`). In **Edit mode** a double click (or Enter, or just typing) on the **Action** or **Alarm Config Key** cell of a
rule opens a picker under the cell instead of a text box:

* **Choose** (top): a search box and the choices - the `Actions` of `AlarmIdMapping.xml`, or the Keys of the List Workflow
  table (read when the picker opens, so a key you have just added is there). Click a choice to add it, click it again to add
  it twice: the real files have `Socket; Socket; Escalate`, and the same key twice in one rule (`SST_ReplaceMB`,
  `SST_ReplaceMB`, `SST_Escalate`). Enter in the search box adds the first match.
* **Order**: what is picked, in order. Drag a row to move it, `x` takes it out.
* **Apply**, Enter in an empty search box, a click outside or Tab (which then goes on to the next cell, as in a text cell)
  apply the list; Esc and **Cancel** drop it. Nothing is sent when the list did not change.

The **Product** box decides what is picked (`pcs_die_file/flow.py`):

| Product | Action cell | Alarm Config Key cell |
| --- | --- | --- |
| HST | the actions of the rule. A ConfigKey action keeps its key when it is dragged to another place, and one that is taken out takes its key with it | only for a rule that has a ConfigKey action: one slot for each of them, in the order of those actions (`x` empties a slot; there cannot be more keys than ConfigKey actions) |
| SST | not edited - it is always `ConfigKey` | the keys: one ConfigKey alarm for each key, and a key may come up again |

The list goes to the server (`pcs_flow`) and the rule gets exactly those alarms - `PcsDieFileDoc.set_flow` - in one undo
step. More alarms are copies of the last one (with the next `Order`), fewer take the last alarms out, and what else an
alarm carries (Order, BinOrToken, Severity ...) stays with its place. The server checks the picked names against the alarm map
and the List Workflow keys; a name the rule has already is always fine, so a pcs_die_file with an odd value can still be edited
(the picker shows it in orange). In SST a rule that has other actions than ConfigKey (an HST rule, or a file opened under the
wrong Product) is only changed after a question, because every one of its actions becomes ConfigKey. Paste, cut and Delete
still work on these cells as text (`a; b; c` = one value per alarm) - except on the SST Action cell, which they leave alone.

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

* **The web pages have no login.** Whoever can reach the site (`http://ppv-pcs1stop-ss.intel.com`) can upload pcs_die_files,
  queue path checks and read the queue. The app listens on `127.0.0.1` only, so IIS is the one way in: restrict there who
  may use the site (IP restrictions, authentication) if the whole network should not. Leave `/api` reachable for the
  workers - they identify themselves with the API token only, not with a Windows login.
* `tasks.api_token` protects the **worker API** (`/api`), not the pages. Workers prove themselves with that shared
  token and their hostname; it is not a per-worker credential. `config.json` and the worker package hold it:
  keep both out of source control and chat. Rotate it with `build_worker_package.py --rotate-token`.
  The worker sends the token only to `server_url` itself: the connection test asks `/api/health` *without* it first, and
  a redirect is never followed (it would carry the token on to wherever it points).
* A pcs_die_file can name any path, including `\\any-server\share\...` - a check makes the worker PC contact that server.
  Set `allowed_roots` (`--allowed-root`) to the folders and shares your pcs_die_files really use.
* The TP Name box makes PC1 query its configured datasources (PyUber/UNIQE by default; the rollback backend uses Oracle
  with PC1's own Kerberos login). Queries are read-only and the TP name is a bind variable. The 10 seconds are a
  convenience, not a rate limit. The unit-level query is the heavy one, so mind who may reach the site.
* Request bodies are size-checked before they are read, so a huge request is refused (413) with or without a token.

## Layout

```
app.py                  start-up: config, database, services, API, pages, scheduler
config.py, config.json  every path and knob (paths are relative to config.json)
DpcConfig/V1.00/        AlarmIdMapping.xml for the pcs_die_file checker
database/               SQLAlchemy models, SQLite engine (WAL, BEGIN IMMEDIATE), init_db
services/               task_service, worker_service, timeout_monitor (+ path_checks: the check_path task, its tracker;
                        program_bins_pyuber: active TP lookups; program_bins: dormant Oracle implementation)
api/                    task_api, worker_api, schemas, token check, request-size limit
scheduler/monitor.py    runs the timeout monitor every 60 s
scheduler/daily.py      runs a job once a day at a fixed time (the TP-bin CSV cleanup)
ui/                     pcs_die_file_page (the editor), tasks_page, workers_page
pcs_die_file/                 model.py (editable tables, undo, export), validation.py, duplicates.py (the duplicate-bins step), missing_bins.py (the missing-bins step, reads the TP's CSVs with Polars), flow.py (what the Action / Alarm Config Key pickers send -> the alarms of a rule), pcs_die_file_checker.py
pcs_oracle_portable/    unchanged dormant Oracle module, client and TNS files
queries/                active PyUber SQL files for program_bins and unit_tp_bins; who.sql remains for Oracle self-test
worker/                 worker_agent.py + worker_config.json (stand-alone; the template of the PC2 package)
deploy/                 build_worker_package.py, pc2/ (install / uninstall / run scripts), iis/web.config
dist/                   the built PC2 package (git-ignored: it holds the API token)
static/                 pcs_grid.js (keyboard / clipboard layer + the pickers' cell editor), pcs.css, ops.css
tests/                  uv run pytest   (Python 3.10+)
```

`pcs_die_file/pcs_die_file_checker.py` is imported unchanged - replace the file to update the checker (its own command-line default
`C:\DpcConfig\...` is not used by the app).

**Working on the UI.** A change to the pages (`ui/`, `static/`) needs no tests - do not write or run any for it, the page is
checked by eye in the browser. The rest (services, API, database, `pcs_die_file/`, the worker) keeps its tests.
