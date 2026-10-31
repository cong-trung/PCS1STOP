from __future__ import annotations

import pytest

from database import TaskStatus, WorkerStatus
from services import InvalidTransition, MonitorLimits, Services


def pending(services: Services, n: int = 1) -> list[int]:
    return [services.tasks.create_task("check_path", {"path": f"p{i}", "product": "SST"}) for i in range(n)]


def stored_workers(services: Services) -> dict[str, tuple[WorkerStatus, int | None]]:
    return {w.hostname: (w.stored_status, w.current_task_id) for w in services.workers.list_workers()}


def test_nothing_to_do(services):
    report = services.monitor.run_once()
    assert not report.changed


def test_worker_goes_offline_after_60_seconds(services, clock):
    services.workers.heartbeat("PC2")
    clock.advance(seconds=60)
    assert services.monitor.run_once().offline_workers == []  # exactly 60 s is still fine
    clock.advance(seconds=1)
    assert services.monitor.run_once().offline_workers == ["PC2"]
    assert stored_workers(services)["PC2"][0] is WorkerStatus.OFFLINE
    assert services.monitor.run_once().offline_workers == []  # reported once, not every minute


def test_a_busy_worker_that_keeps_polling_stays_online(services, clock):
    pending(services)
    services.tasks.claim_next("PC2")
    for _ in range(5):
        clock.advance(seconds=30)
        services.workers.heartbeat("PC2")
        assert services.monitor.run_once().offline_workers == []


def test_polling_again_does_not_postpone_the_assigned_timeout(services, clock):
    """A worker that keeps asking for work but never starts what it was given must not hold the task for ever."""
    (task_id,) = pending(services)
    services.tasks.claim_next("PC2")
    for _ in range(5):  # 6 min 15 s in all: the 5-minute limit ran from the FIRST hand-out
        clock.advance(minutes=1, seconds=15)
        services.tasks.claim_next("PC2")  # the same task again, as long as it has not timed out
        services.monitor.run_once()
    assert services.tasks.get_task(task_id).status is TaskStatus.TIMEOUT
    assert services.tasks.get_task(task_id).retry_count == 5  # handed out again at every poll until the monitor ran
    assert services.tasks.claim_next("PC2") is None


def test_assigned_task_times_out_after_5_minutes(services, clock):
    (task_id,) = pending(services)
    services.tasks.claim_next("PC2")

    clock.advance(minutes=5)
    assert services.monitor.run_once().assigned_timeouts == []
    clock.advance(seconds=1)
    services.workers.heartbeat("PC2")  # the worker is alive, it just never started the task
    report = services.monitor.run_once()

    assert report.assigned_timeouts == [task_id]
    task = services.tasks.get_task(task_id)
    assert task.status is TaskStatus.TIMEOUT
    assert task.completed_time == clock()
    assert "PC2" in task.error_message and "5 min" in task.error_message
    assert task.assigned_worker == "PC2"  # history keeps who had it
    assert stored_workers(services)["PC2"] == (WorkerStatus.ONLINE, None)  # but the worker is free


def test_running_task_times_out_after_60_minutes(services, clock):
    (task_id,) = pending(services)
    services.tasks.claim_next("PC2")
    services.tasks.start_task(task_id, "PC2")

    for _ in range(11):  # keep the worker alive for 55 minutes
        clock.advance(minutes=5)
        services.workers.heartbeat("PC2")
        assert services.monitor.run_once().running_timeouts == []
    clock.advance(minutes=5)
    services.workers.heartbeat("PC2")
    assert services.monitor.run_once().running_timeouts == []  # exactly 60 min is not over the limit
    clock.advance(seconds=1)

    report = services.monitor.run_once()

    assert report.running_timeouts == [task_id]
    task = services.tasks.get_task(task_id)
    assert task.status is TaskStatus.TIMEOUT and "1 hr" in task.error_message
    assert task.duration(clock()).total_seconds() == 3601
    assert stored_workers(services)["PC2"][1] is None


