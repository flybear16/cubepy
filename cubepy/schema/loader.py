"""Public schema DSL: ``measure`` / ``dimension`` / ``segment`` helpers, the
``@cube`` decorator, and a YAML loader.

Decorator usage (full power — supports ``shown`` / ``check_permission`` callbacks)::

    @cube("Orders", "SELECT * FROM orders",
          joins={"Users": {"relationship": "belongsTo",
                           "sql": "Orders.user_id = Users.id"}},
          security_context={"check_permission": _orders_rls})
    class OrdersCube:
        revenue = measure("amount", MeasureType.SUM,
                          shown=lambda ctx: ctx.role == "admin")
        count   = measure(None, MeasureType.COUNT)
        status  = dimension("status", "string")
        created_at = dimension("created_at", "time")

YAML usage (declarative — no callbacks; for simple cubes)::

    cubes:
      - name: Orders
        sql: SELECT * FROM orders
        measures: [{name: revenue, sql: amount, type: sum}, ...]
        dimensions: [{name: status, sql: status, type: string}, ...]
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import yaml

from cubepy.schema.meta import (
    CubeMeta,
    Dimension,
    DimensionType,
    Join,
    Measure,
    MeasureType,
    RelationshipType,
    Segment,
)
from cubepy.schema.registry import registry

__all__ = [
    "cube",
    "dimension",
    "measure",
    "segment",
    "load_cube_file",
    "load_cube_dir",
]


def measure(
    sql: str | None,
    mtype: MeasureType | str,
    *,
    shown: Any = None,
    title: str | None = None,
    description: str | None = None,
    filters: tuple[dict[str, Any], ...] = (),
    formula: str | None = None,
    format: str | None = None,
    drill_members: tuple[str, ...] = (),
) -> Measure:
    """Define a measure. ``name`` is filled in from the class attribute by ``@cube``.

    For a calculated measure pass ``mtype=MeasureType.CALCULATED`` and ``formula``
    (``sql`` may be None); the formula references sibling measures as ``{name}``.
    """
    mtype_v = mtype if isinstance(mtype, MeasureType) else MeasureType(mtype)
    return Measure(
        name="",
        sql=sql,
        type=mtype_v,
        shown=shown,
        title=title,
        description=description,
        filters=filters,
        formula=formula,
        format=format,
        drill_members=drill_members,
    )


def dimension(
    sql: str,
    dtype: DimensionType | str = DimensionType.STRING,
    *,
    shown: Any = None,
    primary_key: bool = False,
    title: str | None = None,
    description: str | None = None,
    format: str | None = None,
    drill_members: tuple[str, ...] = (),
) -> Dimension:
    dtype_v = dtype if isinstance(dtype, DimensionType) else DimensionType(dtype)
    return Dimension(
        name="",
        sql=sql,
        type=dtype_v,
        shown=shown,
        primary_key=primary_key,
        title=title,
        description=description,
        format=format,
        drill_members=drill_members,
    )


def segment(sql: str, *, shown: Any = None) -> Segment:
    return Segment(name="", sql=sql, shown=shown)


def _resolve_join(target: str, spec: Any) -> Join:
    if isinstance(spec, Join):
        return spec
    if isinstance(spec, dict):
        return Join(
            relationship=RelationshipType(spec["relationship"]),
            sql=spec["sql"],
        )
    raise TypeError(f"join {target!r} must be a dict or Join, got {type(spec).__name__}")


def cube(
    name: str,
    sql: str,
    *,
    joins: dict[str, Any] | None = None,
    security_context: Any = None,
    refresh_key: dict[str, Any] | None = None,
    shown: Any = None,
    title: str | None = None,
    description: str | None = None,
) -> Any:
    """Class decorator: extract Measure/Dimension/Segment attributes and register."""

    def decorator(cls: type) -> type:
        measures: list[Measure] = []
        dimensions: list[Dimension] = []
        segments: list[Segment] = []
        for key, val in cls.__dict__.items():
            if isinstance(val, Measure):
                measures.append(replace(val, name=key))
            elif isinstance(val, Dimension):
                dimensions.append(replace(val, name=key))
            elif isinstance(val, Segment):
                segments.append(replace(val, name=key))
        sec_ctx = None
        if security_context is not None:
            sec_ctx = (
                security_context.get("check_permission")
                if isinstance(security_context, dict)
                else security_context
            )
        meta = CubeMeta(
            name=name,
            sql=sql,
            measures=tuple(measures),
            dimensions=tuple(dimensions),
            segments=tuple(segments),
            joins={t: _resolve_join(t, s) for t, s in (joins or {}).items()},
            security_context=sec_ctx,
            refresh_key=refresh_key,
            shown=shown,
            title=title,
            description=description,
        )
        registry.register(meta)
        return cls

    return decorator


def _cube_from_dict(d: dict[str, Any]) -> CubeMeta:
    measures = tuple(
        Measure(
            name=m["name"],
            sql=m.get("sql"),
            type=MeasureType(m["type"]),
            title=m.get("title"),
            description=m.get("description"),
            format=m.get("format"),
            drill_members=tuple(m.get("drillMembers") or ()),
        )
        for m in (d.get("measures") or [])
    )
    dimensions = tuple(
        Dimension(
            name=dd["name"],
            sql=dd["sql"],
            type=DimensionType(dd["type"]),
            primary_key=dd.get("primaryKey", False),
            title=dd.get("title"),
            description=dd.get("description"),
            format=dd.get("format"),
            drill_members=tuple(dd.get("drillMembers") or ()),
        )
        for dd in (d.get("dimensions") or [])
    )
    segments = tuple(
        Segment(name=s["name"], sql=s["sql"]) for s in (d.get("segments") or [])
    )
    joins = {
        t: _resolve_join(t, spec) for t, spec in (d.get("joins") or {}).items()
    }
    meta = CubeMeta(
        name=d["name"],
        sql=d["sql"],
        measures=measures,
        dimensions=dimensions,
        segments=segments,
        joins=joins,
        refresh_key=d.get("refreshKey"),
        title=d.get("title"),
        description=d.get("description"),
    )
    registry.register(meta)
    return meta


def load_cube_file(path: str | Path) -> list[CubeMeta]:
    """Load and register cubes from one YAML file."""
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    cubes = (data or {}).get("cubes") or []
    return [_cube_from_dict(c) for c in cubes]


def load_cube_dir(directory: str | Path) -> int:
    """Load every ``*.yaml``/``*.yml`` cube file under ``directory``. Returns count."""
    base = Path(directory)
    n = 0
    for p in sorted(base.rglob("*.y*ml")):
        n += len(load_cube_file(p))
    return n
