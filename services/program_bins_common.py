"""Shared TP-bin data shape, SQL-file loading, and CSV output for the active PyUber service."""
from __future__ import annotations

import csv
import logging
import os
import re
import tempfile
import threading
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

log = logging.getLogger("pcs.bins")

DEFAULT_QUERY_DIR = Path(__file__).resolve().parent.parent / "queries"
CSV_HEADER = ("PROGRAM_NAME", "BIN_NUMBER")
COPIES = 5
BINS_PREFIX, UNIT_PREFIX = "bins", "unit_bins"
BINS_SQL, UNIT_SQL = "program_bins.sql", "unit_tp_bins.sql"
UNIT_DAYS = 60
UNIT_PROGRAM = "PROGRAM_NAME"
UNIT_BINS = ("DATA_BIN", "FUNCTIONAL_BIN", "INTERFACE_BIN")


class ProgramBinsError(Exception):
    """A TP-bin lookup failed; the message says why."""


@dataclass(frozen=True)
class ProgramBins:
    """The answer of one TP-bin lookup."""

    program: str
    bins: tuple[tuple[Any, Any], ...]
    path: Path
    seconds: float
    prefix: str = BINS_PREFIX

    @property
    def renamed(self) -> bool:
        """True when the usual CSV name was unavailable because another program has it open."""
        return self.path.name != csv_name(self.program, prefix=self.prefix)


@dataclass(frozen=True)
class CleanupReport:
    """What one cleanup of the CSV folder did."""

    folder: Path
    deleted: int
    kept: tuple[str, ...]  # files that could not be deleted (open in another program): the next cleanup tries again


def csv_name(program: str, copy: int = 0, prefix: str = BINS_PREFIX) -> str:
    """Return a safe CSV name; ``copy`` selects a collision suffix."""
    stem = f"{prefix}_" + re.sub(r"[^A-Za-z0-9._-]", "_", program)[:80]
    return f"{stem}_{copy}.csv" if copy else f"{stem}.csv"


def work_week(day: date) -> int:
    """Return Intel's YYYYWW work week (Sunday through Saturday)."""
    sunday = day - timedelta(days=(day.weekday() + 1) % 7)
    year = (sunday + timedelta(days=6)).year
    january1 = date(year, 1, 1)
    first_sunday = january1 - timedelta(days=(january1.weekday() + 1) % 7)
    return year * 100 + (sunday - first_sunday).days // 7 + 1


def bin_number(value: Any) -> Any:
    """Normalize database bin values for CSV output and cross-source deduplication."""
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, Decimal) and value == value.to_integral_value():
        return int(value)
    return value


class ProgramBinsFiles:
    """Read query files and save the unchanged two-column CSV contract."""

    def __init__(
        self,
        out_dir: str | Path | None = None,
        query_dir: str | Path = DEFAULT_QUERY_DIR,
    ) -> None:
        self.out_dir = (
            Path(out_dir)
            if out_dir is not None
            else Path(tempfile.gettempdir()) / "pcs1stop_bins"
        )
        self.query_dir = Path(query_dir)
        self._write_lock = threading.Lock()

    def _read_sql(self, name: str) -> str:
        path = self.query_dir / name
        try:
            text = path.read_text(encoding="utf-8-sig").strip().removesuffix(";").rstrip()
        except FileNotFoundError:
            raise ProgramBinsError(
                f"SQL file not found: {path} - it belongs in the queries folder next to app.py"
            ) from None
        except (OSError, UnicodeError) as error:
            raise ProgramBinsError(f"Cannot read {path}: {error}") from error
        if not text:
            raise ProgramBinsError(f"SQL file is empty: {path}")
        return text

    def _save(
        self, program: str, bins: tuple[tuple[Any, Any], ...], prefix: str = BINS_PREFIX
    ) -> Path:
        try:
            self.out_dir.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise ProgramBinsError(
                f"Cannot write {self.out_dir}: {error.strerror or error}"
            ) from error
        in_use: PermissionError | None = None
        with self._write_lock:
            for copy in range(COPIES + 1):
                path = self.out_dir / csv_name(program, copy, prefix)
                try:
                    with path.open("w", newline="", encoding="utf-8") as handle:
                        writer = csv.writer(handle)
                        writer.writerow(CSV_HEADER)
                        writer.writerows(bins)
                except PermissionError as error:
                    in_use = in_use or error
                    continue
                except OSError as error:
                    raise ProgramBinsError(
                        f"Cannot write {path}: {error.strerror or error}"
                    ) from error
                return path
        raise ProgramBinsError(
            f"Cannot write {self.out_dir / csv_name(program, prefix=prefix)}: "
            f"{in_use.strerror or in_use} - is it open in another program?"
        )

    def clean_out_dir(self) -> CleanupReport:
        """Delete every file in the CSV folder; the folder itself and any folder inside it stay.  A file another program
        has open (Excel locks a CSV) cannot be deleted and is left for the next cleanup.  A lookup that is writing a CSV
        finishes first."""
        deleted = 0
        kept: list[str] = []
        with self._write_lock:
            try:
                with os.scandir(self.out_dir) as entries:
                    files = [entry for entry in entries if entry.is_file(follow_symlinks=False)]
            except FileNotFoundError:  # no lookup has saved anything yet
                files = []
            for entry in files:
                try:
                    os.unlink(entry.path)
                    deleted += 1
                except FileNotFoundError:
                    pass
                except OSError:
                    kept.append(entry.name)
        if kept:
            log.warning("TP-bin cleanup: %d file(s) in use, left for the next cleanup: %s", len(kept), ", ".join(kept))
        log.info("TP-bin cleanup: deleted %d file(s) from %s", deleted, self.out_dir)
        return CleanupReport(self.out_dir, deleted, tuple(kept))
