"""Metric unit-test harness (metrics-platform P0.3).

Write assertions for metric 口径 against in-memory fixture data — no server,
no real warehouse::

    from cubepy.schema.loader import load_cube_file
    from cubepy.testing import fixture_engine, assert_query

    load_cube_file("schemas/shop.yml")
    eng = fixture_engine({
        "orders": [
            {"id": 1, "customer_id": 10, "amount": 100},
            {"id": 2, "customer_id": 10, "amount": 50},
            {"id": 3, "customer_id": 20, "amount": 30},
        ],
    })

    assert_query(
        {"measures": ["orders.total_revenue"]},
        [{"orders.total_revenue": 180}],
        engine=eng,
    )

Requires ``duckdb-engine`` (install extra: ``pip install cubepy-semantic[duckdb]``).
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.sql.elements import TextClause

from cubepy.security.context import SecurityContext
from cubepy.sqlgen.builder import SQLBuilder
from cubepy.sqlgen.query import Query

__all__ = ["fixture_engine", "run_query", "assert_query", "render_query"]


def _json_safe(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def _col_type(values: list[Any]) -> str:
    non_null = [v for v in values if v is not None]
    if not non_null:
        return "VARCHAR"
    if all(isinstance(v, bool) for v in non_null):
        return "BOOLEAN"
    if all(isinstance(v, int) and not isinstance(v, bool) for v in non_null):
        return "BIGINT"
    if all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in non_null):
        return "DOUBLE"
    return "VARCHAR"


def fixture_engine(tables: dict[str, list[dict[str, Any]]]) -> Engine:
    """In-memory DuckDB engine seeded with the given tables (types inferred)."""
    eng = create_engine("duckdb:///:memory:")
    with eng.begin() as conn:
        for name, rows in tables.items():
            if not rows:
                raise ValueError(f"fixture table {name!r} needs at least one row")
            cols = list(rows[0].keys())
            ddl = ", ".join(f'"{c}" {_col_type([r.get(c) for r in rows])}' for c in cols)
            conn.exec_driver_sql(f'CREATE TABLE "{name}" ({ddl})')
            marks = ", ".join("?" for _ in cols)
            conn.exec_driver_sql(
                f'INSERT INTO "{name}" VALUES ({marks})',
                [tuple(r[c] for c in cols) for r in rows],
            )
    return eng


def render_query(query: dict[str, Any] | Query, *, admin: bool = True) -> TextClause:
    """Compile a query to SQL (debug helper; prints what the harness executes)."""
    q = Query.parse(query) if isinstance(query, dict) else query
    ctx = SecurityContext(user_id="_test", role="admin" if admin else "viewer")
    return SQLBuilder(q, ctx).build()


def run_query(
    query: dict[str, Any] | Query,
    engine: Engine,
    *,
    ctx: SecurityContext | None = None,
) -> list[dict[str, Any]]:
    """Execute a Cube Query against the fixture engine; returns JSON-safe rows."""
    q = Query.parse(query) if isinstance(query, dict) else query
    ctx = ctx or SecurityContext(user_id="_test", role="admin")
    stmt = SQLBuilder(q, ctx).build()
    with engine.connect() as conn:
        res = conn.execute(stmt)
        return [
            {k: _json_safe(v) for k, v in row.items()} for row in res.mappings().all()
        ]


def assert_query(
    query: dict[str, Any] | Query,
    expected: list[dict[str, Any]],
    engine: Engine,
    *,
    ctx: SecurityContext | None = None,
    sort_by: str | None = None,
) -> list[dict[str, Any]]:
    """Run ``query`` and assert rows equal ``expected`` (order-insensitive by default).

    ``sort_by``: member path to sort both sides by when order matters.
    Floats are compared with ``round(…, 6)``.
    """
    actual = run_query(query, engine, ctx=ctx)
    sql = str(
        render_query(query).compile(
            dialect=__import__(
                "sqlalchemy.dialects.postgresql", fromlist=["dialect"]
            ).dialect()
        )
    )

    def norm(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out = []
        for r in rows:
            out.append(
                {
                    k: round(v, 6) if isinstance(v, float) else v
                    for k, v in r.items()
                }
            )
        return sorted(out, key=lambda r: sorted(r.items())) if sort_by is None else sorted(
            out, key=lambda r: r[sort_by]
        )

    a, e = norm(actual), norm(expected)
    if a != e:
        raise AssertionError(
            f"metric test failed\n  query : {query}\n  sql   : {sql}\n"
            f"  expect: {e}\n  actual: {a}"
        )
    return actual
