"""Settings of PCS 1-Stop.  Every path and knob lives in ``config.json`` next to the app:

    {
      "paths":    { "alarm_map": "DpcConfig/V1.00/AlarmIdMapping.xml" },
      "checker":  { "skip_workflow_check": true },
      "server":   { "host": "127.0.0.1", "port": 8007, "public_url": "http://ppv-pcs1stop-ss.intel.com" },
      "ui":       { "product": "SST" },
      "database": { "path": "data/queue.db" },
      "tasks":    { "monitor_interval_s": 60, "worker_offline_after_s": 60, "assigned_timeout_s": 300,
                    "running_timeout_s": 3600, "ui_refresh_s": 5, "api_token": "" }
    }

paths.alarm_map             AlarmIdMapping.xml used by recipe_checker (Rule / Action / BinOrToken lookups); the copy
                            that ships with the app lives in DpcConfig/V1.00 next to this file
checker.skip_workflow_check true = do not require the Workflow files to exist on this machine
server.host / server.port   where the web app listens (restart needed).  127.0.0.1 = this PC only: right when a reverse
                            proxy on this PC (IIS) publishes the app, nothing else can reach the port.  0.0.0.0 = the
                            whole network: only for workers that connect to the port directly (it then needs a
                            firewall rule).  The app never opens a browser by itself
server.public_url           the address the workers and the users reach the app at - the site the reverse proxy
                            publishes, e.g. http://ppv-pcs1stop-ss.intel.com (a host name and maybe a port, no folder).
                            build_worker_package.py puts it into the PC2 package.  Empty = a direct connection to
                            http://<this PC's name>:<server.port>.  The web pages have NO login: whoever can reach
                            that address can upload recipes, queue checks and read the queue, so keep it on a trusted
                            network and set tasks.api_token for the API
ui.product                  SST or HST - what the Product box shows and what the workflow path checks send to the workers
database.path               the SQLite task store (restart needed); created on first start
tasks.monitor_interval_s    how often the timeout monitor runs (restart needed)
tasks.worker_offline_after_s  a worker not heard of for this long is OFFLINE
tasks.assigned_timeout_s    an ASSIGNED task the worker never started becomes TIMEOUT after this long
tasks.running_timeout_s     a RUNNING task that never finished becomes TIMEOUT after this long
tasks.ui_refresh_s          refresh interval of the Tasks and Workers pages
tasks.api_token             when not empty (plain ASCII), every /api call except /api/health needs the header
                            X-API-Token: <token>; workers send it from worker_config.json ("api_token").  It protects
                            the worker API only, not the web pages

* Relative paths are relative to the folder that holds config.json; ``~`` and ``%VAR%`` are expanded.
* In JSON a backslash must be written twice (``C:\\\\x``) - or just use forward slashes (``C:/x``).
* Missing keys fall back to the defaults below, unknown keys are kept when the file is saved, and a missing
  file is created with the defaults.  ``python app.py --config other.json`` uses a different file.
* Edits to paths.* / checker.* are picked up by the next check without restarting the app; everything else is read
  once at start-up.
"""
from __future__ import annotations

import copy
import json
import os
import tempfile
import threading
import time
from datetime import timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

HERE = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = HERE / "config.json"

#: the product lines the Product box offers
PRODUCTS = ("SST", "HST")

DEFAULTS: dict[str, dict[str, Any]] = {
    "paths": {"alarm_map": "DpcConfig/V1.00/AlarmIdMapping.xml"},  # the copy next to config.json, no C:\ needed
    "checker": {"skip_workflow_check": True},
    "server": {"host": "127.0.0.1", "port": 8080, "public_url": ""},  # (how workers on other PCs get in: see above)
    "ui": {"product": PRODUCTS[0]},
    "database": {"path": "data/queue.db"},
    "tasks": {
        "monitor_interval_s": 60,
        "worker_offline_after_s": 60,
        "assigned_timeout_s": 300,
        "running_timeout_s": 3600,
        "ui_refresh_s": 5,
        "api_token": "",
    },
}

#: tasks.* keys that are a length of time in seconds
_SECONDS = ("monitor_interval_s", "worker_offline_after_s", "assigned_timeout_s", "running_timeout_s", "ui_refresh_s")


class ConfigError(Exception):
    """config.json cannot be read or has a wrong value (the message says where and why)."""


#: one read-modify-write of a settings file at a time (the web app's threads, the package builder's call, the tests)
_WRITE_LOCK = threading.RLock()


