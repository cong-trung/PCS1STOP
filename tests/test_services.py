from __future__ import annotations

import threading
import time
from datetime import timedelta

import pytest
from sqlalchemy import text

from database import Database, TaskStatus, WorkerStatus, init_db
from services import (
    InvalidRequest,
    InvalidTransition,
    Services,
    TaskNotFound,
    WorkerMismatch,
    format_duration,
)

PAYLOAD = {"path": r"\\share\pcs_die_files\SST_Die.xml", "product": "SST"}


def make_pending(services: Services, count: int) -> list[int]:
    return [services.tasks.create_task("check_path", {"path": f"p{i}", "product": "SST"}) for i in range(count)]


# ------------------------------------------------------------------------------------------------ creating
def test_create_task_is_pending_with_the_payload(services, clock):
    task_id = services.tasks.create_task("check_path", PAYLOAD)

    task = services.tasks.get_task(task_id)
    assert task.status is TaskStatus.PENDING
    assert task.task_type == "check_path"
    assert task.payload == PAYLOAD
    assert task.created_time == clock()
    assert task.assigned_worker is None and task.started_time is None and task.completed_time is None
    assert task.retry_count == 0


def test_payload_keeps_unicode_and_is_stored_compact(db, services):
    task_id = services.tasks.create_task("check_path", {"path": "D:\\配方\\é.xml", "product": "HST"})
    with db.read_session() as session:
        raw = session.execute(text("SELECT payload FROM tasks WHERE id = :i"), {"i": task_id}).scalar_one()
    assert raw == '{"path":"D:\\\\配方\\\\é.xml","product":"HST"}'
    assert services.tasks.get_task(task_id).payload["path"] == "D:\\配方\\é.xml"


@pytest.mark.parametrize("task_type", ["", "   ", None, 5, "x" * 65])
def test_bad_task_type_is_refused(services, task_type):
    with pytest.raises(InvalidRequest):
        services.tasks.create_task(task_type, {})


@pytest.mark.parametrize(
    "payload",
    [["not", "an", "object"], "text", {"x": float("nan")}, {"x": object()}, {"x": "a" * (256 * 1024 + 1)}],
)
def test_bad_payload_is_refused(services, payload):
    with pytest.raises(InvalidRequest):
        services.tasks.create_task("check_path", payload)


def test_create_tasks_is_all_or_nothing(services):
    with pytest.raises(InvalidRequest):
        services.tasks.create_tasks([("check_path", {"a": 1}), ("", {"a": 2})])
    assert services.tasks.list_tasks() == []


def test_create_tasks_returns_ids_in_order(services):
    ids = services.tasks.create_tasks([("check_path", {"n": n}) for n in range(5)])
    assert ids == sorted(ids) and len(set(ids)) == 5
    assert [task.payload["n"] for task in services.tasks.get_tasks(ids)] == list(range(5))


def test_create_tasks_does_not_share_tasks_by_default(services):
    first = services.tasks.create_tasks([("check_path", {"n": 1})])
    again = services.tasks.create_tasks([("check_path", {"n": 1}), ("check_path", {"n": 1})])
    assert len({*first, *again}) == 3 and len(services.tasks.list_tasks()) == 3


def test_reuse_pending_gives_the_waiting_task_instead_of_a_second_one(services):
    (waiting,) = services.tasks.create_tasks([("check_path", {"n": 1})])
    ids = services.tasks.create_tasks([("check_path", {"n": 1}), ("check_path", {"n": 2})], reuse_pending=True)
    assert ids[0] == waiting and ids[1] != waiting
    assert len(services.tasks.list_tasks()) == 2
    assert services.tasks.get_task(waiting).created_time == services.tasks.get_task(ids[0]).created_time  # untouched


def test_reuse_pending_lets_equal_items_of_one_call_share_one_new_task(services):
    items = [("check_path", {"n": 1}), ("check_path", {"n": 2}), ("check_path", {"n": 1})]
    ids = services.tasks.create_tasks(items, reuse_pending=True)
    assert ids[0] == ids[2] != ids[1]
    assert len(services.tasks.list_tasks()) == 2


