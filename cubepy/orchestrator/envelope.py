"""Assemble the cube.js-style response envelope and member annotations."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from cubepy.schema.registry import resolve_member
from cubepy.sqlgen.query import Query


def build_annotation(query: Query) -> dict[str, dict[str, Any]]:
    annotation: dict[str, dict[str, Any]] = {
        "measures": {},
        "dimensions": {},
        "timeDimensions": {},
    }
    for path in query.measures:
        _cube, _kind, member = resolve_member(path)
        annotation["measures"][path] = {"type": str(member.type)}
    for path in query.dimensions:
        _cube, _kind, member = resolve_member(path)
        annotation["dimensions"][path] = {"type": str(member.type)}
    for td in query.timeDimensions:
        _cube, _kind, _member = resolve_member(td.dimension)
        annotation["timeDimensions"][td.dimension] = {
            "type": "time",
            "granularity": td.granularity,
        }
    return annotation


def build_envelope(
    query: Query,
    rows: list[dict[str, Any]],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    ts = (now or datetime.now(UTC)).isoformat()
    return {
        "data": rows,
        "annotation": build_annotation(query),
        "lastRefreshTime": ts,
        "usedPreAggregations": [],
        "refreshKeyMatches": True,
    }