def _replace(src: Path, dst: Path, attempts: int = 8) -> None:
    """``os.replace``, tried again for a moment: on Windows an editor, a virus scanner or a reader that has the target
    open makes the rename fail for a few milliseconds."""
    for attempt in range(1, attempts + 1):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == attempts:
                raise
            time.sleep(0.05 * attempt)


def resolve_path(value: str, base: Path) -> Path:
    """Expand ``~`` / ``%VAR%``; a relative path is taken relative to ``base`` (the config file's folder)."""
    p = Path(os.path.expandvars(os.path.expanduser(value.strip())))
    return p if p.is_absolute() else base / p


def _merge(dst: dict, src: dict) -> None:
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            _merge(dst[k], v)
        else:
            dst[k] = v


def is_site_address(url: str) -> bool:
    """``http://host`` or ``https://host:port``: a scheme, a host name and nothing after it - the app is published at the
    root of its site, a folder in the address would break its links."""
    try:
        parts = urlsplit(url)
        parts.port  # (raises ValueError for a port that is not a number or out of range)
    except ValueError:
        return False
    return (
        parts.scheme in ("http", "https")
        and bool(parts.hostname)
        and "@" not in parts.netloc
        and not parts.path.strip("/")
        and not parts.query
        and not parts.fragment
    )


def _validate(data: dict, where: str) -> None:
    def expect(section: str, key: str, types: tuple, what: str) -> Any:
        v = data[section][key]
        if isinstance(v, bool) and bool not in types or not isinstance(v, types):
            raise ConfigError(f'{where}: "{section}.{key}" must be {what}, got {json.dumps(v)}')
        return v

    expect("paths", "alarm_map", (str,), "a path string")
    expect("checker", "skip_workflow_check", (bool,), "true or false")
    expect("server", "host", (str,), "a host name / address string")
    port = expect("server", "port", (int,), "a whole number between 1 and 65535")
    if not 1 <= port <= 65535:
        raise ConfigError(f'{where}: "server.port" must be between 1 and 65535, got {port}')
    public_url = expect("server", "public_url", (str,), "a web address like http://pc1.example.com, or empty").strip()
    if public_url and not is_site_address(public_url):
        raise ConfigError(
            f'{where}: "server.public_url" must look like http://host or http://host:port (no folder after it), '
            f"got {json.dumps(public_url)}"
        )
    product = expect("ui", "product", (str,), "SST or HST")
    if product.strip().upper() not in PRODUCTS:
        raise ConfigError(f'{where}: "ui.product" must be one of {", ".join(PRODUCTS)}, got {json.dumps(product)}')
    if not expect("database", "path", (str,), "a path string").strip():
        raise ConfigError(f'{where}: "database.path" must not be empty')
    for key in _SECONDS:
        seconds = expect("tasks", key, (int, float), "a number of seconds greater than 0")
        if not 0 < seconds < 10**9:  # (also rejects NaN)
            raise ConfigError(f'{where}: "tasks.{key}" must be a number of seconds greater than 0, got {seconds}')
    token = expect("tasks", "api_token", (str,), "a string (empty = no token)").strip()
    if token and not (token.isascii() and token.isprintable()):
        raise ConfigError(f'{where}: "tasks.api_token" may only contain plain ASCII characters (it is sent in an HTTP header)')


