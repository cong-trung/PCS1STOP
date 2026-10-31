from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time

import pytest
from helpers import LiveServer, ScriptedServer, free_port, wait_for

from database import TaskStatus
from services import Services
from worker import worker_agent as wa
from worker.worker_agent import Agent, ConfigProblem, Rejected, ServerUnavailable, TaskError, WorkerConfig


# ------------------------------------------------------------------------------------------ test rig
@pytest.fixture(autouse=True)
def agent_home(tmp_path, monkeypatch):
    """The agent's own folder (where it leaves worker.log when a setting is wrong) is a temp folder in every test:
    nothing a test does may write into the project's worker/ folder."""
    home = tmp_path / "agent_home"
    home.mkdir()
    monkeypatch.setattr(wa, "HERE", home)
    return home


def make_config(url: str, **overrides) -> WorkerConfig:
    values = dict(
        server_url=url, hostname="PC2-test", poll_interval_s=0.05, heartbeat_interval_s=0.1,
        request_timeout_s=2, task_timeout_s=2, max_backoff_s=0.2, max_delivery_s=30,
    )
    values.update(overrides)
    config = WorkerConfig(**values)
    config.validate()
    return config


class Running:
    """An agent running in a thread for the length of a ``with`` block."""

    def __init__(self, config: WorkerConfig, **kwargs) -> None:
        self.agent = Agent(config, **kwargs)
        self.thread = threading.Thread(target=self.agent.run, daemon=True)

    def __enter__(self) -> Agent:
        self.thread.start()
        return self.agent

    def __exit__(self, *exc) -> None:
        self.agent.stop.set()
        self.thread.join(10)
        assert not self.thread.is_alive(), "the agent did not stop"


@pytest.fixture
def live(services):
    server = LiveServer(services)
    server.start()
    yield server
    server.stop()


def status_of(services: Services, task_id: int) -> TaskStatus:
    return services.tasks.get_task(task_id).status


def queue_check(services: Services, path, product: str = "SST") -> int:
    return services.tasks.create_task("check_path", {"path": str(path), "product": product})


# -------------------------------------------------------------------------------------- check_path()
def test_check_path_of_a_file(tmp_path):
    target = tmp_path / "SST_PowerCycle.xml"
    target.write_text("<Workflow/>", encoding="utf-8")
    result = wa.check_path({"path": f"  {target}  ", "product": " HST "})
    assert result["exists"] is True and result["is_file"] is True
    assert result["path"] == str(target) and result["product"] == "HST"
    assert result["size"] == len("<Workflow/>")
    assert result["modified"].endswith("+00:00")
    assert "detail" not in result


def test_check_path_of_a_folder(tmp_path):
    result = wa.check_path({"path": str(tmp_path), "product": "SST"})
    assert (result["exists"], result["is_file"]) == (True, False)


def test_a_missing_file_is_an_answer_not_a_failure(tmp_path):
    result = wa.check_path({"path": str(tmp_path / "nope.xml"), "product": "SST"})
    assert result == {"path": str(tmp_path / "nope.xml"), "product": "SST", "exists": False, "is_file": False}


def test_a_file_below_a_file_is_not_found(tmp_path):
    (tmp_path / "a.xml").write_text("x")
    assert wa.check_path({"path": str(tmp_path / "a.xml" / "b.xml"), "product": "SST"})["exists"] is False


def test_a_path_that_cannot_exist_is_not_found(tmp_path):
    result = wa.check_path({"path": str(tmp_path) + "\\bad\0name", "product": "SST"})
    assert result["exists"] is False and result["detail"] == "invalid path"


@pytest.mark.parametrize(
    "payload",
    [{}, {"product": "SST"}, {"path": "", "product": "SST"}, {"path": "   ", "product": "SST"}, {"path": 5, "product": "SST"},
     {"path": "x.xml"}, {"path": "x.xml", "product": ""}, {"path": "x.xml", "product": None}],
)
def test_check_path_refuses_a_broken_payload(payload):
    with pytest.raises(TaskError):
        wa.check_path(payload)


def test_run_with_timeout():
    assert wa.run_with_timeout(lambda p: {"ok": p["n"]}, {"n": 3}, 2) == {"ok": 3}
    with pytest.raises(wa.TaskTimeout, match="0.1 s"):
        wa.run_with_timeout(lambda p: time.sleep(2) or {}, {}, 0.1)
    with pytest.raises(ValueError, match="oops"):
        wa.run_with_timeout(lambda p: (_ for _ in ()).throw(ValueError("oops")), {}, 2)
    with pytest.raises(TaskError, match="no result"):
        wa.run_with_timeout(lambda p: None, {}, 2)


