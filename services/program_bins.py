"""Bins of a test program, from Oracle - what the Program box of the pcs_die_file page looks up.

The Oracle side is ``pcs_oracle_portable/oracle_db.py``: python-oracledb in Thick mode, the Kerberos
login of the Windows user that runs this app, and the Test Program query of the old WinForms app.  That file is loaded by
its path on the first lookup, so the app starts without it and the lookup says what is missing; it is never edited here -
replace the folder to update it (``oracle.portable_dir`` in config.json says where it is).

``ProgramBinsService.query(program)`` runs the query with the program name as a bind variable, keeps two of the three
columns it returns (PROGRAM_NAME, BIN_NUMBER) and writes them to ``%TEMP%\\pcs1stop_bins\\bins_<program>.csv`` - or to
``bins_<program>_1.csv`` and so on when that file is open in another program (Excel locks it), so a lookup never fails for
that.  It blocks for a few seconds (login + query): call it from a thread.
"""
from __future__ import annotations

import csv
import importlib.util
import re
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

DEFAULT_PORTABLE_DIR = Path(__file__).resolve().parent.parent / "pcs_oracle_portable"
DEFAULT_ALIAS = "VN.ARIES"
#: the columns of the CSV, as oracle_db.SQL_PROGRAM_BINS names them (it also returns BIN_DESCRIPTION, which is left out)
CSV_HEADER = ("PROGRAM_NAME", "BIN_NUMBER")
#: other names a CSV is tried under when its file is open in another program: bins_X_1.csv ... bins_X_5.csv
COPIES = 5


class ProgramBinsError(Exception):
    """A lookup failed; the message says why (and, when known, what to do about it)."""


@dataclass(frozen=True)
class ProgramBins:
    """The answer of one lookup."""

    program: str
    bins: tuple[tuple[Any, Any], ...]  # (PROGRAM_NAME, BIN_NUMBER) once each, in the order Oracle sent them: the CSV rows
    path: Path  # the CSV
    seconds: float  # Oracle and file together

    @property
    def renamed(self) -> bool:
        """True when the CSV could not have its usual name because that file is open in another program."""
        return self.path.name != csv_name(self.program)


def csv_name(program: str, copy: int = 0) -> str:
    """File name of a program's CSV (``copy`` > 0: the name it gets when the usual file is in use).  Only letters, digits,
    ``.``, ``_`` and ``-`` are kept, so no name can leave the folder, and the ``bins_`` in front keeps a name like CON from
    being a Windows device."""
    stem = "bins_" + re.sub(r"[^A-Za-z0-9._-]", "_", program)[:80]
    return f"{stem}_{copy}.csv" if copy else f"{stem}.csv"


class ProgramBinsService:
    def __init__(self, portable_dir: str | Path = DEFAULT_PORTABLE_DIR, alias: str = DEFAULT_ALIAS,
                 out_dir: str | Path | None = None) -> None:
        self.portable_dir = Path(portable_dir)
        self.alias = alias
        self.out_dir = Path(out_dir) if out_dir is not None else Path(tempfile.gettempdir()) / "pcs1stop_bins"
        self._oracle_db: ModuleType | None = None
        self._load_lock = threading.Lock()
        self._write_lock = threading.Lock()  # two tabs may look up the same program at once

    @property
    def module_path(self) -> Path:
        return self.portable_dir / "oracle_db.py"

    def query(self, program: str) -> ProgramBins:
        """Look ``program`` up in Oracle and save its bins; ProgramBinsError says what went wrong."""
        program = program.strip()
        if not program:
            raise ProgramBinsError("Enter a program name.")
        oracle = self._load()
        started = time.perf_counter()
        try:
            result = oracle.OracleDataAccess(self.alias).get_data(oracle.SQL_PROGRAM_BINS, "programname", program)
        except Exception as error:  # noqa: BLE001 - oracledb, Kerberos and file errors all end up as one message
            raise ProgramBinsError(self._explain(oracle, error)) from error
        columns = [str(name).upper() for name in result.columns]
        missing = [name for name in CSV_HEADER if name not in columns]
        if missing:
            raise ProgramBinsError(f"The Oracle query returned {', '.join(map(str, result.columns))} - no {', '.join(missing)}")
        keep = [columns.index(name) for name in CSV_HEADER]
        # the query is a SELECT DISTINCT: a table without the description column must stay one
        bins = tuple(dict.fromkeys(tuple(row[i] for i in keep) for row in result.rows))
        path = self._save(program, bins)
        return ProgramBins(program, bins, path, time.perf_counter() - started)

    def _load(self) -> ModuleType:
        with self._load_lock:
            if self._oracle_db is None:
                path = self.module_path
                if not path.is_file():
                    raise ProgramBinsError(f"Oracle module not found: {path} - set oracle.portable_dir in config.json")
                spec = importlib.util.spec_from_file_location("pcs_oracle_db", path)
                module = importlib.util.module_from_spec(spec)
                try:
                    spec.loader.exec_module(module)
                except Exception as error:  # noqa: BLE001
                    raise ProgramBinsError(f"Cannot load {path}: {type(error).__name__}: {error}") from error
                self._oracle_db = module
            return self._oracle_db

    @staticmethod
    def _explain(oracle: ModuleType, error: Exception) -> str:
        """The error as oracle_db.py's own command line puts it: one line, plus its hint for the known Oracle codes."""
        message = " ".join(str(error).split())
        hint = next((text for code, text in getattr(oracle, "ERROR_HINTS", {}).items() if code in message), None)
        return f"{type(error).__name__}: {message}" + (f" ({hint})" if hint else "")

    def _save(self, program: str, bins: tuple[tuple[Any, Any], ...]) -> Path:
        """Write the CSV.  A file that is open in another program (Excel locks it) cannot be replaced: the bins then go to
        ``bins_<program>_1.csv`` (``_2`` ...), so the lookup still works."""
        try:
            self.out_dir.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise ProgramBinsError(f"Cannot write {self.out_dir}: {error.strerror or error}") from error
        in_use: PermissionError | None = None
        with self._write_lock:
            for copy in range(COPIES + 1):
                path = self.out_dir / csv_name(program, copy)
                try:
                    with path.open("w", newline="", encoding="utf-8") as handle:
                        writer = csv.writer(handle)
                        writer.writerow(CSV_HEADER)
                        writer.writerows(bins)
                except PermissionError as error:
                    in_use = in_use or error
                    continue
                except OSError as error:
                    raise ProgramBinsError(f"Cannot write {path}: {error.strerror or error}") from error
                return path
        raise ProgramBinsError(
            f"Cannot write {self.out_dir / csv_name(program)}: {in_use.strerror or in_use} - is it open in another program?"
        )
