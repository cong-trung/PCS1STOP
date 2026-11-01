PCS 1-Stop worker for PC2
=========================

This folder is everything a worker PC needs.  It is already set up for PC1 at

    @SERVER_URL@

and holds PC1's API token (treat this folder like a password).
Needs: Windows and Python 3.10 or newer - nothing else, no packages.
Get Python from https://www.python.org/downloads/ and tick "Add python.exe to PATH".


INSTALL (once)

  1. Unzip this folder somewhere permanent, for example D:\PCS1StopWorker.  The worker runs from here.
  2. Log on as the user the worker should run as.  It must be able to open the pcs_die_file shares / mapped
     drives (for example Y:\ for SST and X:\ for HST) - a mapped drive exists only for a logged-on user.
  3. Double-click install.bat   (or in PowerShell:  .\install.ps1 - if scripts are blocked on this PC,
     use install.bat, it works around that)
     It tests the connection to PC1, then creates the scheduled task "PCS1Stop Worker" (starts at
     every logon, no window, restarted if it stops) and starts it.
  4. Open @SERVER_URL@/workers on any PC: this PC should be listed as ONLINE.


TRY IT FIRST (optional)

  run.bat --check     tests the settings and the connection to PC1 and says what is wrong
  run.bat             runs the worker in this window (Ctrl+C stops it); uses the first Python 3.10+ it finds


SETTINGS  (worker_config.json)

  server_url      where PC1 is
  api_token       must be the same as tasks.api_token in PC1's config.json
  hostname        the name on PC1's Workers page; empty = this PC's name.  One worker per name.
  allowed_roots   the folders / shares a check may look at, for example
                  ["Y:\\pcs", "X:\\pcs", "\\\\fileserver\\pcs_die_files"]; empty = anywhere.  It is judged on the
                  path after drive_map (so list Y: / X:, not M:).
                  A path outside them fails with "not allowed".
  drive_map       pcs_die_files name their files on the drive M:; this PC has that drive under another letter,
                  depending on the product:  SST -> Y:,  HST -> X:   (M:\pcs\a.xml is looked up as
                  Y:\pcs\a.xml for SST and X:\pcs\a.xml for HST).  Only the drive letter changes.
                  To change a letter edit it here:  "drive_map": { "SST": { "M": "Y" }, "HST": { "M": "X" } }
                  ({} = paths are looked up as written).
  the rest        timings: leave them as they are unless told otherwise

  After a change: uninstall.bat, then install.bat again.


WHEN SOMETHING IS WRONG

  worker.log      (next to worker_agent.py) what the worker did, and why a check failed
  run.bat --check tests the connection

  "cannot reach PC1"       the app is not running on PC1 (it must be started again after PC1 restarts), or
                           server_url above is wrong.  Open  @SERVER_URL@/api/health  in a browser on this
                           PC: it should show {"status":"ok", ...}.
  "refused the API token"  api_token here differs from tasks.api_token on PC1.
  checks say "Not found"   the worker runs as a user who cannot see that drive or share.
  checks say "not allowed" the path is outside allowed_roots.


REMOVE

  uninstall.bat   removes the scheduled task and stops the worker (this folder stays).
                  It only removes the worker that was installed from THIS folder; if the worker was
                  installed from another copy of the package, run uninstall.bat there - or use
                  uninstall.bat -Force to remove it from here.

  install.bat replaces a worker that is installed already (there is one per PC) and says which one.
