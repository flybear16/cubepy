"""Phase 3 (T3.3): end-to-end pre-aggregation isolation.

The rollup table physically holds every tenant's rows; RLS must still filter at
query time so tenant A never sees tenant B's aggregates. This is the security
contract of the whole feature, exercised here against a real engine (DuckDB
always, Postgres when ``pg_dsn`` is available).
"""

from __future__ import annotations

from collections.abc import Iterator

import fakeredis
import pytest
from sqlalchemy import create_engine

from cubepy.cache.redis_cache import RedisCache
from cubepy.config import Settings
from cubepy.orchestrator.executor import SyncEngineExecutor, make_engine_and_executor
from cubepy.orchestrator.orchestrator import QueryOrchestrator
from cubepy.orchestrator.rollup_builder import RollupBuilderService
from cubepy.schema.loader import cube, dimension, measure
from cubepy.schema.meta import MeasureType, PreAggregation
from cubepy.schema.registry import registry
from cubepy.security.context import SecurityContext
from cubepy.sqlgen.query import Query


@pytest.fixture(autouse=True)
def _orders_with_rollup() -> Iterator[None]:
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


_QUERY = Query.parse({"measures": ["Orders.revenue"], "dimensions": ["Orders.status"]})


async def _assert_tenant_isolation(orch: QueryOrchestrator) -> None:
    # Tenant 42: shipped 10+30=40, pending 5. Tenant 99's 100 must NOT appear.
    env42 = await orch.load(_QUERY, SecurityContext(role="admin", tenant_id="42"))
    by42 = {r["Orders.status"]: r["Orders.revenue"] for r in env42["data"]}
    assert by42 == {"shipped": 40.0, "pending": 5.0}
    assert env42["usedPreAggregations"] == [{"tableName": "cubepy_rollup_orders_daily"}]

    # Tenant 99: shipped 100 only. Tenant 42's rows must NOT appear.
    env99 = await orch.load(_QUERY, SecurityContext(role="admin", tenant_id="99"))
    by99 = {r["Orders.status"]: r["Orders.revenue"] for r in env99["data"]}
    assert by99 == {"shipped": 100.0}
    assert env99["usedPreAggregations"] == [{"tableName": "cubepy_rollup_orders_daily"}]


def _seed_orders_duckdb(path) -> object:
    eng = create_engine(f"duckdb:///{path}/integ.duckdb")
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


async def _build_and_orch(exe) -> QueryOrchestrator:
    cube_meta = registry.get("Orders")
    await RollupBuilderService(exe).build(cube_meta, cube_meta.pre_aggregations[0])
    return QueryOrchestrator(
        RedisCache(fakeredis.FakeAsyncRedis()), exe, settings=Settings(preagg_enabled=True)
    )


pytest.importorskip("duckdb_engine")


async def test_duckdb_preagg_isolates_tenants(tmp_path) -> None:
    orch = await _build_and_orch(SyncEngineExecutor(_seed_orders_duckdb(tmp_path)))
    await _assert_tenant_isolation(orch)


async def test_postgres_preagg_isolates_tenants(pg_reseed) -> None:
    engine, exe, _is_async = make_engine_and_executor(pg_reseed)
    try:
        # Re-seed is handled by the pg_dsn fixture; just build + query.
        orch = await _build_and_orch(exe)
        await _assert_tenant_isolation(orch)
    finally:
        await engine.dispose()  # type: ignore[func-returns-value]
