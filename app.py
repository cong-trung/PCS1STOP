#!/usr/bin/env python3
"""PCS 1-Stop (PC1): the pcs_die_file editor, the task queue and the HTTP API the PC2 workers talk to.

    uv run app.py                     # settings come from config.json (paths, checker options, host/port, queue)
    uv run app.py --config my.json    # use another settings file

    /          pcs_die_file editor (upload a pcs_die_file XML -> parse -> edit -> save; the workflow paths are checked by a worker)
    /tasks     every task of the queue            /workers   the PC2 machines
    /api/...   what the workers call (see README.md)

PC2 runs ``worker/worker_agent.py``, which polls this server every 10 seconds.
"""

from __future__ import annotations

import argparse
import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from nicegui import app, core, ui
from sqlalchemy.exc import SQLAlchemyError

from api import install_api
from config import ConfigError, Settings
from database import Database, init_db
from scheduler import MonitorScheduler, install_scheduler
from services import Services
from ui import register_pages  # our package, not nicegui.ui
from ui.common import STATIC

log = logging.getLogger("pcs.app")
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


def build_services(settings: Settings) -> Services:
    """Open (and, the first time, create) the task store and wire the services to it."""
    db = Database(settings.database_path)
    init_db(db)
    return Services.create(
        db,
        offline_after=settings.worker_offline_after,
        assigned_timeout=settings.assigned_timeout,
        running_timeout=settings.running_timeout,
        api_token=settings.api_token,
    )


class QuietSessionRace(logging.Filter):
    """python-engineio raises KeyError('Session is disconnected') when the last polling request of a browser reaches the
    server just after its tab was closed.  The browser is gone and nothing is wrong, so uvicorn's traceback for it is kept
    out of the log."""

    def filter(self, record: logging.LogRecord) -> bool:
        error = record.exc_info[1] if record.exc_info else None
        return not (isinstance(error, KeyError) and error.args == ("Session is disconnected",))


def setup_logging(settings: Settings) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    log_file = settings.database_path.parent / "pcs1stop.log"
    try:
        handlers.append(RotatingFileHandler(log_file, maxBytes=1_000_000, backupCount=3, encoding="utf-8"))
    except OSError as exc:  # a log file that cannot be opened must not keep the server from starting
        print(f"warning: no log file ({exc})", file=sys.stderr)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        handlers=handlers,
        force=True,
    )
    logging.getLogger("uvicorn.error").addFilter(QuietSessionRace())


def configure_transport(settings: Settings) -> None:
    """How the browsers stay connected to the app: plain HTTP polling unless ``server.websocket`` is on.  A WebSocket is
    lighter, but a reverse proxy that cannot relay it (IIS) either leaves the upgrade unanswered - every page then sits
    dead for 20 seconds before the browser falls back - or lets it open and drops it at once, and the browser keeps
    retrying the WebSocket and never gets to polling.  Polling works behind every proxy straight away."""
    app.config.socket_io_js_transports = ["websocket", "polling"] if settings.websocket else ["polling"]


def configure_compression(settings: Settings) -> None:
    """Nothing the app sends is compressed unless ``server.compression`` is on.  The socket.io engine gzips every polling
    response of 1 KB or more by itself (NiceGUI's own gzip of the pages is switched off in ``run_options``); behind IIS,
    which compresses once more, that makes two layers.  The browser undoes only one of them, the NiceGUI connection
    receives binary garbage, drops the session and reconnects for ever - every big update (a pcs_die_file's tables) is lost,
    small ones get through."""
    core.sio.eio.http_compression = settings.compression


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="PCS 1-Stop (NiceGUI + task queue)")
    ap.add_argument(
        "--config",
        metavar="FILE",
        help="settings file (default: config.json next to app.py)",
    )
    return ap.parse_args(argv)


def run_options(settings: Settings) -> dict:
    """The arguments ``ui.run`` is started with."""
    options = {
        "host": settings.host,
        "port": settings.port,
        "title": "PCS Die File Checker",
        "reload": False,
        "show": False,  # a server: nobody wants a browser window to pop up on it
        "show_welcome_message": False,
        "uvicorn_logging_level": "warning",  # one access-log line per worker poll would drown everything else
        "favicon": "🧾",
    }
    if not settings.compression:
        # NiceGUI gzips its pages by default (the socket.io engine's own gzip is switched off in configure_compression)
        options["gzip_middleware_factory"] = None
    return options


def reachability_notes(settings: Settings) -> list[tuple[int, str]]:
    """What the log says about who can reach the app, as (log level, message).  Listening on this PC only is right
    when a reverse proxy publishes the app (``server.public_url``); without one no worker on another PC can connect."""
    local = settings.host in LOCAL_HOSTS
    notes: list[tuple[int, str]] = []
    if local and settings.public_url:
        notes.append((
            logging.INFO,
            f"listening on {settings.host}:{settings.port} (this PC only) - the workers connect through {settings.public_url}",
        ))
    elif local:
        notes.append((
            logging.WARNING,
            f"listening on {settings.host} only and server.public_url is not set - workers on other PCs cannot connect; "
            "publish the app through a reverse proxy and set server.public_url in config.json (or set server.host to 0.0.0.0)",
        ))
    if (not local or settings.public_url) and not settings.api_token:
        notes.append((logging.WARNING, "the API is open to the network without a token - set tasks.api_token in config.json"))
    return notes


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    try:
        settings = Settings(args.config)
        services = build_services(settings)
    except ConfigError as e:
        sys.exit(f"Cannot start - {e}")
    except (OSError, RuntimeError, SQLAlchemyError) as e:  # folder not writable, file locked, newer schema, ...
        sys.exit(f"Cannot start - the task store could not be opened: {e}")
    setup_logging(settings)

    install_api(app, services)
    app.add_static_files("/pcs", STATIC)
    register_pages(services, settings)
    install_scheduler(app.on_startup, app.on_shutdown, MonitorScheduler(services.monitor, settings.monitor_interval))
    app.on_shutdown(services.db.dispose)

    log.info("task store: %s", settings.database_path)
    if not settings.alarm_map or not Path(settings.alarm_map).is_file():
        log.warning(
            "alarm map not found (%s) - pcs_die_file validation will report errors; set paths.alarm_map in config.json",
            settings.alarm_map or "no path set",
        )
    else:
        log.info("alarm map: %s", settings.alarm_map)
    for level, message in reachability_notes(settings):
        log.log(level, "%s", message)
    configure_transport(settings)
    configure_compression(settings)
    log.info("browsers connect by %s", "WebSocket, polling as the fallback" if settings.websocket else "HTTP polling only")
    log.info("responses are %s", "gzipped by the app" if settings.compression else "sent plain (a reverse proxy may compress them)")
    ui.run(**run_options(settings))


if __name__ in {"__main__", "__mp_main__"}:
    main()