def test_a_timed_out_handler_is_remembered_until_it_ends():
    gate = threading.Event()
    stranded = wa.Stranded()
    with pytest.raises(wa.TaskTimeout):
        wa.run_with_timeout(hang_until(gate), {}, 0.05, stranded)
    assert stranded.count() == 1
    gate.set()
    wait_for(lambda: stranded.count() == 0, what="the stuck handler to end")
    assert wa.run_with_timeout(lambda p: {}, {}, 2, stranded) == {} and stranded.count() == 0  # finished ones never count


# --------------------------------------------------------------------------------------- allowed_roots
def test_no_roots_allow_everything():
    assert wa.is_allowed("anything at all", [])
    assert wa.is_allowed("\\\\evil\\share\\x.xml", ())


def test_a_path_is_allowed_below_a_root_only(tmp_path):
    root = str(tmp_path)
    inside = os.path.join(root, "wf", "a.xml")
    assert wa.is_allowed(root, [root]) and wa.is_allowed(inside, [root])
    assert wa.is_allowed(inside, ["somewhere else", root])  # any of the roots will do
    assert not wa.is_allowed(root + "2" + os.sep + "a.xml", [root])  # a sibling that only starts the same
    assert not wa.is_allowed(os.path.join(root, "..", "x.xml"), [root])  # ".." cannot climb out
    assert not wa.is_allowed(os.path.join(root, "wf", "..", "..", "x.xml"), [root])
    assert wa.is_allowed(os.path.join(root, "wf", "..", "a.xml"), [root])  # ... but may stay inside
    assert wa.is_allowed(inside, [root + os.sep])  # a trailing separator on the root changes nothing
    assert not wa.is_allowed(inside, [os.path.join(root, "other")])


@pytest.mark.skipif(os.name != "nt", reason="drive letters and UNC names are Windows paths")
@pytest.mark.parametrize(
    ("path", "roots", "allowed"),
    [
        (r"M:\pcs\prod\wf\a.xml", [r"M:\pcs"], True),
        (r"m:\PCS\prod\a.xml", [r"M:\pcs"], True),  # case does not matter
        ("M:/pcs/prod/a.xml", [r"M:\pcs"], True),  # nor the kind of slash
        (r"M:\pcs2\a.xml", [r"M:\pcs"], False),
        (r"M:\pcs\..\other\a.xml", [r"M:\pcs"], False),
        (r"M:pcs\a.xml", [r"M:\pcs"], False),  # drive-relative: whatever the current folder on M: is
        (r"D:\pcs\a.xml", [r"M:\pcs"], False),
        (r"M:\anything", ["M:\\"], True),  # a whole drive
        (r"D:\anything", ["M:\\"], False),
        (r"\\srv\share\wf\a.xml", [r"\\srv\share"], True),
        (r"\\SRV\Share\a.xml", [r"\\srv\share"], True),
        (r"\\srv\share2\a.xml", [r"\\srv\share"], False),
        (r"\\evil\share\a.xml", [r"\\srv\share"], False),
        (r"\\srv\share\..\other\a.xml", [r"\\srv\share"], True),  # the share is the root: ".." cannot leave it
        (r"\\?\UNC\evil\share\a.xml", [r"\\srv\share"], False),
        (r"\\.\C:\x", [r"M:\pcs"], False),
    ],
)
def test_allowed_roots_on_windows(path, roots, allowed):
    assert wa.is_allowed(path, roots) is allowed


