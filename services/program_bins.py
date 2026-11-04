"""Bins of a test program (TP), from Oracle - what the TP Name box of the pcs_die_file page looks up.

The Oracle side is ``pcs_oracle_portable/oracle_db.py``: python-oracledb in Thick mode and the Kerberos login of the Windows
user that runs this app.  It only opens the connection (``OracleDataAccess.connect()``) and executes a SQL string
(``get_data()``); no SQL lives in it.  The SQL of this app is in ``queries/*.sql`` (``query_dir``) - the Test Program query of
the old WinForms app and the unit level one - so the portable folder can go to another project, which brings the SQL files of
its own.  oracle_db.py is loaded by its path on the first lookup, so the app starts without it and the lookup says what is
missing; it is never edited here - replace the folder to update it (``oracle.portable_dir`` in config.json says where it is).

``ProgramBinsService.query(program)`` runs ``queries/program_bins.sql`` with the TP name as a bind variable, keeps two of the
three columns it returns (PROGRAM_NAME, BIN_NUMBER) and writes them to ``%TEMP%\\pcs1stop_bins\\bins_<program>.csv`` - or to
``bins_<program>_1.csv`` and so on when that file is open in another program (Excel locks it), so a lookup never fails for
that.  ``query_units(program)`` does the same for the bins the TP's *units* had in the last UNIT_DAYS days
(``queries/unit_tp_bins.sql``) and writes ``unit_bins_<program>.csv``.  Both block while they log in and query - the second
for much longer: call them from a thread.
"""

from __future__ import annotations

import csv
import importlib.util
import re
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from types import ModuleType
from typing import Any

from .clock import Clock, utcnow

DEFAULT_PORTABLE_DIR = Path(__file__).resolve().parent.parent / "pcs_oracle_portable"
DEFAULT_QUERY_DIR = Path(__file__).resolve().parent.parent / "queries"
DEFAULT_ALIAS = "VN.ARIES"
#: the columns of the CSV, as queries/program_bins.sql names them (it also returns BIN_DESCRIPTION, which is left out)
CSV_HEADER = ("PROGRAM_NAME", "BIN_NUMBER")
#: other names a CSV is tried under when its file is open in another program: bins_X_1.csv ... bins_X_5.csv
COPIES = 5
#: the start of a CSV's file name: the TP's own bins / the bins of its units
BINS_PREFIX, UNIT_PREFIX = "bins", "unit_bins"
#: the SQL files (in query_dir): the bins the TP defines, and the bins its units had - looked up from the work week of
#: today minus UNIT_DAYS
BINS_SQL, UNIT_SQL = "program_bins.sql", "unit_tp_bins.sql"
UNIT_DAYS = 60
#: what that SQL returns: the TP name, and the three bins of a unit, the most detailed first.  The bins nest (91030000 is in
#: 9103, which is in 91), so a unit counts once, with the first of them it has - that is all a rule has to cover
UNIT_PROGRAM = "PROGRAM_NAME"
UNIT_BINS = ("DATA_BIN", "FUNCTIONAL_BIN", "INTERFACE_BIN")


class ProgramBinsError(Exception):
    """A lookup failed; the message says why (and, when known, what to do about it)."""


@dataclass(frozen=True)
class ProgramBins:
    """The answer of one lookup."""

    program: str
    bins: tuple[
        tuple[Any, Any], ...
    ]  # (PROGRAM_NAME, BIN_NUMBER) once each, in the order Oracle sent them: the CSV rows
    path: Path  # the CSV
    seconds: float  # Oracle and file together
    prefix: str = BINS_PREFIX  # which CSV it is: bins_<program>.csv (the TP's own bins) or unit_bins_<program>.csv

    @property
    def renamed(self) -> bool:
        """True when the CSV could not have its usual name because that file is open in another program."""
        return self.path.name != csv_name(self.program, prefix=self.prefix)


