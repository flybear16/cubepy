"""Assemble the cube.js-style response envelope and member annotations."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from cubepy.schema.registry import resolve_member
from cubepy.security.masking import mask_rows
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
    ctx: Any = None,
    now: datetime | None = None,
    used_pre_aggregations: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    ts = (now or datetime.now(UTC)).isoformat()
    # Column masking: VALUES are rewritten per-caller before the envelope
    # leaves the engine — every surface (REST/WS/GraphQL/ask) inherits it.
    if ctx is not None:
        rows = mask_rows(query, rows, ctx)
    return {
        "data": rows,
        "annotation": build_annotation(query),
        "lastRefreshTime": ts,
        "usedPreAggregations": used_pre_aggregations or [],
        "refreshKeyMatches": True,
    }
