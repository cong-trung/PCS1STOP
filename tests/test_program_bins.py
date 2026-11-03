"""The Program box's Oracle lookup: the portable module is loaded by its path, the query runs with a bind variable, and
PROGRAM_NAME + BIN_NUMBER end up in a CSV in the temp folder.  Oracle itself is never reached: the tests load a stand-in
for oracle_db.py (the names the service uses are the real ones), and one test checks that the real file still has them."""
from __future__ import annotations

import csv
import threading
from pathlib import Path

import pytest

from services.program_bins import (
    COPIES,
    CSV_HEADER,
    DEFAULT_ALIAS,
    DEFAULT_PORTABLE_DIR,
    ProgramBinsError,
    ProgramBinsService,
    csv_name,
)

COLUMNS = ["PROGRAM_NAME", "BIN_DESCRIPTION", "BIN_NUMBER"]  # what the real query returns
ROWS = [
    ("EMRR1X3B13K2634A", "ACTUATION_ACTUATE", 9103),
    ("EMRR1X3B13K2634A", "ACTUATION_ACTUATE_FAILURE", 91030000),
    ("EMRR1X3B13K2634A", "ACTUATION_DEACTUATE", 9104),
]

STAND_IN = '''
from typing import NamedTuple

SQL_PROGRAM_BINS = "select program_name, description, bin_number from bins where program_name = :programname"
ERROR_HINTS = {"ORA-12154": "tnsnames.ora not found"}
CALLS = []
ERROR = @ERROR@
ANSWER = (@COLUMNS@, @ROWS@)


class QueryResult(NamedTuple):
    columns: list
    rows: list


class OracleDataAccess:
    def __init__(self, tns_alias="VN.ARIES"):
        self.alias = tns_alias

    def get_data(self, query, parameter_name=None, parameter_value=None):
        CALLS.append((self.alias, query, parameter_name, parameter_value))
        if ERROR is not None:
            raise RuntimeError(ERROR)
        return QueryResult(*ANSWER)
'''


def portable(tmp_path: Path, *, columns=COLUMNS, rows=ROWS, error=None, source=None) -> Path:
    """A folder with a stand-in oracle_db.py that answers with ``rows`` (or raises ``error``)."""
    folder = tmp_path / "portable"
    folder.mkdir(exist_ok=True)
    text = source or (
        STAND_IN.replace("@COLUMNS@", repr(columns)).replace("@ROWS@", repr(rows)).replace("@ERROR@", repr(error))
    )
    (folder / "oracle_db.py").write_text(text, encoding="utf-8")
    return folder


def service(tmp_path: Path, **kwargs) -> ProgramBinsService:
    alias = kwargs.pop("alias", DEFAULT_ALIAS)
    return ProgramBinsService(portable(tmp_path, **kwargs), alias, tmp_path / "out")


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
    assert ":programname" in sql and hostile not in sql  # the name never becomes part of the SQL text


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
        with pytest.raises(ProgramBinsError, match="Enter a program name"):
            lookup.query(blank)


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


REAL = DEFAULT_PORTABLE_DIR / "oracle_db.py"


@pytest.mark.skipif(not REAL.is_file(), reason="pcs_oracle_portable is not part of this checkout")
def test_the_real_oracle_db_has_what_the_service_uses(tmp_path, monkeypatch):
    lookup = ProgramBinsService(out_dir=tmp_path)  # the defaults: the folder next to this project
    oracle = lookup._load()
    assert ":programname" in oracle.SQL_PROGRAM_BINS and isinstance(oracle.ERROR_HINTS, dict)
    for column in CSV_HEADER:
        assert column.lower() in oracle.SQL_PROGRAM_BINS.lower()
    calls = []

    def answer(self, query, parameter_name=None, parameter_value=None):
        calls.append((self.dsn, query, parameter_name, parameter_value))
        return oracle.QueryResult(COLUMNS, list(ROWS))

    monkeypatch.setattr(oracle.OracleDataAccess, "get_data", answer)  # no login, no query
    found = lookup.query("EMRR1X3B13K2634A")
    assert calls == [(DEFAULT_ALIAS, oracle.SQL_PROGRAM_BINS, "programname", "EMRR1X3B13K2634A")]
    assert [row[1] for row in read_csv(found.path)[1:]] == ["9103", "91030000", "9104"]
