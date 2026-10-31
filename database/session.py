"""SQLite engine and session factories.

Two kinds of transactions are offered:

* ``read_session()``  - an ordinary deferred transaction (WAL lets any number of readers run next to a writer).
* ``write_session()`` - starts with ``BEGIN IMMEDIATE``: the write lock is taken *before* anything is read, so
  "find the oldest PENDING task, then mark it ASSIGNED" cannot interleave with another worker doing the same.
  Writers simply queue up on SQLite's lock (``busy_timeout``) instead of failing half way.
"""
from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import URL, create_engine, event
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.orm import Session, sessionmaker

log = logging.getLogger("pcs.database")

#: execution option that tells the ``begin`` hook which BEGIN to emit
BEGIN_MODE_OPTION = "sqlite_begin"
_BEGIN_MODES = ("DEFERRED", "IMMEDIATE", "EXCLUSIVE")


def _on_connect(dbapi_connection: sqlite3.Connection, _record: object) -> None:
    # SQLAlchemy emits BEGIN itself (see _on_begin); sqlite3 must not start transactions behind its back.
    dbapi_connection.isolation_level = None
    cursor = dbapi_connection.cursor()
    try:
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
        except sqlite3.OperationalError as exc:  # the mode is stored in the file, so an earlier connection set it
            log.warning("could not switch the database to WAL mode: %s", exc)
        cursor.execute("PRAGMA synchronous=FULL")  # a committed task must survive a power cut
        cursor.execute("PRAGMA foreign_keys=ON")
    finally:
        cursor.close()


def _on_begin(conn: Connection) -> None:
    mode = conn.get_execution_options().get(BEGIN_MODE_OPTION, "DEFERRED")
    if mode not in _BEGIN_MODES:
        raise ValueError(f"unknown BEGIN mode {mode!r}")
    conn.exec_driver_sql(f"BEGIN {mode}")


class Database:
    """The SQLite file of the task store plus its session factories."""

    def __init__(self, path: str | Path, *, busy_timeout: float = 30.0, echo: bool = False) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.engine: Engine = create_engine(
            URL.create("sqlite", database=str(self.path)),
            echo=echo,
            connect_args={"timeout": busy_timeout},  # seconds a statement waits for a lock held by someone else
        )
        event.listen(self.engine, "connect", _on_connect)
        event.listen(self.engine, "begin", _on_begin)
        self._read = sessionmaker(self.engine, expire_on_commit=False)
        self._write = sessionmaker(
            self.engine.execution_options(**{BEGIN_MODE_OPTION: "IMMEDIATE"}), expire_on_commit=False
        )

    @contextmanager
    def read_session(self) -> Iterator[Session]:
        with self._read() as session:
            yield session

    @contextmanager
    def write_session(self) -> Iterator[Session]:
        """One write transaction: BEGIN IMMEDIATE ... COMMIT, or ROLLBACK when the block raises."""
        with self._write() as session:
            try:
                yield session
                session.commit()
            except BaseException:
                session.rollback()
                raise

    def dispose(self) -> None:
        """Close every pooled connection (releases the file on Windows)."""
        self.engine.dispose()
