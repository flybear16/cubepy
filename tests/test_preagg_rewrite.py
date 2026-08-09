"""Phase 2 (T2.1): RollupBuilder produces results identical to the base SQLBuilder.

Runs both builders against the same DuckDB dataset (base table vs a hand-built
rollup table that mirrors what RollupBuilderService will CTAS in Phase 3) and
asserts the row sets are equal for SUM, COUNT, filtered SUM, time roll-up and a
date-range filter. RLS is applied on both sides (same security_context fragment).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine

from cubepy.orchestrator.preagg import router
from cubepy.schema.loader import cube, dimension, measure
from cubepy.schema.meta import MeasureType, PreAggregation
from cubepy.schema.registry import registry
from cubepy.security.context import SecurityContext
from cubepy.sqlgen.builder import SQLBuilder
from cubepy.sqlgen.query import Query
from cubepy.sqlgen.rollup import RollupBuilder

pytest.importorskip("duckdb_engine")

NOW = datetime(2026, 8, 7, 12, 0, 0, tzinfo=UTC)
CTX = SecurityContext(role="admin", tenant_id="42")  # RLS: orders.tenant_id = 42


@pytest.fixture
def engine(tmp_path) -> object:
    eng = create_engine(f"duckdb:///{tmp_path}/rewrite.duckdb")
    with eng.begin() as conn:
        conn.exec_driver_sql(
            "CREATE TABLE orders (id INTEGER, amount DOUBLE, user_id INTEGER, "
            "tenant_id INTEGER, status VARCHAR, created_at TIMESTAMP)"
        )
        conn.exec_driver_sql(
            "INSERT INTO orders VALUES "
            "(1, 10, 1, 42, 'shipped', '2026-08-01 10:00:00'), "
            "(2, 30, 2, 42, 'shipped', '2026-08-02 10:00:00'), "
            "(3, 5,  1, 42, 'pending','2026-08-03 10:00:00'), "
            "(4, 100,3, 99, 'shipped', '2026-08-04 10:00:00')"
        )
        # Hand-built rollup mirroring the Phase-3 CTAS: dims/security/time cols named
        # after member.sql; measure cols named after the measure (quoted).
        conn.exec_driver_sql(
            'CREATE TABLE cubepy_rollup_orders_daily AS '
            'SELECT orders.status AS status, orders.tenant_id AS tenant_id, '
            "date_trunc('day', orders.created_at) AS created_at, "
            'sum(orders.amount) AS "revenue", count(*) AS "count", '
            'sum(CASE WHEN orders.status = \'shipped\' THEN orders.amount ELSE 0 END) '
            'AS "filtered_revenue" '
            "FROM orders AS orders GROUP BY orders.status, orders.tenant_id, "
            "date_trunc('day', orders.created_at)"
        )
    return eng


@pytest.fixture(autouse=True)
def _cube() -> Iterator[None]:
    registry.clear()

    @cube(
        "Orders",
        "orders",
        security_context={"check_permission": lambda ctx: [f"orders.tenant_id = {ctx.tenant_id}"]},
        security_columns=("tenant_id",),
        pre_aggregations=[
            PreAggregation(
                "daily",
                ("Orders.revenue", "Orders.count", "Orders.filtered_revenue"),
                ("Orders.status",),
                "Orders.created_at",
                "day",
                security_columns=("tenant_id",),
            )
        ],
    )
    class _O:
        revenue = measure("amount", MeasureType.SUM)
        count = measure(None, MeasureType.COUNT)
        filtered_revenue = measure(
            "amount", MeasureType.SUM,
            filters=({"member": "Orders.status", "operator": "equals", "values": ["shipped"]},),
        )
        status = dimension("status", "string")
        created_at = dimension("created_at", "time")

    yield
    registry.clear()


def _run(eng, stmt) -> list[dict]:
    # The orchestrator pins the session to UTC before rollup execution (plan §5/G1):
    # date_range bounds render as +00:00, and naive day-buckets only compare
    # consistently when the session tz is UTC. Run both builders under UTC so the
    # equivalence check is timezone-independent.
    with eng.connect() as conn:
        conn.exec_driver_sql("SET TIME ZONE 'UTC'")
        return [dict(r) for r in conn.execute(stmt).mappings().all()]


def _norm(rows: list[dict]) -> list[tuple]:
    """Sort-insensitive comparison key: sorted tuples of sorted items."""
    out = []
    for r in rows:
        out.append(tuple(sorted((k, _scalar(v)) for k, v in r.items())))
    return sorted(out, key=lambda x: repr(x))


def _scalar(v):
    # Normalise numeric/date types that DuckDB may return differently per query shape.
    if isinstance(v, float):
        return round(v, 6)
    return v


def _assert_equiv(eng, raw: dict) -> None:
    q = Query.parse(raw)
    base_rows = _run(eng, SQLBuilder(q, CTX, now=NOW).build())
    route = router.match(q, CTX)
    assert route is not None, "matcher should route this query to the rollup"
    roll_rows = _run(eng, RollupBuilder(q, CTX, route, now=NOW).build())
    assert _norm(base_rows) == _norm(roll_rows), (
        f"base != rollup for {raw}\n base={base_rows}\n rollup={roll_rows}"
    )


def test_sum_count_by_status(engine) -> None:
    _assert_equiv(
        engine,
        {"measures": ["Orders.revenue", "Orders.count"], "dimensions": ["Orders.status"]},
    )


def test_filtered_sum_by_status(engine) -> None:
    _assert_equiv(
        engine,
        {"measures": ["Orders.filtered_revenue"], "dimensions": ["Orders.status"]},
    )


def test_time_rollover_month(engine) -> None:
    # rollup is day-bucketed; query groups by month -> day must roll up to month.
    _assert_equiv(
        engine,
        {
            "measures": ["Orders.revenue"],
            "timeDimensions": [{"dimension": "Orders.created_at", "granularity": "month"}],
            "order": [["Orders.created_at", "asc"]],
        },
    )


def test_date_range_filter(engine) -> None:
    _assert_equiv(
        engine,
        {
            "measures": ["Orders.revenue", "Orders.count"],
            "dimensions": ["Orders.status"],
            "timeDimensions": [
                {
                    "dimension": "Orders.created_at",
                    "granularity": "day",
                    "dateRange": ["2026-08-02", "2026-08-03"],
                }
            ],
        },
    )


def test_where_filter_equivalence(engine) -> None:
    _assert_equiv(
        engine,
        {
            "measures": ["Orders.revenue"],
            "dimensions": ["Orders.status"],
            "filters": [{"member": "Orders.status", "operator": "equals", "values": ["shipped"]}],
        },
    )


def test_rollup_route_name(engine) -> None:
    route = router.match(
        Query.parse({"measures": ["Orders.revenue"], "dimensions": ["Orders.status"]}), CTX
    )
    assert route is not None and route.table_name == "cubepy_rollup_orders_daily"
