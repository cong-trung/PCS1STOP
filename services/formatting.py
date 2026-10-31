"""How times and durations are shown in the UI."""
from __future__ import annotations

from datetime import datetime, timedelta


def format_duration(delta: timedelta | None) -> str:
    """``12 sec``, ``5 min``, ``1 hr 5 min`` (``-`` when there is no duration)."""
    if delta is None:
        return "-"
    seconds = max(0, int(delta.total_seconds()))
    if seconds < 60:
        return f"{seconds} sec"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} min"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} hr {minutes} min" if minutes else f"{hours} hr"


def local_time(value: datetime | None) -> str:
    """A UTC timestamp as local ``YYYY-MM-DD HH:MM:SS`` (``-`` when empty)."""
    return "-" if value is None else value.astimezone().strftime("%Y-%m-%d %H:%M:%S")