def test_running_time_counts_from_start_not_from_assignment(services, clock):
    (task_id,) = pending(services)
    services.tasks.claim_next("PC2")
    clock.advance(minutes=4)
    services.tasks.start_task(task_id, "PC2")
    clock.advance(minutes=59)
    services.workers.heartbeat("PC2")
    assert services.monitor.run_once().running_timeouts == []


def test_a_late_result_after_a_timeout_is_refused(services, clock):
    (task_id,) = pending(services)
    services.tasks.claim_next("PC2")
    services.tasks.start_task(task_id, "PC2")
    clock.advance(minutes=61)
    services.monitor.run_once()
    with pytest.raises(InvalidTransition):
        services.tasks.complete_task(task_id, "PC2", {"exists": True})
    with pytest.raises(InvalidTransition):
        services.tasks.fail_task(task_id, "PC2", "late")
    assert services.tasks.get_task(task_id).status is TaskStatus.TIMEOUT


def test_a_timed_out_task_is_not_handed_out_again(services, clock):
    (task_id,) = pending(services)
    services.tasks.claim_next("PC2")
    clock.advance(minutes=6)
    services.monitor.run_once()
    assert services.tasks.claim_next("PC2") is None
    assert services.tasks.claim_next("PC3") is None
    assert services.tasks.get_task(task_id).status is TaskStatus.TIMEOUT


def test_pending_tasks_never_time_out(services, clock):
    (task_id,) = pending(services)
    clock.advance(hours=48)
    assert not services.monitor.run_once().assigned_timeouts
    assert services.tasks.get_task(task_id).status is TaskStatus.PENDING


def test_finished_tasks_are_left_alone(services, clock):
    done, failed = pending(services, 2)
    services.tasks.claim_next("PC2")
    services.tasks.start_task(done, "PC2")
    services.tasks.complete_task(done, "PC2", {})
    services.tasks.claim_next("PC2")
    services.tasks.start_task(failed, "PC2")
    services.tasks.fail_task(failed, "PC2", "boom")
    clock.advance(hours=5)
    report = services.monitor.run_once()
    assert report.assigned_timeouts == [] and report.running_timeouts == []
    assert [t.status for t in services.tasks.get_tasks([done, failed])] == [TaskStatus.COMPLETED, TaskStatus.FAILED]


def test_timing_out_one_task_does_not_free_a_worker_that_moved_on(services, clock):
    first, second = pending(services, 2)
    services.tasks.claim_next("PC2")
    clock.advance(minutes=6)
    services.monitor.run_once()  # first -> TIMEOUT
    assert services.tasks.claim_next("PC2").id == second  # the worker takes the next task
    clock.advance(minutes=1)
    assert services.tasks.get_task(first).status is TaskStatus.TIMEOUT
    assert stored_workers(services)["PC2"][1] == second


def test_everything_in_one_pass(services, clock):
    a, b, c = pending(services, 3)
    services.tasks.claim_next("PC2")  # a: ASSIGNED
    services.tasks.claim_next("PC3")  # b: ASSIGNED -> RUNNING
    services.tasks.start_task(b, "PC3")
    clock.advance(minutes=61)
    services.workers.heartbeat("PC3")

    report = services.monitor.run_once()

    assert report.offline_workers == ["PC2"]
    assert report.assigned_timeouts == [a] and report.running_timeouts == [b]
    assert services.tasks.get_task(c).status is TaskStatus.PENDING


def test_custom_limits(db, clock):
    from datetime import timedelta

    services = Services.create(
        db, clock=clock, offline_after=timedelta(seconds=5),
        assigned_timeout=timedelta(seconds=10), running_timeout=timedelta(seconds=20),
    )
    assert services.monitor.limits == MonitorLimits(timedelta(seconds=10), timedelta(seconds=20))
    (task_id,) = pending(services)
    services.tasks.claim_next("PC2")
    clock.advance(seconds=11)
    assert services.monitor.run_once().assigned_timeouts == [task_id]