def csv_name(program: str, copy: int = 0, prefix: str = BINS_PREFIX) -> str:
    """File name of a program's CSV (``copy`` > 0: the name it gets when the usual file is in use).  Only letters, digits,
    ``.``, ``_`` and ``-`` are kept, so no name can leave the folder, and the ``bins_`` in front keeps a name like CON from
    being a Windows device."""
    stem = f"{prefix}_" + re.sub(r"[^A-Za-z0-9._-]", "_", program)[:80]
    return f"{stem}_{copy}.csv" if copy else f"{stem}.csv"


def work_week(day: date) -> int:
    """The Intel work week of ``day`` as YYYYWW, the way ``lao_start_ww`` is written: a week runs from Sunday to Saturday,
    week 1 is the one that holds January 1, and a week belongs to the year of its Saturday (2022-12-25 is 202253,
    2024-12-29 is 202501)."""
    sunday = day - timedelta(days=(day.weekday() + 1) % 7)
    year = (sunday + timedelta(days=6)).year
    january1 = date(year, 1, 1)
    first_sunday = january1 - timedelta(days=(january1.weekday() + 1) % 7)
    return year * 100 + (sunday - first_sunday).days // 7 + 1


def bin_number(value: Any) -> Any:
    """A bin as Oracle sent it, ready for the CSV: a whole number written without ``.0`` (a NUMBER column may come as float
    or Decimal), None for no bin at all."""
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, Decimal) and value == value.to_integral_value():
        return int(value)
    return value


