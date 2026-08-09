"""RollupBuilder: render a query against a pre-aggregated rollup table.

Paired with :class:`cubepy.orchestrator.preagg.PreAggRouter`: when the matcher
returns a ``RollupRoute`` the orchestrator builds the query here instead of via
:class:`cubepy.sqlgen.builder.SQLBuilder`.

Rollup table column model (established by the CTAS in ``rollup_builder.py``):
  * dimension / time columns are named after ``member.sql`` (same as the base
    table) so filter fragments, RLS fragments (``Orders.tenant_id`` folds to the
    ``orders`` alias) and ``date_trunc`` reuse the exact same expressions;
  * measure columns are named after ``measure.name`` (quoted — measure names may
    be reserved words like ``count``) and hold the already-aggregated value, so a
    SUM/COUNT measure re-aggregates as a plain ``sum("<name>")`` (sum-of-sums /
    sum-of-counts, lossless).

The matcher guarantees the query is single-cube, join-free, SUM/COUNT-only, no
window/calculated measures, UTC time bucketing, and RLS column coverage — so this
builder stays small. Anything disallowed never reaches it.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.sql.elements import TextClause

from cubepy.orchestrator.preagg import RollupRoute
from cubepy.schema.meta import CubeMeta
from cubepy.schema.registry import registry
from cubepy.security.context import SecurityContext
from cubepy.security.permissions import PermissionBuilder
from cubepy.sqlgen.builder import _Params  # same param binder -> shared, not duplicated
from cubepy.sqlgen.date_range import resolve_date_range
from cubepy.sqlgen.operators import OPERATORS
from cubepy.sqlgen.query import Filter, Query


class RollupBuilder:
    def __init__(
        self,
        query: Query,
        ctx: SecurityContext,
        route: RollupRoute,
        *,
        now: Any = None,
    ) -> None:
        self.query = query
        self.ctx = ctx
        self.route = route
        self.now = now
        self._params = _Params()
        self._cube: CubeMeta = registry.get(route.cube)
        self._alias = self._cube.name.lower()

    def build(self) -> TextClause:
        stmt = text(self._render())
        if self._params.values:
            stmt = stmt.bindparams(**self._params.values)
        return stmt

    def render_literal(self) -> str:
        return str(self.build().compile(compile_kwargs={"literal_binds": True}))

    # -- internals ------------------------------------------------------------

    def _ensure_visible(self, kind: str, member: Any) -> None:
        shown = getattr(member, "shown", None)
        if shown is not None and not shown(self.ctx):
            raise ValueError(f"{kind} {member.name!r} is not available to this user")

    def _member(self, path: str) -> str:
        return path.rsplit(".", 1)[-1]

    def _render(self) -> str:
        alias = self._alias

        select_items: list[str] = []
        group_items: list[str] = []

        # Measures: re-aggregate the rollup's pre-aggregated column (lossless for
        # SUM/COUNT, the only types the matcher admits).
        for path in self.query.measures:
            mname = self._member(path)
            member = self._cube.measure(mname)
            self._ensure_visible("measure", member)
            col = f'{alias}."{mname}"'
            select_items.append(f'sum({col}) AS "{path}"')

        # Dimensions: rollup column is member.sql (same as base).
        for path in self.query.dimensions:
            member = self._cube.dimension(self._member(path))
            self._ensure_visible("dimension", member)
            col = f"{alias}.{member.sql}"
            select_items.append(f'{col} AS "{path}"')
            group_items.append(col)

        # Time dimensions: re-bucket the rollup's stored bucket up to query granularity.
        time_ranges: list[str] = []
        for td in self.query.timeDimensions:
            member = self._cube.dimension(self._member(td.dimension))
            self._ensure_visible("dimension", member)
            col = f"{alias}.{member.sql}"
            expr = f"date_trunc('{td.granularity}', {col})" if td.granularity else col
            select_items.append(f'{expr} AS "{td.dimension}"')
            group_items.append(expr)
            if td.dateRange is not None:
                start, end = resolve_date_range(
                    td.dateRange, now=self.now, tz=self.query.timezone
                )
                time_ranges.append(
                    f"{col} >= {self._params.bind(start)} AND {col} <= {self._params.bind(end)}"
                )

        where = self._collect_where(time_ranges)
        from_sql = f"{self.route.table_name} AS {alias}"

        parts = ["SELECT " + ",\n       ".join(select_items), f"FROM {from_sql}"]
        if where:
            parts.append("WHERE " + "\n  AND ".join(where))
        if group_items:
            parts.append("GROUP BY " + ", ".join(group_items))
        return "\n".join(parts) + "\n" + self._tail()

    def _collect_where(self, time_ranges: list[str]) -> list[str]:
        where: list[str] = []
        if not PermissionBuilder.cube_visible(self._cube, self.ctx):
            raise ValueError(f"cube {self._cube.name!r} is not available to this user")
        # RLS fragments reference ``{Cube}.col`` which folds to the lowercased alias;
        # the rollup materialises every declared security column (matcher guarantee).
        where.extend(PermissionBuilder.apply_row_level(self._cube, self.ctx))
        for f in self.query.filters:
            where.append(self._compile_filter(f))
        where.extend(time_ranges)
        return where

    def _compile_filter(self, f: Filter) -> str:
        if f.or_:
            return "(" + " OR ".join(self._compile_filter(x) for x in f.or_) + ")"
        if f.and_:
            return "(" + " AND ".join(self._compile_filter(x) for x in f.and_) + ")"
        if not f.member or not f.operator:
            raise ValueError("filter requires member and operator")
        mname = self._member(f.member)
        # Filters target dimensions (never measures); rollup dim column == member.sql.
        member = self._cube.dimension(mname)
        self._ensure_visible("dimension", member)
        col = f"{self._alias}.{member.sql}"
        op = OPERATORS.get(f.operator)
        if op is None:
            raise ValueError(f"unknown filter operator {f.operator!r}")
        values = list(f.values)
        if f.operator in {"inDateRange", "notInDateRange"}:
            start, end = resolve_date_range(
                values if len(values) > 1 else (values[0] if values else None),
                now=self.now,
                tz=self.query.timezone,
            )
            values = [start, end]
        elif f.operator in {"beforeDate", "afterDate"} and values:
            start, _ = resolve_date_range(values[0], now=self.now, tz=self.query.timezone)
            values = [start]
        return op(values, col, self._params.bind)

    def _tail(self) -> str:
        parts: list[str] = []
        order_items = self.query.order_items()
        if order_items:
            clauses = ", ".join(f'"{p}" {d.upper()}' for p, d in order_items)
            parts.append("ORDER BY " + clauses)
        if self.query.limit is not None:
            parts.append(f"LIMIT {int(self.query.limit)}")
        if self.query.offset is not None:
            parts.append(f"OFFSET {int(self.query.offset)}")
        return ("\n".join(parts) + "\n") if parts else ""
