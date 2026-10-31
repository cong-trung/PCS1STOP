from __future__ import annotations

import pytest
from sqlalchemy import text

from database import TaskStatus
from services import path_checks as pc

PATH = r"\\share\wf\SST_PowerCycle.xml"


def finished(services, result=None, error=None, worker="PC2"):
    """A task that went all the way through the queue (or was failed by the worker)."""
    task_id = services.tasks.create_task(pc.CHECK_PATH, pc.check_path_payload(PATH, "sst"))
    services.tasks.claim_next(worker)
    services.tasks.start_task(task_id, worker)
    if error is not None:
        services.tasks.fail_task(task_id, worker, error)
    else:
        services.tasks.complete_task(task_id, worker, result)
    return services.tasks.get_task(task_id)


def test_payload_is_the_path_and_the_product():
    assert pc.check_path_payload("  M:\\wf\\a.xml  ", " hst ") == {"path": "M:\\wf\\a.xml", "product": "HST"}


def test_every_state_has_a_text(services, clock):
    task_id = services.tasks.create_task(pc.CHECK_PATH, pc.check_path_payload(PATH, "SST"))
    assert pc.describe_check(services.tasks.get_task(task_id)) == "Queued"
    services.tasks.claim_next("PC2")
    assert pc.describe_check(services.tasks.get_task(task_id)) == "Assigned: PC2"
    services.tasks.start_task(task_id, "PC2")
    assert pc.describe_check(services.tasks.get_task(task_id)) == "Checking: PC2"
    clock.advance(minutes=61)
    services.monitor.run_once()
    task = services.tasks.get_task(task_id)
    assert task.status is TaskStatus.TIMEOUT
    assert pc.describe_check(task) == "Timeout: PC2 did not answer in time"


def test_timeout_of_a_task_nobody_took():
    from datetime import datetime, timezone

    from services.views import TaskView

    now = datetime(2025, 1, 1, tzinfo=timezone.utc)
    task = TaskView(1, pc.CHECK_PATH, TaskStatus.TIMEOUT, None, now, None, None, now, {}, None, None, 0)
    assert pc.describe_check(task) == "Timeout: no worker took it"


@pytest.mark.parametrize(
    ("result", "text"),
    [
        ({"exists": True, "is_file": True, "size": 0}, "OK (0 B)"),
        ({"exists": True, "is_file": True, "size": 1023}, "OK (1023 B)"),
        ({"exists": True, "is_file": True, "size": 24 * 1024 + 100}, "OK (24.1 KB)"),
        ({"exists": True, "is_file": True, "size": 5 * 1024 * 1024}, "OK (5.0 MB)"),
        ({"exists": True, "is_file": True, "size": 3 * 1024**3}, "OK (3.0 GB)"),
        ({"exists": True}, "OK"),
        ({"exists": True, "is_file": True, "size": True}, "OK"),
        ({"exists": True, "is_file": True, "size": -1}, "OK"),
        ({"exists": True, "is_file": False}, "Not a file"),
        ({"exists": False}, "Not found"),
        ({"exists": False, "detail": "network path not found"}, "Not found (network path not found)"),
        ({"size": 1}, "Failed: the worker sent no usable result"),
        (None, "Failed: the worker sent no usable result"),
    ],
)
def test_result_texts(services, result, text):
    assert pc.describe_check(finished(services, result)) == text


def test_failed_task_shows_the_error_on_one_line(services):
    task = finished(services, error="PermissionError:\n  access is denied\t" + "x" * 200)
    text = pc.describe_check(task)
    assert text.startswith("Failed: PermissionError: access is denied x")
    assert "\n" not in text and "\t" not in text and len(text) <= len("Failed: ") + 90 and text.endswith("…")


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("", ""),
        ("OK", "ok"),
        ("OK (3.0 KB)", "ok"),
        ("Not found", "bad"),
        ("Not found (network path not found)", "bad"),
        ("Not a file", "bad"),
        ("Failed: access denied", "warn"),
        ("Timeout: PC2 did not answer in time", "warn"),
        ("Queued", "wait"),
        ("Assigned: PC2", "wait"),
        ("Checking: PC2", "wait"),
    ],
)
def test_check_kind(text, kind):
    assert pc.check_kind(text) == kind


