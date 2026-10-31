"""Errors the services raise; the API turns them into HTTP responses via ``status_code``."""
from __future__ import annotations


class ServiceError(Exception):
    status_code = 400


class InvalidRequest(ServiceError):
    """The request itself is wrong (empty hostname, payload that is not JSON, ...)."""

    status_code = 422


class TaskNotFound(ServiceError):
    status_code = 404


class Conflict(ServiceError):
    """The request is fine but does not fit the current state of the task."""

    status_code = 409


class InvalidTransition(Conflict):
    """The task is not in a state the requested step may start from (e.g. completing a TIMEOUT task)."""


class WorkerMismatch(Conflict):
    """The task belongs to another worker."""
