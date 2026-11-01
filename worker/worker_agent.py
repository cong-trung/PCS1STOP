#!/usr/bin/env python3
"""PCS 1-Stop worker agent - runs on PC2.

PC2 cannot accept connections, so the agent calls PC1 (the NiceGUI server) over HTTP:

    every 10 s   GET  /api/task/next?worker=<host>      oldest PENDING task -> ASSIGNED to this worker (or none)
    per task     POST /api/task/start, then /api/task/complete (result) or /api/task/fail (error)
    every 20 s   POST /api/worker/heartbeat             keeps the worker ONLINE while a long task runs

After a task it asks for the next one at once, so a queue is worked off without waiting 10 s per task.  When PC1 is
down the agent keeps trying (back-off up to a minute) and carries on by itself once PC1 is back; a result that could
not be delivered is delivered after the reconnect.

This file needs nothing but Python 3.10+ (no packages): copy it with ``worker_config.json`` to PC2 and run

    python worker_agent.py                       # reads worker_config.json next to this file
    python worker_agent.py --server http://pc1:8080 --token <api_token>
    python worker_agent.py --check               # only test the settings and the connection to PC1, then exit

Task types it knows:  ``check_path``  payload ``{"path": ..., "product": "SST" | "HST"}``  - is the file there?
``allowed_roots`` in worker_config.json limits which folders / shares a check may look at (empty = anywhere): set it to
the server and share names your pcs_die_files really use, so a pcs_die_file cannot make this PC probe an arbitrary machine.

PCS die files name their workflow files on the drive M:.  ``drive_map`` in worker_config.json says which drive this PC has that
as, per product: SST -> Y:, HST -> X: (``M:\\pcs\\a.xml`` is looked up as ``Y:\\pcs\\a.xml`` for SST).  Every other path is
looked up as written, and ``allowed_roots`` is judged on the path that is really looked at.
"""
from __future__ import annotations

import argparse
import errno
import json
import logging
import os
import random
import re
import signal
import socket
import stat
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, fields
from datetime import datetime, timezone
from http.client import HTTPException
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

__version__ = "1.0.0"

HERE = Path(__file__).resolve().parent
DEFAULT_CONFIG = HERE / "worker_config.json"
#: what PC1 accepts as the result of a task (services.task_service.MAX_JSON_BYTES)
MAX_RESULT_BYTES = 256 * 1024
#: Where the drive a pcs_die_file names ends up on this PC, per product (``drive_map`` in worker_config.json): pcs_die_files name
#: their workflow files on the drive M:, which this PC has as Y: for SST and as X: for HST
DEFAULT_DRIVE_MAP: dict[str, dict[str, str]] = {"SST": {"M": "Y"}, "HST": {"M": "X"}}
#: "M:\..." and "m:/..." and plain "M:" - but not the drive-relative "M:file" (whatever the current folder of M: is)
_DRIVE_PREFIX = re.compile(r"^([A-Za-z]):(?=[\\/]|$)")

log = logging.getLogger("pcs.worker")

Handler = Callable[[Mapping[str, Any]], dict[str, Any]]


# ----------------------------------------------------------------------------------------------- errors
class ConfigProblem(Exception):
    """worker_config.json (or a command line option) has a wrong value."""


class ServerUnavailable(Exception):
    """PC1 did not answer properly (down, network trouble, HTTP 5xx): the same request may work later."""


class Rejected(Exception):
    """PC1 understood the request and refused it (HTTP 4xx): sending it again would not change that."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"HTTP {status}: {message}")
        self.status = status


class TaskError(Exception):
    """A task could not be carried out; the message becomes the task's error (status FAILED)."""


class TaskTimeout(TaskError):
    """The handler did not finish within ``task_timeout_s``."""


