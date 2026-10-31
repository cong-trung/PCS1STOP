"""Shared fixtures: a throw-away SQLite file, a clock the tests move by hand, the wired-up services."""
from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta, timezone

import pytest

from database import Database, init_db
from services import Services


class FakeClock:
    """``clock()`` returns "now"; ``advance(...)`` moves it."""

    def __init__(self) -> None:
        self.now = datetime(2025, 1, 6, 8, 0, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: float) -> None:
        self.now += timedelta(**kwargs)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def db(tmp_path) -> Iterator[Database]:
    database = Database(tmp_path / "queue.db")
    init_db(database)
    yield database
    database.dispose()


@pytest.fixture
def services(db: Database, clock: FakeClock) -> Services:
    return Services.create(db, clock=clock)