def test_reuse_pending_ignores_tasks_a_worker_already_holds(services):
    key = [("check_path", {"n": 1})]
    (task_id,) = services.tasks.create_tasks(key)
    services.tasks.claim_next("PC2")  # ASSIGNED
    (second,) = services.tasks.create_tasks(key, reuse_pending=True)
    assert second != task_id
    services.tasks.start_task(task_id, "PC2")  # RUNNING
    (third,) = services.tasks.create_tasks(key, reuse_pending=True)
    assert third == second  # the second one is still waiting
    services.tasks.complete_task(task_id, "PC2", {})  # COMPLETED
    assert services.tasks.create_tasks(key, reuse_pending=True) == [second]
    assert len(services.tasks.list_tasks()) == 2


def test_reuse_pending_needs_the_same_type_and_the_same_payload(services):
    (waiting,) = services.tasks.create_tasks([("check_path", {"path": "a", "product": "SST"})])
    ids = services.tasks.create_tasks(
        [("check_path", {"path": "a", "product": "HST"}), ("other", {"path": "a", "product": "SST"})],
        reuse_pending=True,
    )
    assert waiting not in ids and len(set(ids)) == 2


def test_reuse_pending_takes_the_oldest_of_several_equal_waiting_tasks(services):
    oldest, _newer = services.tasks.create_tasks([("check_path", {}), ("check_path", {})])
    assert services.tasks.create_tasks([("check_path", {})], reuse_pending=True) == [oldest]


def test_reuse_pending_keeps_the_all_or_nothing_rule(services):
    with pytest.raises(InvalidRequest):
        services.tasks.create_tasks([("check_path", {"n": 1}), ("", {})], reuse_pending=True)
    assert services.tasks.list_tasks() == []


def test_many_items_at_once_are_queued_and_read_back(services):
    items = [("check_path", {"n": n}) for n in range(1200)]  # (more than one chunk of the IN (...) queries)
    first = services.tasks.create_tasks(items, reuse_pending=True)
    assert services.tasks.create_tasks(items, reuse_pending=True) == first
    assert len(set(first)) == 1200 and len(services.tasks.list_tasks(limit=2000)) == 1200
    assert [task.id for task in services.tasks.get_tasks(first)] == first


def test_get_tasks_skips_unknown_ids_and_repeated_ones(services):
    (task_id,) = make_pending(services, 1)
    assert [task.id for task in services.tasks.get_tasks([task_id, 999, task_id])] == [task_id]
    assert services.tasks.get_tasks([]) == []


def test_get_unknown_task(services):
    with pytest.raises(TaskNotFound):
        services.tasks.get_task(999)


# ----------------------------------------------------------------------------------------------- assigning
def test_claim_on_an_empty_queue_returns_none_but_registers_the_worker(services):
    assert services.tasks.claim_next("PC2") is None
    workers = services.workers.list_workers()
    assert [(w.hostname, w.status) for w in workers] == [("PC2", WorkerStatus.ONLINE)]


def test_claim_gives_the_oldest_pending_task_first(services, clock):
    first, second, third = make_pending(services, 3)
    clock.advance(seconds=1)
    assert services.tasks.claim_next("PC2").id == first
    services.tasks.start_task(first, "PC2")
    services.tasks.complete_task(first, "PC2", {})
    assert services.tasks.claim_next("PC2").id == second
    services.tasks.start_task(second, "PC2")
    services.tasks.complete_task(second, "PC2", {})
    assert services.tasks.claim_next("PC2").id == third


def test_claim_marks_task_assigned_and_worker_busy(services, clock):
    (task_id,) = make_pending(services, 1)
    clock.advance(seconds=5)

    claimed = services.tasks.claim_next("PC2")

    assert claimed.id == task_id and claimed.task_type == "check_path"
    assert claimed.payload == {"path": "p0", "product": "SST"}
    stored = services.tasks.get_task(task_id)
    assert stored.status is TaskStatus.ASSIGNED
    assert stored.assigned_worker == "PC2"
    assert stored.assigned_time == clock()
    assert services.workers.list_workers()[0].current_task_id == task_id


