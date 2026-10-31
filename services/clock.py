"""Time source shared by the services (replaceable in tests)."""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone

Clock = Callable[[], datetime]


def utcnow() -> datetime:
    """The current time as a timezone-aware UTC datetime."""
    return datetime.now(timezone.utc)