def test_check_path_refuses_paths_outside_the_allowed_roots(tmp_path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    (allowed / "a.xml").write_text("<x/>")
    (tmp_path / "secret.xml").write_text("<x/>")
    roots = [str(allowed)]

    assert wa.check_path({"path": str(allowed / "a.xml"), "product": "SST"}, roots)["exists"] is True
    assert wa.check_path({"path": str(allowed / "missing.xml"), "product": "SST"}, roots)["exists"] is False
    with pytest.raises(TaskError, match="not allowed"):
        wa.check_path({"path": str(tmp_path / "secret.xml"), "product": "SST"}, roots)
    with pytest.raises(TaskError, match="not allowed"):
        wa.check_path({"path": str(allowed / ".." / "secret.xml"), "product": "SST"}, roots)
    assert wa.check_path({"path": str(tmp_path / "secret.xml"), "product": "SST"})["exists"] is True  # no roots: anywhere


def test_the_default_handlers_use_the_configured_roots(tmp_path):
    (tmp_path / "in").mkdir()
    (tmp_path / "in" / "a.xml").write_text("<x/>")
    config = make_config("http://127.0.0.1:1", allowed_roots=[str(tmp_path / "in")])
    handler = wa.default_handlers(config)["check_path"]
    assert handler({"path": str(tmp_path / "in" / "a.xml"), "product": "HST"})["exists"] is True
    with pytest.raises(TaskError, match="not allowed"):
        handler({"path": str(tmp_path / "other.xml"), "product": "HST"})


def test_a_refused_path_fails_the_task_through_the_whole_stack(live, services, tmp_path):
    (tmp_path / "in").mkdir()
    ok = queue_check(services, tmp_path / "in" / "a.xml")
    refused = queue_check(services, tmp_path / "out.xml")
    with Running(make_config(live.url, allowed_roots=[str(tmp_path / "in")])):
        wait_for(lambda: status_of(services, refused) is TaskStatus.FAILED, what="the refusal")
        wait_for(lambda: status_of(services, ok) is TaskStatus.COMPLETED, what="the allowed check")
    assert "not allowed" in services.tasks.get_task(refused).error_message
    assert services.tasks.get_task(ok).result["exists"] is False  # (the file is not there - but it was looked for)


# ------------------------------------------------------------------------------------- configuration
def test_config_defaults_are_valid():
    config = WorkerConfig()
    config.validate()
    assert (config.poll_interval_s, config.heartbeat_interval_s) == (10.0, 20.0)


def test_the_shipped_config_file_is_valid():
    config = WorkerConfig.load()
    assert config.server_url.startswith("http") and config.poll_interval_s == 10


def test_config_file_and_overrides(tmp_path):
    path = tmp_path / "w.json"
    path.write_text(json.dumps({"server_url": "http://pc1:9000", "poll_interval_s": 3, "api_token": "t", "bogus": 1}))
    config = WorkerConfig.load(path, hostname="PC9", api_token=None, server_url=None)
    assert (config.server_url, config.poll_interval_s, config.api_token, config.hostname) == ("http://pc1:9000", 3, "t", "PC9")
    assert config.base_dir == tmp_path
    assert WorkerConfig.load(path, server_url="http://other:1").server_url == "http://other:1"


@pytest.mark.parametrize(
    "values",
    [{"server_url": "pc1:8080"}, {"server_url": "ftp://pc1"}, {"server_url": ""}, {"poll_interval_s": 0},
     {"poll_interval_s": "10"}, {"heartbeat_interval_s": -1}, {"task_timeout_s": True}, {"api_token": 5}, {"hostname": 1},
     {"api_token": "tøken"}, {"api_token": "to\nken"},
     {"allowed_roots": "M:\\pcs"}, {"allowed_roots": [""]}, {"allowed_roots": ["  "]}, {"allowed_roots": [5]},
     {"max_stuck_tasks": 0}, {"max_stuck_tasks": "8"}, {"max_stuck_tasks": True}, {"max_stuck_tasks": 2.5}],
)
def test_bad_config_values(tmp_path, values):
    path = tmp_path / "w.json"
    path.write_text(json.dumps(values))
    with pytest.raises(ConfigProblem):
        WorkerConfig.load(path)


def test_allowed_roots_and_the_stuck_limit_come_from_the_file(tmp_path):
    path = tmp_path / "w.json"
    path.write_text(json.dumps({"allowed_roots": ["M:\\pcs", "\\\\srv\\share"], "max_stuck_tasks": 3, "api_token": " t0k3n "}))
    config = WorkerConfig.load(path)
    assert config.allowed_roots == ["M:\\pcs", "\\\\srv\\share"] and config.max_stuck_tasks == 3
    assert wa.Client(config).token == "t0k3n"  # surrounding blanks are not part of the token
    assert WorkerConfig().allowed_roots == [] and WorkerConfig().max_stuck_tasks == 8


def test_config_file_problems(tmp_path):
    with pytest.raises(ConfigProblem, match="does not exist"):
        WorkerConfig.load(tmp_path / "missing.json")
    broken = tmp_path / "broken.json"
    broken.write_text("{ nope")
    with pytest.raises(ConfigProblem):
        WorkerConfig.load(broken)
    broken.write_text("[1]")
    with pytest.raises(ConfigProblem, match="JSON object"):
        WorkerConfig.load(broken)


def test_main_reports_a_bad_config(tmp_path, capsys):
    assert wa.main(["--config", str(tmp_path / "missing.json")]) == 2
    assert "Cannot start" in capsys.readouterr().err


def test_backoff_grows_and_is_capped():
    agent = Agent(WorkerConfig(max_backoff_s=60, poll_interval_s=10), client=object())
    waits = [agent._backoff(n) for n in (1, 2, 3, 4, 5, 6, 50)]
    assert 1.6 <= waits[0] <= 2.4 and 3.2 <= waits[1] <= 4.8 and 6.4 <= waits[2] <= 9.6
    assert all(w <= 60 for w in waits) and 48 <= waits[-1] <= 60


# ------------------------------------------------------------------------------ the agent, end to end
def test_the_agent_works_off_the_queue(live, services, tmp_path):
    good = tmp_path / "SST_PowerCycle.xml"
    good.write_text("<Workflow/>")
    ids = [
        queue_check(services, good, "HST"),
        queue_check(services, tmp_path / "missing.xml", "HST"),
        queue_check(services, tmp_path, "HST"),
    ]

    with Running(make_config(live.url)):
        wait_for(lambda: all(status_of(services, i) is TaskStatus.COMPLETED for i in ids), what="the three checks")

    found, missing, folder = services.tasks.get_tasks(ids)
    assert (found.result["exists"], found.result["is_file"], found.result["product"]) == (True, True, "HST")
    assert found.result["path"] == str(good)
    assert (missing.result["exists"], folder.result["exists"], folder.result["is_file"]) == (False, True, False)
    assert {t.assigned_worker for t in (found, missing, folder)} == {"PC2-test"}
    assert all(t.started_time and t.completed_time and t.error_message is None for t in (found, missing, folder))
    (worker,) = services.workers.list_workers()
    assert (worker.hostname, worker.version, worker.ip_address) == ("PC2-test", wa.__version__, "127.0.0.1")
    assert worker.current_task_id is None


def test_the_queue_is_drained_without_waiting_for_the_poll_interval(live, services, tmp_path):
    ids = [queue_check(services, tmp_path / f"f{i}.xml") for i in range(15)]
    started = time.monotonic()
    with Running(make_config(live.url, poll_interval_s=30)):
        wait_for(lambda: all(status_of(services, i) is TaskStatus.COMPLETED for i in ids), timeout=15, what="the queue")
    assert time.monotonic() - started < 10  # 15 tasks with a 30 s poll interval: only possible when draining


def test_two_agents_share_the_queue_without_duplicates(live, services, tmp_path):
    ids = [queue_check(services, tmp_path / f"f{i}.xml") for i in range(40)]
    first, second = make_config(live.url, hostname="PC2-a"), make_config(live.url, hostname="PC2-b")
    with Running(first), Running(second):
        wait_for(lambda: all(status_of(services, i) is TaskStatus.COMPLETED for i in ids), timeout=20, what="the queue")
    workers = [t.assigned_worker for t in services.tasks.get_tasks(ids)]
    assert set(workers) == {"PC2-a", "PC2-b"} and len(workers) == 40


def test_unknown_task_types_fail(live, services):
    task_id = services.tasks.create_task("mystery", {"x": 1})
    with Running(make_config(live.url)):
        wait_for(lambda: status_of(services, task_id) is TaskStatus.FAILED, what="the failure")
    assert "mystery" in services.tasks.get_task(task_id).error_message


def test_a_broken_payload_fails_the_task_and_the_agent_goes_on(live, services, tmp_path):
    bad = services.tasks.create_task("check_path", {"product": "SST"})
    good = queue_check(services, tmp_path / "x.xml")
    with Running(make_config(live.url)):
        wait_for(lambda: status_of(services, good) is TaskStatus.COMPLETED, what="the second task")
    task = services.tasks.get_task(bad)
    assert task.status is TaskStatus.FAILED and "no path" in task.error_message


def test_handler_crashes_and_hangs_fail_only_their_own_task(live, services, tmp_path):
    def boom(payload):
        raise RuntimeError("kaboom")

    def hang(payload):
        time.sleep(5)
        return {}

    handlers = {"boom": boom, "hang": hang, "check_path": wa.check_path}
    crash = services.tasks.create_task("boom", {})
    stuck = services.tasks.create_task("hang", {})
    fine = queue_check(services, tmp_path / "x.xml")
    with Running(make_config(live.url, task_timeout_s=0.3), handlers=handlers):
        wait_for(lambda: status_of(services, fine) is TaskStatus.COMPLETED, what="the good task")
    assert services.tasks.get_task(crash).error_message == "RuntimeError: kaboom"
    assert "no answer after 0.3 s" in services.tasks.get_task(stuck).error_message
    assert status_of(services, crash) is status_of(services, stuck) is TaskStatus.FAILED


def test_the_api_token_is_sent(live, services, tmp_path):
    services.api_token = "s3cret"
    task_id = queue_check(services, tmp_path / "x.xml")
    with Running(make_config(live.url)):  # no token: refused, logged, nothing happens
        time.sleep(0.6)
        assert status_of(services, task_id) is TaskStatus.PENDING
    with Running(make_config(live.url, api_token="s3cret")):
        wait_for(lambda: status_of(services, task_id) is TaskStatus.COMPLETED, what="the checked task")


def test_the_agent_reconnects_by_itself_after_an_outage(live, services, tmp_path):
    first = queue_check(services, tmp_path / "a.xml")
    with Running(make_config(live.url)):
        wait_for(lambda: status_of(services, first) is TaskStatus.COMPLETED, what="the first task")
        live.stop()
        second = queue_check(services, tmp_path / "b.xml")  # queued while PC1's web server is down
        time.sleep(0.7)
        assert status_of(services, second) is TaskStatus.PENDING
        live.start()
        wait_for(lambda: status_of(services, second) is TaskStatus.COMPLETED, what="the task after the reconnect")


def test_a_result_is_delivered_after_the_server_comes_back(live, services):
    gate, entered = threading.Event(), threading.Event()

    def slow(payload):
        entered.set()
        gate.wait(10)
        return {"exists": True, "is_file": True, "size": 1}

    task_id = services.tasks.create_task("slow", {})
    with Running(make_config(live.url), handlers={"slow": slow}):
        wait_for(entered.is_set, what="the handler to start")
        assert status_of(services, task_id) is TaskStatus.RUNNING
        live.stop()
        gate.set()  # the work is done while PC1 is unreachable
        time.sleep(0.7)
        assert status_of(services, task_id) is TaskStatus.RUNNING
        live.start()
        wait_for(lambda: status_of(services, task_id) is TaskStatus.COMPLETED, what="the late delivery")
    assert services.tasks.get_task(task_id).result == {"exists": True, "is_file": True, "size": 1}


def test_a_long_task_keeps_the_worker_online_through_the_heartbeat(live, services, clock):
    gate, entered = threading.Event(), threading.Event()

    def slow(payload):
        entered.set()
        gate.wait(10)
        return {}

    services.tasks.create_task("slow", {})
    with Running(make_config(live.url, poll_interval_s=30), handlers={"slow": slow}):
        wait_for(entered.is_set, what="the handler to start")
        clock.advance(seconds=45)  # (the polling is 30 s apart: only the heartbeat thread can report in meanwhile)
        wait_for(lambda: services.workers.list_workers()[0].last_seen == clock(), what="a heartbeat while busy")
        assert services.workers.list_workers()[0].status.value == "ONLINE"
        gate.set()


def test_the_agent_stops_promptly(live):
    config = make_config(live.url, poll_interval_s=30, heartbeat_interval_s=30)
    started = time.monotonic()
    with Running(config):
        time.sleep(0.2)
    assert time.monotonic() - started < 3


# --------------------------------------------------------------------- the agent against a scripted PC1
class ScriptedClient:
    def __init__(self, tasks, start=None, complete=None, fail=None) -> None:
        self.tasks, self.calls, self.deliveries = list(tasks), [], []
        self._start, self._complete, self._fail = start, complete, fail

    def next_task(self, hostname):
        self.calls.append(("next", hostname))
        return self.tasks.pop(0) if self.tasks else None

    def start(self, task_id, hostname, delivery=None):
        self.calls.append(("start", task_id))
        self.deliveries.append(delivery)
        if self._start:
            raise self._start

    def complete(self, task_id, hostname, result):
        self.calls.append(("complete", task_id, result))
        if self._complete:
            raise self._complete

    def fail(self, task_id, hostname, error):
        self.calls.append(("fail", task_id, error))
        if self._fail:
            raise self._fail

    def heartbeat(self, hostname, ip):
        self.calls.append(("heartbeat",))


def agent_with(client, handlers=None, **overrides) -> Agent:
    return Agent(make_config("http://127.0.0.1:1", **overrides), client=client, handlers=handlers)


TASK = {"task_id": 7, "task_type": "echo", "payload": {"n": 1}}


def test_a_task_that_pc1_refuses_to_start_is_dropped_without_running():
    ran = []
    client = ScriptedClient([TASK], start=Rejected(409, "it is TIMEOUT"))
    agent = agent_with(client, {"echo": lambda p: ran.append(p) or {}})
    assert agent.step() == agent.config.poll_interval_s  # no hurry to ask again: the same task could come straight back
    assert not ran and [c[0] for c in client.calls] == ["next", "start"]
    assert agent.current_task is None


def test_after_a_task_that_was_carried_out_the_next_is_asked_for_at_once():
    agent = agent_with(ScriptedClient([TASK]), {"echo": lambda p: {}})
    assert agent.step() == 0


def test_the_delivery_number_is_passed_back_to_start():
    client = ScriptedClient([{**TASK, "delivery": 3}, {**TASK, "task_id": 8}, {**TASK, "task_id": 9, "delivery": True}])
    agent = agent_with(client, {"echo": lambda p: {}})
    for _ in range(3):
        agent.step()
    assert client.deliveries == [3, None, None]  # (a PC1 that sends none, or something silly, is not an error)


@pytest.mark.parametrize("task", [{"task_id": "abc"}, {"task_id": None}, {"task_id": [1]}, {"task_type": "echo"}])
def test_a_task_without_a_usable_id_is_ignored(task):
    client = ScriptedClient([task])
    agent = agent_with(client, {"echo": lambda p: {}})
    assert agent.step() == agent.config.poll_interval_s
    assert [c[0] for c in client.calls] == ["next"]


def test_an_outcome_that_pc1_refuses_is_only_logged():
    client = ScriptedClient([TASK], complete=Rejected(409, "it is TIMEOUT"))
    agent = agent_with(client, {"echo": lambda p: {"n": p["n"]}})
    assert agent.step() == 0
    assert client.calls[-1] == ("complete", 7, {"n": 1})


def test_a_result_that_is_too_large_fails_the_task_instead_of_hanging_it():
    big = {"data": "x" * (wa.MAX_RESULT_BYTES + 1)}
    client = ScriptedClient([TASK])
    agent = agent_with(client, {"echo": lambda p: big})
    assert agent.step() == 0
    kind, task_id, message = client.calls[-1]
    assert (kind, task_id) == ("fail", 7) and "too large" in message and "256 KB" in message
    assert not any(call[0] == "complete" for call in client.calls)

    edge = {"data": "x" * (wa.MAX_RESULT_BYTES - len('{"data":""}'))}  # exactly at the limit: still sent
    client = ScriptedClient([TASK])
    agent_with(client, {"echo": lambda p: edge}).step()
    assert client.calls[-1][0] == "complete"


def test_a_server_that_stays_down_ends_the_delivery_in_the_end():
    client = ScriptedClient([TASK], complete=ServerUnavailable("connection refused"))
    agent = agent_with(client, {"echo": lambda p: {}}, max_delivery_s=0.3)
    agent.step()
    assert [c[0] for c in client.calls].count("complete") >= 2  # it tried again and again, then gave up
    assert agent.current_task is None


def test_polling_failures_back_off_and_recovery_resets():
    class Flaky(ScriptedClient):
        def next_task(self, hostname):
            if not self.calls:
                self.calls.append(("next",))
                raise ServerUnavailable("connection refused")
            return super().next_task(hostname)

    agent = agent_with(Flaky([]), poll_interval_s=10, max_backoff_s=60)
    assert 1.6 <= agent.step() <= 2.4  # first failure
    assert agent._failures == 1
    assert agent.step() == 10  # reached PC1 again: normal polling
    assert agent._failures == 0 and agent._down_since is None


def test_an_unexpected_error_does_not_end_the_agent():
    class Buggy(ScriptedClient):
        def next_task(self, hostname):
            if not self.calls:
                self.calls.append(("next",))
                raise RuntimeError("a bug")
            return super().next_task(hostname)

    ran = threading.Event()
    client = Buggy([TASK])
    with Running(make_config("http://127.0.0.1:1", poll_interval_s=0.05), client=client, handlers={"echo": lambda p: ran.set() or {}}):
        wait_for(ran.is_set, what="the task after the unexpected error")
    assert ("complete", 7, {}) in client.calls


# ------------------------------------------------------------------- stale deliveries, end to end
def test_an_agent_that_holds_a_stale_delivery_does_not_run_the_task(live, services):
    """The answer to agent A's poll got lost / a second agent with the same name polled: only the newest delivery runs."""
    ran = []
    task_id = services.tasks.create_task("t", {"n": 1})
    first = services.tasks.claim_next("PC2-test")  # delivery 0 (what the first agent got)
    second = services.tasks.claim_next("PC2-test")  # delivery 1 (what its replacement got)
    assert (first.retry_count, second.retry_count) == (0, 1)
    agent = Agent(make_config(live.url), handlers={"t": lambda p: ran.append(p) or {"ok": 1}})

    stale = {"task_id": task_id, "task_type": "t", "payload": {"n": 1}, "delivery": 0}
    assert agent.execute(stale) is False and not ran
    assert status_of(services, task_id) is TaskStatus.ASSIGNED  # the refused start changed nothing

    assert agent.execute({**stale, "delivery": 1}) is True and ran == [{"n": 1}]
    assert status_of(services, task_id) is TaskStatus.COMPLETED
    assert agent.execute(stale) is False and len(ran) == 1  # a late echo of the old delivery runs nothing again


def test_agents_with_the_same_name_never_run_a_task_twice(live, services):
    runs: dict[int, int] = {}
    lock = threading.Lock()

    def count(payload):
        with lock:
            runs[payload["n"]] = runs.get(payload["n"], 0) + 1
        time.sleep(0.002)
        return {"n": payload["n"]}

    ids = [services.tasks.create_task("count", {"n": n}) for n in range(60)]
    config = make_config(live.url, hostname="PC2-twin", poll_interval_s=0.01)
    with Running(config, handlers={"count": count}), Running(config, handlers={"count": count}):
        wait_for(lambda: all(status_of(services, i) is TaskStatus.COMPLETED for i in ids), timeout=30, what="the queue")
    assert runs == {n: 1 for n in range(60)}  # every task ran exactly once, whoever got it


# ------------------------------------------------------------------------------------ stuck checks
def hang_until(gate: threading.Event, answer: dict | None = None):
    """A handler that blocks until ``gate`` is set (a share that does not answer), then returns ``answer``."""

    def handler(payload):
        gate.wait(30)
        return dict(answer or {"n": payload.get("n")})

    return handler


def test_too_many_stuck_checks_pause_the_polling_until_they_are_gone(live, services):
    gate = threading.Event()
    ids = [services.tasks.create_task("hang", {"n": n}) for n in range(4)]
    config = make_config(live.url, task_timeout_s=0.15, max_stuck_tasks=2)
    with Running(config, handlers={"hang": hang_until(gate)}) as agent:
        wait_for(lambda: sum(status_of(services, i) is TaskStatus.FAILED for i in ids) == 2, what="two timed-out checks")
        time.sleep(0.8)  # plenty of polls' worth of time
        assert [status_of(services, i) for i in ids] == [TaskStatus.FAILED] * 2 + [TaskStatus.PENDING] * 2
        assert agent.stranded.count() == 2 and agent._refusing

        gate.set()  # the share answers at last: the stuck threads end
        wait_for(lambda: all(status_of(services, i) in (TaskStatus.FAILED, TaskStatus.COMPLETED) for i in ids), what="the rest")
    assert [status_of(services, i) for i in ids[2:]] == [TaskStatus.COMPLETED] * 2
    assert "no answer after" in services.tasks.get_task(ids[0]).error_message


def test_the_stuck_limit_does_not_stop_the_heartbeat(live, services, clock):
    gate = threading.Event()
    for n in range(2):
        services.tasks.create_task("hang", {"n": n})
    config = make_config(live.url, task_timeout_s=0.1, max_stuck_tasks=2, heartbeat_interval_s=0.1)
    with Running(config, handlers={"hang": hang_until(gate)}) as agent:
        wait_for(lambda: agent.stranded.count() == 2, what="two stuck checks")
        wait_for(lambda: agent._refusing, what="the agent to stop taking tasks")
        clock.advance(seconds=45)  # (the services run on a hand-moved clock) - only the heartbeat thread can report in now
        wait_for(lambda: services.workers.list_workers()[0].last_seen == clock(), what="a heartbeat while refusing work")
        gate.set()


# ------------------------------------------------------------------------------- --check (deployment test)
def report_of(config) -> tuple[int, str]:
    lines: list[str] = []
    return wa.run_check(config, lines.append), "\n".join(lines)


def test_check_passes_against_a_running_pc1_and_registers_the_worker(live, services):
    code, report = report_of(make_config(live.url, hostname="PC2-new"))
    assert code == 0
    assert "[ ok ] PC1 is reachable" in report and "[ ok ] the API token is accepted" in report
    assert "FAIL" not in report and "PC2-new" in report and live.url in report
    assert [w.hostname for w in services.workers.list_workers()] == ["PC2-new"]  # it shows on the Workers page


def test_check_with_the_right_api_token(live, services):
    services.api_token = "s3cret-Token_1"
    code, report = report_of(make_config(live.url, api_token="s3cret-Token_1"))
    assert code == 0 and "API token:       set" in report


@pytest.mark.parametrize("token", ["", "wrong-token"])
def test_check_says_when_the_api_token_is_missing_or_wrong(live, services, token):
    services.api_token = "s3cret-Token_1"
    code, report = report_of(make_config(live.url, api_token=token))
    assert code == 1
    assert "[ ok ] PC1 is reachable" in report  # (the health check needs no token: PC1 is there, the token is the problem)
    assert "[FAIL] PC1 refused the API token" in report and "tasks.api_token" in report
    assert services.workers.list_workers() == []  # nothing was registered
    assert ("API token:       set" in report) == bool(token) and ("NOT SET" in report) == (not token)


def test_check_says_when_pc1_cannot_be_reached():
    code, report = report_of(make_config(f"http://127.0.0.1:{free_port()}", request_timeout_s=2))
    assert code == 1
    assert "[FAIL] cannot reach PC1" in report and "0.0.0.0" in report and "firewall" in report


def test_check_says_when_something_else_answers(live):
    code, report = report_of(make_config(live.url + "/not-the-app"))
    assert code == 1 and "not the PCS 1-Stop app" in report and "[ ok ]" not in report


def json_answer(payload: dict, status: int = 200, **headers: str) -> tuple[int, dict[str, str], bytes]:
    return status, {"Content-Type": "application/json", **headers}, json.dumps(payload).encode()


def test_the_health_check_does_not_send_the_api_token_but_the_registration_does():
    """Nothing is known about who answers at server_url until /api/health said so: the token must not go there first."""
    answers = {"/api/health": json_answer({"status": "ok"}), "/api/worker/heartbeat": json_answer({"status": "ok"})}
    with ScriptedServer(answers) as pc1:
        code, report = report_of(make_config(pc1.url, api_token="s3cret"))
    assert code == 0, report
    assert [(path, headers.get("x-api-token")) for _, path, headers in pc1.requests] == [
        ("/api/health", None),
        ("/api/worker/heartbeat", "s3cret"),
    ]


def test_a_redirect_is_refused_so_the_token_is_never_handed_on():
    with ScriptedServer({}) as elsewhere:  # what a redirect would point at
        answers = {
            "/api/health": json_answer({"status": "ok"}),
            "/api/worker/heartbeat": (307, {"Location": elsewhere.url + "/api/worker/heartbeat"}, b""),
            "/api/task/next": (302, {"Location": elsewhere.url + "/api/task/next"}, b""),
        }
        with ScriptedServer(answers) as pc1:
            code, report = report_of(make_config(pc1.url, api_token="s3cret"))
            with pytest.raises(Rejected, match="redirected to .*/api/task/next"):
                wa.Client(make_config(pc1.url, api_token="s3cret")).next_task("PC2-test")
        assert code == 1 and "[FAIL]" in report and "redirected to" in report
        assert elsewhere.requests == []  # nobody there saw a request, let alone the token


def test_a_redirected_health_check_says_it_is_not_the_app():
    with ScriptedServer({}) as elsewhere:
        with ScriptedServer({"/api/health": (301, {"Location": elsewhere.url + "/api/health"}, b"")}) as pc1:
            code, report = report_of(make_config(pc1.url))
        assert code == 1 and "not the PCS 1-Stop app" in report and "HTTP 301" in report
        assert elsewhere.requests == []


def test_a_redirecting_pc1_does_not_stop_the_agent_it_is_logged_and_retried(caplog):
    with ScriptedServer({"/api/task/next": (302, {"Location": "http://127.0.0.1:9/x"}, b"")}) as pc1:
        agent = agent_with(wa.Client(make_config(pc1.url)))
        with caplog.at_level(logging.WARNING, logger="pcs.worker"):
            delay = agent.step()
    assert delay > 0 and "redirected to" in caplog.text


def test_check_reports_the_allowed_folders_without_failing_on_them(live, tmp_path):
    (tmp_path / "wf").mkdir()
    config = make_config(live.url, allowed_roots=[str(tmp_path / "wf"), str(tmp_path / "gone")])
    code, report = report_of(config)
    assert code == 0  # a folder that is not there is a warning, not a failed install
    assert f"[ ok ] allowed folder found: {tmp_path / 'wf'}" in report
    assert f"[warn] allowed folder not found from this PC: {tmp_path / 'gone'}" in report


def test_check_from_the_command_line(live, services, tmp_path, capsys):
    path = tmp_path / "w.json"
    path.write_text(json.dumps({"server_url": live.url, "hostname": "PC2-cli", "log_file": ""}))
    assert wa.main(["--check", "--config", str(path)]) == 0
    out = capsys.readouterr().out
    assert "[ ok ] the API token is accepted" in out
    assert [w.hostname for w in services.workers.list_workers()] == ["PC2-cli"]
    assert not list(tmp_path.glob("*.log"))  # a test run leaves no log file behind


def test_check_from_the_command_line_fails_with_exit_code_1(tmp_path, capsys):
    path = tmp_path / "w.json"
    path.write_text(json.dumps({"server_url": f"http://127.0.0.1:{free_port()}", "request_timeout_s": 2}))
    assert wa.main(["--check", "--config", str(path)]) == 1
    assert "cannot reach PC1" in capsys.readouterr().out


# ----------------------------------------------------------------------- running without a console (pythonw)
@pytest.fixture
def keep_logging():
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    yield
    for handler in root.handlers[:]:
        root.removeHandler(handler)
    for handler in handlers:
        root.addHandler(handler)
    root.setLevel(level)


def test_a_bad_setting_is_written_to_worker_log_too(agent_home, capsys):
    broken = agent_home / "w.json"
    broken.write_text(json.dumps({"server_url": "pc1-without-http"}))

    assert wa.main(["--config", str(broken)]) == 2

    assert "Cannot start" in capsys.readouterr().err
    logged = (agent_home / "worker.log").read_text(encoding="utf-8")
    assert "ERROR" in logged and "Cannot start" in logged and "server_url" in logged


def test_logging_works_when_there_is_no_console(tmp_path, monkeypatch, keep_logging):
    monkeypatch.setattr(sys, "stderr", None)  # what pythonw.exe has
    log_file = tmp_path / "worker.log"
    wa.setup_logging(make_config("http://127.0.0.1:1", log_file=str(log_file)))
    logging.getLogger("pcs.worker").warning("no console, still logged")
    for handler in logging.getLogger().handlers:
        handler.flush()
    assert "no console, still logged" in log_file.read_text(encoding="utf-8")


def test_logging_without_console_and_without_log_file_does_not_crash(monkeypatch, keep_logging):
    monkeypatch.setattr(sys, "stderr", None)
    wa.setup_logging(make_config("http://127.0.0.1:1", log_file=""))
    logging.getLogger("pcs.worker").warning("nobody listens")  # must simply vanish