def test_a_task_is_given_to_one_worker_only(services):
    (task_id,) = make_pending(services, 1)
    assert services.tasks.claim_next("PC2").id == task_id
    assert services.tasks.claim_next("PC3") is None


def test_two_workers_get_different_tasks(services):
    a, b = make_pending(services, 2)
    assert services.tasks.claim_next("PC2").id == a
    assert services.tasks.claim_next("PC3").id == b


def test_restarted_worker_gets_its_assigned_task_again(services, clock):
    first, second = make_pending(services, 2)
    first_view = services.tasks.claim_next("PC2")
    assigned_at = clock()
    assert (first_view.id, first_view.retry_count) == (first, 0)
    clock.advance(seconds=30)

    again = services.tasks.claim_next("PC2")  # (the answer to the first request got lost, or the agent was restarted)

    assert again.id == first  # not the second one
    assert again.retry_count == 1  # ... and it is a new delivery
    stored = services.tasks.get_task(first)
    assert stored.assigned_time == assigned_at  # the 5-minute ASSIGNED clock keeps running from the first hand-out
    assert stored.status is TaskStatus.ASSIGNED and stored.retry_count == 1
    assert services.tasks.get_task(second).status is TaskStatus.PENDING


def test_every_hand_out_gets_its_own_delivery_number(services):
    (task_id,) = make_pending(services, 1)
    assert [services.tasks.claim_next("PC2").retry_count for _ in range(4)] == [0, 1, 2, 3]
    assert services.tasks.get_task(task_id).retry_count == 3


def test_only_the_newest_delivery_may_start_a_task(services):
    """Two agents that share a name (or one whose answer got lost, and its replacement) must not both run the task."""
    (task_id,) = make_pending(services, 1)
    old = services.tasks.claim_next("PC2")
    new = services.tasks.claim_next("PC2")

    with pytest.raises(InvalidTransition, match="handed out again"):
        services.tasks.start_task(task_id, "PC2", old.retry_count)
    assert services.tasks.get_task(task_id).status is TaskStatus.ASSIGNED  # the refused start changed nothing

    services.tasks.start_task(task_id, "PC2", new.retry_count)
    assert services.tasks.get_task(task_id).status is TaskStatus.RUNNING
    services.tasks.start_task(task_id, "PC2", new.retry_count)  # a repeated request of the winner is still fine
    with pytest.raises(InvalidTransition, match="handed out again"):  # ... the loser stays refused, RUNNING or not
        services.tasks.start_task(task_id, "PC2", old.retry_count)


def test_start_without_a_delivery_number_is_not_checked(services):
    (task_id,) = make_pending(services, 1)
    services.tasks.claim_next("PC2")
    services.tasks.claim_next("PC2")
    services.tasks.start_task(task_id, "PC2")  # the API of architect.md has no such field
    assert services.tasks.get_task(task_id).status is TaskStatus.RUNNING


def test_a_task_is_not_handed_out_again_once_it_runs(services):
    (task_id,) = make_pending(services, 1)
    view = services.tasks.claim_next("PC2")
    services.tasks.start_task(task_id, "PC2", view.retry_count)
    assert services.tasks.claim_next("PC2") is None
    assert services.tasks.get_task(task_id).retry_count == 0


def test_a_running_task_stays_running_when_its_worker_asks_for_more_work(services, clock):
    """Crash recovery is the running timeout's job (see test_monitor): a poll of the same name may as well come from a
    second agent that shares the hostname, so it must never fail the first one's task."""
    first, second = make_pending(services, 2)
    services.tasks.claim_next("PC2")
    services.tasks.start_task(first, "PC2")
    clock.advance(seconds=30)

    assert services.tasks.claim_next("PC2").id == second

    assert services.tasks.get_task(first).status is TaskStatus.RUNNING
    services.tasks.complete_task(first, "PC2", {"ok": True})  # the first agent still delivers its result
    assert services.tasks.get_task(first).status is TaskStatus.COMPLETED


def test_other_workers_running_tasks_are_not_touched_by_a_claim(services):
    first, second = make_pending(services, 2)
    services.tasks.claim_next("PC2")
    services.tasks.start_task(first, "PC2")
    assert services.tasks.claim_next("PC3").id == second
    assert services.tasks.get_task(first).status is TaskStatus.RUNNING


