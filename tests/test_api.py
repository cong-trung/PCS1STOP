from __future__ import annotations

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from api import install_api
from database import TaskStatus
from services import Services

PATH = r"\\share\pcs_die_files\SST_Die.xml"


@pytest.fixture
def client(services: Services) -> TestClient:
    app = FastAPI()
    install_api(app, services)
    return TestClient(app, raise_server_exceptions=True)


def test_create_next_start_complete_as_in_the_spec(client, services):
    created = client.post(
        "/api/task/create", json={"task_type": "check_path", "payload": {"path": PATH, "product": "HST"}}
    )
    assert created.status_code == 200
    body = created.json()
    assert body["success"] is True and isinstance(body["task_id"], int)
    task_id = body["task_id"]

    assert client.get("/api/task/next", params={"worker": "PC2"}).json() == {
        "task_id": task_id,
        "task_type": "check_path",
        "payload": {"path": PATH, "product": "HST"},
        "delivery": 0,  # (an addition to architect.md's answer: how often this task was handed out before)
    }
    assert services.tasks.get_task(task_id).status is TaskStatus.ASSIGNED

    assert client.post("/api/task/start", json={"task_id": task_id, "worker": "PC2"}).json() == {"success": True}
    assert services.tasks.get_task(task_id).status is TaskStatus.RUNNING

    done = client.post(
        "/api/task/complete", json={"task_id": task_id, "worker": "PC2", "result": {"exists": True, "size": 12}}
    )
    assert done.json() == {"success": True}
    task = services.tasks.get_task(task_id)
    assert task.status is TaskStatus.COMPLETED and task.result == {"exists": True, "size": 12}


def test_fail_endpoint(client, services):
    task_id = client.post("/api/task/create", json={"task_type": "check_path", "payload": {}}).json()["task_id"]
    client.get("/api/task/next", params={"worker": "PC2"})
    client.post("/api/task/start", json={"task_id": task_id, "worker": "PC2"})
    assert client.post("/api/task/fail", json={"task_id": task_id, "worker": "PC2", "error": "no access"}).json() == {
        "success": True
    }
    task = services.tasks.get_task(task_id)
    assert task.status is TaskStatus.FAILED and task.error_message == "no access"


def test_next_when_idle(client):
    assert client.get("/api/task/next", params={"worker": "PC2"}).json() == {"task": None}


def test_next_is_also_a_heartbeat(client, services):
    client.get("/api/task/next", params={"worker": "PC2"})
    assert [w.hostname for w in services.workers.list_workers()] == ["PC2"]


def test_heartbeat_records_ip_and_version(client, services):
    reply = client.post("/api/worker/heartbeat", json={"hostname": "PC2", "ip": "10.1.2.3", "version": "1.0.0"})
    assert reply.json() == {"success": True}
    (worker,) = services.workers.list_workers()
    assert (worker.ip_address, worker.version) == ("10.1.2.3", "1.0.0")


def test_heartbeat_falls_back_to_the_callers_address(client, services):
    client.post("/api/worker/heartbeat", json={"hostname": "PC2"})
    (worker,) = services.workers.list_workers()
    assert worker.ip_address == "testclient"  # TestClient's fake peer; a real call carries the socket address


def test_get_task_shows_where_it_stands(client):
    task_id = client.post(
        "/api/task/create", json={"task_type": "check_path", "payload": {"path": PATH, "product": "SST"}}
    ).json()["task_id"]
    shown = client.get(f"/api/task/{task_id}").json()
    assert shown["status"] == "PENDING" and shown["payload"] == {"path": PATH, "product": "SST"}
    assert shown["assigned_worker"] is None and shown["result"] is None and shown["retry_count"] == 0


def test_a_task_that_is_handed_out_again_can_only_be_started_with_the_newest_delivery(client, services):
    task_id = client.post("/api/task/create", json={"task_type": "check_path", "payload": {}}).json()["task_id"]
    first = client.get("/api/task/next", params={"worker": "PC2"}).json()
    second = client.get("/api/task/next", params={"worker": "PC2"}).json()  # same name: the answer got lost / 2nd agent
    assert (first["task_id"], first["delivery"]) == (task_id, 0) and (second["task_id"], second["delivery"]) == (task_id, 1)

    stale = client.post("/api/task/start", json={"task_id": task_id, "worker": "PC2", "delivery": 0})
    assert stale.status_code == 409 and "handed out again" in stale.json()["error"]
    assert services.tasks.get_task(task_id).status is TaskStatus.ASSIGNED

    assert client.post("/api/task/start", json={"task_id": task_id, "worker": "PC2", "delivery": 1}).json() == {"success": True}
    assert client.get(f"/api/task/{task_id}").json()["retry_count"] == 1


def test_a_negative_delivery_is_refused(client):
    reply = client.post("/api/task/start", json={"task_id": 1, "worker": "PC2", "delivery": -1})
    assert reply.status_code == 422 and reply.json()["success"] is False


@pytest.mark.parametrize(
    ("method", "url", "body", "status"),
    [
        ("post", "/api/task/create", {"task_type": "", "payload": {}}, 422),
        ("post", "/api/task/create", {"payload": {}}, 422),
        ("post", "/api/task/create", {"task_type": "x", "payload": [1]}, 422),
        ("post", "/api/task/start", {"task_id": 0, "worker": "PC2"}, 422),
        ("post", "/api/task/start", {"task_id": 1}, 422),
        ("post", "/api/task/start", {"task_id": 999, "worker": "PC2"}, 404),
        ("post", "/api/task/complete", {"task_id": 999, "worker": "PC2", "result": {}}, 404),
        ("post", "/api/task/fail", {"task_id": 999, "worker": "PC2", "error": "x"}, 404),
        ("post", "/api/worker/heartbeat", {"hostname": ""}, 422),
        ("get", "/api/task/999", None, 404),
        ("get", "/api/task/next", None, 422),
        ("get", "/api/task/next?worker=", None, 422),
    ],
)
def test_errors_come_back_as_json(client, method, url, body, status):
    reply = client.request(method, url, json=body) if body is not None else client.request(method, url)
    assert reply.status_code == status
    data = reply.json()
    assert data["success"] is False and data["error"]


