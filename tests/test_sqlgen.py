"""Unit tests for the SQL generator (G005)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine

from cubepy.schema.loader import cube, dimension, measure, segment
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
        cumulative = measure("revenue", MeasureType.RUNNING_TOTAL)
        amount_avg = measure("amount", MeasureType.AVG)
        amount_min = measure("amount", MeasureType.MIN)
        amount_max = measure("amount", MeasureType.MAX)
        approx_users = measure("user_id", MeasureType.COUNT_DISTINCT_APPROX)
        filtered_revenue = measure(
            "amount", MeasureType.SUM,
            filters=({"member": "Orders.status", "operator": "equals", "values": ["shipped"]},),
        )
        status = dimension("status", "string")
        created_at = dimension("created_at", "time")

    @cube("Users", "users")
    class _U:
        country = dimension("country", "string")

    @cube("Products", "products")
    class _P:
        name = dimension("name", "string")

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


def test_filters_remaining_operators() -> None:
    sql = _sql(
        {
            "measures": ["Orders.revenue"],
            "filters": [
                {"member": "Orders.status", "operator": "notEquals", "values": ["shipped"]},
                {"member": "Orders.status", "operator": "notIn", "values": ["a", "b"]},
                {"member": "Orders.status", "operator": "notContains", "values": ["hip"]},
                {"member": "Orders.status", "operator": "startsWith", "values": ["sh"]},
                {"member": "Orders.status", "operator": "endsWith", "values": ["ed"]},
                {"member": "Orders.status", "operator": "notSet"},
                {"member": "Orders.created_at", "operator": "beforeDate", "values": ["2026-08-03"]},
                {"member": "Orders.created_at", "operator": "afterDate", "values": ["2026-08-01"]},
                {
                    "member": "Orders.created_at",
                    "operator": "inDateRange",
                    "values": ["2026-08-01", "2026-08-05"],
                },
                {
                    "member": "Orders.created_at",
                    "operator": "notInDateRange",
                    "values": ["2026-08-01", "2026-08-05"],
                },
            ],
        }
    )
    assert "status <> 'shipped'" in sql
    assert "status NOT IN ('a', 'b')" in sql
    assert "status NOT LIKE '%hip%'" in sql
    assert "status LIKE 'sh%'" in sql
    assert "status LIKE '%ed'" in sql
    assert "status IS NULL" in sql
    assert "created_at <" in sql and "created_at >" in sql  # beforeDate / afterDate
    assert "created_at >=" in sql and "created_at <=" in sql  # inDateRange
    assert " OR " in sql  # notInDateRange


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


def test_window_measure_wraps_in_subquery() -> None:
    sql = _sql(
        {
            "measures": ["Orders.cumulative"],
            "timeDimensions": [
                {"dimension": "Orders.created_at", "granularity": "day"}
            ],
        }
    )
    assert 'sum(sub."Orders.revenue") OVER (ORDER BY sub."Orders.created_at")' in sql
    assert 'AS "Orders.cumulative"' in sql
    assert "FROM (" in sql and ") sub" in sql


def test_window_measure_without_ordering_raises() -> None:
    with pytest.raises(ValueError):
        _sql({"measures": ["Orders.cumulative"]})


def test_aggregate_measure_types_execute_on_sqlite() -> None:
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "CREATE TABLE orders (id INTEGER, amount REAL, user_id INTEGER, "
            "tenant_id INTEGER, status TEXT, created_at TEXT)"
        )
        conn.exec_driver_sql(
            "INSERT INTO orders (id, amount, user_id, tenant_id, status) VALUES "
            "(1, 10, 1, 42, 'shipped'), (2, 30, 2, 42, 'shipped'), (3, 5, 1, 42, 'pending')"
        )
    stmt = SQLBuilder(
        Query.parse(
            {
                "measures": ["Orders.amount_avg", "Orders.amount_min", "Orders.amount_max", "Orders.uniq"],
            }
        ),
        _ctx(),
        now=NOW,
    ).build()
    with engine.connect() as conn:
        row = dict(conn.execute(stmt).mappings().all()[0])
    assert row["Orders.amount_avg"] == 15.0  # (10 + 30 + 5) / 3
    assert row["Orders.amount_min"] == 5.0
    assert row["Orders.amount_max"] == 30.0
    assert row["Orders.uniq"] == 2  # distinct user_id


def test_count_distinct_approx_sql_shape() -> None:
    sql = _sql({"measures": ["Orders.approx_users"]})
    assert "hll_cardinality(hll_add_agg(hll_hash_any(user_id)))" in sql


def test_filtered_measure_uses_case_when() -> None:
    sql = _sql({"measures": ["Orders.filtered_revenue"]})
    assert "sum(CASE WHEN status = 'shipped' THEN amount ELSE 0 END)" in sql


def test_unjoined_cube_raises() -> None:
    # Products is not joined to Orders in the fixture.
    with pytest.raises(ValueError, match="not joined"):
        _sql({"measures": ["Orders.revenue"], "dimensions": ["Products.name"]})


# --- remaining branch coverage ------------------------------------------------


def test_segment_query_adds_segment_where() -> None:
    registry.clear()

    @cube("Orders", "orders")
    class _OSeg:
        revenue = measure("amount", MeasureType.SUM)
        active = segment("status = 'active'")

    sql = _sql({"measures": ["Orders.revenue"], "segments": ["Orders.active"]})
    assert "status = 'active'" in sql


def test_count_measure_with_explicit_sql() -> None:
    registry.clear()

    @cube("Orders", "orders")
    class _O:
        c = measure("user_id", MeasureType.COUNT)

    assert "count(user_id)" in _sql({"measures": ["Orders.c"]})


def test_calculated_measure_without_formula_raises() -> None:
    registry.clear()

    @cube("Orders", "orders")
    class _O:
        bad = measure(None, MeasureType.CALCULATED)

    with pytest.raises(ValueError, match="no formula"):
        _sql({"measures": ["Orders.bad"]})


def test_calculated_measure_referencing_calculated_raises() -> None:
    registry.clear()

    @cube("Orders", "orders")
    class _O:
        base = measure("amount", MeasureType.SUM)
        a = measure(None, MeasureType.CALCULATED, formula="{base} * 2")
        b = measure(None, MeasureType.CALCULATED, formula="{a} + 1")

    with pytest.raises(ValueError, match="nesting is not supported"):
        _sql({"measures": ["Orders.b"]})


def test_cube_with_subquery_sql_wraps_in_parens() -> None:
    registry.clear()

    @cube("Orders", "select * from orders where active")
    class _O:
        revenue = measure("amount", MeasureType.SUM)

    sql = _sql({"measures": ["Orders.revenue"]})
    assert "FROM (select * from orders where active) AS orders" in sql


def test_empty_query_references_no_cubes() -> None:
    with pytest.raises(ValueError, match="references no cubes"):
        _sql({})


def test_and_filter_compiles() -> None:
    sql = _sql(
        {
            "measures": ["Orders.revenue"],
            "filters": [
                {
                    "and": [
                        {"member": "Orders.status", "operator": "equals", "values": ["a"]},
                        {"member": "Orders.status", "operator": "equals", "values": ["b"]},
                    ]
                }
            ],
        }
    )
    assert "(status = 'a' AND status = 'b')" in sql


def test_filter_without_member_raises() -> None:
    with pytest.raises(ValueError, match="requires member and operator"):
        _sql({"measures": ["Orders.revenue"], "filters": [{"values": ["x"]}]})


def test_window_measure_without_sql_raises() -> None:
    registry.clear()

    @cube("Orders", "orders")
    class _O:
        rt = measure(None, MeasureType.RUNNING_TOTAL)

    with pytest.raises(ValueError, match="must reference a sibling measure"):
        _sql({"measures": ["Orders.rt"]})


def test_window_measure_unknown_ref_raises() -> None:
    registry.clear()

    @cube("Orders", "orders")
    class _O:
        rt = measure("nope", MeasureType.RUNNING_TOTAL)
        status = dimension("status", "string")

    with pytest.raises(ValueError, match="references unknown"):
        _sql({"measures": ["Orders.rt"], "dimensions": ["Orders.status"]})


def test_window_measure_referencing_window_raises() -> None:
    registry.clear()

    @cube("Orders", "orders")
    class _O:
        rt = measure("amount", MeasureType.RUNNING_TOTAL)
        rank = measure("rt", MeasureType.RANK)
        status = dimension("status", "string")

    with pytest.raises(ValueError, match="may only reference a concrete aggregate"):
        _sql({"measures": ["Orders.rank"], "dimensions": ["Orders.status"]})


def test_rank_and_row_number_window_functions() -> None:
    registry.clear()

    @cube("Orders", "orders")
    class _O:
        revenue = measure("amount", MeasureType.SUM)
        rank = measure("revenue", MeasureType.RANK)
        rn = measure("revenue", MeasureType.ROW_NUMBER)
        created_at = dimension("created_at", "time")

    sql = _sql(
        {
            "measures": ["Orders.rank", "Orders.rn"],
            "timeDimensions": [{"dimension": "Orders.created_at", "granularity": "day"}],
        }
    )
    assert 'rank() OVER (ORDER BY sub."Orders.created_at")' in sql
    assert 'row_number() OVER (ORDER BY sub."Orders.created_at")' in sql


def test_window_query_with_plain_measures_and_dimensions() -> None:
    registry.clear()

    @cube("Orders", "orders")
    class _O:
        revenue = measure("amount", MeasureType.SUM)
        cumulative = measure("revenue", MeasureType.RUNNING_TOTAL)
        status = dimension("status", "string")
        created_at = dimension("created_at", "time")

    sql = _sql(
        {
            "measures": ["Orders.revenue", "Orders.cumulative"],
            "dimensions": ["Orders.status"],
            "timeDimensions": [{"dimension": "Orders.created_at", "granularity": "day"}],
        }
    )
    # Non-window members are re-selected from the inner query in the outer wrapper.
    assert 'sub."Orders.revenue" AS "Orders.revenue"' in sql
    assert 'sub."Orders.status" AS "Orders.status"' in sql
    assert (
        'sum(sub."Orders.revenue") OVER '
        '(ORDER BY sub."Orders.created_at", sub."Orders.status")' in sql
    )


def test_invisible_cube_fails_closed() -> None:
    registry.clear()

    @cube("Orders", "orders", shown=lambda ctx: False)
    class _O:
        revenue = measure("amount", MeasureType.SUM)

    with pytest.raises(ValueError, match="not available"):
        _sql({"measures": ["Orders.revenue"]})


# --- query model --------------------------------------------------------------


def test_filter_parse_direct() -> None:
    from cubepy.sqlgen.query import Filter

    f = Filter.parse({"member": "Orders.status", "operator": "equals", "values": ["x"]})
    assert f.operator == "equals"
    assert f.values == ["x"]


def test_order_dict_normalisation() -> None:
    sql = _sql({"measures": ["Orders.revenue"], "order": {"Orders.revenue": "desc"}})
    assert 'ORDER BY "Orders.revenue" DESC' in sql
