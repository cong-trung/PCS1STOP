from __future__ import annotations

import logging
import os
import sys
import threading
import types
from decimal import Decimal
from pathlib import Path

import pytest

from services.program_bins_common import BINS_SQL, UNIT_SQL, ProgramBinsError
from services.program_bins_pyuber import DEFAULT_DATASOURCES, PyUberProgramBinsService

PROGRAM_COLUMNS = ["PROGRAM_NAME", "BIN_DESCRIPTION", "BIN_NUMBER"]
UNIT_COLUMNS = ["PROGRAM_NAME", "INTERFACE_BIN", "FUNCTIONAL_BIN", "DATA_BIN"]


class FakeCursor:
    def __init__(self, datasource, answers, calls, failures):
        self.datasource = datasource
        self.answers = answers
        self.calls = calls
        self.failures = failures
        self.description = None
        self.rows = []

    def execute(self, query, parameters=None):
        binds = dict(parameters or {})
        self.calls.append((self.datasource, query, binds))
        if self.datasource in self.failures:
            raise RuntimeError(self.failures[self.datasource])
        kind = "unit" if " from units" in query.lower() else "program"
        columns, self.rows = self.answers[(self.datasource, kind)]
        self.description = [
            (name, (18, "System.String") if name == "PROGRAM_NAME" else (9, "System.Int32"))
            for name in columns
        ]

    def fetchall(self):
        return self.rows

    def close(self):
        pass


class FakeConnection:
    def __init__(self, datasource, answers, calls, failures):
        self.datasource = datasource
        self.answers = answers
        self.calls = calls
        self.failures = failures

    def __enter__(self):
        return self

    def __exit__(self, *error):
        return False

    def cursor(self):
        return FakeCursor(self.datasource, self.answers, self.calls, self.failures)


def install_pyuber(monkeypatch, answers, failures=None):
    calls = []
    failures = failures or {}
    fake = types.ModuleType("PyUber")
    fake.connect = lambda datasource: FakeConnection(datasource, answers, calls, failures)
    monkeypatch.setitem(sys.modules, "PyUber", fake)
    return calls


def make_service(tmp_path: Path, datasources=DEFAULT_DATASOURCES, *, clock=None):
    query_dir = tmp_path / "queries"
    query_dir.mkdir()
    (query_dir / BINS_SQL).write_text(
        "select program_name, bin_description, bin_number from bins where program_name = :programname;",
        encoding="utf-8",
    )
    (query_dir / UNIT_SQL).write_text(
        "select program_name, interface_bin, functional_bin, data_bin from units "
        "where lao_start_ww >= :min_ww and program_name = :programname",
        encoding="utf-8",
    )
    kwargs = {"out_dir": tmp_path / "out", "query_dir": query_dir}
    if clock is not None:
        kwargs["clock"] = clock
    return PyUberProgramBinsService(datasources, **kwargs)


def test_program_bins_read_sql_through_polars_and_deduplicate_across_datasources(
    tmp_path, monkeypatch
):
    sources = ("VN_PROD_ARIES", "SVC_PROD_ARIES")
    answers = {
        (sources[0], "program"): (
            PROGRAM_COLUMNS,
            [("P", "first", 9103), ("P", "duplicate description", 9103), ("P", "next", 9104)],
        ),
        (sources[1], "program"): (
            PROGRAM_COLUMNS,
            [("P", "duplicate bin", 9104.0), ("P", "last", 9105)],
        ),
    }
    calls = install_pyuber(monkeypatch, answers)
    service = make_service(tmp_path, sources)

    result = service.query(" p ")

    assert [source for source, _, _ in calls] == list(sources)
    assert all(sql.endswith("from bins where program_name = :programname") for _, sql, _ in calls)
    assert [binds for _, _, binds in calls] == [{"programname": "P"}] * 2
    assert result.bins == (("P", 9103), ("P", 9104), ("P", 9105))
    with result.path.open(newline="", encoding="utf-8") as handle:
        assert handle.read().splitlines() == [
            "PROGRAM_NAME,BIN_NUMBER",
            "P,9103",
            "P,9104",
            "P,9105",
        ]


def test_program_columns_are_selected_by_name_and_missing_columns_are_reported(
    tmp_path, monkeypatch
):
    source = "VN_PROD_ARIES"
    answers = {
        (source, "program"): (
            ["BIN_NUMBER", "PROGRAM_NAME", "BIN_DESCRIPTION"],
            [(9103, "P", "bin")],
        ),
    }
    install_pyuber(monkeypatch, answers)
    service = make_service(tmp_path, (source,))

    assert service.query("P").bins == (("P", 9103),)

    missing = {
        (source, "program"): (["BIN_NUMBER", "BIN_DESCRIPTION"], [(9103, "bin")]),
    }
    install_pyuber(monkeypatch, missing)
    with pytest.raises(ProgramBinsError, match="no PROGRAM_NAME"):
        service.query("P")


def test_unit_bins_keep_most_specific_bin_and_deduplicate_across_datasources(
    tmp_path, monkeypatch, clock
):
    sources = ("VN_PROD_ARIES", "SVC_PROD_ARIES")
    answers = {
        (sources[0], "unit"): (
            UNIT_COLUMNS,
            [
                ("P", 91, 9103, 91030000),
                ("P", 70, 7006, None),
                ("P", 5, None, None),
                ("P", None, None, None),
            ],
        ),
        (sources[1], "unit"): (
            UNIT_COLUMNS,
            [("P", 91, 9103, 91030000.0), ("P", 80, None, Decimal("8001"))],
        ),
    }
    calls = install_pyuber(monkeypatch, answers)
    service = make_service(tmp_path, sources, clock=clock)

    result = service.query_units("p")

    assert [source for source, _, _ in calls] == list(sources)
    assert [binds for _, _, binds in calls] == [
        {"programname": "P", "min_ww": 202445},
        {"programname": "P", "min_ww": 202445},
    ]
    assert result.bins == (("P", 91030000), ("P", 7006), ("P", 5), ("P", 8001))