class ProgramBinsService:
    def __init__(
        self,
        portable_dir: str | Path = DEFAULT_PORTABLE_DIR,
        alias: str = DEFAULT_ALIAS,
        out_dir: str | Path | None = None,
        query_dir: str | Path = DEFAULT_QUERY_DIR,
        clock: Clock = utcnow,
    ) -> None:
        self.portable_dir = Path(portable_dir)
        self.alias = alias
        self.out_dir = (
            Path(out_dir)
            if out_dir is not None
            else Path(tempfile.gettempdir()) / "pcs1stop_bins"
        )
        self.query_dir = Path(query_dir)
        self.clock = clock
        self._oracle_db: ModuleType | None = None
        self._load_lock = threading.Lock()
        self._write_lock = (
            threading.Lock()
        )  # two tabs may look up the same program at once

    @property
    def module_path(self) -> Path:
        return self.portable_dir / "oracle_db.py"

    def query(self, program: str) -> ProgramBins:
        """Look ``program`` up in Oracle and save its bins; ProgramBinsError says what went wrong."""
        program = program.strip()
        if not program:
            raise ProgramBinsError("Enter a TP name.")
        oracle = self._load()
        sql = self._read_sql(BINS_SQL)
        started = time.perf_counter()
        try:
            result = oracle.OracleDataAccess(self.alias).get_data(
                sql, "programname", program
            )
        except Exception as error:
            raise ProgramBinsError(self._explain(oracle, error)) from error
        columns = [str(name).upper() for name in result.columns]
        missing = [name for name in CSV_HEADER if name not in columns]
        if missing:
            raise ProgramBinsError(
                f"The Oracle query returned {', '.join(map(str, result.columns))} - no {', '.join(missing)}"
            )
        keep = [columns.index(name) for name in CSV_HEADER]
        # the query is a SELECT DISTINCT: a table without the description column must stay one
        bins = tuple(dict.fromkeys(tuple(row[i] for i in keep) for row in result.rows))
        path = self._save(program, bins)
        return ProgramBins(program, bins, path, time.perf_counter() - started)

    def query_units(self, program: str) -> ProgramBins:
        """Look up the bins the units of ``program`` had in the last UNIT_DAYS days (``queries/unit_tp_bins.sql``) and save
        them as ``unit_bins_<program>.csv``; ProgramBinsError says what went wrong.  The connection is the one of
        oracle_db.py; the SQL file may use any of the bind variables ``programname`` and ``min_ww`` (the work week
        UNIT_DAYS days ago)."""
        program = program.strip()
        if not program:
            raise ProgramBinsError("Enter a TP name.")
        oracle = self._load()
        sql = self._read_sql(UNIT_SQL)
        wanted = {
            "programname": program,
            "min_ww": work_week((self.clock() - timedelta(days=UNIT_DAYS)).date()),
        }
        binds = {
            name: value
            for name, value in wanted.items()
            if re.search(rf":{name}\b", sql, re.IGNORECASE)
        }
        started = time.perf_counter()
        try:
            with oracle.OracleDataAccess(
                self.alias
            ).connect() as connection:  # get_data() takes one bind variable only
                cursor = connection.cursor()
                cursor.execute(sql, binds)
                columns, rows = (
                    [column[0] for column in cursor.description],
                    cursor.fetchall(),
                )
        except Exception as error:
            raise ProgramBinsError(self._explain(oracle, error)) from error
        bins = self._unit_bins(columns, rows)
        path = self._save(program, bins, UNIT_PREFIX)
        return ProgramBins(
            program, bins, path, time.perf_counter() - started, UNIT_PREFIX
        )

    def _read_sql(self, name: str) -> str:
        """The text of ``query_dir/<name>``.  It is read at every lookup, so an edited query is used at once; a ``;`` at its
        end (SQL Developer writes one) is dropped, Oracle refuses it."""
        path = self.query_dir / name
        try:
            text = (
                path.read_text(encoding="utf-8-sig").strip().removesuffix(";").rstrip()
            )
        except FileNotFoundError:
            raise ProgramBinsError(
                f"SQL file not found: {path} - it belongs in the queries folder next to app.py"
            ) from None
        except (OSError, UnicodeError) as error:
            raise ProgramBinsError(f"Cannot read {path}: {error}") from error
        if not text:
            raise ProgramBinsError(f"SQL file is empty: {path}")
        return text

    @staticmethod
    def _unit_bins(
        columns: list[str], rows: list[tuple[Any, ...]]
    ) -> tuple[tuple[Any, Any], ...]:
        """(PROGRAM_NAME, BIN_NUMBER) of the units, each pair once: a unit counts with its most detailed bin (UNIT_BINS)."""
        names = [str(name).upper() for name in columns]
        missing = [name for name in (UNIT_PROGRAM, *UNIT_BINS) if name not in names]
        if missing:
            raise ProgramBinsError(
                f"The Oracle query returned {', '.join(map(str, columns))} - no {', '.join(missing)}"
            )
        program_at, bin_at = (
            names.index(UNIT_PROGRAM),
            [names.index(name) for name in UNIT_BINS],
        )
        found: dict[tuple[Any, Any], None] = {}
        for row in rows:
            number = next(
                (n for n in (bin_number(row[i]) for i in bin_at) if n is not None), None
            )
            if number is not None:  # a unit without any bin has nothing to check
                found[(row[program_at], number)] = None
        return tuple(found)

    def _load(self) -> ModuleType:
        with self._load_lock:
            if self._oracle_db is None:
                path = self.module_path
                if not path.is_file():
                    raise ProgramBinsError(
                        f"Oracle module not found: {path} - set oracle.portable_dir in config.json"
                    )
                spec = importlib.util.spec_from_file_location("pcs_oracle_db", path)
                module = importlib.util.module_from_spec(spec)
                try:
                    spec.loader.exec_module(module)
                except Exception as error:
                    raise ProgramBinsError(
                        f"Cannot load {path}: {type(error).__name__}: {error}"
                    ) from error
                self._oracle_db = module
            return self._oracle_db

    @staticmethod
    def _explain(oracle: ModuleType, error: Exception) -> str:
        """The error as oracle_db.py's own command line puts it: one line, plus its hint for the known Oracle codes."""
        message = " ".join(str(error).split())
        hint = next(
            (
                text
                for code, text in getattr(oracle, "ERROR_HINTS", {}).items()
                if code in message
            ),
            None,
        )
        return f"{type(error).__name__}: {message}" + (f" ({hint})" if hint else "")

    def _save(
        self, program: str, bins: tuple[tuple[Any, Any], ...], prefix: str = BINS_PREFIX
    ) -> Path:
        """Write the CSV.  A file that is open in another program (Excel locks it) cannot be replaced: the bins then go to
        ``bins_<program>_1.csv`` (``_2`` ...), so the lookup still works."""
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
