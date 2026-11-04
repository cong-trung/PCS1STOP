"""The TP Name box's Oracle lookups: the portable module is loaded by its path, the query runs with bind variables, and
PROGRAM_NAME + BIN_NUMBER end up in a CSV in the temp folder.  Oracle itself is never reached: the tests load a stand-in
for oracle_db.py (the names the service uses are the real ones), and one test checks that the real file still has them.
The unit level lookup also reads its SQL from the queries folder."""
from __future__ import annotations

import csv
import re
import threading
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from services.program_bins import (
    BINS_SQL,
    COPIES,
    CSV_HEADER,
    DEFAULT_ALIAS,
    DEFAULT_PORTABLE_DIR,
    DEFAULT_QUERY_DIR,
    UNIT_DAYS,
    UNIT_SQL,
    ProgramBinsError,
    ProgramBinsService,
    bin_number,
    csv_name,
    work_week,
)

COLUMNS = ["PROGRAM_NAME", "BIN_DESCRIPTION", "BIN_NUMBER"]  # what queries/program_bins.sql returns
ROWS = [
    ("EMRR1X3B13K2634A", "ACTUATION_ACTUATE", 9103),
    ("EMRR1X3B13K2634A", "ACTUATION_ACTUATE_FAILURE", 91030000),
    ("EMRR1X3B13K2634A", "ACTUATION_DEACTUATE", 9104),
]
UNIT_COLUMNS = ["PROGRAM_NAME", "INTERFACE_BIN", "FUNCTIONAL_BIN", "DATA_BIN"]  # what queries/unit_tp_bins.sql returns
UNIT_ROWS = [
    ("EMRR1X3B13K2634A", 91, 9103, 91030000),
    ("EMRR1X3B13K2634A", 91, 9103, 91030001),
    ("EMRR1X3B13K2634A", 70, 7006, 70060000),
]
BINS_SQL_TEXT = "select program_name, description, bin_number from bins where program_name = :programname"
UNIT_SQL_TEXT = (
    "-- bins of the units\n"
    "select program_name, interface_bin, functional_bin, data_bin from units\n"
    "where lao_start_ww >= :min_ww and upper(program_name) = upper(:programname);\n"
)

STAND_IN = '''
from decimal import Decimal
from typing import NamedTuple

ERROR_HINTS = {"ORA-12154": "tnsnames.ora not found"}
CALLS = []
EXECUTED = []
CLOSED = []
ERROR = @ERROR@
ANSWER = (@COLUMNS@, @ROWS@)
UNIT_ANSWER = (@UNIT_COLUMNS@, @UNIT_ROWS@)


class QueryResult(NamedTuple):
    columns: list
    rows: list


class Cursor:
    def __init__(self, alias):
        self.alias, self.description, self.rows = alias, None, []

    def execute(self, sql, binds=None):
        EXECUTED.append((self.alias, sql, dict(binds or {})))
        if ERROR is not None:
            raise RuntimeError(ERROR)
        self.description = [(name, None) for name in UNIT_ANSWER[0]]
        self.rows = list(UNIT_ANSWER[1])

    def fetchall(self):
        return self.rows


class Connection:
    def __init__(self, alias):
        self.alias = alias

    def __enter__(self):
        return self

    def __exit__(self, *error):
        CLOSED.append(self.alias)
        return False

    def cursor(self):
        return Cursor(self.alias)


class OracleDataAccess:
    def __init__(self, tns_alias="VN.ARIES"):
        self.alias = tns_alias

    def connect(self):
        return Connection(self.alias)

    def get_data(self, query, parameter_name=None, parameter_value=None):
        CALLS.append((self.alias, query, parameter_name, parameter_value))
        if ERROR is not None:
            raise RuntimeError(ERROR)
        return QueryResult(*ANSWER)
'''


def portable(tmp_path: Path, *, columns=COLUMNS, rows=ROWS, unit_columns=UNIT_COLUMNS, unit_rows=UNIT_ROWS, error=None,
             source=None) -> Path:
    """A folder with a stand-in oracle_db.py that answers with ``rows`` (or raises ``error``)."""
    folder = tmp_path / "portable"
    folder.mkdir(exist_ok=True)
    text = source or (
        STAND_IN.replace("@COLUMNS@", repr(columns))
        .replace("@ROWS@", repr(rows))
        .replace("@UNIT_COLUMNS@", repr(unit_columns))
        .replace("@UNIT_ROWS@", repr(unit_rows))
        .replace("@ERROR@", repr(error))
    )
    (folder / "oracle_db.py").write_text(text, encoding="utf-8")
    return folder


