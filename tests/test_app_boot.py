"""Start the real app (NiceGUI + API + scheduler) in a subprocess and talk to it over HTTP."""
from __future__ import annotations

import http.client
import json
import logging
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def get(url: str, headers: dict | None = None) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers or {}), timeout=10) as reply:
            return reply.status, reply.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


def post(url: str, body: dict) -> dict:
    request = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=10) as reply:
        return json.loads(reply.read())


@pytest.fixture(scope="module")
def app_url(tmp_path_factory):
    folder = tmp_path_factory.mktemp("app")
    port = free_port()
    config = folder / "config.json"
    config.write_text(
        json.dumps(
            {
                "paths": {"alarm_map": ""},
                "server": {"host": "127.0.0.1", "port": port},
                "database": {"path": "db/queue.db"},
                "tasks": {"monitor_interval_s": 1},
            }
        ),
        encoding="utf-8",
    )
    log = (folder / "app.out").open("wb")
    env = {key: value for key, value in os.environ.items() if key != "PYTEST_CURRENT_TEST"}  # NiceGUI would think it is a screen test
    process = subprocess.Popen(
        [sys.executable, str(ROOT / "app.py"), "--config", str(config)],
        cwd=folder, env=env, stdout=log, stderr=subprocess.STDOUT,
    )
    url = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            if process.poll() is not None:
                pytest.fail(f"the app exited with {process.returncode}:\n{(folder / 'app.out').read_text(errors='replace')}")
            try:
                if get(url + "/api/health")[0] == 200:
                    break
            except OSError:
                time.sleep(0.2)
        else:
            pytest.fail("the app did not start in 40 s")
        yield url, folder
    finally:
        process.terminate()
        process.wait(15)
        log.close()


def test_the_app_never_opens_a_browser(tmp_path):
    import app as pcs_app
    from config import Settings

    options = pcs_app.run_options(Settings(tmp_path / "config.json"))
    assert options["show"] is False and options["reload"] is False
    assert (options["host"], options["port"]) == ("127.0.0.1", 8080)


def notes_for(tmp_path, server: dict, token: str = "s3cret") -> list[tuple[int, str]]:
    import app as pcs_app
    from config import Settings

    path = tmp_path / "config.json"
    path.write_text(json.dumps({"server": server, "tasks": {"api_token": token}}), encoding="utf-8")
    return pcs_app.reachability_notes(Settings(path))


def test_listening_on_this_pc_only_is_fine_behind_a_reverse_proxy(tmp_path):
    notes = notes_for(tmp_path, {"host": "127.0.0.1", "port": 8007, "public_url": "http://ppv-pcs1stop-ss.intel.com"})
    assert [level for level, _ in notes] == [logging.INFO]
    assert "127.0.0.1:8007" in notes[0][1] and "http://ppv-pcs1stop-ss.intel.com" in notes[0][1]


def test_listening_on_this_pc_only_without_a_public_url_says_no_worker_can_connect(tmp_path):
    notes = notes_for(tmp_path, {"host": "127.0.0.1"})
    assert [level for level, _ in notes] == [logging.WARNING]
    assert "cannot connect" in notes[0][1] and "server.public_url" in notes[0][1]


def test_an_api_the_network_can_reach_without_a_token_is_warned_about(tmp_path):
    for server in ({"host": "0.0.0.0"}, {"host": "127.0.0.1", "public_url": "http://pc1.example.com"}):  # direct / proxied
        warnings = [text for level, text in notes_for(tmp_path, server, token="") if level == logging.WARNING]
        assert any("without a token" in text for text in warnings), server
    assert notes_for(tmp_path, {"host": "0.0.0.0"}) == []  # on the network with a token: nothing to say
    only = notes_for(tmp_path, {"host": "127.0.0.1"}, token="")  # nobody can reach it at all: the token is no issue
    assert not any("without a token" in text for _, text in only)