def test_hostname_is_trimmed(services):
    make_pending(services, 1)
    claimed = services.tasks.claim_next("  PC2  ")
    assert services.tasks.get_task(claimed.id).assigned_worker == "PC2"
    assert [w.hostname for w in services.workers.list_workers()] == ["PC2"]


@pytest.mark.parametrize("worker", ["", "   ", "x" * 256])
def test_bad_worker_name_is_refused(services, worker):
    with pytest.raises(InvalidRequest):
        services.tasks.claim_next(worker)


def test_claims_from_many_threads_never_hand_out_a_task_twice(db, clock):
    services = Services.create(db, clock=clock)
    total = 120
    ids = make_pending(services, total)
    got: dict[str, list[int]] = {f"PC{n}": [] for n in range(8)}
    barrier = threading.Barrier(len(got))
    errors: list[BaseException] = []

    def work(name: str) -> None:
        try:
            barrier.wait()
            while True:
                task = services.tasks.claim_next(name)
                if task is None:
                    return
                services.tasks.start_task(task.id, name, task.retry_count)
                time.sleep(0.001)  # "work": the database lock is free meanwhile, so the claims of the others overlap it
                services.tasks.complete_task(task.id, name, {"by": name})
                got[name].append(task.id)
        except BaseException as exc:  # noqa: BLE001 - surfaced by the assertion below
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(name,)) for name in got]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert not errors
    handed_out = sorted(task_id for tasks in got.values() for task_id in tasks)
    assert handed_out == sorted(ids)  # every task exactly once, none lost
    assert all(task.status is TaskStatus.COMPLETED for task in services.tasks.get_tasks(ids))
    assert all(task.retry_count == 0 for task in services.tasks.get_tasks(ids))  # (nobody was handed a task twice)


# --------------------------------------------------------------------------------------------- life cycle
def test_full_life_cycle(services, clock):
    (task_id,) = make_pending(services, 1)
    services.tasks.claim_next("PC2")

    clock.advance(seconds=2)
    services.tasks.start_task(task_id, "PC2")
    running = services.tasks.get_task(task_id)
    assert running.status is TaskStatus.RUNNING and running.started_time == clock()
    assert services.workers.list_workers()[0].current_task_id == task_id

    clock.advance(seconds=7)
    services.tasks.complete_task(task_id, "PC2", {"exists": True, "size": 10})
    done = services.tasks.get_task(task_id)
    assert done.status is TaskStatus.COMPLETED
    assert done.result == {"exists": True, "size": 10}
    assert done.completed_time == clock()
    assert done.error_message is None
    assert done.duration(clock()) == timedelta(seconds=7)
    assert services.workers.list_workers()[0].current_task_id is None


def test_fail_stores_the_error_and_frees_the_worker(services, clock):
    (task_id,) = make_pending(services, 1)
    services.tasks.claim_next("PC2")
    services.tasks.start_task(task_id, "PC2")
    clock.advance(seconds=3)

    services.tasks.fail_task(task_id, "PC2", "share not reachable")

    task = services.tasks.get_task(task_id)
    assert task.status is TaskStatus.FAILED and task.error_message == "share not reachable"
    assert task.result is None and task.completed_time == clock()
    assert services.workers.list_workers()[0].current_task_id is None


def test_long_error_messages_are_cut(services):
    (task_id,) = make_pending(services, 1)
    services.tasks.claim_next("PC2")
    services.tasks.start_task(task_id, "PC2")
    services.tasks.fail_task(task_id, "PC2", "e" * 10000)
    assert len(services.tasks.get_task(task_id).error_message) == 4000


def test_repeating_a_step_is_harmless(services, clock):
    (task_id,) = make_pending(services, 1)
    services.tasks.claim_next("PC2")

    services.tasks.start_task(task_id, "PC2")
    started = services.tasks.get_task(task_id).started_time
    clock.advance(seconds=5)
    services.tasks.start_task(task_id, "PC2")
    assert services.tasks.get_task(task_id).started_time == started  # not restarted

    services.tasks.complete_task(task_id, "PC2", {"ok": 1})
    completed = services.tasks.get_task(task_id).completed_time
    clock.advance(seconds=5)
    services.tasks.complete_task(task_id, "PC2", {"ok": 2})
    task = services.tasks.get_task(task_id)
    assert task.result == {"ok": 1} and task.completed_time == completed


