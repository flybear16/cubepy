"""SQLBuilder: turn a :class:`Query` + cube + security context into a parameterised
SQL statement (``sqlalchemy.text``).

Conventions (see ``docs/06``):
- Each cube's ``sql`` is the FROM table expression. If it is a SELECT/WITH it is
  wrapped in parens; otherwise used verbatim (e.g. a table name).
- Cubes are aliased by their **lowercased** name. Postgres folds unquoted
  identifiers to lowercase, so author-written refs like ``Orders.user_id`` and
  ``Users.id`` resolve against the lowercased aliases consistently.
- Member ``sql`` strings, join ``sql``, segment ``sql`` and RLS fragments are
  trusted author SQL (same trust model as cube.js). Filter *values* are bound
  as parameters (never interpolated).
- A requested member that is not visible to ``ctx`` fails closed (ValueError);
  the API layer maps that to HTTP 400.
"""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import text
from sqlalchemy.sql.elements import TextClause

from cubepy.schema.meta import CubeMeta, Measure, MeasureType
from cubepy.schema.registry import registry
from cubepy.security.context import SecurityContext
from cubepy.security.permissions import PermissionBuilder
from cubepy.sqlgen.date_range import resolve_date_range
from cubepy.sqlgen.operators import OPERATORS
from cubepy.sqlgen.query import Filter, Query


class _Params:
    def __init__(self) -> None:
        self._values: dict[str, Any] = {}
        self._n = 0

    def bind(self, value: Any) -> str:
        name = f"p{self._n}"
        self._n += 1
        self._values[name] = value
        return f":{name}"

    @property
    def values(self) -> dict[str, Any]:
        return dict(self._values)


