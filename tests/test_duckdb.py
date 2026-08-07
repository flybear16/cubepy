"""DuckDB data-source integration (optional extra).

Run with ``uv sync --extra dev`` (duckdb-engine is a dev dep). Skips if the
``duckdb_engine`` package isn't installed.
"""

from __future__ import annotations

from collections.abc import Iterator

import fakeredis
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import create_engine

from cubepy.api.app import create_app
from cubepy.cache.redis_cache import RedisCache
from cubepy.config import settings
from cubepy.orchestrator.executor import SyncEngineExecutor
from cubepy.orchestrator.orchestrator import QueryOrchestrator
from cubepy.schema.loader import cube, dimension, measure
from cubepy.schema.meta import MeasureType
from cubepy.schema.registry import registry
from cubepy.security.context import create_token

pytest.importorskip("duckdb_engine")


@pytest.fixture(autouse=True)
def _orders() -> Iterator[None]:
    registry.clear()

    @cube(
        "Orders",
        "orders",
        security_context={"check_permission": lambda ctx: [f"orders.tenant_id = {ctx.tenant_id}"]},
    )
    class _O:
        revenue = measure("amount", MeasureType.SUM)
        count = measure(None, MeasureType.COUNT)
        cumulative_revenue = measure("revenue", MeasureType.RUNNING_TOTAL)
        status = dimension("status", "string")
        created_at = dimension("created_at", "time")

    yield
    registry.clear()


def _seeded_engine(path) -> object:
    # File-based (not :memory:) so connections across worker threads share state.
    eng = create_engine(f"duckdb:///{path}/test.duckdb")
    with eng.begin() as conn:
        conn.exec_driver_sql(
            "CREATE TABLE orders (id INTEGER, amount DOUBLE, user_id INTEGER, "
            "tenant_id INTEGER, status VARCHAR, created_at TIMESTAMP)"
        )
        conn.exec_driver_sql(
            "INSERT INTO orders VALUES "
            "(1, 10, 1, 42, 'shipped', '2026-08-01 10:00:00'), "
            "(2, 30, 2, 42, 'shipped', '2026-08-02 10:00:00'), "
            "(3, 5, 1, 42, 'pending', '2026-08-03 10:00:00'), "
            "(4, 100, 3, 99, 'shipped', '2026-08-04 10:00:00')"
        )
    return eng


def _app_and_token(eng: object) -> tuple[object, str]:
    orch = QueryOrchestrator(
        RedisCache(fakeredis.FakeAsyncRedis()), SyncEngineExecutor(eng), settings=settings
    )
    app = create_app(orchestrator=orch)
    token = create_token({"sub": "u1", "role": "admin", "tid": "42"}, secret=settings.jwt_secret)
    return app, token


async def test_duckdb_load_aggregates_with_rls(tmp_path) -> None:
    app, token = _app_and_token(_seeded_engine(tmp_path))
    headers = {"Authorization": f"Bearer {token}"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        r = await ac.post(
            "/cubejs-api/v1/load",
            headers=headers,
            json={
                "query": {
                    "measures": ["Orders.revenue", "Orders.count"],
                    "dimensions": ["Orders.status"],
                }
            },
        )
    assert r.status_code == 200, r.text
    by = {row["Orders.status"]: row for row in r.json()["data"]}
    # tenant 42: rows 1+2 shipped (10+30=40, count 2), row 3 pending (5); row 4 (tenant 99) excluded.
    assert by["shipped"]["Orders.revenue"] == 40.0
    assert by["shipped"]["Orders.count"] == 2
    assert by["pending"]["Orders.revenue"] == 5.0


def test_make_engine_and_executor_picks_by_scheme(tmp_path) -> None:
    from cubepy.orchestrator.executor import (
        AsyncEngineExecutor,
        SyncEngineExecutor,
        make_engine_and_executor,
    )

    duck_eng, duck_exe, duck_is_async = make_engine_and_executor(
        f"duckdb:///{tmp_path}/m.duckdb"
    )
    assert duck_is_async is False
    assert isinstance(duck_exe, SyncEngineExecutor)
    duck_eng.dispose()

    pg_eng, pg_exe, pg_is_async = make_engine_and_executor(
        "postgresql+asyncpg://u:p@localhost:5432/x"
    )
    assert pg_is_async is True
    assert isinstance(pg_exe, AsyncEngineExecutor)


async def test_duckdb_window_measure(tmp_path) -> None:
    app, token = _app_and_token(_seeded_engine(tmp_path))
    headers = {"Authorization": f"Bearer {token}"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        r = await ac.post(
            "/cubejs-api/v1/load",
            headers=headers,
            json={
                "query": {
                    "measures": ["Orders.cumulative_revenue"],
                    "timeDimensions": [
                        {
                            "dimension": "Orders.created_at",
                            "granularity": "day",
                            "dateRange": ["2026-08-01", "2026-08-03"],
                        }
                    ],
                    "order": [["Orders.created_at", "asc"]],
                }
            },
        )
    assert r.status_code == 200, r.text
    rows = sorted(r.json()["data"], key=lambda x: x["Orders.created_at"])
    # tenant 42 daily revenue: 08-01=10, 08-02=30, 08-03=5 -> cumulative 10, 40, 45
    assert [row["Orders.cumulative_revenue"] for row in rows] == [10.0, 40.0, 45.0]
