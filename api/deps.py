"""Dependencies shared by the API routes."""
from __future__ import annotations

import secrets

from fastapi import Header, Request

from services import Services, ServiceError


class Unauthorized(ServiceError):
    status_code = 401


def get_services(request: Request) -> Services:
    return request.app.state.services


def require_token(request: Request, x_api_token: str | None = Header(default=None)) -> None:
    """When an API token is configured every caller has to send it as ``X-API-Token``."""
    expected = get_services(request).api_token
    # (bytes: ``compare_digest`` refuses str arguments with non-ASCII characters)
    if expected and not secrets.compare_digest((x_api_token or "").encode("utf-8"), expected.encode("utf-8")):
        raise Unauthorized("missing or wrong X-API-Token")
