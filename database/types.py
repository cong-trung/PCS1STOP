"""Column types shared by the models."""
from __future__ import annotations

import enum
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import DateTime, Text
from sqlalchemy.types import TypeDecorator


class UTCDateTime(TypeDecorator):
    """A ``DATETIME`` that is always UTC.

    SQLite keeps it as a naive ``YYYY-MM-DD HH:MM:SS.ffffff`` string (which sorts and compares correctly as text),
    Python always sees timezone-aware UTC values.  Naive datetimes are refused on the way in: silently mixing local
    time and UTC is the classic way to break timeouts.
    """

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetime: pass a timezone-aware value (UTC)")
        return value.astimezone(timezone.utc).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect: Any) -> datetime | None:
        return None if value is None else value.replace(tzinfo=timezone.utc)


class EnumText(TypeDecorator):
    """A Python ``Enum`` kept as its value in a plain ``TEXT`` column (what the schema in architect.md asks for).

    Plain strings are accepted on the way in as long as they are valid members, so filters such as
    ``Task.status == "PENDING"`` keep working.
    """

    impl = Text
    cache_ok = True

    def __init__(self, enum_class: type[enum.Enum]) -> None:
        super().__init__()
        self.enum_class = enum_class

    def process_bind_param(self, value: Any, dialect: Any) -> str | None:
        if value is None:
            return None
        return self.enum_class(value).value

    def process_result_value(self, value: str | None, dialect: Any) -> enum.Enum | None:
        return None if value is None else self.enum_class(value)
