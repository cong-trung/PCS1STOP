"""Worker endpoints."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request

from services import Services

from .deps import get_services, require_token
from .schemas import HeartbeatRequest

router = APIRouter(prefix="/api/worker", tags=["workers"], dependencies=[Depends(require_token)])


@router.post("/heartbeat")
def heartbeat(body: HeartbeatRequest, request: Request, services: Services = Depends(get_services)) -> dict[str, Any]:
    """Register a worker or refresh it: ``last_seen`` = now, status ONLINE."""
    ip = body.ip or (request.client.host if request.client else None)
    services.workers.heartbeat(body.hostname, ip, body.version)
    return {"success": True}
