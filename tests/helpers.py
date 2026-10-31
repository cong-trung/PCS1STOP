"""Test rig shared by the test modules: the PC1 API on a real port, a free port, a polling wait."""
from __future__ import annotations

import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import uvicorn
from fastapi import FastAPI

from api import install_api
from services import Services


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def wait_for(predicate, timeout: float = 10.0, what: str = "the condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {what}")


class LiveServer:
    """The PC1 API on a real port (what the worker's urllib talks to), startable again on the same port."""

    def __init__(self, services: Services, port: int | None = None) -> None:
        self.port = port or free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.app = FastAPI()
        install_api(self.app, services)
        self.server: uvicorn.Server | None = None
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        config = uvicorn.Config(self.app, host="127.0.0.1", port=self.port, log_level="warning", lifespan="off")
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        wait_for(lambda: self.server.started, what="the test server to start")

    def stop(self) -> None:
        if self.server is not None:
            self.server.should_exit = True
            self.thread.join(10)
            self.server = None


class ScriptedServer:
    """A throw-away HTTP server for what the real API never does (redirects, odd answers): every path is answered from
    ``answers`` (path -> (status, headers, body); anything else is a 404) and every request is recorded in ``requests``
    as (method, path, lower-cased headers).  Use it in a ``with`` block."""

    def __init__(self, answers: dict[str, tuple[int, dict[str, str], bytes]]) -> None:
        self.answers = answers
        self.requests: list[tuple[str, str, dict[str, str]]] = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def answer(self) -> None:
                owner.requests.append((self.command, self.path, {k.lower(): v for k, v in self.headers.items()}))
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                status, headers, body = owner.answers.get(self.path.split("?")[0], (404, {}, b""))
                self.send_response(status)
                for key, value in headers.items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_GET = do_POST = answer

            def log_message(self, *args) -> None:  # (keep the test output clean)
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self) -> ScriptedServer:
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(10)
