"""Resolve cube.js dateRange values to concrete ``(start, end)`` datetimes.

Accepts: ``[startISO, endISO]``, a single ISO date, or relative keywords
(``today``, ``yesterday``, ``this week|month|quarter|year``,
``last week|month|quarter|year``, ``last N days|weeks|months|quarters|years``).
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

_REL_RE = re.compile(r"^last\s+(\d+)\s+(day|week|month|quarter|year)s?$")


def _tz(tz: str | None) -> ZoneInfo | timezone:
    return ZoneInfo(tz) if tz else UTC


def _parse_iso(value: str, tzinfo: ZoneInfo | timezone) -> datetime:
    # date-only -> midnight; datetime -> as-is. Attach tz if naive.
    txt = value.strip()
    try:
        dt = datetime.fromisoformat(txt)
    except ValueError:
        dt = datetime.strptime(txt, "%Y-%m-%d")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tzinfo)
    return dt


def _day_bounds(day: datetime) -> tuple[datetime, datetime]:
    start = day.replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start.replace(hour=23, minute=59, second=59, microsecond=999999)


def _week_start(dt: datetime) -> datetime:
    # Monday as the first day of the week (ISO).
    start = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    return start - timedelta(days=start.weekday())


def _month_start(dt: datetime) -> datetime:
    return dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _quarter_start(dt: datetime) -> datetime:
    q = (dt.month - 1) // 3
    return dt.replace(month=q * 3 + 1, day=1, hour=0, minute=0, second=0, microsecond=0)


def _year_start(dt: datetime) -> datetime:
    return dt.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)


def _add_months(dt: datetime, n: int) -> datetime:
    idx = dt.month - 1 + n
    y, m = dt.year + idx // 12, idx % 12 + 1
    return dt.replace(year=y, month=m)


def resolve_date_range(
    value: object,
    *,
    now: datetime | None = None,
    tz: str | None = None,
) -> tuple[datetime, datetime]:
    tzinfo = _tz(tz)
    now = now or datetime.now(tzinfo)

    if isinstance(value, (list, tuple)) and len(value) == 2:
        start = _parse_iso(str(value[0]), tzinfo)
        end = _parse_iso(str(value[1]), tzinfo)
        if isinstance(value[0], str) and "T" not in str(value[0]):
            start = start.replace(hour=0, minute=0, second=0, microsecond=0)
        if isinstance(value[1], str) and "T" not in str(value[1]):
            end = end.replace(hour=23, minute=59, second=59, microsecond=999999)
        return start, end

    if isinstance(value, datetime):
        return _day_bounds(value)

    if isinstance(value, date):
        return _day_bounds(datetime.combine(value, datetime.min.time(), tzinfo=tzinfo))

    if not isinstance(value, str):
        raise ValueError(f"Unsupported dateRange: {value!r}")

    key = value.strip().lower()
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)

    if key == "today":
        return _day_bounds(now)
    if key == "yesterday":
        return _day_bounds(now - timedelta(days=1))

    m = _REL_RE.match(key)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        if unit == "day":
            start = today - timedelta(days=n)
            return start, _day_bounds(now)[1]
        if unit == "week":
            start = _week_start(now) - timedelta(weeks=n)
            return start, _week_bounds_end(now)
        if unit == "month":
            start = _add_months(_month_start(now), -n)
            return start, _month_end(now)
        if unit == "quarter":
            start = _add_months(_quarter_start(now), -n * 3)
            return start, _quarter_end(now)
        if unit == "year":
            ys = _year_start(now)
            return ys.replace(year=ys.year - n), _year_end(now)

    if key == "this week":
        return _week_start(now), _week_bounds_end(now)
    if key == "this month":
        return _month_start(now), _month_end(now)
    if key == "this quarter":
        return _quarter_start(now), _quarter_end(now)
    if key == "this year":
        return _year_start(now), _year_end(now)
    if key == "last week":
        s = _week_start(now) - timedelta(weeks=1)
        return s, s + timedelta(weeks=1) - timedelta(microseconds=1)
    if key == "last month":
        s = _add_months(_month_start(now), -1)
        return s, _add_months(s, 1) - timedelta(microseconds=1)
    if key == "last quarter":
        s = _add_months(_quarter_start(now), -3)
        return s, _add_months(s, 3) - timedelta(microseconds=1)
    if key == "last year":
        s = _year_start(now).replace(year=_year_start(now).year - 1)
        return s, _add_months(s, 12) - timedelta(microseconds=1)

    # Fallback: treat as an ISO date/datetime.
    dt = _parse_iso(value, tzinfo)
    return _day_bounds(dt)


def _week_bounds_end(now: datetime) -> datetime:
    return _week_start(now) + timedelta(weeks=1) - timedelta(microseconds=1)


def _month_end(now: datetime) -> datetime:
    return _add_months(_month_start(now), 1) - timedelta(microseconds=1)


def _quarter_end(now: datetime) -> datetime:
    return _add_months(_quarter_start(now), 3) - timedelta(microseconds=1)


def _year_end(now: datetime) -> datetime:
    return _year_start(now).replace(year=_year_start(now).year + 1) - timedelta(microseconds=1)
