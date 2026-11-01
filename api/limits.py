"""Request-size guard of the JSON API.

FastAPI reads a request body completely (and parses it) before the route - and its token check - runs, so without this
anybody who can reach the port could make PC1 buffer an arbitrarily large body.  The limit is decided from the headers
alone, before a single body byte is read.
"""
from __future__ import annotations

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

#: a task's payload and a result are each limited to 256 KB (services.task_service); this leaves room for the envelope
MAX_BODY_BYTES = 1024 * 1024


class ApiBodyLimit:
    """ASGI middleware for the requests below ``prefix``: a body larger than ``max_bytes`` is refused with 413, a
    body without a declared length (``Transfer-Encoding: chunked``) with 411.  Everything else - the web pages and
    their pcs_die_file uploads included - passes untouched."""

    def __init__(self, app: ASGIApp, max_bytes: int = MAX_BODY_BYTES, prefix: str = "/api/") -> None:
        self.app = app
        self.max_bytes = max_bytes
        self.prefix = prefix

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope["path"].startswith(self.prefix):
            await self.app(scope, receive, send)
            return
        headers = {name.lower(): value for name, value in scope["headers"]}
        if b"chunked" in headers.get(b"transfer-encoding", b"").lower():
            await self._refuse(scope, receive, send, 411, "send the request with a Content-Length (chunked is not accepted)")
            return
        raw = headers.get(b"content-length")
        if raw is not None:
            try:
                length = int(raw)
            except ValueError:
                length = -1
            if length < 0:
                await self._refuse(scope, receive, send, 400, "invalid Content-Length")
                return
            if length > self.max_bytes:
                await self._refuse(scope, receive, send, 413, f"request body is larger than {self.max_bytes // 1024} KB")
                return
        await self.app(scope, receive, send)

    @staticmethod
    async def _refuse(scope: Scope, receive: Receive, send: Send, status: int, message: str) -> None:
        # "Connection: close": the unread body must not be mistaken for the start of the next request
        response = JSONResponse({"success": False, "error": message}, status_code=status, headers={"Connection": "close"})
        await response(scope, receive, send)