def test_repeating_fail_is_harmless(services):
    (task_id,) = make_pending(services, 1)
    services.tasks.claim_next("PC2")
    services.tasks.start_task(task_id, "PC2")
    services.tasks.fail_task(task_id, "PC2", "first")
    services.tasks.fail_task(task_id, "PC2", "second")
    assert services.tasks.get_task(task_id).error_message == "first"


def test_only_the_owner_may_touch_a_task(services):
    (task_id,) = make_pending(services, 1)
    services.tasks.claim_next("PC2")
    for step in (
        lambda: services.tasks.start_task(task_id, "PC3"),
        lambda: services.tasks.complete_task(task_id, "PC3", {}),
        lambda: services.tasks.fail_task(task_id, "PC3", "x"),
    ):
        with pytest.raises(WorkerMismatch):
            step()
    assert services.tasks.get_task(task_id).status is TaskStatus.ASSIGNED


def test_steps_out_of_order_are_refused(services):
    (task_id,) = make_pending(services, 1)
    with pytest.raises(InvalidTransition):
        services.tasks.start_task(task_id, "PC2")  # nobody holds it yet
    services.tasks.claim_next("PC2")
    with pytest.raises(InvalidTransition):
        services.tasks.complete_task(task_id, "PC2", {})  # never started
    with pytest.raises(InvalidTransition):
        services.tasks.fail_task(task_id, "PC2", "x")
    services.tasks.start_task(task_id, "PC2")
    services.tasks.complete_task(task_id, "PC2", {})
    with pytest.raises(InvalidTransition):
        services.tasks.fail_task(task_id, "PC2", "late")
    with pytest.raises(InvalidTransition):
        services.tasks.start_task(task_id, "PC2")


def test_unknown_task_ids(services):
    for step in (
        lambda: services.tasks.start_task(42, "PC2"),
        lambda: services.tasks.complete_task(42, "PC2", {}),
        lambda: services.tasks.fail_task(42, "PC2", "x"),
    ):
        with pytest.raises(TaskNotFound):
            step()


def test_result_must_be_a_json_object(services):
    (task_id,) = make_pending(services, 1)
    services.tasks.claim_next("PC2")
    services.tasks.start_task(task_id, "PC2")
    with pytest.raises(InvalidRequest):
        services.tasks.complete_task(task_id, "PC2", [1, 2])
    assert services.tasks.get_task(task_id).status is TaskStatus.RUNNING  # a refused result changes nothing


def test_complete_without_result(services):
    (task_id,) = make_pending(services, 1)
    services.tasks.claim_next("PC2")
    services.tasks.start_task(task_id, "PC2")
    services.tasks.complete_task(task_id, "PC2")
    task = services.tasks.get_task(task_id)
    assert task.status is TaskStatus.COMPLETED and task.result is None


def test_a_task_that_skips_start_cannot_complete_the_next_one(services):
    first, second = make_pending(services, 2)
    services.tasks.claim_next("PC2")
    with pytest.raises(InvalidTransition):
        services.tasks.complete_task(second, "PC2", {})


# ----------------------------------------------------------------------------------------------- reading
def test_list_tasks_newest_first_and_filtered(services):
    a, b, c = make_pending(services, 3)
    services.tasks.claim_next("PC2")
    services.tasks.start_task(a, "PC2")

    assert [t.id for t in services.tasks.list_tasks()] == [c, b, a]
    assert [t.id for t in services.tasks.list_tasks(TaskStatus.PENDING)] == [c, b]
    assert [t.id for t in services.tasks.list_tasks("RUNNING")] == [a]
    assert [t.id for t in services.tasks.list_tasks(limit=2)] == [c, b]
    assert services.tasks.list_tasks(TaskStatus.FAILED) == []