def test_the_three_pages_and_the_assets_are_served(app_url):
    url, _ = app_url
    status, html = get(url + "/")
    assert status == 200 and "PCS Recipe Checker" in html
    assert "/pcs/pcs_grid.js" in html and "/pcs/pcs.css" in html
    status, html = get(url + "/tasks")
    assert status == 200 and "PCS 1-Stop - Tasks" in html and "/pcs/ops.css" in html
    status, html = get(url + "/workers")
    assert status == 200 and "PCS 1-Stop - Workers" in html
    for asset in ("pcs_grid.js", "pcs.css", "ops.css"):
        assert get(f"{url}/pcs/{asset}")[0] == 200


def test_the_worker_api_round_trip(app_url):
    url, folder = app_url
    created = post(url + "/api/task/create", {"task_type": "check_path", "payload": {"path": r"M:\wf\a.xml", "product": "HST"}})
    assert created["success"] is True
    task = json.loads(get(url + "/api/task/next?worker=PC2")[1])
    assert task == {
        "task_id": created["task_id"],
        "task_type": "check_path",
        "payload": {"path": r"M:\wf\a.xml", "product": "HST"},
        "delivery": 0,
    }
    assert post(url + "/api/task/start", {"task_id": task["task_id"], "worker": "PC2", "delivery": task["delivery"]}) == {"success": True}
    assert post(url + "/api/task/complete", {"task_id": task["task_id"], "worker": "PC2", "result": {"exists": False}}) == {"success": True}
    shown = json.loads(get(f"{url}/api/task/{task['task_id']}")[1])
    assert shown["status"] == "COMPLETED" and shown["result"] == {"exists": False} and shown["assigned_worker"] == "PC2"
    assert json.loads(get(url + "/api/task/next?worker=PC2")[1]) == {"task": None}
    assert (folder / "db" / "queue.db").exists()


def test_errors_are_json_and_unknown_pages_are_404(app_url):
    url, _ = app_url
    status, body = get(url + "/api/task/999")
    assert status == 404 and json.loads(body)["success"] is False
    assert get(url + "/no-such-page")[0] == 404


def test_the_body_size_guard_is_active_on_the_real_app(app_url):
    """Only the headers are sent: the answer must come without the (declared, never sent) 5 MB being read."""
    url, _ = app_url
    connection = http.client.HTTPConnection(urllib.parse.urlsplit(url).netloc, timeout=10)
    try:
        connection.putrequest("POST", "/api/task/create")
        connection.putheader("Content-Type", "application/json")
        connection.putheader("Content-Length", str(5 * 1024 * 1024))
        connection.endheaders()
        reply = connection.getresponse()
        assert reply.status == 413 and json.loads(reply.read())["success"] is False
    finally:
        connection.close()


def test_the_monitor_and_the_log_are_running(app_url):
    _, folder = app_url
    deadline = time.monotonic() + 10
    log_file = folder / "db" / "pcs1stop.log"
    while time.monotonic() < deadline and not (log_file.exists() and "timeout monitor started" in log_file.read_text(errors="replace")):
        time.sleep(0.2)
    text = log_file.read_text(errors="replace")
    assert "timeout monitor started" in text and "task store:" in text


def test_a_broken_config_stops_the_start_with_a_message(tmp_path):
    config = tmp_path / "config.json"
    config.write_text('{"server": {"port": 99999}}', encoding="utf-8")
    run = subprocess.run(
        [sys.executable, str(ROOT / "app.py"), "--config", str(config)],
        cwd=tmp_path, capture_output=True, text=True, timeout=60,
    )
    assert run.returncode != 0 and "Cannot start" in run.stderr and "server.port" in run.stderr


def test_an_unusable_database_folder_stops_the_start_with_a_message(tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("a file where the database folder should be")
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"database": {"path": "blocker/queue.db"}}), encoding="utf-8")
    run = subprocess.run(
        [sys.executable, str(ROOT / "app.py"), "--config", str(config)],
        cwd=tmp_path, capture_output=True, text=True, timeout=60,
    )
    assert run.returncode != 0 and "task store could not be opened" in run.stderr