class SQLBuilder:
    def __init__(
        self,
        query: Query,
        ctx: SecurityContext,
        *,
        now: Any = None,
    ) -> None:
        self.query = query
        self.ctx = ctx
        self.now = now
        self._params = _Params()
        # alias_lower -> CubeMeta, in insertion order (primary first).
        self._cubes: dict[str, CubeMeta] = {}

    # -- public ---------------------------------------------------------------

    def build(self) -> TextClause:
        stmt = text(self._render())
        if self._params.values:
            stmt = stmt.bindparams(**self._params.values)
        return stmt

    def render_literal(self) -> str:
        return str(self.build().compile(compile_kwargs={"literal_binds": True}))

    # -- internals ------------------------------------------------------------

    def _alias_for(self, cube: CubeMeta) -> str:
        return cube.name.lower()

    def _resolve_member(self, path: str) -> tuple[CubeMeta, str, Any]:
        cube_name, member_name = path.split(".", 1)
        cube = registry.get(cube_name)
        alias = self._alias_for(cube)
        self._cubes.setdefault(alias, cube)
        if member_name in {m.name for m in cube.measures}:
            return cube, "measure", cube.measure(member_name)
        if member_name in {d.name for d in cube.dimensions}:
            return cube, "dimension", cube.dimension(member_name)
        if member_name in {s.name for s in cube.segments}:
            return cube, "segment", cube.segment(member_name)
        raise KeyError(f"member {path!r} not found in cube {cube_name!r}")

    def _ensure_visible(self, kind: str, member: Any) -> None:
        shown = getattr(member, "shown", None)
        if shown is not None and not shown(self.ctx):
            raise ValueError(f"{kind} {member.name!r} is not available to this user")

    def _measure_sql(self, measure: Measure) -> str:
        inner = measure.sql if measure.sql is not None else "*"
        t = measure.type
        if t == MeasureType.COUNT and measure.sql is None:
            return "count(*)"
        if t == MeasureType.SUM:
            agg = f"sum({inner})"
        elif t == MeasureType.COUNT:
            agg = f"count({inner})"
        elif t == MeasureType.COUNT_DISTINCT:
            agg = f"count(distinct {inner})"
        elif t == MeasureType.COUNT_DISTINCT_APPROX:
            # Requires the pg_hll extension on Postgres; faithful to cube.js.
            agg = f"round(hll_cardinality(hll_add_agg(hll_hash_any({inner}))))"
        elif t == MeasureType.AVG:
            agg = f"avg({inner})"
        elif t == MeasureType.MIN:
            agg = f"min({inner})"
        elif t == MeasureType.MAX:
            agg = f"max({inner})"
        else:  # pragma: no cover - exhaustive enum
            raise ValueError(f"unsupported measure type {t!r}")

        if measure.filters:
            cond = " AND ".join(
                self._compile_filter(Filter.model_validate(f)) for f in measure.filters
            )
            return f"sum(CASE WHEN {cond} THEN {inner} ELSE 0 END)"
        return agg

    def _calculated_sql(self, cube: CubeMeta, measure: Measure) -> str:
        """Inline a calculated measure's ``{name}`` refs with sibling aggregate SQL.

        Non-recursive: a referenced measure must be a concrete aggregate in the
        same cube (sum/count/...), not another calculated measure.
        """
        if not measure.formula:
            raise ValueError(f"calculated measure {measure.name!r} has no formula")

        def _replace(match: re.Match[str]) -> str:
            ref_name = match.group(1)
            try:
                ref = cube.measure(ref_name)
            except KeyError:
                raise ValueError(
                    f"calculated measure {measure.name!r} references unknown measure {{{ref_name}}}"
                ) from None
            if ref.type == MeasureType.CALCULATED:
                raise ValueError(
                    f"calculated measure {measure.name!r} references another "
                    f"calculated measure {{{ref_name}}}; nesting is not supported"
                )
            return f"({self._measure_sql(ref)})"

        return re.sub(r"\{([a-zA-Z_]\w*)\}", _replace, measure.formula)

    def _base_table(self, cube: CubeMeta) -> str:
        sql = cube.sql.strip()
        if sql.lower().startswith(("select", "with")):
            return f"({sql})"
        return sql

    def _from_clause(self) -> tuple[str, list[str]]:
        if not self._cubes:
            raise ValueError("query references no cubes")
        primary_alias = next(iter(self._cubes))
        primary = self._cubes[primary_alias]
        from_sql = f"{self._base_table(primary)} AS {primary_alias}"
        joins: list[str] = []
        for alias, cube in self._cubes.items():
            if alias == primary_alias:
                continue
            join = primary.joins.get(cube.name)
            if join is None:
                raise ValueError(
                    f"cube {cube.name!r} is referenced but not joined to {primary.name!r}"
                )
            joins.append(f"LEFT JOIN {self._base_table(cube)} AS {alias} ON ({join.sql})")
        return from_sql, joins

    # HAVING only supports aggregate comparisons; string/date operators have
    # no meaning on an aggregated value (docs/06 §2 measureFilter).
    _MEASURE_FILTER_OPS = {"equals", "notEquals", "gt", "gte", "lt", "lte"}

    def _compile_filter(self, f: Filter, *, having: bool = False) -> str:
        if f.or_:
            return "(" + " OR ".join(self._compile_filter(x, having=having) for x in f.or_) + ")"
        if f.and_:
            return "(" + " AND ".join(self._compile_filter(x, having=having) for x in f.and_) + ")"
        if not f.member or not f.operator:
            raise ValueError("filter requires member and operator")
        cube, kind, member = self._resolve_member(f.member)
        self._ensure_visible(kind, member)
        if kind == "measure":
            if not having:
                raise ValueError(
                    "measure filters inside a composite filter are not supported "
                    "(cannot mix WHERE and HAVING); move the measure filter to the top level"
                )
            if member.type in self._WINDOW_TYPES:
                raise ValueError(f"window measure {f.member!r} cannot be used in filters")
            if f.operator not in self._MEASURE_FILTER_OPS:
                raise ValueError(
                    f"operator {f.operator!r} is not supported on measure {f.member!r}"
                )
            col = self._measure_expr(f.member)  # HAVING repeats the aggregate expr
        else:
            col = member.sql
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

    _WINDOW_TYPES = {
        MeasureType.RUNNING_TOTAL,
        MeasureType.RUNNING_SUM,
        MeasureType.RANK,
        MeasureType.ROW_NUMBER,
    }

    def _window_specs(self) -> list[tuple[str, MeasureType, str]]:
        """Resolved window measures: (path, type, referenced_measure_path)."""
        specs: list[tuple[str, MeasureType, str]] = []
        for path in self.query.measures:
            cube, _kind, member = self._resolve_member(path)
            if member.type in self._WINDOW_TYPES:
                self._ensure_visible("measure", member)
                ref_name = member.sql
                if not ref_name:
                    raise ValueError(
                        f"window measure {member.name!r} must reference a sibling "
                        f"measure via its sql"
                    )
                try:
                    ref = cube.measure(ref_name)
                except KeyError:
                    raise ValueError(
                        f"window measure {member.name!r} references unknown measure {ref_name!r}"
                    ) from None
                if ref.type in self._WINDOW_TYPES or ref.type == MeasureType.CALCULATED:
                    raise ValueError(
                        f"window measure {member.name!r} may only reference a "
                        f"concrete aggregate, not {{{ref_name}}}"
                    )
                self._ensure_visible("measure", ref)
                specs.append((path, member.type, f"{cube.name}.{ref_name}"))
        return specs

    def _window_fn(self, wtype: MeasureType, ref_path: str, order_cols: str) -> str:
        col = f'sub."{ref_path}"'
        if wtype in (MeasureType.RUNNING_TOTAL, MeasureType.RUNNING_SUM):
            return f"sum({col}) OVER (ORDER BY {order_cols})"
        if wtype == MeasureType.RANK:
            return f"rank() OVER (ORDER BY {order_cols})"
        if wtype == MeasureType.ROW_NUMBER:
            return f"row_number() OVER (ORDER BY {order_cols})"
        raise ValueError(f"unsupported window type {wtype!r}")  # pragma: no cover

    def _window_order_cols(self) -> str:
        # Only timeDimensions WITH granularity produce a sub column to order by.
        cols = [f'sub."{td.dimension}"' for td in self.query.timeDimensions if td.granularity]
        cols += [f'sub."{p}"' for p in self.query.dimensions]
        if not cols:
            raise ValueError("window measures require a dimension or timeDimension to order by")
        return ", ".join(cols)

    def _measure_select_item(self, path: str) -> str:
        return f'{self._measure_expr(path)} AS "{path}"'

    def _measure_expr(self, path: str) -> str:
        """Aggregate SQL for a measure, without the output alias.

        Also used by HAVING, which (unlike ORDER BY) may not reference
        select-list aliases in PG — the expression must be repeated.
        """
        cube, _kind, member = self._resolve_member(path)
        self._ensure_visible("measure", member)
        if member.type == MeasureType.CALCULATED:
            return self._calculated_sql(cube, member)
        return self._measure_sql(member)

    def _collect_where(self, time_ranges: list[str]) -> list[str]:
        where: list[str] = []
        for _alias, cube in self._cubes.items():
            if not PermissionBuilder.cube_visible(cube, self.ctx):
                raise ValueError(f"cube {cube.name!r} is not available to this user")
            for cond in PermissionBuilder.apply_row_level(cube, self.ctx):
                where.append(cond)
        for seg_path in self.query.segments:
            _cube, _kind, member = self._resolve_member(seg_path)
            where.append(member.sql)
        where.extend(time_ranges)
        return where

    def _is_measure_filter(self, f: Filter) -> bool:
        """A (possibly composite) filter whose leaves ALL target measures.

        Pure-measure composites render into HAVING; mixed composites raise
        (WHERE/HAVING cannot be joined inside one boolean tree).
        """
        if f.or_ or f.and_:
            children = list(f.or_ or f.and_)
            return bool(children) and all(self._is_measure_filter(c) for c in children)
        if not f.member:
            return False
        _cube, kind, _member = self._resolve_member(f.member)
        return kind == "measure"

    def _split_filters(self) -> tuple[list[str], list[str]]:
        """Route query filters into (where_fragments, having_fragments).

        Dimension-member filters are row-level predicates (WHERE); measure-member
        filters are aggregate predicates (HAVING) — docs/06 §2 ``measureFilter``.
        """
        where_frags: list[str] = []
        having_frags: list[str] = []
        for f in self.query.filters:
            if self._is_measure_filter(f):
                having_frags.append(self._compile_filter(f, having=True))
            else:
                where_frags.append(self._compile_filter(f))
        return where_frags, having_frags

    def _assemble_body(
        self,
        select_items: list[str],
        from_sql: str,
        joins: list[str],
        where: list[str],
        group_items: list[str],
        having: list[str] | None = None,
    ) -> str:
        parts = ["SELECT " + ",\n       ".join(select_items), f"FROM {from_sql}"]
        parts.extend(joins)
        if where:
            parts.append("WHERE " + "\n  AND ".join(where))
        if group_items:
            parts.append("GROUP BY " + ", ".join(group_items))
        if having:
            parts.append("HAVING " + "\n  AND ".join(having))
        return "\n".join(parts)

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

    def _render(self) -> str:
        window_specs = self._window_specs()
        window_paths = {s[0] for s in window_specs}

        inner_select: list[str] = []
        group_items: list[str] = []
        time_ranges: list[str] = []

        for path in self.query.measures:
            if path in window_paths:
                continue
            inner_select.append(self._measure_select_item(path))
        # Window functions need their referenced aggregate available in the inner query.
        for _path, _t, ref in window_specs:
            if not any(s.endswith(f'AS "{ref}"') for s in inner_select):
                inner_select.append(self._measure_select_item(ref))

        # A member listed in BOTH dimensions and timeDimensions must render
        # once — the timeDimensions loop below owns it (granularity + dateRange).
        # Without this guard the SQL carries two identical aliases, a polluted
        # GROUP BY and an ambiguous ORDER BY (caught by the M3 pilot).
        td_paths = {td.dimension for td in self.query.timeDimensions}
        for path in self.query.dimensions:
            if path in td_paths:
                continue
            cube, _kind, member = self._resolve_member(path)
            self._ensure_visible("dimension", member)
            inner_select.append(f'{member.sql} AS "{path}"')
            group_items.append(member.sql)

        for td in self.query.timeDimensions:
            cube, _kind, member = self._resolve_member(td.dimension)
            self._ensure_visible("dimension", member)
            col = member.sql
            if td.dateRange is not None:
                start, end = resolve_date_range(td.dateRange, now=self.now, tz=self.query.timezone)
                time_ranges.append(
                    f"{col} >= {self._params.bind(start)} AND {col} <= {self._params.bind(end)}"
                )
            if td.granularity is None:
                # dateRange-only timeDimension: pure filter, no grouping column.
                # Rendering it into SELECT/GROUP BY exploded "sum over last 30
                # days" into per-timestamp detail rows (live acceptance catch).
                continue
            expr = f"date_trunc('{td.granularity}', {col})"
            inner_select.append(f'{expr} AS "{td.dimension}"')
            group_items.append(expr)

        where = self._collect_where(time_ranges)
        where_filters, having = self._split_filters()
        where += where_filters
        from_sql, joins = self._from_clause()

        if not window_specs:
            return (
                self._assemble_body(inner_select, from_sql, joins, where, group_items, having)
                + "\n"
                + self._tail()
            )

        # Window path: grouped inner query wrapped by an outer query applying window fns.
        # HAVING belongs to the grouped inner query (logical evaluation order).
        inner_sql = self._assemble_body(inner_select, from_sql, joins, where, group_items, having)
        order_cols = self._window_order_cols()

        outer_select: list[str] = []
        for path in self.query.measures:
            if path in window_paths:
                continue
            outer_select.append(f'sub."{path}" AS "{path}"')
        for path in self.query.dimensions:
            outer_select.append(f'sub."{path}" AS "{path}"')
        for td in self.query.timeDimensions:
            if td.granularity:  # dateRange-only tds have no sub column
                outer_select.append(f'sub."{td.dimension}" AS "{td.dimension}"')
        for path, wtype, ref in window_specs:
            outer_select.append(f'{self._window_fn(wtype, ref, order_cols)} AS "{path}"')

        sql = "SELECT " + ",\n       ".join(outer_select) + f"\nFROM (\n{inner_sql}\n) sub\n"
        return sql + self._tail()


def filters_contain_measure(filters: list) -> bool:
    """True if any leaf filter targets a measure member (i.e. becomes HAVING).

    Used by the pre-agg router to fail closed: the rollup rewrite compiles
    filters as row-level WHERE on the rollup table, which would silently
    change semantics for aggregate (measure) filters.
    """
    for f in filters:
        if getattr(f, "or_", None) or getattr(f, "and_", None):
            if filters_contain_measure(list(f.or_ or f.and_)):
                return True
            continue
        member = getattr(f, "member", None)
        if not member or "." not in member:
            continue
        cube_name, member_name = member.split(".", 1)
        try:
            cube = registry.get(cube_name)
        except KeyError:
            continue  # unknown member: Query.parse / builder will raise properly
        if any(m.name == member_name for m in cube.measures):
            return True
    return False