def test_unit_binds_only_variables_present_in_the_sql_file(tmp_path, monkeypatch, clock):
    service = make_service(tmp_path, ("VN_PROD_ARIES",), clock=clock)
    (service.query_dir / UNIT_SQL).write_text(
        "select program_name, interface_bin, functional_bin, data_bin from units",
        encoding="utf-8",
    )
    answers = {
        ("VN_PROD_ARIES", "unit"): (UNIT_COLUMNS, [("P", 1, None, None)]),
    }
    calls = install_pyuber(monkeypatch, answers)

    service.query_units("P")

    assert calls[0][2] == {}


def test_datasource_failure_names_source_and_writes_no_partial_csv(tmp_path, monkeypatch):
    sources = ("VN_PROD_ARIES", "SVC_PROD_ARIES")
    answers = {
        (sources[0], "program"): (PROGRAM_COLUMNS, [("P", "bin", 1)]),
        (sources[1], "program"): (PROGRAM_COLUMNS, []),
    }
    install_pyuber(monkeypatch, answers, {sources[1]: "access denied"})
    service = make_service(tmp_path, sources)

    with pytest.raises(ProgramBinsError, match=r"SVC_PROD_ARIES: RuntimeError: access denied"):
        service.query("P")

    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize("datasources", ["", [], ["VN_PROD_ARIES", " "]])
def test_empty_or_invalid_datasource_list_is_refused(datasources):
    with pytest.raises(ProgramBinsError, match="PyUber datasource"):
        PyUberProgramBinsService(datasources)


def fill(folder: Path, *names: str) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    for name in names:
        (folder / name).write_text("PROGRAM_NAME,BIN_NUMBER\n", encoding="utf-8")


def test_cleanup_deletes_every_file_and_keeps_the_folders(tmp_path, caplog):
    service = make_service(tmp_path)
    fill(service.out_dir, "bins_A.csv", "unit_bins_A.csv", "notes.txt")
    fill(service.out_dir / "inside", "keep.csv")

    with caplog.at_level(logging.INFO, logger="pcs.bins"):
        report = service.clean_out_dir()

    assert (report.folder, report.deleted, report.kept) == (service.out_dir, 3, ())
    assert [path.name for path in service.out_dir.iterdir()] == ["inside"]
    assert (service.out_dir / "inside" / "keep.csv").exists()
    assert f"deleted 3 file(s) from {service.out_dir}" in caplog.text


def test_cleanup_without_a_folder_does_nothing(tmp_path):
    service = make_service(tmp_path)

    report = service.clean_out_dir()

    assert (report.deleted, report.kept) == (0, ())
    assert not service.out_dir.exists()  # a cleanup creates nothing


def test_cleanup_leaves_a_file_another_program_has_open_for_the_next_one(tmp_path, monkeypatch, caplog):
    service = make_service(tmp_path)
    fill(service.out_dir, "bins_A.csv", "bins_B.csv", "bins_C.csv")
    unlink = os.unlink

    def locked(path, *args, **kwargs):  # Windows refuses to delete a file that Excel has open
        if Path(path).name == "bins_B.csv":
            raise PermissionError(13, "The process cannot access the file because it is being used by another process")
        unlink(path, *args, **kwargs)

    with monkeypatch.context() as patch, caplog.at_level(logging.INFO, logger="pcs.bins"):
        patch.setattr(os, "unlink", locked)
        first = service.clean_out_dir()

    assert (first.deleted, first.kept) == (2, ("bins_B.csv",))
    assert [path.name for path in service.out_dir.iterdir()] == ["bins_B.csv"]
    assert any(r.levelno == logging.WARNING and "bins_B.csv" in r.getMessage() for r in caplog.records)

    second = service.clean_out_dir()  # the other program let go

    assert (second.deleted, second.kept) == (1, ())
    assert list(service.out_dir.iterdir()) == []


def test_cleanup_waits_for_a_lookup_that_is_saving(tmp_path):
    service = make_service(tmp_path)
    fill(service.out_dir, "bins_A.csv")
    finished = threading.Event()

    def clean() -> None:
        service.clean_out_dir()
        finished.set()

    with service._write_lock:  # what _save holds while it writes a CSV
        threading.Thread(target=clean, daemon=True).start()
        assert not finished.wait(0.3)
        assert (service.out_dir / "bins_A.csv").exists()

    assert finished.wait(5)
    assert not (service.out_dir / "bins_A.csv").exists()


def test_a_lookup_after_a_cleanup_saves_again(tmp_path, monkeypatch):
    answers = {("VN_PROD_ARIES", "program"): (PROGRAM_COLUMNS, [("P", "bin", 9103)])}
    install_pyuber(monkeypatch, answers)
    service = make_service(tmp_path, ("VN_PROD_ARIES",))
    first = service.query("P")

    assert service.clean_out_dir().deleted == 1
    assert not first.path.exists()

    again = service.query("P")

    assert again.path == first.path and again.path.exists()
