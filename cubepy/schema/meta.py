"""Schema domain model: the in-memory representation of a compiled Cube.

Mirrors the relevant subset of cube.js's schema-compiler output (see
``docs/06-cubejs-contract-notes.md``). All ``shown`` / ``check_permission`` callbacks
take the security context as an opaque object so this module never imports the
security layer (dependency direction: security -> schema, never the reverse).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

# A security-context callback receives the auth context and returns whether the
# member is visible, or (for check_permission) the raw SQL WHERE fragments.
ShownFn = Callable[[Any], bool]
CheckPermissionFn = Callable[[Any], list[str]]


class MeasureType(StrEnum):
    SUM = "sum"
    COUNT = "count"
    COUNT_DISTINCT = "countDistinct"
    COUNT_DISTINCT_APPROX = "countDistinctApprox"
    AVG = "avg"
    MIN = "min"
    MAX = "max"
    CALCULATED = "calculated"
    # Window measures (G016): the builder wraps the grouped query and applies
    # the function OVER the dimension/time order. ``sql`` names a sibling measure.
    RUNNING_TOTAL = "runningTotal"
    RUNNING_SUM = "runningSum"
    RANK = "rank"
    ROW_NUMBER = "rowNumber"


class DimensionType(StrEnum):
    TIME = "time"
    STRING = "string"
    NUMBER = "number"
    BOOLEAN = "boolean"
    GEO = "geo"
    ARRAY = "array"
    PRIMARY_KEY = "primaryKey"


class RelationshipType(StrEnum):
    BELONGS_TO = "belongsTo"
    HAS_ONE = "hasOne"
    HAS_MANY = "hasMany"


@dataclass(frozen=True)
class Measure:
    name: str
    sql: str | None
    type: MeasureType
    shown: ShownFn | None = None
    title: str | None = None
    description: str | None = None
    # Inline filters for a filtered measure (member, operator, values); resolved to
    # a CASE/aggregate in the SQL generator (G005).
    filters: tuple[dict[str, Any], ...] = ()
    # Formula for a calculated measure, referencing sibling measures as {name}.
    # The SQL generator inlines each {name} with that measure's aggregate SQL.
    formula: str | None = None
    # Display format hint (e.g. "currency", "percent") and drillable members;
    # surfaced in /meta for clients, not used by the SQL generator.
    format: str | None = None
    drill_members: tuple[str, ...] = ()


@dataclass(frozen=True)
class Dimension:
    name: str
    sql: str
    type: DimensionType
    shown: ShownFn | None = None
    primary_key: bool = False
    title: str | None = None
    description: str | None = None
    format: str | None = None
    drill_members: tuple[str, ...] = ()


@dataclass(frozen=True)
class Segment:
    name: str
    sql: str
    shown: ShownFn | None = None


@dataclass(frozen=True)
class Join:
    relationship: RelationshipType
    sql: str


@dataclass(frozen=True)
class CubeMeta:
    name: str
    sql: str
    measures: tuple[Measure, ...] = ()
    dimensions: tuple[Dimension, ...] = ()
    segments: tuple[Segment, ...] = ()
    # joins keyed by the target cube name, e.g. {"Users": Join(...)}
    joins: dict[str, Join] = field(default_factory=dict)
    shown: ShownFn | None = None
    security_context: CheckPermissionFn | None = None
    refresh_key: dict[str, Any] | None = None
    title: str | None = None
    description: str | None = None

    def measure(self, name: str) -> Measure:
        for m in self.measures:
            if m.name == name:
                return m
        raise KeyError(f"measure {name!r} not in cube {self.name!r}")

    def dimension(self, name: str) -> Dimension:
        for d in self.dimensions:
            if d.name == name:
                return d
        raise KeyError(f"dimension {name!r} not in cube {self.name!r}")

    def segment(self, name: str) -> Segment:
        for s in self.segments:
            if s.name == name:
                return s
        raise KeyError(f"segment {name!r} not in cube {self.name!r}")