def test_describe_check_always_lands_in_a_known_kind(services):
    for result in ({"exists": True, "is_file": True, "size": 5}, {"exists": False}, {"exists": True, "is_file": False}):
        assert pc.check_kind(pc.describe_check(finished(services, result))) in {"ok", "bad"}
    assert pc.check_kind(pc.describe_check(finished(services, error="x"))) == "warn"


def test_summary():
    assert pc.summarize(["OK (1 B)", "OK", "Not found", "Failed: x", "Timeout: y", "Queued", ""]) == (
        "2 OK, 1 not found, 2 failed / timed out, 1 waiting"
    )
    assert pc.summarize([]) == "0 OK"


# ------------------------------------------------------------------------------------------- the tracker
def tracker(services, product="SST") -> pc.PathCheckTracker:
    return pc.PathCheckTracker(services.tasks, product)


def run_through(services, task_id, result=None, error=None, worker="PC2"):
    services.tasks.claim_next(worker)
    services.tasks.start_task(task_id, worker)
    if error is not None:
        services.tasks.fail_task(task_id, worker, error)
    else:
        services.tasks.complete_task(task_id, worker, result)


def test_sync_queues_one_task_per_new_path_with_path_and_product(services):
    t = tracker(services, " hst ")
    assert t.product == "HST"

    assert t.sync(["a.xml", " b.xml ", "a.xml", "", "   "]) == ["a.xml", "b.xml"]

    tasks = list(reversed(services.tasks.list_tasks()))
    assert [task.task_type for task in tasks] == ["check_path", "check_path"]
    assert [task.payload for task in tasks] == [
        {"path": "a.xml", "product": "HST"},
        {"path": "b.xml", "product": "HST"},
    ]
    assert t.texts == {"a.xml": "Queued", "b.xml": "Queued"} and t.unfinished
    assert t.follows("a.xml") and t.follows("  b.xml ") and not t.follows("c.xml")


def test_a_path_is_asked_about_only_once(services):
    t = tracker(services)
    t.sync(["a"])
    assert t.sync(["a", "b"]) == ["b"]
    assert t.sync(["b", "a"]) == []
    assert t.sync([]) == []
    assert len(services.tasks.list_tasks()) == 2


def test_poll_follows_the_task_through_its_life(services):
    t = tracker(services)
    (path,) = (PATH,)
    t.sync([path])
    (task_id,) = [task.id for task in services.tasks.list_tasks()]
    assert t.poll() == {path: "Queued"} and t.unfinished

    services.tasks.claim_next("PC2")
    assert t.poll() == {path: "Assigned: PC2"} and t.unfinished
    services.tasks.start_task(task_id, "PC2")
    assert t.poll() == {path: "Checking: PC2"} and t.unfinished
    services.tasks.complete_task(task_id, "PC2", {"exists": True, "is_file": True, "size": 2048})
    assert t.poll() == {path: "OK (2.0 KB)"} and not t.unfinished


def test_finished_tasks_are_not_read_again(services, db):
    t = tracker(services)
    t.sync([PATH])
    (task_id,) = [task.id for task in services.tasks.list_tasks()]
    run_through(services, task_id, {"exists": False})
    assert t.poll() == {PATH: "Not found"} and not t.unfinished

    with db.write_session() as session:  # even a task that is deleted afterwards cannot change the verdict
        session.execute(text("DELETE FROM tasks"))
    assert t.poll() == {PATH: "Not found"}


