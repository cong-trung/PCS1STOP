"""PyUber/Polars TP-bin lookup."""

from __future__ import annotations

import re
import time
from collections.abc import Iterable
from datetime import timedelta
from pathlib import Path
from typing import Any

import polars as pl

from .clock import Clock, utcnow
from .program_bins_common import (
    BINS_SQL,
    CSV_HEADER,
    DEFAULT_QUERY_DIR,
    UNIT_BINS,
    UNIT_DAYS,
    UNIT_PREFIX,
    UNIT_PROGRAM,
    UNIT_SQL,
    ProgramBins,
    ProgramBinsError,
    ProgramBinsFiles,
    bin_number,
    work_week,
)

DEFAULT_DATASOURCES = ("VN_PROD_ARIES", "SVC_PROD_ARIES")

_PROGRAM_SCHEMA = {
    "PROGRAM_NAME": pl.String,
    "BIN_DESCRIPTION": pl.String,
    "BIN_NUMBER": pl.Float64,
}
_UNIT_SCHEMA = {
    "PROGRAM_NAME": pl.String,
    "INTERFACE_BIN": pl.Float64,
    "FUNCTIONAL_BIN": pl.Float64,
    "DATA_BIN": pl.Float64,
}


class _PolarsCursor:
    """Adapt PyUber's two-field column descriptions to Polars' DB-API expectations."""

    def __init__(self, cursor: Any) -> None:
        self._cursor = cursor

    @property
    def description(self) -> list[tuple[Any, ...]] | None:
        description = self._cursor.description
        if description is None:
            return None
        return [
            (column[0], None, None, None, None, None, None) for column in description
        ]

    def __getattr__(self, name: str) -> Any:
        return getattr(self._cursor, name)


class PyUberProgramBinsService(ProgramBinsFiles):
    """Run the SQL-file lookups through PyUber and Polars."""

    def __init__(
        self,
        datasources: str | Iterable[str] = DEFAULT_DATASOURCES,
        out_dir: str | Path | None = None,
        query_dir: str | Path = DEFAULT_QUERY_DIR,
        clock: Clock = utcnow,
    ) -> None:
        names = (datasources,) if isinstance(datasources, str) else tuple(datasources)
        if not names or not all(
            isinstance(name, str) and name.strip() for name in names
        ):
            raise ProgramBinsError("At least one PyUber datasource name is required.")
        names = tuple(dict.fromkeys(name.strip() for name in names))
        super().__init__(out_dir, query_dir)
        self.datasources = names
        self.clock = clock

    def query(self, program: str) -> ProgramBins:
        """Look up a TP's bins in each PyUber datasource and write distinct program/bin pairs."""
        program = program.strip().upper()
        if not program:
            raise ProgramBinsError("Enter a TP name.")
        sql = self._read_sql(BINS_SQL)
        started = time.perf_counter()
        found: dict[tuple[Any, Any], None] = {}
        for datasource in self.datasources:
            frame = self._read_frame(
                datasource, sql, {"programname": program}, _PROGRAM_SCHEMA
            )
            columns = [str(name).upper() for name in frame.columns]
            missing = [name for name in CSV_HEADER if name not in columns]
            if missing:
                raise ProgramBinsError(
                    f"{datasource}: The PyUber query returned "
                    f"{', '.join(map(str, frame.columns))} - no {', '.join(missing)}"
                )
            keep = [columns.index(name) for name in CSV_HEADER]
            found.update(
                dict.fromkeys(
                    (row[keep[0]], bin_number(row[keep[1]]))
                    for row in frame.iter_rows()
                )
            )
        bins = tuple(found)
        path = self._save(program, bins)
        return ProgramBins(program, bins, path, time.perf_counter() - started)

    def query_units(self, program: str) -> ProgramBins:
        """Look up the unit-level bins from the last ``UNIT_DAYS`` days in each PyUber datasource."""
        program = program.strip().upper()
        if not program:
            raise ProgramBinsError("Enter a TP name.")
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
        found: dict[tuple[Any, Any], None] = {}
        for datasource in self.datasources:
            frame = self._read_frame(datasource, sql, binds, _UNIT_SCHEMA)
            rows = list(frame.iter_rows())
            found.update(
                dict.fromkeys(self._unit_bins(datasource, frame.columns, rows))
            )
        bins = tuple(found)
        path = self._save(program, bins, UNIT_PREFIX)
        return ProgramBins(
            program, bins, path, time.perf_counter() - started, UNIT_PREFIX
        )

    @staticmethod
    def _read_frame(
        datasource: str,
        sql: str,
        parameters: dict[str, Any],
        schema_overrides: dict[str, Any],
    ) -> pl.DataFrame:
        try:
            import PyUber

            with PyUber.connect(datasource=datasource) as connection:
                cursor = _PolarsCursor(connection.cursor())
                return pl.read_database(
                    query=sql,
                    connection=cursor,
                    execute_options={"parameters": parameters},
                    schema_overrides=schema_overrides,
                )
        except Exception as error:
            message = " ".join(str(error).split())
            raise ProgramBinsError(
                f"{datasource}: {type(error).__name__}: {message}"
            ) from error

    @staticmethod
    def _unit_bins(
        datasource: str, columns: list[str], rows: list[tuple[Any, ...]]
    ) -> tuple[tuple[Any, Any], ...]:
        names = [str(name).upper() for name in columns]
        missing = [name for name in (UNIT_PROGRAM, *UNIT_BINS) if name not in names]
        if missing:
            raise ProgramBinsError(
                f"{datasource}: The PyUber query returned "
                f"{', '.join(map(str, columns))} - no {', '.join(missing)}"
            )
        names = [str(name).upper() for name in columns]
        program_at = names.index(UNIT_PROGRAM)
        bin_at = [names.index(name) for name in UNIT_BINS]
        found: dict[tuple[Any, Any], None] = {}
        for row in rows:
            number = next(
                (
                    value
                    for value in (bin_number(row[i]) for i in bin_at)
                    if value is not None
                ),
                None,
            )
            if number is not None:
                found[(row[program_at], number)] = None
        return tuple(found)
