from __future__ import annotations

from datetime import timedelta

from database import TaskStatus
from services import utcnow
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
