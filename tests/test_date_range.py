"""Coverage of date_range keyword/branch resolution (G-coverage)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from cubepy.sqlgen.date_range import resolve_date_range

NOW = datetime(2026, 8, 7, 12, 0, 0, tzinfo=UTC)


def _d(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


# (label, value, expected_start)
SIMPLE = [
    ("today", "today", _d(2026, 8, 7)),
    ("yesterday", "yesterday", _d(2026, 8, 6)),
    ("last 7 days", "last 7 days", _d(2026, 7, 31)),
    ("last 1 day", "last 1 day", _d(2026, 8, 6)),
    ("last 2 weeks", "last 2 weeks", _d(2026, 7, 20)),  # Monday of this week - 2 weeks
    ("last 1 month", "last 1 month", _d(2026, 7, 1)),
    ("last 3 months", "last 3 months", _d(2026, 5, 1)),
    ("last 1 quarter", "last 1 quarter", _d(2026, 4, 1)),
    ("last 1 year", "last 1 year", _d(2025, 1, 1)),
    ("this week", "this week", _d(2026, 8, 3)),  # Monday 2026-08-03
    ("this month", "this month", _d(2026, 8, 1)),
    ("this quarter", "this quarter", _d(2026, 7, 1)),
    ("this year", "this year", _d(2026, 1, 1)),
    ("last week", "last week", _d(2026, 7, 27)),
    ("last month", "last month", _d(2026, 7, 1)),
    ("last quarter", "last quarter", _d(2026, 4, 1)),
    ("last year", "last year", _d(2025, 1, 1)),
    ("iso date", "2026-08-01", _d(2026, 8, 1)),
]


@pytest.mark.parametrize("label,value,expected_start", SIMPLE, ids=[s[0] for s in SIMPLE])
def test_resolve_keyword_start(label: str, value: str, expected_start: datetime) -> None:
    start, end = resolve_date_range(value, now=NOW, tz="UTC")
    assert start == expected_start, f"{label}: start {start} != {expected_start}"
    assert start <= end
    assert end >= NOW - timedelta(days=366 * 2)  # sanity: end isn't absurd


def test_two_element_iso_list() -> None:
    start, end = resolve_date_range(["2026-08-01", "2026-08-03"], now=NOW, tz="UTC")
    assert start == _d(2026, 8, 1)
    assert end == _d(2026, 8, 3).replace(hour=23, minute=59, second=59, microsecond=999999)


def test_two_element_datetime_list() -> None:
    s = _d(2026, 8, 1, 6, 0)
    e = _d(2026, 8, 2, 18, 0)
    start, end = resolve_date_range([s.isoformat(), e.isoformat()], now=NOW, tz="UTC")
    assert start == s
    assert end == e


def test_datetime_input() -> None:
    start, end = resolve_date_range(_d(2026, 8, 5, 10, 0), now=NOW, tz="UTC")
    assert start == _d(2026, 8, 5)
    assert end.hour == 23 and end.minute == 59


def test_date_input() -> None:
    start, end = resolve_date_range(date(2026, 8, 5), now=NOW, tz="UTC")
    assert start == _d(2026, 8, 5)


def test_unsupported_value_raises() -> None:
    with pytest.raises(ValueError):
        resolve_date_range(12345, now=NOW, tz="UTC")  # type: ignore[arg-type]


def test_default_now_is_timezone_aware() -> None:
    # When `now` is omitted, the fallback datetime.now(tz) is tz-aware.
    start, _ = resolve_date_range("today", tz="UTC")
    assert start.tzinfo is not None
