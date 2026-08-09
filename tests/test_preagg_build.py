"""Phase 3 (T3.1): RollupBuilderService CTAS — builds a correct, idempotent rollup."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine

from cubepy.orchestrator.executor import SyncEngineExecutor
from cubepy.orchestrator.rollup_builder import RollupBuilderService
from cubepy.schema.loader import cube, dimension, measure
from cubepy.schema.meta import MeasureType, PreAggregation
from cubepy.schema.registry import registry

pytest.importorskip("duckdb_engine")


@pytest.fixture
def engine(tmp_path) -> object:
    eng = create_engine(f"duckdb:///{tmp_path}/build.duckdb")
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
                ("Orders.revenue", "Orders.count"),
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
        status = dimension("status", "string")
        created_at = dimension("created_at", "time")

    yield
    registry.clear()


async def test_build_creates_table_with_correct_rows(engine) -> None:
    service = RollupBuilderService(SyncEngineExecutor(engine))
    table = await service.build(registry.get("Orders"), registry.get("Orders").pre_aggregations[0])
    assert table == "cubepy_rollup_orders_daily"

    with engine.connect() as conn:
        # 4 distinct (status, tenant_id, day) combos across both tenants.
        rows = conn.exec_driver_sql(
            'SELECT status, tenant_id, created_at, "revenue", "count" '
            "FROM cubepy_rollup_orders_daily ORDER BY tenant_id, created_at"
        ).mappings().all()
        assert len(rows) == 4
        by_key = {(r["status"], r["tenant_id"]): r for r in rows}
        # tenant 42, pending day-bucket -> the single 08-03 row (revenue 5, count 1).
        assert by_key[("pending", 42)]["revenue"] == 5.0
        assert by_key[("pending", 42)]["count"] == 1
        # tenant 99 shipped -> the 08-04 row (revenue 100).
        assert by_key[("shipped", 99)]["revenue"] == 100.0


async def test_build_is_idempotent(engine) -> None:
    service = RollupBuilderService(SyncEngineExecutor(engine))
    cube_meta = registry.get("Orders")
    pa = cube_meta.pre_aggregations[0]
    await service.build(cube_meta, pa)
    # Second build drops + recreates without error and yields the same row count.
    await service.build(cube_meta, pa)
    with engine.connect() as conn:
        n = conn.exec_driver_sql(
            "SELECT COUNT(*) FROM cubepy_rollup_orders_daily"
        ).scalar()
        assert n == 4


async def test_built_rollup_serves_equivalent_query(engine) -> None:
    # End-to-end: build it, then query via RollupBuilder and get the right number.
    from cubepy.orchestrator.preagg import router
    from cubepy.security.context import SecurityContext
    from cubepy.sqlgen.query import Query
    from cubepy.sqlgen.rollup import RollupBuilder

    await RollupBuilderService(SyncEngineExecutor(engine)).build(
        registry.get("Orders"), registry.get("Orders").pre_aggregations[0]
    )
    ctx = SecurityContext(role="admin", tenant_id="42")
    q = Query.parse({"measures": ["Orders.revenue"], "dimensions": ["Orders.status"]})
    route = router.match(q, ctx)
    assert route is not None
    with engine.connect() as conn:
        conn.exec_driver_sql("SET TIME ZONE 'UTC'")
        rows = [dict(r) for r in conn.execute(RollupBuilder(q, ctx, route).build()).mappings().all()]
    by_status = {r["Orders.status"]: r["Orders.revenue"] for r in rows}
    # tenant 42 only: shipped 10+30=40, pending 5.
    assert by_status == {"shipped": 40.0, "pending": 5.0}