# ----------------------------------------------------------------------------------------- configuration
@dataclass
class WorkerConfig:
    server_url: str = "http://127.0.0.1:8080"
    hostname: str = ""  # "" = the name of this machine
    poll_interval_s: float = 10.0  # how often an idle worker asks for work
    heartbeat_interval_s: float = 20.0  # "still here" while a long task runs (PC1 calls a worker OFFLINE after 60 s)
    request_timeout_s: float = 15.0  # one HTTP call
    task_timeout_s: float = 120.0  # one task; a share that hangs must not block the worker forever
    max_backoff_s: float = 60.0  # longest wait between two attempts while PC1 is down
    max_delivery_s: float = 3600.0  # how long a finished task's result is retried (PC1 times a task out after 60 min)
    api_token: str = ""  # tasks.api_token of PC1's config.json, when one is set
    allowed_roots: list = field(default_factory=list)  # folders / shares a check may look at; [] = anywhere
    # per product: the drive a pcs_die_file names -> the drive this PC has it as; {} = every path is looked up as written
    drive_map: dict = field(default_factory=lambda: {product: dict(drives) for product, drives in DEFAULT_DRIVE_MAP.items()})
    max_stuck_tasks: int = 8  # no new tasks while this many checks hang on a share that does not answer
    log_file: str = "worker.log"  # next to the config file; "" = console only
    base_dir: Path = field(default=HERE, init=False, repr=False)  # (not a setting) where a relative log_file goes

    @classmethod
    def load(cls, path: str | Path | None = None, **overrides: Any) -> WorkerConfig:
        """Defaults < the JSON file < ``overrides`` (command line options that were given)."""
        config_path = Path(path) if path else DEFAULT_CONFIG
        values: dict[str, Any] = {}
        if config_path.exists():
            try:
                raw = json.loads(config_path.read_text(encoding="utf-8-sig"))
            except (OSError, ValueError) as exc:
                raise ConfigProblem(f"{config_path}: {exc}") from exc
            if not isinstance(raw, dict):
                raise ConfigProblem(f"{config_path}: the top level must be a JSON object")
            values.update(raw)
        elif path:
            raise ConfigProblem(f"{config_path} does not exist")
        values.update({key: value for key, value in overrides.items() if value is not None})
        known = {f.name for f in fields(cls) if f.init}
        for key in sorted(values.keys() - known):
            log.warning("unknown setting %r ignored", key)
        config = cls(**{key: value for key, value in values.items() if key in known})
        config.base_dir = config_path.resolve().parent
        config.validate()
        return config

    def validate(self) -> None:
        for name in ("server_url", "hostname", "api_token", "log_file"):
            if not isinstance(getattr(self, name), str):
                raise ConfigProblem(f'"{name}" must be a string')
        parts = urllib.parse.urlsplit(self.server_url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ConfigProblem(f'"server_url" must look like http://host:port, got {self.server_url!r}')
        for name in (
            "poll_interval_s",
            "heartbeat_interval_s",
            "request_timeout_s",
            "task_timeout_s",
            "max_backoff_s",
            "max_delivery_s",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value < 10**7:
                raise ConfigProblem(f'"{name}" must be a number of seconds greater than 0, got {value!r}')
        token = self.api_token.strip()
        if token and not (token.isascii() and token.isprintable()):
            raise ConfigProblem('"api_token" may only contain plain ASCII characters (it is sent in an HTTP header)')
        if not isinstance(self.allowed_roots, list) or not all(
            isinstance(root, str) and root.strip() for root in self.allowed_roots
        ):
            raise ConfigProblem('"allowed_roots" must be a list of folder / share paths')
        normalized_drive_map(self.drive_map)  # (raises ConfigProblem)
        if isinstance(self.max_stuck_tasks, bool) or not isinstance(self.max_stuck_tasks, int) or self.max_stuck_tasks < 1:
            raise ConfigProblem(f'"max_stuck_tasks" must be a whole number of 1 or more, got {self.max_stuck_tasks!r}')


# ------------------------------------------------------------------------------------------- HTTP client
def _error_text(exc: urllib.error.HTTPError) -> str:
    """The ``error`` of PC1's JSON error answer, or the plain reason."""
    try:
        body = json.loads(exc.read().decode("utf-8", "replace"))
        if isinstance(body, dict) and body.get("error"):
            return str(body["error"])
    except (ValueError, OSError):
        pass
    return str(exc.reason or exc.code)


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    """PC1 never redirects, and urllib would send the API token on to wherever a redirect points: so none is followed."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


_opener = urllib.request.build_opener(_NoRedirects)


class Client:
    """The few PC1 calls the agent makes.  Raises ServerUnavailable / Rejected, nothing else."""

    def __init__(self, config: WorkerConfig) -> None:
        self.base = config.server_url.rstrip("/")
        self.timeout = config.request_timeout_s
        self.token = config.api_token.strip()

    def _call(
        self, method: str, path: str, *, body: Any = None, query: Mapping[str, str] | None = None, auth: bool = True
    ) -> dict:
        """One request; ``auth=False`` leaves the API token out (for asking who is there before trusting it)."""
        url = self.base + path + ("?" + urllib.parse.urlencode(query) if query else "")
        headers = {"Accept": "application/json", "User-Agent": f"pcs-worker/{__version__}"}
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if auth and self.token:
            headers["X-API-Token"] = self.token
        request = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with _opener.open(request, timeout=self.timeout) as response:
                text = response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            if 300 <= exc.code < 400:
                raise Rejected(exc.code, f"redirected to {exc.headers.get('Location') or 'another address'}") from None
            message = _error_text(exc)
            if 400 <= exc.code < 500 and exc.code not in (408, 425, 429):
                raise Rejected(exc.code, message) from None
            raise ServerUnavailable(f"HTTP {exc.code}: {message}") from None
        except (urllib.error.URLError, HTTPException, OSError) as exc:  # (TimeoutError is an OSError)
            raise ServerUnavailable(str(getattr(exc, "reason", None) or exc)) from None
        try:
            answer = json.loads(text)
        except ValueError:
            raise ServerUnavailable("PC1 sent something that is not JSON - is server_url the PCS 1-Stop app?") from None
        if not isinstance(answer, dict):
            raise ServerUnavailable("PC1 sent an unexpected answer")
        return answer

    def heartbeat(self, hostname: str, ip: str | None) -> None:
        self._call("POST", "/api/worker/heartbeat", body={"hostname": hostname, "ip": ip, "version": __version__})

    def health(self) -> None:
        """Is PC1 (the PCS 1-Stop app) there?  Needs no token and sends none: nothing is known about whoever answers
        yet, so the token must not go to it."""
        answer = self._call("GET", "/api/health", auth=False)
        if answer.get("status") != "ok":
            raise Rejected(200, "that is not the answer of the PCS 1-Stop app")

    def next_task(self, hostname: str) -> dict | None:
        answer = self._call("GET", "/api/task/next", query={"worker": hostname})
        return None if answer.get("task_id") is None else answer

    def start(self, task_id: int, hostname: str, delivery: int | None = None) -> None:
        body: dict[str, Any] = {"task_id": task_id, "worker": hostname}
        if delivery is not None:
            body["delivery"] = delivery
        self._call("POST", "/api/task/start", body=body)

    def complete(self, task_id: int, hostname: str, result: dict) -> None:
        self._call("POST", "/api/task/complete", body={"task_id": task_id, "worker": hostname, "result": result})

    def fail(self, task_id: int, hostname: str, error: str) -> None:
        self._call("POST", "/api/task/fail", body={"task_id": task_id, "worker": hostname, "error": error})


# ------------------------------------------------------------------------------------------ the tasks
#: GetLastError codes of a share that cannot be found: "network path not found" / "network name not found"
_NETWORK_NOT_FOUND = (53, 67)
#: errors that mean "this text cannot be a path" (bad characters, too long): the file is simply not there
_BAD_PATH_ERRNO = (errno.EINVAL, errno.ENAMETOOLONG)
_BAD_PATH_WINERROR = (123, 161, 206)  # bad name syntax, bad path, name too long


def _normalized(path: str) -> str:
    return os.path.normcase(os.path.normpath(path))


def is_allowed(path: str, roots: Sequence[str]) -> bool:
    """Is ``path`` one of the ``roots`` or below one of them?  (No roots = everything is allowed.)

    The comparison is on the text: ``..`` is resolved and case is ignored on Windows, so ``M:\\pcs\\..\\x`` is not below
    ``M:\\pcs`` and ``\\\\server\\share2`` is not below ``\\\\server\\share``."""
    if not roots:
        return True
    target = _normalized(path)
    for root in roots:
        base = _normalized(root).rstrip("\\/")
        if target == base or target.startswith(base + os.sep):
            return True
    return False


def _drive_letter(value: Any) -> str | None:
    """"m", "M" and "M:" all name the drive M (None when ``value`` is not a drive letter)."""
    if not isinstance(value, str):
        return None
    letter = value.strip().removesuffix(":")
    return letter.upper() if len(letter) == 1 and letter.isascii() and letter.isalpha() else None


def normalized_drive_map(raw: Any) -> dict[str, dict[str, str]]:
    """The ``drive_map`` setting as {PRODUCT: {FROM: TO}} - product names and drive letters in upper case.
    Raises ConfigProblem when it is not an object of objects of drive letters."""
    if not isinstance(raw, dict):
        raise ConfigProblem('"drive_map" must be an object like {"SST": {"M": "Y"}, "HST": {"M": "X"}}')
    result: dict[str, dict[str, str]] = {}
    for product, drives in raw.items():
        name = product.strip().upper() if isinstance(product, str) else ""
        if not name or name in result or not isinstance(drives, dict):
            raise ConfigProblem(f'"drive_map": {product!r} must be a product name (once) with an object of drives like {{"M": "Y"}}')
        mapped: dict[str, str] = {}
        for source, target in drives.items():
            source_letter, target_letter = _drive_letter(source), _drive_letter(target)
            if source_letter is None or target_letter is None or source_letter in mapped:
                raise ConfigProblem(
                    f'"drive_map": {name}: {source!r} -> {target!r} must name each drive once, as a drive letter to a drive letter, like "M": "Y"'
                )
            mapped[source_letter] = target_letter
        result[name] = mapped
    return result


def describe_drive_map(drive_map: Any) -> str:
    """"SST M: -> Y:, HST M: -> X:" (or "none")."""
    parts = [
        f"{product} {source}: -> {target}:"
        for product, drives in normalized_drive_map(drive_map).items()
        for source, target in drives.items()
    ]
    return ", ".join(parts) or "none"


def map_drive(path: str, product: str, drive_map: Mapping[str, Mapping[str, str]]) -> str:
    """The path as this PC has it: a pcs_die_file names its files on the drive M:, which is Y: for SST and X: for HST
    (``drive_map``), so ``M:\\pcs\\a.xml`` becomes ``Y:\\pcs\\a.xml`` for SST and ``X:\\pcs\\a.xml`` for HST.  Only the
    drive letter changes; a path on another drive, a ``\\\\server\\share`` path and a product that has no entry stay as
    they are.  The drive is replaced once - the drive it is replaced with is not looked up again."""
    match = _DRIVE_PREFIX.match(path)
    if match is None:
        return path
    target = normalized_drive_map(drive_map).get(product.strip().upper(), {}).get(match.group(1).upper())
    return path if target is None else target + path[1:]


def check_path(
    payload: Mapping[str, Any],
    allowed_roots: Sequence[str] = (),
    drive_map: Mapping[str, Mapping[str, str]] | None = None,
) -> dict[str, Any]:
    """Is the file there, as seen from this machine?  A missing file is an *answer* (``exists: false``).

    The drive of the pcs_die_file is mapped first (``drive_map``: ``M:\\...`` is ``Y:\\...`` for SST, ``X:\\...`` for HST); the
    result keeps the pcs_die_file's ``path`` and, when it differs, adds ``checked_path`` - the one that was looked at.
    ``allowed_roots`` is judged on the path that is looked at.

    Trouble that stops the worker from looking at all (access denied, an I/O error, a path outside ``allowed_roots``)
    raises TaskError instead."""
    path, product = payload.get("path"), payload.get("product")
    if not isinstance(path, str) or not path.strip():
        raise TaskError("the task has no path")
    if not isinstance(product, str) or not product.strip():
        raise TaskError("the task has no product")
    path, product = path.strip(), product.strip()
    target = map_drive(path, product, drive_map or {})
    where = path if target == path else f"{target} (the pcs_die_file says {path})"
    if not is_allowed(target, allowed_roots):
        raise TaskError(f"not allowed: the path is outside the folders this worker may look at (allowed_roots): {where}")
    result: dict[str, Any] = {"path": path, "product": product, "exists": False, "is_file": False}
    if target != path:
        result["checked_path"] = target
    try:
        info = Path(target).stat()
    except (FileNotFoundError, NotADirectoryError) as exc:
        if getattr(exc, "winerror", None) in _NETWORK_NOT_FOUND:
            result["detail"] = "network path not found"
        return result
    except ValueError:  # an embedded NUL character
        result["detail"] = "invalid path"
        return result
    except PermissionError as exc:
        raise TaskError(f"access denied: {where}") from exc
    except OSError as exc:
        if exc.errno in _BAD_PATH_ERRNO or getattr(exc, "winerror", None) in _BAD_PATH_WINERROR:
            result["detail"] = "invalid path"
            return result
        raise TaskError(f"{exc.strerror or exc}: {where}") from exc
    result.update(
        exists=True,
        is_file=stat.S_ISREG(info.st_mode),
        size=info.st_size,
        modified=datetime.fromtimestamp(info.st_mtime, timezone.utc).isoformat(timespec="seconds"),
    )
    return result


def default_handlers(config: WorkerConfig) -> dict[str, Handler]:
    """The task types this worker knows, bound to its settings."""
    roots = tuple(config.allowed_roots)
    drives = normalized_drive_map(config.drive_map)
    return {"check_path": lambda payload: check_path(payload, roots, drives)}


class Stranded:
    """The handler threads that outlived their timeout.  A call that is stuck on a share that does not answer cannot be
    interrupted: its thread stays behind (it is a daemon) until the operating system gives up on it."""

    def __init__(self) -> None:
        self._threads: list[threading.Thread] = []
        self._lock = threading.Lock()

    def add(self, thread: threading.Thread) -> None:
        with self._lock:
            self._threads.append(thread)

    def count(self) -> int:
        """How many of them are still stuck."""
        with self._lock:
            self._threads = [thread for thread in self._threads if thread.is_alive()]
            return len(self._threads)


def run_with_timeout(
    handler: Handler, payload: Mapping[str, Any], timeout: float, stranded: Stranded | None = None
) -> dict[str, Any]:
    """Run ``handler`` in a helper thread; after ``timeout`` seconds give up on it (the thread is left behind and
    registered in ``stranded``) so a share that hangs fails the task instead of hanging the agent."""
    outcome: dict[str, Any] = {}

    def target() -> None:
        try:
            outcome["result"] = handler(payload)
        except BaseException as exc:  # noqa: BLE001 - handed to the caller
            outcome["error"] = exc

    thread = threading.Thread(target=target, name="pcs-task", daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        if stranded is not None:
            stranded.add(thread)
        raise TaskTimeout(f"no answer after {timeout:g} s (is the share reachable from this PC?)")
    if "error" in outcome:
        raise outcome["error"]
    result = outcome["result"]
    if not isinstance(result, dict):
        raise TaskError("the task handler returned no result")
    return result


# ------------------------------------------------------------------------------------------- the agent
def detect_ip(server_url: str) -> str | None:
    """The address of this machine that faces PC1 (a UDP "connect" sends nothing, it only picks the route)."""
    parts = urllib.parse.urlsplit(server_url)
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect((parts.hostname or "127.0.0.1", parts.port or 80))
            return probe.getsockname()[0]
    except OSError:
        try:
            return socket.gethostbyname(socket.gethostname())
        except OSError:
            return None


class Agent:
    def __init__(
        self,
        config: WorkerConfig,
        *,
        client: Client | None = None,
        handlers: Mapping[str, Handler] | None = None,
        stop: threading.Event | None = None,
    ) -> None:
        self.config = config
        self.hostname = config.hostname.strip() or socket.gethostname()
        self.client = client or Client(config)
        self.handlers = dict(default_handlers(config) if handlers is None else handlers)
        self.stop = stop or threading.Event()
        self.ip = detect_ip(config.server_url)
        self.stranded = Stranded()
        self.current_task: int | None = None
        self._failures = 0  # consecutive failed polls
        self._last_problem = ""
        self._down_since: float | None = None
        self._refusing = False  # True while too many checks are stuck and no new tasks are taken

    # -------------------------------------------------------------------------------------- main loop
    def run(self) -> None:
        log.info(
            "worker %s %s (%s) polling %s every %g s",
            self.hostname, __version__, self.ip or "ip unknown", self.config.server_url, self.config.poll_interval_s,
        )
        threading.Thread(target=self._heartbeat_loop, name="pcs-heartbeat", daemon=True).start()
        try:
            while not self.stop.is_set():
                try:
                    delay = self.step()
                except Exception:  # noqa: BLE001 - unattended on PC2: a bug must not end the agent
                    log.exception("unexpected error - the agent carries on")
                    delay = self.config.poll_interval_s
                if delay > 0:
                    self.stop.wait(delay)
        except KeyboardInterrupt:
            log.info("interrupted")
        finally:
            self.stop.set()
            log.info("worker stopped")

    def step(self) -> float:
        """Ask PC1 for a task and carry it out.  Returns the seconds to wait before the next step (0 = right away)."""
        stuck = self.stranded.count()
        if stuck >= self.config.max_stuck_tasks:  # every further check on that share would only add to the pile
            if not self._refusing:
                self._refusing = True
                log.warning("%d checks are stuck (a share that does not answer?) - no new tasks until some are done", stuck)
            return self.config.poll_interval_s
        if self._refusing:
            self._refusing = False
            log.info("the stuck checks are gone - taking tasks again")
        try:
            task = self.client.next_task(self.hostname)
        except (ServerUnavailable, Rejected) as exc:
            return self._poll_failed(exc)
        self._poll_ok()
        if task is None:
            return self.config.poll_interval_s
        # more may be waiting after a task that was carried out; one that was dropped may come straight back
        return 0.0 if self.execute(task) else self.config.poll_interval_s

    def _poll_failed(self, exc: Exception) -> float:
        self._failures += 1
        if self._down_since is None:
            self._down_since = time.monotonic()
        problem = str(exc)
        if problem != self._last_problem:  # say it once, not every few seconds
            self._last_problem = problem
            refused = isinstance(exc, Rejected)
            hint = ""
            if refused and exc.status == 401:
                hint = " - is tasks.api_token set in PC1's config.json? (worker_config.json: api_token)"
            log.log(
                logging.ERROR if refused else logging.WARNING,
                "cannot reach PC1 (%s): %s%s - retrying", self.config.server_url, problem, hint,
            )
        return self._backoff(self._failures)

    def _poll_ok(self) -> None:
        if self._down_since is not None:
            log.info("PC1 is reachable again (after %.0f s)", time.monotonic() - self._down_since)
        self._failures, self._last_problem, self._down_since = 0, "", None

    def _backoff(self, attempt: int) -> float:
        """2, 4, 8 ... seconds up to ``max_backoff_s`` (never below the poll interval's 2 s), with a little jitter."""
        base = min(self.config.max_backoff_s, 2.0 ** min(attempt, 20))
        base = min(self.config.max_backoff_s, max(base, min(self.config.poll_interval_s, 2.0)))
        return min(self.config.max_backoff_s, base * random.uniform(0.8, 1.2))

    def _heartbeat_loop(self) -> None:
        while True:
            try:
                self.client.heartbeat(self.hostname, self.ip)
            except (ServerUnavailable, Rejected) as exc:
                log.debug("heartbeat failed: %s", exc)
            if self.stop.wait(self.config.heartbeat_interval_s):
                return

    # ------------------------------------------------------------------------------------- one task
    def execute(self, task: Mapping[str, Any]) -> bool:
        """Carry out one task and report its outcome.  False when it was dropped because PC1 refused to start it
        (it timed out meanwhile, was handed out again, ...) or could not be asked."""
        try:
            task_id = int(task["task_id"])
        except (KeyError, TypeError, ValueError):
            log.error("PC1 sent a task without a usable id: %r", task)
            return False
        task_type = str(task.get("task_type", ""))
        payload = task.get("payload") if isinstance(task.get("payload"), dict) else {}
        delivery = task.get("delivery")
        if isinstance(delivery, bool) or not isinstance(delivery, int):
            delivery = None
        self.current_task = task_id
        log.info("task %s (%s) received: %s", task_id, task_type, json.dumps(payload, ensure_ascii=False))
        try:
            try:
                self._deliver(lambda: self.client.start(task_id, self.hostname, delivery))
            except Rejected as exc:  # timed out on PC1 meanwhile, or handed out again: nothing to do
                log.warning("task %s dropped, PC1 refused to start it (%s)", task_id, exc)
                return False
            except ServerUnavailable:
                log.warning("task %s dropped, PC1 could not be reached to start it", task_id)
                return False
            handler = self.handlers.get(task_type)
            try:
                if handler is None:
                    raise TaskError(f"this worker does not know the task type {task_type!r}")
                result = run_with_timeout(handler, payload, self.config.task_timeout_s, self.stranded)
            except TaskError as exc:
                self._finish(task_id, error=str(exc))
            except KeyboardInterrupt:
                self._finish(task_id, error="the worker was stopped", patience=2.0)
                raise
            except Exception as exc:  # noqa: BLE001 - a bug in a handler must not stop the agent
                log.exception("task %s crashed", task_id)
                self._finish(task_id, error=f"{type(exc).__name__}: {exc}")
            else:
                self._finish(task_id, result=result)
            return True
        finally:
            self.current_task = None

    def _finish(
        self, task_id: int, *, result: dict | None = None, error: str | None = None, patience: float | None = None
    ) -> None:
        if error is None:
            size = len(json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
            if size > MAX_RESULT_BYTES:  # PC1 would refuse it, and the task would then hang in RUNNING until it times out
                error = f"the result is too large to send ({size // 1024} KB, the limit is {MAX_RESULT_BYTES // 1024} KB)"
        if error is None:
            log.info("task %s completed: %s", task_id, json.dumps(result, ensure_ascii=False))

            def send() -> None:
                self.client.complete(task_id, self.hostname, result or {})

        else:
            log.warning("task %s failed: %s", task_id, error)

            def send() -> None:
                self.client.fail(task_id, self.hostname, error)

        try:
            self._deliver(send, patience)
        except Rejected as exc:  # e.g. PC1 already timed the task out
            log.warning("task %s: PC1 did not accept the outcome (%s)", task_id, exc)
        except ServerUnavailable:
            log.error("task %s: the outcome could not be delivered to PC1", task_id)

    def _deliver(self, call: Callable[[], None], patience: float | None = None) -> None:
        """Make a call to PC1 until it gets through (back-off in between), for at most ``patience`` seconds."""
        deadline = time.monotonic() + (self.config.max_delivery_s if patience is None else patience)
        attempt = 0
        while True:
            try:
                call()
                return
            except ServerUnavailable as exc:
                attempt += 1
                wait = self._backoff(attempt)
                if self.stop.is_set() or time.monotonic() + wait > deadline:
                    raise
                log.warning("PC1 not reachable (%s) - trying again in %.0f s", exc, wait)
                self.stop.wait(wait)

# ------------------------------------------------------------------------------------- command line
def run_check(config: WorkerConfig, out: Callable[[str], None] = print) -> int:
    """Try the connection once and say what is wrong (``install.ps1`` runs this before it installs anything).

    PC1 is asked to register this worker, which is harmless: it shows up on the Workers page and goes OFFLINE again a
    minute later unless the agent is running.  Returns 0 when it all works, 1 when it does not."""
    hostname = config.hostname.strip() or socket.gethostname()
    client = Client(config)
    roots = config.allowed_roots
    drive_map = normalized_drive_map(config.drive_map)

    def is_there(path: str) -> bool | None:
        """Does this PC see ``path``?  None = no answer within 10 s (a share that hangs)."""
        try:
            return run_with_timeout(lambda _payload: {"found": os.path.exists(path)}, {}, 10)["found"]
        except TaskTimeout:
            return None

    out(f"PCS 1-Stop worker {__version__} on {hostname}")
    out(f"  PC1:             {config.server_url}")
    out(f"  API token:       {'set' if config.api_token.strip() else 'NOT SET'}")
    out(f"  Allowed folders: {', '.join(roots) if roots else 'any'}")
    out(f"  Drive map:       {describe_drive_map(drive_map)}")
    try:
        client.health()
    except ServerUnavailable as exc:
        out(f"[FAIL] cannot reach PC1: {exc}")
        out("       Is the app running on PC1?  Is server_url in worker_config.json right?")
        out(f"       Open {config.server_url.rstrip('/')}/api/health in a browser on this PC: it should show status ok.")
        return 1
    except Rejected as exc:
        out(f"[FAIL] something answered, but it is not the PCS 1-Stop app ({exc}) - is server_url right?")
        return 1
    out("[ ok ] PC1 is reachable")
    try:
        client.heartbeat(hostname, detect_ip(config.server_url))
    except Rejected as exc:
        if exc.status == 401:
            out("[FAIL] PC1 refused the API token: api_token in worker_config.json must be the same as")
            out("       tasks.api_token in PC1's config.json.")
        else:
            out(f"[FAIL] PC1 refused to register this worker: {exc}")
        return 1
    except ServerUnavailable as exc:
        out(f"[FAIL] PC1 stopped answering: {exc}")
        return 1
    out("[ ok ] the API token is accepted - this PC now shows on the Workers page of PC1")
    for root in roots:  # (a mapped drive only exists for a logged-on user: say so now, not when a check fails)
        found = is_there(root)
        if found is None:
            out(f"[warn] allowed folder gave no answer within 10 s: {root}")
        else:
            out(f"[ ok ] allowed folder found: {root}" if found else
                f"[warn] allowed folder not found from this PC: {root} (mapped drive? share offline?)")
    for product, drives in drive_map.items():  # (the same for the drives that the pcs_die_file's drive stands for)
        for source, target in drives.items():
            what = f"{product}: {source}:\\ is looked up as {target}:\\"
            found = is_there(f"{target}:\\")
            if found is None:
                out(f"[warn] {what} - no answer within 10 s")
            elif found:
                out(f"[ ok ] {what}")
            else:
                out(f"[warn] {what} - this PC has no drive {target}: (mapped drive? it exists only for a logged-on user)")
    return 0


def report_startup_error(message: str) -> None:
    """A wrong setting must be seen even when the agent runs without a console (pythonw): console, then worker.log."""
    print(message, file=sys.stderr)
    try:
        with open(HERE / "worker.log", "a", encoding="utf-8") as handle:
            handle.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} ERROR   {message}\n")
    except OSError:
        pass


def setup_logging(config: WorkerConfig, verbose: bool = False) -> None:
    handlers: list[logging.Handler] = []
    if sys.stderr is not None:  # (there is none under pythonw.exe)
        handlers.append(logging.StreamHandler())
    if config.log_file:
        path = Path(config.log_file)
        path = path if path.is_absolute() else config.base_dir / path
        try:
            handlers.append(RotatingFileHandler(path, maxBytes=1_000_000, backupCount=3, encoding="utf-8"))
        except OSError as exc:
            print(f"warning: no log file ({exc})", file=sys.stderr)
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        handlers=handlers or [logging.NullHandler()],
        force=True,
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="PCS 1-Stop worker agent (PC2)")
    ap.add_argument("--config", metavar="FILE", help="settings file (default: worker_config.json next to this script)")
    ap.add_argument("--server", metavar="URL", help="PC1 address, e.g. http://pc1:8080 (overrides the config file)")
    ap.add_argument("--hostname", metavar="NAME", help="name shown on the Workers page (default: this machine's name)")
    ap.add_argument("--token", metavar="TOKEN", help="API token (PC1's tasks.api_token)")
    ap.add_argument("--check", action="store_true", help="test the settings and the connection to PC1, then exit")
    ap.add_argument("-v", "--verbose", action="store_true", help="also log heartbeats and other details")
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        config = WorkerConfig.load(args.config, server_url=args.server, hostname=args.hostname, api_token=args.token)
    except ConfigProblem as exc:
        report_startup_error(f"Cannot start - {exc}")
        return 2
    if args.check:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(errors="replace")  # (an old console code page must not break the report)
        return run_check(config)
    setup_logging(config, args.verbose)
    agent = Agent(config)
    for name in ("SIGTERM", "SIGBREAK"):  # service managers / Ctrl+Break: stop cleanly like Ctrl+C
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), lambda *_: agent.stop.set())
    agent.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