def test_non_finite_numbers_are_refused(client):
    reply = client.post(
        "/api/task/create",
        content='{"task_type": "check_path", "payload": {"size": NaN}}',
        headers={"Content-Type": "application/json"},
    )
    assert reply.status_code == 422 and reply.json()["success"] is False


def test_out_of_order_step_is_a_conflict(client):
    task_id = client.post("/api/task/create", json={"task_type": "check_path", "payload": {}}).json()["task_id"]
    client.get("/api/task/next", params={"worker": "PC2"})
    reply = client.post("/api/task/complete", json={"task_id": task_id, "worker": "PC2", "result": {}})
    assert reply.status_code == 409 and reply.json()["success"] is False


def test_another_workers_task_is_a_conflict(client):
    task_id = client.post("/api/task/create", json={"task_type": "check_path", "payload": {}}).json()["task_id"]
    client.get("/api/task/next", params={"worker": "PC2"})
    reply = client.post("/api/task/start", json={"task_id": task_id, "worker": "PC3"})
    assert reply.status_code == 409 and "PC2" in reply.json()["error"]


def test_health_is_open(client):
    assert client.get("/api/health").json()["status"] == "ok"


class TestBodyLimit:
    """Oversized bodies are refused from the headers alone, before FastAPI would read them (and before any token check)."""

    def test_a_body_over_the_limit_is_refused(self, client, services):
        big = '{"task_type": "check_path", "payload": {"x": "' + "a" * (1024 * 1024) + '"}}'
        reply = client.post("/api/task/create", content=big, headers={"Content-Type": "application/json"})
        assert reply.status_code == 413 and reply.json()["success"] is False and "1024 KB" in reply.json()["error"]
        assert services.tasks.list_tasks() == []

    def test_the_largest_task_that_is_allowed_still_goes_through(self, client):
        payload = {"data": "a" * (250 * 1024)}  # (the service allows 256 KB of payload)
        reply = client.post("/api/task/create", json={"task_type": "check_path", "payload": payload})
        assert reply.status_code == 200 and reply.json()["success"] is True

    def test_a_payload_over_256_kb_is_still_refused_by_the_service(self, client):
        reply = client.post("/api/task/create", json={"task_type": "check_path", "payload": {"data": "a" * (300 * 1024)}})
        assert reply.status_code == 422 and "256 KB" in reply.json()["error"]

    def test_chunked_bodies_are_refused(self, client):
        def chunks():
            yield b'{"task_type": "check_path",'
            yield b' "payload": {}}'

        reply = client.post("/api/task/create", content=chunks(), headers={"Content-Type": "application/json"})
        assert reply.status_code == 411 and reply.json()["success"] is False

    def test_a_bad_content_length_is_refused(self, client):
        reply = client.post(
            "/api/task/create", content=b"{}", headers={"Content-Type": "application/json", "Content-Length": "-5"}
        )
        assert reply.status_code == 400

    def test_the_limit_comes_before_the_token_check(self, services):
        services.api_token = "s3cret"
        app = FastAPI()
        install_api(app, services)
        client = TestClient(app)
        reply = client.post("/api/task/create", content=b"x" * (1024 * 1024 + 1))
        assert reply.status_code == 413  # no token was needed to be told off

    def test_other_paths_are_not_limited(self, services):
        app = FastAPI()

        @app.post("/upload")
        async def upload(request: Request) -> dict:
            return {"size": len(await request.body())}

        install_api(app, services)
        reply = TestClient(app).post("/upload", content=b"x" * (3 * 1024 * 1024))
        assert reply.status_code == 200 and reply.json() == {"size": 3 * 1024 * 1024}


class TestToken:
    @pytest.fixture
    def secured(self, services) -> TestClient:
        services.api_token = "s3cret"
        app = FastAPI()
        install_api(app, services)
        return TestClient(app)

    def test_calls_without_the_token_are_refused(self, secured):
        for method, url in (("get", "/api/task/next?worker=PC2"), ("get", "/api/task/1")):
            reply = secured.request(method, url)
            assert reply.status_code == 401 and reply.json()["success"] is False
        assert secured.post("/api/worker/heartbeat", json={"hostname": "PC2"}).status_code == 401
        assert secured.post("/api/task/create", json={"task_type": "x", "payload": {}}).status_code == 401

    def test_wrong_token(self, secured):
        reply = secured.get("/api/task/next?worker=PC2", headers={"X-API-Token": "nope"})
        assert reply.status_code == 401

    def test_right_token(self, secured):
        reply = secured.get("/api/task/next?worker=PC2", headers={"X-API-Token": "s3cret"})
        assert reply.status_code == 200 and reply.json() == {"task": None}

    def test_health_stays_open(self, secured):
        assert secured.get("/api/health").status_code == 200

    def test_a_non_ascii_token_gives_401_not_a_crash(self, services):
        services.api_token = "tøken"  # (config.py refuses such tokens; the comparison must not blow up if one gets here)
        app = FastAPI()
        install_api(app, services)
        client = TestClient(app)
        assert client.get("/api/task/next?worker=PC2").status_code == 401
        assert client.get("/api/task/next?worker=PC2", headers={"X-API-Token": "token"}).status_code == 401