class Settings:
    """The parsed config.json.  ``reload_if_changed`` picks up hand edits while the app is running."""

    def __init__(self, path: str | os.PathLike | None = None):
        self.path = Path(path).resolve() if path else DEFAULT_CONFIG_PATH
        self.data: dict[str, dict[str, Any]] = copy.deepcopy(DEFAULTS)
        self._stamp: int | None = None
        self.load()

    # ----------------------------------------------------------------------------------- file I/O
    def load(self) -> None:
        """(Re)read the file; creates it with the defaults if it is missing.  Keeps the old values on error."""
        if not self.path.exists():
            self.save()
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8-sig"))
        except json.JSONDecodeError as e:
            hint = " (in JSON write backslashes twice, or use forward slashes)" if "escape" in e.msg.lower() else ""
            raise ConfigError(f"{self.path.name}: line {e.lineno}, column {e.colno}: {e.msg}{hint}") from e
        except OSError as e:
            raise ConfigError(f"{self.path}: {e.strerror or e}") from e
        if not isinstance(raw, dict):
            raise ConfigError(f"{self.path.name}: the top level must be a JSON object")
        merged = copy.deepcopy(DEFAULTS)
        _merge(merged, raw)
        for section in DEFAULTS:
            if not isinstance(merged[section], dict):
                raise ConfigError(f'{self.path.name}: "{section}" must be an object')
        _validate(merged, self.path.name)
        self.data = merged
        self._stamp = self.path.stat().st_mtime_ns

    def reload_if_changed(self) -> bool:
        """True when the file was edited since the last load (raises ConfigError if the edit is broken)."""
        try:
            stamp = self.path.stat().st_mtime_ns
        except OSError:
            return False
        if stamp == self._stamp:
            return False
        self.load()
        return True

    def save(self) -> None:
        """Write ``self.data``: to a temp file next to the real one first, then renamed over it."""
        text = json.dumps(self.data, indent=2, ensure_ascii=False) + "\n"
        with _WRITE_LOCK:
            fd, name = tempfile.mkstemp(dir=self.path.parent, prefix=self.path.name + ".", suffix=".tmp")
            tmp = Path(name)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(text)
                _replace(tmp, self.path)
            except BaseException:
                tmp.unlink(missing_ok=True)
                raise
            self._stamp = self.path.stat().st_mtime_ns

    def update(self, *, alarm_map: str | None = None, skip_workflow_check: bool | None = None,
               product: str | None = None, api_token: str | None = None) -> None:
        """Change the given values and write the file.

        The file is read again first, so whatever somebody else wrote since this object last looked (a hand edit, the
        token that ``deploy/build_worker_package.py --rotate-token`` made) is kept: only the given values change.  A file
        that is broken right now is left alone and ConfigError says why."""
        if api_token is not None:
            token = api_token.strip()
            if token and not (token.isascii() and token.isprintable()):
                raise ConfigError("the API token may only contain plain ASCII characters (it is sent in an HTTP header)")
        with _WRITE_LOCK:
            if self.path.exists():
                self.load()
            data = copy.deepcopy(self.data)
            if alarm_map is not None:
                data["paths"]["alarm_map"] = alarm_map.strip()
            if skip_workflow_check is not None:
                data["checker"]["skip_workflow_check"] = bool(skip_workflow_check)
            if product is not None:
                data["ui"]["product"] = product.strip().upper()
            if api_token is not None:
                data["tasks"]["api_token"] = token
            _validate(data, self.path.name)  # a wrong value never reaches the file
            self.data = data
            self.save()

    # ----------------------------------------------------------------------------------- values
    @property
    def alarm_map_raw(self) -> str:
        """The alarm-map path exactly as written in config.json."""
        return self.data["paths"]["alarm_map"]

    @property
    def alarm_map(self) -> str:
        """Absolute alarm-map path ('' when none is configured)."""
        raw = self.alarm_map_raw
        return str(resolve_path(raw, self.path.parent)) if raw.strip() else ""

    @property
    def skip_workflow_check(self) -> bool:
        return self.data["checker"]["skip_workflow_check"]

    @property
    def host(self) -> str:
        return self.data["server"]["host"]

    @property
    def port(self) -> int:
        return self.data["server"]["port"]

    @property
    def public_url(self) -> str:
        """The address the workers reach the app at (the reverse proxy's site, no trailing slash); '' when not set."""
        return self.data["server"]["public_url"].strip().rstrip("/")

    @property
    def product(self) -> str:
        """The selected product line, upper case (one of PRODUCTS)."""
        return self.data["ui"]["product"].strip().upper()

    @property
    def database_path(self) -> Path:
        """Absolute path of the SQLite task store."""
        return resolve_path(self.data["database"]["path"], self.path.parent)

    @property
    def monitor_interval(self) -> float:
        return float(self.data["tasks"]["monitor_interval_s"])

    @property
    def worker_offline_after(self) -> timedelta:
        return timedelta(seconds=self.data["tasks"]["worker_offline_after_s"])

    @property
    def assigned_timeout(self) -> timedelta:
        return timedelta(seconds=self.data["tasks"]["assigned_timeout_s"])

    @property
    def running_timeout(self) -> timedelta:
        return timedelta(seconds=self.data["tasks"]["running_timeout_s"])

    @property
    def ui_refresh(self) -> float:
        """Seconds between two refreshes of the Tasks and Workers pages."""
        return float(self.data["tasks"]["ui_refresh_s"])

    @property
    def api_token(self) -> str:
        return self.data["tasks"]["api_token"].strip()
