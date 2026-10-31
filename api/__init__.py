"""The HTTP API PC2 workers talk to.  ``install_api`` plugs it into any FastAPI app (NiceGUI's own ``app`` included)."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, FastAPI, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from services import Services, ServiceError

from .limits import ApiBodyLimit
from .task_api import router as task_router
from .worker_api import router as worker_router

health_router = APIRouter(tags=["health"])


@health_router.get("/api/health")
def health() -> dict[str, str]:
    """Is PC1 up?  (workers use it to tell "server down" from "my request was wrong")"""
    return {"status": "ok", "time": datetime.now(timezone.utc).isoformat()}


async def _service_error(_request: Request, exc: ServiceError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content={"success": False, "error": str(exc)})


async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    if not request.url.path.startswith("/api/"):
        return await request_validation_exception_handler(request, exc)  # not ours: FastAPI's usual answer
    problems = "; ".join(
        f"{'.'.join(str(part) for part in error['loc'] if part != 'body')}: {error['msg']}" for error in exc.errors()
    )
    return JSONResponse(status_code=422, content={"success": False, "error": problems or "invalid request"})


def install_api(app: FastAPI, services: Services) -> None:
    """Make ``services`` available to the routes and register routes + error handlers on ``app``."""
    app.state.services = services
    app.add_middleware(ApiBodyLimit)
    app.include_router(health_router)
    app.include_router(task_router)
    app.include_router(worker_router)
    app.add_exception_handler(ServiceError, _service_error)
    app.add_exception_handler(RequestValidationError, _validation_error)
