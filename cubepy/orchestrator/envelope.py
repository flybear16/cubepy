"""Assemble the cube.js-style response envelope and member annotations."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
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


def _numeric_wire(value: Any) -> Any:
    """数值 measure 的线上形态：字符串（对齐 Cube.js 驱动透传 PG 数值的文本形态，
    避免浮点精度丢失）。Decimal/int → str；bool 是 int 子类需先排除；float 保留
    （产线 numeric 列走 Decimal 到不了这里，float 只出现在 DuckDB/测试桩）。"""
    if value is None or isinstance(value, (bool, str, float)):
        return value
    if isinstance(value, Decimal):
        # format 'f'：定点展开，避免 0E-20 这类科学计数法（Cube.js 是全位定点文本）
        return format(value, "f")
    if isinstance(value, int):
        return str(value)
    return value


def _time_wire(value: Any) -> Any:
    """时间维度值的线上形态：'%Y-%m-%dT%H:%M:%S.mmm'（UTC，无时区后缀）。
    对齐 Cube.js（2026-09-05 契约探针：原始时间戳与粒度桶同走此格式）。"""
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, date):
        dt = datetime(value.year, value.month, value.day)
    elif isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value)
        except ValueError:
            return value
    else:
        return value
    if dt.tzinfo is not None:
        dt = dt.astimezone(UTC)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}"


def _present_rows(query: Query, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rows → cube.js wire format（#5/#6，双引擎对拍发现）：

    - 数值 measure 字符串化（见 _numeric_wire）
    - 时间维度值格式化（含时间类型成员当普通维度用的场景——Cube.js 同样格式化）；
      带粒度时按 Cube.js 回双键：``dim``（桶值）与 ``dim.<granularity>``（同桶值）
      ——executor 已按粒度分组，两键同源
    - 维度/布尔/字符串值不动
    """
    from cubepy.schema.meta import DimensionType

    measure_keys = set(query.measures)
    td_map = {td.dimension: td.granularity for td in query.timeDimensions}
    time_dim_keys = {
        path
        for path in query.dimensions
        if path not in td_map and resolve_member(path)[2].type == DimensionType.TIME
    }
    if not measure_keys and not td_map and not time_dim_keys:
        return rows
    out: list[dict[str, Any]] = []
    for row in rows:
        new_row: dict[str, Any] = {}
        for key, value in row.items():
            if key in measure_keys:
                new_row[key] = _numeric_wire(value)
            elif key in td_map:
                bucket = _time_wire(value)
                new_row[key] = bucket
                granularity = td_map[key]
                if granularity:
                    new_row[f"{key}.{granularity}"] = bucket
            elif key in time_dim_keys:
                new_row[key] = _time_wire(value)
            else:
                new_row[key] = value
        out.append(new_row)
    return out


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
    rows = _present_rows(query, rows)
    return {
        "data": rows,
        "annotation": build_annotation(query),
        "lastRefreshTime": ts,
        "usedPreAggregations": used_pre_aggregations or [],
        "refreshKeyMatches": True,
    }