def test_counts_by_status(services):
    a, _, _ = make_pending(services, 3)
    services.tasks.claim_next("PC2")
    counts = services.tasks.counts_by_status()
    assert counts[TaskStatus.PENDING] == 2 and counts[TaskStatus.ASSIGNED] == 1
    assert counts[TaskStatus.COMPLETED] == 0 and set(counts) == set(TaskStatus)


def test_duration_of_unstarted_and_running_tasks(services, clock):
    (task_id,) = make_pending(services, 1)
    assert services.tasks.get_task(task_id).duration(clock()) is None
    services.tasks.claim_next("PC2")
    assert services.tasks.get_task(task_id).duration(clock()) is None
    services.tasks.start_task(task_id, "PC2")
    clock.advance(seconds=90)
    assert services.tasks.get_task(task_id).duration(clock()) == timedelta(seconds=90)


@pytest.mark.parametrize(
    ("seconds", "text_"),
    [(None, "-"), (0, "0 sec"), (12, "12 sec"), (59.9, "59 sec"), (60, "1 min"), (300, "5 min"), (3599, "59 min"),
     (3600, "1 hr"), (3900, "1 hr 5 min"), (7260, "2 hr 1 min"), (-5, "0 sec")],
)
def test_format_duration(seconds, text_):
    assert format_duration(None if seconds is None else timedelta(seconds=seconds)) == text_


# ---------------------------------------------------------------------------------------------- workers
def test_heartbeat_registers_and_refreshes(services, clock):
    services.workers.heartbeat("PC2", "10.0.0.5", "1.0.0")
    clock.advance(seconds=20)
    services.workers.heartbeat("PC2", None, None)  # an absent ip/version keeps what is known

    (worker,) = services.workers.list_workers()
    assert (worker.hostname, worker.ip_address, worker.version) == ("PC2", "10.0.0.5", "1.0.0")
    assert worker.last_seen == clock() and worker.status is WorkerStatus.ONLINE

    services.workers.heartbeat("PC2", "10.0.0.9", "1.1.0")
    (worker,) = services.workers.list_workers()
    assert (worker.ip_address, worker.version) == ("10.0.0.9", "1.1.0")


def test_workers_are_listed_by_name(services):
    for name in ("pc3", "PC1", "pc2"):
        services.workers.heartbeat(name)
    assert [w.hostname for w in services.workers.list_workers()] == ["PC1", "pc2", "pc3"]


def test_a_silent_worker_shows_offline_even_before_the_monitor_runs(services, clock):
    services.workers.heartbeat("PC2")
    assert services.workers.count_online() == 1
    clock.advance(seconds=61)
    (worker,) = services.workers.list_workers()
    assert worker.status is WorkerStatus.OFFLINE and worker.stored_status is WorkerStatus.ONLINE
    assert services.workers.count_online() == 0


def test_a_returning_worker_is_online_again(services, clock):
    services.workers.heartbeat("PC2")
    clock.advance(minutes=10)
    services.monitor.run_once()
    assert services.workers.list_workers()[0].stored_status is WorkerStatus.OFFLINE
    services.tasks.claim_next("PC2")
    assert services.workers.list_workers()[0].status is WorkerStatus.ONLINE


# ------------------------------------------------------------------------------------------- persistence
def test_tasks_and_workers_survive_a_restart(tmp_path, clock):
    path = tmp_path / "restart.db"
    first = Database(path)
    init_db(first)
    services = Services.create(first, clock=clock)
    ids = make_pending(services, 3)
    services.tasks.claim_next("PC2")
    services.tasks.start_task(ids[0], "PC2")
    first.dispose()

    second = Database(path)
    init_db(second)  # re-running the initialisation must not touch the data
    services = Services.create(second, clock=clock)
    try:
        assert [t.status for t in services.tasks.get_tasks(ids)] == [
            TaskStatus.RUNNING, TaskStatus.PENDING, TaskStatus.PENDING,
        ]
        assert services.workers.list_workers()[0].current_task_id == ids[0]
        services.tasks.complete_task(ids[0], "PC2", {"ok": True})
        assert services.tasks.claim_next("PC2").id == ids[1]
    finally:
        second.dispose()