def queries(tmp_path: Path, sql: str | None = UNIT_SQL_TEXT, bins_sql: str | None = BINS_SQL_TEXT) -> Path:
    """A queries folder with the two SQL files (None: no such file)."""
    folder = tmp_path / "queries"
    folder.mkdir(exist_ok=True)
    for name, text in ((UNIT_SQL, sql), (BINS_SQL, bins_sql)):
        if text is not None:
            (folder / name).write_text(text, encoding="utf-8")
    return folder


def service(tmp_path: Path, **kwargs) -> ProgramBinsService:
    alias = kwargs.pop("alias", DEFAULT_ALIAS)
    sql, bins_sql = kwargs.pop("sql", UNIT_SQL_TEXT), kwargs.pop("bins_sql", BINS_SQL_TEXT)
    clock = kwargs.pop("clock", None)
    extra = {} if clock is None else {"clock": clock}
    return ProgramBinsService(portable(tmp_path, **kwargs), alias, tmp_path / "out", queries(tmp_path, sql, bins_sql), **extra)


def read_csv(path: Path) -> list[list[str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.reader(handle))


def test_the_bins_are_saved_as_program_name_and_bin_number(tmp_path):
    found = service(tmp_path).query("  EMRR1X3B13K2634A ")  # blanks around the name are not part of it
    assert found.program == "EMRR1X3B13K2634A"
    assert found.path == tmp_path / "out" / "bins_EMRR1X3B13K2634A.csv"
    assert read_csv(found.path) == [
        ["PROGRAM_NAME", "BIN_NUMBER"],
        ["EMRR1X3B13K2634A", "9103"],
        ["EMRR1X3B13K2634A", "91030000"],
        ["EMRR1X3B13K2634A", "9104"],
    ]  # no BIN_DESCRIPTION
    assert found.bins == (("EMRR1X3B13K2634A", 9103), ("EMRR1X3B13K2634A", 91030000), ("EMRR1X3B13K2634A", 9104))
    assert found.seconds >= 0 and CSV_HEADER == ("PROGRAM_NAME", "BIN_NUMBER")


def test_the_name_is_a_bind_variable_and_the_alias_is_the_configured_one(tmp_path):
    lookup = service(tmp_path, alias="PG.ARIES")
    hostile = "X' OR '1'='1"
    lookup.query(hostile)
    ((alias, sql, name, value),) = lookup._oracle_db.CALLS
    assert (alias, name, value) == ("PG.ARIES", "programname", hostile)
    assert sql == BINS_SQL_TEXT  # the text of queries/program_bins.sql: oracle_db.py has no SQL of its own
    assert ":programname" in sql and hostile not in sql  # the name never becomes part of the SQL text


def test_the_tp_query_is_the_text_of_its_sql_file_read_at_every_lookup(tmp_path):
    lookup = service(tmp_path, bins_sql="select 1 from dual;  \n")  # (the ; is dropped, Oracle refuses it)
    lookup.query("P")
    (tmp_path / "queries" / BINS_SQL).write_text("select 2 from dual", encoding="utf-8")  # edited: no restart
    lookup.query("P")
    assert [sql for _, sql, _, _ in lookup._oracle_db.CALLS] == ["select 1 from dual", "select 2 from dual"]


def test_a_missing_tp_sql_file_says_so_and_nothing_reaches_oracle(tmp_path):
    lookup = service(tmp_path, bins_sql=None)
    with pytest.raises(ProgramBinsError, match=r"SQL file not found: .*program_bins\.sql") as caught:
        lookup.query("P")
    assert str(tmp_path / "queries") in str(caught.value)
    assert lookup._oracle_db.CALLS == [] and not (tmp_path / "out").exists()


def test_a_table_without_the_description_stays_a_set(tmp_path):
    """The real query is a SELECT DISTINCT; two bins that differ only in their description are one row here."""
    rows = [("P", "A", 1), ("P", "B", 2), ("P", "C", 1), ("P", "D", 3)]
    found = service(tmp_path, rows=rows).query("P")
    assert found.bins == (("P", 1), ("P", 2), ("P", 3))  # first appearance wins, Oracle's order is kept
    assert read_csv(found.path)[1:] == [["P", "1"], ["P", "2"], ["P", "3"]]


def test_the_columns_are_found_by_name_not_by_position(tmp_path):
    found = service(tmp_path, columns=["bin_number", "program_name"], rows=[(7, "P")]).query("P")
    assert read_csv(found.path) == [["PROGRAM_NAME", "BIN_NUMBER"], ["P", "7"]]
    with pytest.raises(ProgramBinsError, match="no BIN_NUMBER"):
        service(tmp_path, columns=["PROGRAM_NAME", "BIN_DESCRIPTION"], rows=[("P", "x")]).query("P")


def test_a_program_without_bins_leaves_a_csv_with_only_the_header(tmp_path):
    lookup = service(tmp_path)
    assert lookup.query("EMRR1X3B13K2634A").bins
    lookup._oracle_db.OracleDataAccess.get_data = lambda self, *args: lookup._oracle_db.QueryResult(COLUMNS, [])
    found = lookup.query("EMRR1X3B13K2634A")  # the program has no bins any more: the file of the last lookup is replaced
    assert found.bins == () and read_csv(found.path) == [["PROGRAM_NAME", "BIN_NUMBER"]]


@pytest.mark.parametrize(
    "name",
    ["..\\..\\evil", "../../evil", "C:\\Windows\\x", "a b", "CON", "NUL.txt", "ü€", "x" * 500, ".", ".."],
)
def test_no_name_can_make_the_file_leave_its_folder(tmp_path, name):
    found = service(tmp_path).query(name)
    assert found.path.parent == tmp_path / "out" and found.path.is_file()
    assert found.path.name.startswith("bins_") and found.path.suffix == ".csv" and len(found.path.name) <= 90
    assert csv_name(name) == found.path.name


def test_an_empty_name_is_refused_before_anything_is_loaded(tmp_path):
    lookup = ProgramBinsService(tmp_path / "nothing-here", out_dir=tmp_path)
    for blank in ("", "   ", "\t\n"):
        with pytest.raises(ProgramBinsError, match="Enter a TP name"):
            lookup.query(blank)
        with pytest.raises(ProgramBinsError, match="Enter a TP name"):
            lookup.query_units(blank)


def test_a_missing_oracle_folder_says_where_it_looked_and_what_to_set(tmp_path):
    lookup = ProgramBinsService(tmp_path / "nothing-here", out_dir=tmp_path)
    with pytest.raises(ProgramBinsError) as caught:
        lookup.query("P")
    assert str(tmp_path / "nothing-here" / "oracle_db.py") in str(caught.value)
    assert "oracle.portable_dir" in str(caught.value)
    assert lookup.module_path == tmp_path / "nothing-here" / "oracle_db.py"


def test_the_folder_may_appear_after_the_app_started(tmp_path):
    lookup = ProgramBinsService(tmp_path / "portable", out_dir=tmp_path / "out")
    with pytest.raises(ProgramBinsError):
        lookup.query("P")
    portable(tmp_path)  # somebody copies the folder in: the next lookup finds it, no restart
    assert lookup.query("P").bins


def test_a_module_that_cannot_be_loaded_says_so(tmp_path):
    lookup = ProgramBinsService(portable(tmp_path, source="raise ImportError('no oracledb here')"), out_dir=tmp_path / "out")
    with pytest.raises(ProgramBinsError, match=r"Cannot load .*oracle_db\.py: ImportError: no oracledb here"):
        lookup.query("P")


def test_an_oracle_error_becomes_one_line_with_its_hint_and_writes_nothing(tmp_path):
    lookup = service(tmp_path, error="ORA-12154: TNS:could not resolve\n   the connect identifier")
    with pytest.raises(ProgramBinsError) as caught:
        lookup.query("P")
    assert str(caught.value) == (
        "RuntimeError: ORA-12154: TNS:could not resolve the connect identifier (tnsnames.ora not found)"
    )
    assert not (tmp_path / "out").exists()


def test_an_error_without_a_known_code_has_no_hint(tmp_path):
    with pytest.raises(ProgramBinsError) as caught:
        service(tmp_path, error="ORA-99999: something new").query("P")
    assert str(caught.value) == "RuntimeError: ORA-99999: something new"


def test_a_folder_that_cannot_be_written_is_reported(tmp_path):
    (tmp_path / "blocker").write_text("a file where the folder should be")
    lookup = ProgramBinsService(portable(tmp_path), out_dir=tmp_path / "blocker")
    with pytest.raises(ProgramBinsError, match="Cannot write"):
        lookup.query("P")


@pytest.fixture
def locked(monkeypatch) -> set[str]:
    """Names of files that behave like files open in Excel: opening them for writing is refused (reading is not)."""
    names: set[str] = set()
    real_open = Path.open

    def open_unless_locked(self, *args, **kwargs):
        mode = args[0] if args else kwargs.get("mode", "r")
        if self.name in names and "w" in mode:
            raise PermissionError(13, "Permission denied", str(self))
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", open_unless_locked)
    return names


def test_a_csv_that_is_open_in_another_program_does_not_fail_the_lookup(tmp_path, locked):
    """The user opens bins_<program>.csv in Excel, which locks it - the next lookup of that program must still work."""
    lookup = service(tmp_path)
    first = lookup.query("EMRR1X3B13K2634A")
    assert first.path.name == "bins_EMRR1X3B13K2634A.csv" and not first.renamed

    locked.add("bins_EMRR1X3B13K2634A.csv")
    again = lookup.query("EMRR1X3B13K2634A")
    assert again.path == tmp_path / "out" / "bins_EMRR1X3B13K2634A_1.csv" and again.renamed
    assert again.bins == first.bins and read_csv(again.path) == read_csv(first.path)  # the same table, in the next file

    locked.add("bins_EMRR1X3B13K2634A_1.csv")
    assert lookup.query("EMRR1X3B13K2634A").path.name == "bins_EMRR1X3B13K2634A_2.csv"

    locked.clear()  # Excel is closed again: the usual file is used (and replaced) again
    last = lookup.query("EMRR1X3B13K2634A")
    assert last.path == first.path and not last.renamed


def test_when_every_name_is_in_use_the_lookup_says_so(tmp_path, locked):
    locked.update({csv_name("P", copy) for copy in range(COPIES + 1)})
    with pytest.raises(ProgramBinsError, match=r"Cannot write .*bins_P\.csv: Permission denied - is it open in another program\?"):
        service(tmp_path).query("P")


def test_other_files_are_not_in_the_way_of_a_locked_one(tmp_path, locked):
    locked.add("bins_P.csv")
    assert service(tmp_path).query("Q").path.name == "bins_Q.csv"


def test_a_copy_has_the_name_of_the_csv_and_a_number():
    assert csv_name("P") == "bins_P.csv" and csv_name("P", 0) == "bins_P.csv"
    assert csv_name("P", 2) == "bins_P_2.csv" and csv_name("a b", 1) == "bins_a_b_1.csv"


def test_two_lookups_of_one_program_at_once_leave_one_whole_file(tmp_path):
    rows = [("BIG", f"bin {n}", n) for n in range(3000)]
    lookup = service(tmp_path, rows=rows)
    errors: list[BaseException] = []

    def run() -> None:
        try:
            for _ in range(5):
                lookup.query("BIG")
        except BaseException as error:  # noqa: BLE001
            errors.append(error)

    threads = [threading.Thread(target=run) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    table = read_csv(tmp_path / "out" / "bins_BIG.csv")
    assert table[0] == ["PROGRAM_NAME", "BIN_NUMBER"] and table[1:] == [["BIG", str(n)] for n in range(3000)]


# --------------------------------------------------------------------------------------- the unit level lookup
def test_the_bins_of_the_units_are_saved_as_program_name_and_bin_number(tmp_path):
    found = service(tmp_path).query_units("  EMRR1X3B13K2634A ")  # blanks around the name are not part of it
    assert found.program == "EMRR1X3B13K2634A" and found.prefix == "unit_bins"
    assert found.path == tmp_path / "out" / "unit_bins_EMRR1X3B13K2634A.csv" and not found.renamed
    assert read_csv(found.path) == [
        ["PROGRAM_NAME", "BIN_NUMBER"],
        ["EMRR1X3B13K2634A", "91030000"],
        ["EMRR1X3B13K2634A", "91030001"],
        ["EMRR1X3B13K2634A", "70060000"],
    ]  # one bin per unit: its data bin
    assert found.bins == (("EMRR1X3B13K2634A", 91030000), ("EMRR1X3B13K2634A", 91030001), ("EMRR1X3B13K2634A", 70060000))
    assert found.seconds >= 0


def test_a_unit_counts_with_its_most_detailed_bin_and_once(tmp_path):
    rows = [
        ("P", 91, 9103, 91030000),
        ("P", 91, 9103, None),  # no data bin: the functional bin
        ("P", 70, None, None),  # only an interface bin
        ("P", None, None, None),  # no bin at all: nothing to check
        ("P", 91, 9103, 91030000.0),  # a NUMBER that arrives as float is the same bin - and is not written as 91030000.0
        ("P", 91, Decimal("9104"), None),
        ("P", 1, 100, "  "),  # a blank is no bin
    ]
    found = service(tmp_path, unit_rows=rows).query_units("P")
    assert found.bins == (("P", 91030000), ("P", 9103), ("P", 70), ("P", 9104), ("P", 100))
    assert read_csv(found.path)[1:] == [["P", "91030000"], ["P", "9103"], ["P", "70"], ["P", "9104"], ["P", "100"]]


def test_the_unit_query_binds_the_name_and_the_first_work_week(tmp_path, clock):
    lookup = service(tmp_path, alias="PG.ARIES", clock=clock)
    hostile = "X' OR '1'='1"
    lookup.query_units(hostile)
    ((alias, sql, binds),) = lookup._oracle_db.EXECUTED
    assert alias == "PG.ARIES"
    assert binds == {"programname": hostile, "min_ww": 202445}  # 2025-01-06 minus 60 days is 2024-11-07, in work week 45
    assert ":programname" in sql and ":min_ww" in sql and hostile not in sql  # the name never becomes part of the SQL text
    assert sql.startswith("-- bins of the units") and not sql.endswith(";")  # the file's text, without its closing ;
    assert lookup._oracle_db.CLOSED == ["PG.ARIES"] and lookup._oracle_db.CALLS == []  # connect() is used, and closed again


def test_the_window_is_counted_back_from_today(tmp_path, clock):
    assert UNIT_DAYS == 60
    lookup = service(tmp_path, clock=clock)
    lookup.query_units("P")
    clock.advance(days=7)  # a week later the first work week is one later
    lookup.query_units("P")
    assert [binds["min_ww"] for _, _, binds in lookup._oracle_db.EXECUTED] == [202445, 202446]


def test_a_bind_variable_the_sql_does_not_use_is_not_passed(tmp_path, clock):
    """Oracle refuses a bind variable that is not in the statement: a query edited down must still run."""
    only_name = service(tmp_path, clock=clock, sql="select program_name from units where program_name = :PROGRAMNAME")
    only_name.query_units("P")
    assert only_name._oracle_db.EXECUTED[0][2] == {"programname": "P"}  # (a name is found in any case)
    no_binds = service(tmp_path, clock=clock, sql="select program_name from units")
    no_binds.query_units("P")
    assert no_binds._oracle_db.EXECUTED[0][2] == {}


def test_the_sql_file_is_read_at_every_lookup_so_an_edit_needs_no_restart(tmp_path):
    lookup = service(tmp_path)
    lookup.query_units("P")
    (tmp_path / "queries" / UNIT_SQL).write_text("select 2 from dual", encoding="utf-8")
    lookup.query_units("P")
    assert [sql for _, sql, _ in lookup._oracle_db.EXECUTED][1] == "select 2 from dual"


def test_a_file_saved_with_a_byte_order_mark_and_a_semicolon_is_fine(tmp_path):
    lookup = service(tmp_path, sql=None)
    (tmp_path / "queries" / UNIT_SQL).write_bytes(b"\xef\xbb\xbfselect 1 from dual;  \r\n")  # as Notepad / SQL Developer write it
    lookup.query_units("P")
    assert lookup._oracle_db.EXECUTED[0][1] == "select 1 from dual"


def test_a_missing_empty_or_unreadable_sql_file_says_so(tmp_path):
    lookup = service(tmp_path, sql=None)
    path = tmp_path / "queries" / UNIT_SQL
    with pytest.raises(ProgramBinsError, match=r"SQL file not found: .*unit_tp_bins\.sql") as caught:
        lookup.query_units("P")
    assert str(tmp_path / "queries") in str(caught.value)
    path.write_text("  \n", encoding="utf-8")
    with pytest.raises(ProgramBinsError, match="SQL file is empty"):
        lookup.query_units("P")
    path.write_bytes(b"\xff\xfe select")  # not UTF-8
    with pytest.raises(ProgramBinsError, match="Cannot read"):
        lookup.query_units("P")
    path.unlink()
    path.mkdir()  # a folder where the file should be
    with pytest.raises(ProgramBinsError, match="Cannot read"):
        lookup.query_units("P")
    assert lookup._oracle_db.EXECUTED == [] and not (tmp_path / "out").exists()  # nothing reached Oracle, nothing was written


def test_a_missing_oracle_folder_is_reported_by_the_unit_lookup_too(tmp_path):
    lookup = ProgramBinsService(tmp_path / "nothing-here", out_dir=tmp_path, query_dir=queries(tmp_path))
    with pytest.raises(ProgramBinsError, match="Oracle module not found") as caught:
        lookup.query_units("P")
    assert "oracle.portable_dir" in str(caught.value)


def test_the_unit_columns_are_found_by_name_and_the_missing_ones_are_named(tmp_path):
    shuffled = service(
        tmp_path,
        unit_columns=["data_bin", "x", "functional_bin", "program_name", "interface_bin"],
        unit_rows=[(91030000, "?", 9103, "P", 91)],
    ).query_units("P")
    assert shuffled.bins == (("P", 91030000),)
    with pytest.raises(ProgramBinsError, match="The Oracle query returned PROGRAM_NAME, DATA_BIN - no FUNCTIONAL_BIN, INTERFACE_BIN"):
        service(tmp_path, unit_columns=["PROGRAM_NAME", "DATA_BIN"], unit_rows=[("P", 1)]).query_units("P")


def test_an_oracle_error_of_the_unit_query_is_one_line_with_its_hint_and_writes_nothing(tmp_path):
    lookup = service(tmp_path, error="ORA-12154: TNS:could not resolve\n   the connect identifier")
    with pytest.raises(ProgramBinsError) as caught:
        lookup.query_units("P")
    assert str(caught.value) == (
        "RuntimeError: ORA-12154: TNS:could not resolve the connect identifier (tnsnames.ora not found)"
    )
    assert not (tmp_path / "out").exists() and lookup._oracle_db.CLOSED == ["VN.ARIES"]  # (the connection is closed anyway)


def test_a_tp_without_units_leaves_a_csv_with_only_the_header(tmp_path):
    found = service(tmp_path, unit_rows=[]).query_units("P")
    assert found.bins == () and read_csv(found.path) == [["PROGRAM_NAME", "BIN_NUMBER"]]


def test_the_two_lookups_of_a_tp_keep_their_own_files(tmp_path):
    lookup = service(tmp_path)
    own, units = lookup.query("P"), lookup.query_units("P")
    assert (own.path.name, units.path.name) == ("bins_P.csv", "unit_bins_P.csv")
    assert read_csv(own.path)[1:] != read_csv(units.path)[1:]  # each file keeps its own rows
    assert csv_name("P", prefix="unit_bins") == "unit_bins_P.csv" and csv_name("a b", 2, "unit_bins") == "unit_bins_a_b_2.csv"


@pytest.mark.parametrize("name", ["..\\..\\evil", "../../evil", "C:\\Windows\\x", "CON", "x" * 500, ".."])
def test_no_name_can_make_the_unit_csv_leave_its_folder(tmp_path, name):
    found = service(tmp_path).query_units(name)
    assert found.path.parent == tmp_path / "out" and found.path.is_file()
    assert found.path.name.startswith("unit_bins_") and found.path.suffix == ".csv" and len(found.path.name) <= 100
    assert csv_name(name, prefix="unit_bins") == found.path.name


def test_a_unit_csv_that_is_open_in_another_program_does_not_fail_the_lookup(tmp_path, locked):
    lookup = service(tmp_path)
    locked.add("unit_bins_P.csv")
    found = lookup.query_units("P")
    assert found.path.name == "unit_bins_P_1.csv" and found.renamed
    assert lookup.query("P").path.name == "bins_P.csv"  # (the other CSV is not in its way)
    locked.clear()
    assert lookup.query_units("P").path.name == "unit_bins_P.csv"


@pytest.mark.parametrize(
    ("day", "week"),
    [
        (date(2025, 5, 18), 202521),  # the work week the old query started at: 2025 WW21 is May 18 - 24
        (date(2025, 5, 24), 202521),
        (date(2025, 5, 25), 202522),
        (date(2023, 1, 1), 202301),  # a year that starts on a Sunday starts its week 1 that day
        (date(2024, 12, 28), 202452),
        (date(2024, 12, 29), 202501),  # the week that holds January 1 is week 1 of the new year, though it starts in December
        (date(2025, 1, 4), 202501),
        (date(2025, 1, 5), 202502),
        (date(2021, 12, 26), 202201),
        (date(2022, 1, 1), 202201),  # (a Saturday: January 1 is the last day of its week 1)
        (date(2022, 12, 25), 202253),  # 2022 had 53 weeks
        (date(2022, 12, 31), 202253),
        (date(2025, 12, 27), 202552),
        (date(2025, 12, 28), 202601),
        (date(2026, 9, 5), 202636),
        (date(2026, 11, 4), 202645),
    ],
)
def test_the_work_week_is_the_intel_one(day, week):
    assert work_week(day) == week


def test_work_weeks_are_seven_days_from_sunday_and_follow_one_another():
    days: dict[int, list[date]] = {}
    day = date(2019, 12, 1)
    while day < date(2031, 1, 1):
        days.setdefault(work_week(day), []).append(day)
        day += timedelta(days=1)
    weeks = sorted(days)
    for week in weeks[1:-1]:  # (the first and the last are cut off by the loop)
        assert len(days[week]) == 7 and days[week][0].weekday() == 6, week  # Sunday to Saturday
    for before, after in zip(weeks, weeks[1:]):
        same_year = after == before + 1
        new_year = after % 100 == 1 and before % 100 in (52, 53) and after // 100 == before // 100 + 1
        assert same_year or new_year, (before, after)


@pytest.mark.parametrize(
    ("raw", "wanted"),
    [
        (None, None), ("", None), ("  ", None), (" 91 ", "91"), ("abc", "abc"), (0, 0), (91, 91), (91.0, 91),
        (Decimal("91030000"), 91030000), (91.5, 91.5), (Decimal("91.5"), Decimal("91.5")),
    ],
)
def test_a_bin_is_written_as_a_whole_number_or_not_at_all(raw, wanted):
    got = bin_number(raw)
    assert got == wanted and type(got) is type(wanted)


def test_the_sql_files_of_the_project_have_the_bind_variables_and_the_columns_the_service_uses(tmp_path, clock):
    lookup = ProgramBinsService(portable(tmp_path), out_dir=tmp_path / "out", clock=clock)  # the queries folder of the project
    assert lookup.query_dir == DEFAULT_QUERY_DIR
    assert (DEFAULT_QUERY_DIR / BINS_SQL).is_file() and (DEFAULT_QUERY_DIR / UNIT_SQL).is_file()

    lookup.query("EMRR1X3B13K2634A")  # the Test Program query, moved here from oracle_db.py
    ((_, sql, name, value),) = lookup._oracle_db.CALLS
    assert (name, value) == ("programname", "EMRR1X3B13K2634A")
    squeezed = " ".join(sql.upper().split())
    assert "A_TEST_PROGRAM_BIN" in squeezed and "<> 'ETEST'" in squeezed and "= :PROGRAMNAME" in squeezed
    assert all(column.lower() in sql.lower() for column in CSV_HEADER)

    lookup.query_units("EMRR1X3B13K2634A")
    ((_, sql, binds),) = lookup._oracle_db.EXECUTED
    assert binds == {"programname": "EMRR1X3B13K2634A", "min_ww": 202445}  # the file uses both
    squeezed = " ".join(re.sub(r"--[^\n]*", "", sql).upper().split())  # the code, without the comments
    assert squeezed.startswith("SELECT /*+ LEADING(V0 DT) USE_NL(DT) */ DISTINCT V0.PROGRAM_NAME AS PROGRAM_NAME ,DT.INTERFACE_BIN")  # only the bins come out
    assert "V0.PROGRAM_NAME = UPPER(:PROGRAMNAME)" in squeezed and "V0.LAO_START_WW >= :MIN_WW" in squeezed
    assert "UPPER(V0.PROGRAM_NAME)" not in squeezed  # a function on the column hides the index TS_TP_FK_I: Oracle would read every session
    assert "A_DEVICE_ITEM" not in squeezed  # none of their columns reaches the bins, and Oracle probes them for every unit
    assert "LAO_START_WW >= 2025" not in squeezed  # (no work week is written into the text any more)
    assert sql.count("(") == sql.count(")")  # nothing is left open


REAL = DEFAULT_PORTABLE_DIR / "oracle_db.py"


@pytest.mark.skipif(not REAL.is_file(), reason="pcs_oracle_portable is not part of this checkout")
def test_the_real_oracle_db_has_what_the_service_uses(tmp_path, monkeypatch):
    lookup = ProgramBinsService(out_dir=tmp_path)  # the defaults: the folder next to this project, its queries folder
    oracle = lookup._load()
    assert isinstance(oracle.ERROR_HINTS, dict)
    assert callable(oracle.OracleDataAccess.connect) and callable(oracle.OracleDataAccess.get_data)
    calls = []

    def answer(self, query, parameter_name=None, parameter_value=None):
        calls.append((self.dsn, query, parameter_name, parameter_value))
        return oracle.QueryResult(COLUMNS, list(ROWS))

    monkeypatch.setattr(oracle.OracleDataAccess, "get_data", answer)  # no login, no query
    found = lookup.query("EMRR1X3B13K2634A")
    assert calls == [(DEFAULT_ALIAS, lookup._read_sql(BINS_SQL), "programname", "EMRR1X3B13K2634A")]
    assert [row[1] for row in read_csv(found.path)[1:]] == ["9103", "91030000", "9104"]


@pytest.mark.skipif(not REAL.is_file(), reason="pcs_oracle_portable is not part of this checkout")
def test_the_real_oracle_db_opens_the_connection_the_unit_lookup_runs_its_sql_on(tmp_path, monkeypatch, clock):
    lookup = ProgramBinsService(out_dir=tmp_path, clock=clock)
    oracle = lookup._load()
    executed = []

    class Cursor:
        description = [(name, None) for name in UNIT_COLUMNS]

        def execute(self, sql, binds):
            executed.append((sql, binds))

        def fetchall(self):
            return list(UNIT_ROWS)

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *error):
            return False

        def cursor(self):
            return Cursor()

    opened = []

    def connect(self):  # what OracleDataAccess.connect() does is not touched: only its answer is replaced
        opened.append(self.dsn)
        return Connection()

    monkeypatch.setattr(oracle.OracleDataAccess, "connect", connect)  # no login, no query
    found = lookup.query_units("EMRR1X3B13K2634A")
    assert opened == [DEFAULT_ALIAS]
    assert [binds for _, binds in executed] == [{"programname": "EMRR1X3B13K2634A", "min_ww": 202445}]
    assert [row[1] for row in read_csv(found.path)[1:]] == ["91030000", "91030001", "70060000"]


@pytest.mark.skipif(not REAL.is_file(), reason="pcs_oracle_portable is not part of this checkout")
def test_the_self_test_of_the_real_oracle_db_opens_the_connection_and_executes_a_sql_file(tmp_path, monkeypatch, capsys):
    """oracle_db.py has no SQL of its own: its command line logs in and, with --sql, executes the text of a file."""
    oracle = ProgramBinsService()._load()
    assert not [name for name in vars(oracle) if name.startswith("SQL_")]  # only the connection and the execution are left
    executed = []

    class Cursor:
        description = [("A", None), ("B", None)]

        def execute(self, sql, binds):
            executed.append((sql, binds))

        def fetchall(self):
            return [(1, "x"), (2, "y")]

    class Connection:
        version = "11.2.0.4.0"

        def __enter__(self):
            return self

        def __exit__(self, *error):
            return False

        def cursor(self):
            return Cursor()

    monkeypatch.setattr(oracle.OracleDataAccess, "connect", lambda self: Connection())
    monkeypatch.setattr("oracledb.clientversion", lambda: (11, 2, 0, 1, 0))  # (thick mode is not started here)
    sql = tmp_path / "q.sql"
    sql.write_text("select a, b from t where x = :a and y = :b;\n", encoding="utf-8")

    assert oracle.main(["--sql", str(sql), "--bind", "a=1", "--bind", "b=x=y"]) == 0
    assert executed == [("select a, b from t where x = :a and y = :b", {"a": "1", "b": "x=y"})]  # no ; , the value keeps its =
    out = capsys.readouterr().out
    assert "login OK   VN.ARIES: Oracle Database 11.2.0.4.0" in out and "query OK   q.sql: 2 rows ['A', 'B']" in out

    executed.clear()
    assert oracle.main([]) == 0 and executed == []  # no --sql: the login is all that is checked
    with pytest.raises(SystemExit):
        oracle.main(["--bind", "novalue"])  # (argparse says NAME=VALUE is needed)

    def refuse(self):
        raise RuntimeError("ORA-12154: TNS:could not resolve")

    monkeypatch.setattr(oracle.OracleDataAccess, "connect", refuse)
    capsys.readouterr()
    assert oracle.main([]) == 1
    out = capsys.readouterr().out
    assert "FAILED     RuntimeError: ORA-12154: TNS:could not resolve" in out and "hint: tnsnames.ora not found" in out
