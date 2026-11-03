from __future__ import annotations

import re
from datetime import timedelta

from database import TaskStatus
from pcs_die_file import duplicates as dup
from pcs_die_file import validation as rv
from pcs_die_file.model import READ_ONLY_FIELDS
from services import utcnow
from ui.common import STATIC
from ui.pcs_die_file_page import column_defs, program_status, validation_notice, validation_status
from ui.tasks_page import FILTERS, detail_lines, summary_text, task_rows
from ui.workers_page import summary_text as workers_summary
from ui.workers_page import worker_rows


def make_task(services, payload=None, task_type="check_path") -> int:
    return services.tasks.create_task(task_type, payload or {"path": r"M:\wf\a.xml", "product": "SST"})


def test_filters_offer_all_and_every_status():
    assert list(FILTERS) == ["All", "PENDING", "ASSIGNED", "RUNNING", "COMPLETED", "FAILED", "TIMEOUT"]
    assert FILTERS["PENDING"] == "Pending" and set(FILTERS) - {"All"} == {s.value for s in TaskStatus}


def test_task_rows_through_the_life_cycle(services, clock):
    task_id = make_task(services)
    (row,) = task_rows(services.tasks.list_tasks(), clock())
    assert row == {
        "id": task_id, "type": "check_path", "status": "PENDING", "worker": "-",
        "created": row["created"], "duration": "-",
    }
    assert len(row["created"]) == len("2025-01-06 08:00:00")

    services.tasks.claim_next("PC2")
    services.tasks.start_task(task_id, "PC2")
    clock.advance(seconds=75)
    (row,) = task_rows(services.tasks.list_tasks(), clock())
    assert (row["status"], row["worker"], row["duration"]) == ("RUNNING", "PC2", "1 min")

    services.tasks.complete_task(task_id, "PC2", {"exists": True})
    clock.advance(hours=3)  # a finished task's duration stops growing
    (row,) = task_rows(services.tasks.list_tasks(), clock())
    assert (row["status"], row["duration"]) == ("COMPLETED", "1 min")


def test_rows_keep_the_order_of_the_tasks(services, clock):
    ids = [make_task(services) for _ in range(3)]
    assert [row["id"] for row in task_rows(services.tasks.list_tasks(), clock())] == ids[::-1]


def test_summary_text_names_every_status(services):
    make_task(services)
    make_task(services)
    services.tasks.claim_next("PC2")
    text = summary_text(services.tasks.counts_by_status())
    assert text == "Pending 1   ·   Assigned 1   ·   Running 0   ·   Completed 0   ·   Failed 0   ·   Timeout 0"


def test_detail_lines_show_payload_result_and_error(services, clock):
    task_id = make_task(services, {"path": r"M:\wf\ü.xml", "product": "HST"})
    services.tasks.claim_next("PC2")
    services.tasks.start_task(task_id, "PC2")
    services.tasks.fail_task(task_id, "PC2", "access denied")
    lines = dict(detail_lines(services.tasks.get_task(task_id), clock()))
    assert lines["Task"] == f"#{task_id}  (check_path)" and lines["Status"] == "FAILED" and lines["Worker"] == "PC2"
    assert '"product": "HST"' in lines["Payload"] and "ü" in lines["Payload"]
    assert lines["Result"] == "-" and lines["Error"] == "access denied"
    assert lines["Created"] != "-" and lines["Completed"] != "-"


def test_detail_lines_of_a_waiting_task(services, clock):
    task_id = make_task(services)
    lines = dict(detail_lines(services.tasks.get_task(task_id), clock()))
    assert (lines["Worker"], lines["Assigned"], lines["Started"], lines["Completed"], lines["Duration"]) == ("-",) * 5
    assert "Error" not in lines and "Handed out" not in lines


def test_detail_lines_mention_a_task_that_was_handed_out_again(services, clock):
    task_id = make_task(services)
    services.tasks.claim_next("PC2")
    services.tasks.claim_next("PC2")
    lines = detail_lines(services.tasks.get_task(task_id), clock())
    assert dict(lines)["Handed out"].startswith("2 times")
    assert [label for label, _ in lines][:4] == ["Task", "Status", "Worker", "Handed out"]


def test_worker_rows(services, clock):
    task_id = make_task(services)
    services.workers.heartbeat("PC-idle", "10.0.0.2", "1.0.0")
    services.tasks.claim_next("PC-busy", ip="10.0.0.3", version="1.0.1")
    services.tasks.start_task(task_id, "PC-busy")
    clock.advance(seconds=30)
    services.workers.heartbeat("PC-busy")
    clock.advance(seconds=45)  # PC-idle has now been silent for 75 s, PC-busy for 45 s

    views = services.workers.list_workers()
    tasks = {t.id: t for t in services.tasks.get_tasks([task_id])}
    rows = {row["hostname"]: row for row in worker_rows(views, tasks, clock())}

    busy, idle = rows["PC-busy"], rows["PC-idle"]
    assert (busy["status"], busy["task"], busy["version"], busy["ip"]) == ("ONLINE", f"#{task_id}  check_path", "1.0.1", "10.0.0.3")
    assert busy["last_seen"].endswith("(45 sec ago)")
    assert (idle["status"], idle["task"], idle["version"], idle["ip"]) == ("OFFLINE", "-", "1.0.0", "10.0.0.2")
    assert idle["last_seen"].endswith("(1 min ago)")
    assert [row["hostname"] for row in worker_rows(views, tasks, clock())] == ["PC-busy", "PC-idle"]