@pytest.mark.parametrize(
    ("outcome", "text_"),
    [
        ({"result": {"exists": True, "is_file": True, "size": 5}}, "OK (5 B)"),
        ({"result": {"exists": False, "detail": "network path not found"}}, "Not found (network path not found)"),
        ({"result": {"exists": True, "is_file": False}}, "Not a file"),
        ({"error": "access denied: x"}, "Failed: access denied: x"),
    ],
)
def test_every_outcome_ends_the_wait(services, outcome, text_):
    t = tracker(services)
    t.sync([PATH])
    (task_id,) = [task.id for task in services.tasks.list_tasks()]
    run_through(services, task_id, **outcome)
    assert t.poll() == {PATH: text_} and not t.unfinished


def test_a_task_that_timed_out_ends_the_wait(services, clock):
    t = tracker(services)
    t.sync([PATH])
    services.tasks.claim_next("PC2")  # taken, never started
    clock.advance(minutes=6)
    services.monitor.run_once()
    assert t.poll() == {PATH: "Timeout: PC2 did not answer in time"} and not t.unfinished


def test_a_task_that_vanished_is_reported_and_ends_the_wait(services, db):
    t = tracker(services)
    t.sync([PATH, "b.xml"])
    with db.write_session() as session:
        session.execute(text("DELETE FROM tasks WHERE id = 1"))
    assert t.poll() == {PATH: "Failed: the task is gone", "b.xml": "Queued"}
    assert t.unfinished  # (b.xml is still waiting)


def test_new_paths_join_the_ones_that_are_already_followed(services):
    t = tracker(services)
    t.sync(["a"])
    run_through(services, services.tasks.list_tasks()[0].id, {"exists": True, "is_file": True, "size": 1})
    assert t.poll() == {"a": "OK (1 B)"} and not t.unfinished

    t.sync(["a", "b"])  # the user typed a second path: only that one is asked about
    assert t.unfinished and t.texts == {"a": "OK (1 B)", "b": "Queued"}
    assert len(services.tasks.list_tasks()) == 2


def test_trackers_for_different_products_ask_separately(services):
    tracker(services, "SST").sync([PATH])
    tracker(services, "HST").sync([PATH])
    assert sorted(task.payload["product"] for task in services.tasks.list_tasks()) == ["HST", "SST"]


def test_two_tabs_share_a_task_that_is_still_waiting(services):
    one, two = tracker(services), tracker(services)
    one.sync([PATH, "b.xml"])
    two.sync(["b.xml", PATH, "c.xml"])
    assert len(services.tasks.list_tasks()) == 3  # a, b and c: nothing was asked twice

    for task in reversed(services.tasks.list_tasks()):  # (the worker answers everything in turn)
        run_through(services, task.id, {"exists": True, "is_file": True, "size": 1})
    assert one.poll() == {PATH: "OK (1 B)", "b.xml": "OK (1 B)"}
    assert two.poll() == {PATH: "OK (1 B)", "b.xml": "OK (1 B)", "c.xml": "OK (1 B)"}


def test_a_task_a_worker_already_holds_is_not_shared(services):
    one = tracker(services)
    one.sync([PATH])
    services.tasks.claim_next("PC2")  # ASSIGNED: its answer may be old, or never come
    two = tracker(services)
    two.sync([PATH])
    assert len(services.tasks.list_tasks()) == 2


def test_a_failed_sync_leaves_nothing_followed_and_is_retried(services, monkeypatch):
    t = tracker(services)
    real = services.tasks.create_tasks

    def broken(*args, **kwargs):
        raise RuntimeError("the database is locked")

    monkeypatch.setattr(services.tasks, "create_tasks", broken)
    with pytest.raises(RuntimeError):
        t.sync([PATH])
    assert not t.follows(PATH) and not t.unfinished and t.texts == {}

    monkeypatch.setattr(services.tasks, "create_tasks", real)
    assert t.sync([PATH]) == [PATH] and t.follows(PATH)
