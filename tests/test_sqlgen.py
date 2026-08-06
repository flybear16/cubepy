"""Unit tests for the SQL generator (G005)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine

from cubepy.schema.loader import cube, dimension, measure
from cubepy.schema.meta import MeasureType
from cubepy.schema.registry import registry
from cubepy.security.context import SecurityContext
from cubepy.sqlgen.builder import SQLBuilder
from cubepy.sqlgen.query import Query

NOW = datetime(2026, 8, 7, 12, 0, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _orders_users() -> Iterator[None]:
    registry.clear()

    @cube(
        "Orders",
        "orders",
        joins={"Users": {"relationship": "belongsTo", "sql": "Orders.user_id = Users.id"}},
        security_context={"check_permission": lambda ctx: [f"orders.tenant_id = {ctx.tenant_id}"]},
    )
    class _O:
        revenue = measure("amount", MeasureType.SUM)
        count = measure(None, MeasureType.COUNT)
        uniq = measure("user_id", MeasureType.COUNT_DISTINCT)
        avg = measure(None, MeasureType.CALCULATED, formula="{revenue} / NULLIF({count}, 0)")
        status = dimension("status", "string")
        created_at = dimension("created_at", "time")

    @cube("Users", "users")
    class _U:
        country = dimension("country", "string")

    yield
    registry.clear()


def _ctx(role: str = "admin", tenant_id: str = "42") -> SecurityContext:
    return SecurityContext(role=role, tenant_id=tenant_id)


def _sql(query: dict, ctx: SecurityContext | None = None) -> str:
    stmt = SQLBuilder(Query.parse(query), ctx or _ctx(), now=NOW).render_literal()
    return " ".join(stmt.split())


# --- shape assertions ---------------------------------------------------------

def test_measure_dimension_groupby_and_rls() -> None:
    sql = _sql({"measures": ["Orders.revenue", "Orders.count"], "dimensions": ["Orders.status"]})
    assert 'sum(amount) AS "Orders.revenue"' in sql
    assert 'count(*) AS "Orders.count"' in sql
    assert 'status AS "Orders.status"' in sql
    assert "FROM orders AS orders" in sql
    assert "GROUP BY status" in sql
    assert "orders.tenant_id = 42" in sql  # RLS from check_permission


def test_count_distinct_and_join() -> None:
    sql = _sql(
        {"measures": ["Orders.uniq"], "dimensions": ["Users.country"]},
    )
    assert "count(distinct user_id)" in sql
    assert "FROM orders AS orders" in sql
    assert "LEFT JOIN users AS users ON (Orders.user_id = Users.id)" in sql
    assert "GROUP BY country" in sql


def test_time_dimension_granularity_and_relative_range() -> None:
    sql = _sql(
        {
            "measures": ["Orders.revenue"],
            "timeDimensions": [
                {"dimension": "Orders.created_at", "granularity": "day", "dateRange": "last 7 days"}
            ],
        }
    )
    assert "date_trunc('day', created_at)" in sql
    assert "created_at >= '2026-07-31 00:00:00+00:00'" in sql
    assert "created_at <= '2026-08-07 23:59:59.999999+00:00'" in sql
    assert "GROUP BY date_trunc('day', created_at)" in sql


def test_filters_operators() -> None:
    sql = _sql(
        {
            "measures": ["Orders.revenue"],
            "filters": [
                {"member": "Orders.status", "operator": "equals", "values": ["shipped"]},
                {"member": "Orders.revenue", "operator": "gte", "values": [100]},
                {"member": "Orders.status", "operator": "in", "values": ["a", "b"]},
                {"member": "Orders.status", "operator": "contains", "values": ["hip"]},
                {"member": "Orders.status", "operator": "set"},
                {
                    "or": [
                        {"member": "Orders.status", "operator": "equals", "values": ["x"]},
                        {"member": "Orders.status", "operator": "equals", "values": ["y"]},
                    ]
                },
            ],
        }
    )
    assert "orders.tenant_id = 42" in sql
    assert "status = 'shipped'" in sql
    assert "amount >= 100" in sql
    assert "status IN ('a', 'b')" in sql
    assert "status LIKE '%hip%'" in sql
    assert "status IS NOT NULL" in sql
    assert "(status = 'x' OR status = 'y')" in sql


def test_order_limit_offset() -> None:
    sql = _sql(
        {
            "measures": ["Orders.revenue"],
            "dimensions": ["Orders.status"],
            "order": [["Orders.revenue", "desc"]],
            "limit": 10,
            "offset": 5,
        }
    )
    assert 'ORDER BY "Orders.revenue" DESC' in sql
    assert "LIMIT 10" in sql
    assert "OFFSET 5" in sql


def test_hidden_member_fails_closed() -> None:
    registry.clear()

    @cube("Orders", "orders")
    class _O2:
        secret = measure("amount", MeasureType.SUM, shown=lambda ctx: ctx.role == "admin")
        status = dimension("status", "string")

    with pytest.raises(ValueError):
        _sql({"measures": ["Orders.secret"]}, ctx=_ctx(role="viewer"))


# --- execution smoke (SQLite) -------------------------------------------------

def test_executes_and_aggregates_on_sqlite() -> None:
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "CREATE TABLE orders (id INTEGER, amount REAL, status TEXT, "
            "created_at TEXT, tenant_id INTEGER, user_id INTEGER)"
        )
        conn.exec_driver_sql(
            "INSERT INTO orders (id, amount, status, tenant_id, user_id) VALUES "
            "(1, 10, 'shipped', 42, 1), (2, 30, 'shipped', 42, 2), "
            "(3, 5, 'pending', 42, 1), (4, 100, 'shipped', 99, 3)"
        )

    stmt = SQLBuilder(
        Query.parse(
            {
                "measures": ["Orders.revenue", "Orders.count"],
                "dimensions": ["Orders.status"],
                "filters": [{"member": "Orders.status", "operator": "equals", "values": ["shipped"]}],
                "order": [["Orders.revenue", "desc"]],
            }
        ),
        _ctx(role="admin", tenant_id="42"),
    ).build()

    with engine.connect() as conn:
        rows = conn.execute(stmt).mappings().all()

    # RLS tenant_id=42 + status=shipped -> rows 1 and 2 only (row 4 is tenant 99).
    by_status = {r["Orders.status"]: r for r in rows}
    assert by_status["shipped"]["Orders.revenue"] == 40
    assert by_status["shipped"]["Orders.count"] == 2
    assert "pending" not in by_status


def test_unknown_operator_raises() -> None:
    with pytest.raises(ValueError):
        _sql(
            {
                "measures": ["Orders.revenue"],
                "filters": [{"member": "Orders.status", "operator": "bogus", "values": ["x"]}],
            }
        )


def test_calculated_measure_inlines_refs() -> None:
    sql = _sql({"measures": ["Orders.avg"]})
    assert "(sum(amount)) / NULLIF((count(*)), 0)" in sql
    assert 'AS "Orders.avg"' in sql


def test_calculated_measure_unknown_ref_raises() -> None:
    registry.clear()

    @cube("Orders", "orders")
    class _OBad:
        bad = measure(None, MeasureType.CALCULATED, formula="{nope} + 1")

    with pytest.raises(ValueError):
        _sql({"measures": ["Orders.bad"]})