def test_worker_without_known_details_and_with_a_vanished_task(services, clock):
    services.workers.heartbeat("PC2")
    (view,) = services.workers.list_workers()
    (row,) = worker_rows([view], {}, clock())
    assert (row["version"], row["ip"], row["task"]) == ("-", "-", "-")
    assert worker_rows([view.__class__(**{**view.__dict__, "current_task_id": 99})], {}, clock())[0]["task"] == "#99"


def test_workers_summary(services, clock):
    assert workers_summary([]) == "No worker has connected yet."
    services.workers.heartbeat("PC1")
    services.workers.heartbeat("PC2")
    clock.advance(seconds=90)
    services.workers.heartbeat("PC2")
    assert workers_summary(services.workers.list_workers()) == "1 online of 2 worker(s)"


def test_times_are_shown_in_local_time(services):
    from services import local_time

    stamp = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    assert local_time(stamp) == stamp.astimezone().strftime("%Y-%m-%d %H:%M:%S")
    assert local_time(None) == "-" and stamp + timedelta(0) == stamp


# ------------------------------------------------------------------------------------------ the pcs_die_file page
def test_the_program_status_is_the_name_and_the_number_of_bins_only():
    assert program_status("EMRA1X3B13F2633", 21848) == "program 'EMRA1X3B13F2633': 21848 bin(s) queried"
    assert program_status("X", 0) == "program 'X': 0 bin(s) queried"


def test_the_validation_status_counts_errors_and_warnings():
    assert validation_status(rv.CheckResult(text="", ok=True, seconds=0.31)) == "Validation: OK  (0.3s)"
    assert validation_status(rv.CheckResult(text="", ok=False, n_errors=2, seconds=1.0)) == "Validation: 2 error(s)  (1.0s)"
    duplicate = dup.Duplicate(dup.BinRule("x1", "A", "FB7006", ()), ())
    only_warnings = rv.CheckResult(text="", ok=True, duplicates=[duplicate] * 3)
    assert validation_status(only_warnings) == "Validation: OK, 3 warning(s)  (0.0s)"  # warnings do not turn OK into errors
    both = rv.CheckResult(text="", ok=False, n_errors=1, duplicates=[duplicate])
    assert validation_status(both) == "Validation: 1 error(s), 1 warning(s)  (0.0s)"


def test_the_validation_status_counts_missing_bins_too():
    duplicate = dup.Duplicate(dup.BinRule("x1", "A", "FB7006", ()), ())
    assert validation_status(rv.CheckResult(text="", ok=True, missing=[1, 2, 3])) == "Validation: OK, 3 bin(s) missing  (0.0s)"
    both = rv.CheckResult(text="", ok=True, duplicates=[duplicate], missing=[1])
    assert validation_status(both) == "Validation: OK, 1 warning(s), 1 bin(s) missing  (0.0s)"


def test_the_validate_button_tells_what_it_found():
    duplicate = dup.Duplicate(dup.BinRule("x1", "A", "FB7006", ()), ())
    assert validation_notice(rv.CheckResult(text="", ok=True)) == ("No errors found.", "positive")
    assert validation_notice(rv.CheckResult(text="", ok=False, n_errors=2, missing=[1])) == (
        "2 error(s) - see the Error tab.", "warning")  # an error is all it says
    warned = rv.CheckResult(text="", ok=True, duplicates=[duplicate] * 3)
    assert validation_notice(warned) == ("No errors found - 3 warning(s), see the Warning tab.", "warning")
    short = rv.CheckResult(text="", ok=True, missing=[5, 6])
    assert validation_notice(short) == ("No errors found - 2 bin(s) missing, see the Missing Bins tab.", "warning")
    both = rv.CheckResult(text="", ok=True, duplicates=[duplicate], missing=[5])
    assert validation_notice(both) == (
        "No errors found - 1 warning(s), see the Warning tab; 1 bin(s) missing, see the Missing Bins tab.", "warning")


def test_the_rule_grid_ends_with_the_read_only_note_column_and_the_trash_icon():
    defs = column_defs("xofy")
    assert [d.get("field") for d in defs[-3:]] == ["error", "note", None] and defs[-2]["headerName"] == "Note"
    assert defs[-1]["colId"] == "_del"
    assert all(d.get("field") != "note" for d in column_defs("workflow") + column_defs("parametric"))


def test_the_grid_script_and_the_model_agree_on_the_read_only_columns():
    script = (STATIC / "pcs_grid.js").read_text(encoding="utf-8")
    listed = re.search(r"const READ_ONLY = \[([^\]]*)\]", script).group(1)
    assert set(re.findall(r"'([a-z_]+)'", listed)) == set(READ_ONLY_FIELDS)


def test_every_row_colour_the_grid_uses_is_styled():
    css = (STATIC / "pcs.css").read_text(encoding="utf-8")
    for name in ("pcs-row-err", "pcs-row-warn", "pcs-row-ok"):
        assert f".ag-row.{name}" in css
    assert re.search(r"\.pcs-row-warn\s*\{[^}]*background-color:\s*#fff[0-9a-f]{3}", css)  # yellow
