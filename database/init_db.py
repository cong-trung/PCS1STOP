"""Creates the SQLite file and its tables."""
from __future__ import annotations

import logging

from .models import Base
from .session import Database

log = logging.getLogger("pcs.database")

#: bump when the tables change; ``init_db`` refuses a file written by a newer version of the program
SCHEMA_VERSION = 1


def init_db(db: Database) -> None:
    """Create the tables if they are missing (safe to call on every start) and stamp the schema version."""
    Base.metadata.create_all(db.engine)
    with db.engine.begin() as conn:
        version = conn.exec_driver_sql("PRAGMA user_version").scalar_one()
        if version > SCHEMA_VERSION:
            raise RuntimeError(
                f"{db.path} has schema version {version}, this program only knows version {SCHEMA_VERSION}"
            )
        if version < SCHEMA_VERSION:
            conn.exec_driver_sql(f"PRAGMA user_version = {SCHEMA_VERSION}")
    log.info("task store ready: %s", db.path)
