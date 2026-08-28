"""End-to-end integration tests against a real Postgres (G010).

Requires either ``$CUBEPY_TEST_PG_DSN`` or Docker (for testcontainers); skipped
otherwise. Verifies the generated SQL actually executes on Postgres and returns
correct aggregates, with row-level security enforced.
"""

from __future__ import annotations

from typing import Any

import fakeredis
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from cubepy.api.app import create_app
from cubepy.cache.redis_cache import RedisCache
from cubepy.config import settings
from cubepy.orchestrator.executor import AsyncEngineExecutor
from cubepy.orchestrator.orchestrator import QueryOrchestrator
from cubepy.samples.orders_schema import register_samples
from cubepy.schema.registry import registry
from cubepy.security.context import create_token

pytestmark = pytest.mark.integration


def _token(tenant_id: int, role: str = "admin") -> str:
    return create_token(
        {"sub": "u1", "role": role, "tid": str(tenant_id)}, secret=settings.jwt_secret
    )


def _auth(tenant_id: int = 42) -> dict[str, str]:
    return {"Authorization": f"Bearer {_token(tenant_id)}"}


@pytest_asyncio.fixture
async def client(pg_dsn: str) -> Any:
    registry.clear()
    register_samples()
    engine = create_async_engine(pg_dsn)
    orch = QueryOrchestrator(
        RedisCache(fakeredis.FakeAsyncRedis()),
        AsyncEngineExecutor(engine),
        settings=settings,
    )
    app = create_app(orchestrator=orch)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac, orch
    await engine.dispose()


async def _rows_by(ac: AsyncClient, key: str, body: dict, tenant: int = 42) -> dict:
    r = await ac.post("/cubejs-api/v1/load", headers=_auth(tenant), json={"query": body})
    assert r.status_code == 200, r.text
    return {row[key]: row for row in r.json()["data"]}


async def test_load_status_aggregate_with_rls(
    client: tuple[AsyncClient, QueryOrchestrator],
) -> None:
    ac, _ = client
    by_status = await _rows_by(
        ac,
        "Orders.status",
        {"measures": ["Orders.revenue", "Orders.count"], "dimensions": ["Orders.status"]},
    )
    # tenant 42 -> rows 1,2 (shipped 10+30) and 3 (pending 5); tenant-99 row excluded.
    assert by_status["shipped"]["Orders.revenue"] == 40.0
    assert by_status["shipped"]["Orders.count"] == 2
    assert by_status["pending"]["Orders.revenue"] == 5.0
    assert by_status["pending"]["Orders.count"] == 1


async def test_load_time_dimension_range(client: tuple[AsyncClient, QueryOrchestrator]) -> None:
    ac, _ = client
    by_day = await _rows_by(
        ac,
        "Orders.created_at",
        {
            "measures": ["Orders.revenue"],
            "timeDimensions": [
                {
                    "dimension": "Orders.created_at",
                    "granularity": "day",
                    "dateRange": ["2026-08-01", "2026-08-02"],
                }
            ],
        },
    )
    # Two distinct days (2026-08-01 and 2026-08-02), tz format is DB-dependent.
    assert len(by_day) == 2
    assert all(k.startswith(("2026-08-01", "2026-08-02")) for k in by_day)
    assert sum(r["Orders.revenue"] for r in by_day.values()) == 40.0


async def test_load_join_users_country(client: tuple[AsyncClient, QueryOrchestrator]) -> None:
    ac, _ = client
    by_country = await _rows_by(
        ac,
        "Users.country",
        {"measures": ["Orders.revenue"], "dimensions": ["Users.country"]},
    )
    assert by_country["CN"]["Orders.revenue"] == 15.0  # rows 1 (10) + 3 (5)
    assert by_country["JP"]["Orders.revenue"] == 30.0  # row 2


async def test_load_calculated_measure(client: tuple[AsyncClient, QueryOrchestrator]) -> None:
    ac, _ = client
    by_status = await _rows_by(
        ac,
        "Orders.status",
        {"measures": ["Orders.avg_order_value"], "dimensions": ["Orders.status"]},
    )
    assert by_status["shipped"]["Orders.avg_order_value"] == 20.0  # 40 / 2
    assert by_status["pending"]["Orders.avg_order_value"] == 5.0  # 5 / 1


async def test_load_window_measure_cumulative(
    client: tuple[AsyncClient, QueryOrchestrator],
) -> None:
    ac, _ = client
    by_day = await _rows_by(
        ac,
        "Orders.created_at",
        {
            "measures": ["Orders.cumulative_revenue"],
            "timeDimensions": [
                {
                    "dimension": "Orders.created_at",
                    "granularity": "day",
                    "dateRange": ["2026-08-01", "2026-08-03"],
                }
            ],
            "order": [["Orders.created_at", "asc"]],
        },
    )
    # tenant 42 daily revenue: 08-01=10, 08-02=30, 08-03=5 -> cumulative 10, 40, 45
    rows = sorted(by_day.values(), key=lambda r: r["Orders.created_at"])
    assert [r["Orders.cumulative_revenue"] for r in rows] == [10.0, 40.0, 45.0]


async def test_rls_excludes_other_tenant(client: tuple[AsyncClient, QueryOrchestrator]) -> None:
    ac, _ = client
    by_status = await _rows_by(
        ac,
        "Orders.status",
        {"measures": ["Orders.revenue"], "dimensions": ["Orders.status"]},
        tenant=99,
    )
    assert by_status["shipped"]["Orders.revenue"] == 100.0  # only row 4


async def test_meta_and_sql(client: tuple[AsyncClient, QueryOrchestrator]) -> None:
    ac, _ = client
    meta = (await ac.get("/cubejs-api/v1/meta", headers=_auth())).json()
    assert {"Orders", "Users"} <= {c["name"] for c in meta["cubes"]}

    sql = (
        await ac.post(
            "/cubejs-api/v1/sql",
            headers=_auth(),
            json={"query": {"measures": ["Orders.revenue"]}},
        )
    ).json()["sql"][0]["sql"]
    assert "sum(amount)" in sql and 'AS "Orders.revenue"' in sql


async def test_graphql_against_postgres(client: tuple[AsyncClient, QueryOrchestrator]) -> None:
    ac, _ = client
    gql = '{ load(query: {measures: ["Orders.revenue"], dimensions: ["Orders.status"]}) { data } }'
    r = await ac.post(
        "/cubejs-api/graphql",
        headers={**_auth(), "Content-Type": "application/json"},
        json={"query": gql},
    )
    assert r.status_code == 200, r.text
    data = r.json()["data"]["load"]["data"]
    statuses = {row["Orders.status"] for row in data}
    assert statuses == {"shipped", "pending"}


async def test_load_compare_date_range(client: tuple[AsyncClient, QueryOrchestrator]) -> None:
    ac, _ = client
    r = await ac.post(
        "/cubejs-api/v1/load",
        headers=_auth(),
        json={
            "queryType": "compareDateRange",
            "query": {
                "measures": ["Orders.revenue"],
                "timeDimensions": [
                    {
                        "dimension": "Orders.created_at",
                        "granularity": "day",
                        "dateRange": [["2026-08-01", "2026-08-01"], ["2026-08-02", "2026-08-02"]],
                    }
                ],
            },
        },
    )
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert len(data) == 2  # one envelope per range
    totals = [sum(row["Orders.revenue"] for row in env["data"]) for env in data]
    assert totals == [10.0, 30.0]  # row1 / row2 (tenant 42)


async def test_graphql_dynamic_per_cube(client: tuple[AsyncClient, QueryOrchestrator]) -> None:
    ac, _ = client
    r = await ac.post(
        "/cubejs-api/graphql",
        headers={**_auth(), "Content-Type": "application/json"},
        json={"query": "{ orders { revenue status } }"},
    )
    assert r.status_code == 200, r.text
    by = {row["status"]: row["revenue"] for row in r.json()["data"]["orders"]}
    assert by["shipped"] == 40.0
    assert by["pending"] == 5.0


async def test_orchestrator_detects_db_change(
    client: tuple[AsyncClient, QueryOrchestrator], pg_dsn: str
) -> None:
    from cubepy.security.context import SecurityContext
    from cubepy.sqlgen.query import Query

    _ac, orch = client
    query_body = {"measures": ["Orders.revenue"], "dimensions": ["Orders.status"]}
    ctx = SecurityContext(role="admin", tenant_id="42", user_id="u1")

    before = await orch.load(Query.parse(query_body), ctx, use_cache=False)
    before_total = sum(r["Orders.revenue"] for r in before["data"])

    # Insert a new tenant-42 order via a fresh connection.
    engine = create_async_engine(pg_dsn)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO orders (id, user_id, amount, status, created_at, tenant_id) "
                    "VALUES (99, 1, 7.00, 'shipped', '2026-08-05 10:00:00', 42)"
                )
            )
    finally:
        await engine.dispose()

    after = await orch.load(Query.parse(query_body), ctx, use_cache=False)
    after_total = sum(r["Orders.revenue"] for r in after["data"])
    assert after_total == before_total + 7.0


async def test_load_measure_filter_having(
    client: tuple[AsyncClient, QueryOrchestrator], pg_reseed: str
) -> None:
    """docs/06 §2 measureFilter: a filter on a measure is an aggregate
    predicate (HAVING) — pending (revenue 5) drops, shipped (40) stays.

    ``pg_reseed``: an earlier test inserts an order without restoring it."""
    ac, _ = client
    by_status = await _rows_by(
        ac,
        "Orders.status",
        {
            "measures": ["Orders.revenue", "Orders.count"],
            "dimensions": ["Orders.status"],
            "filters": [{"member": "Orders.revenue", "operator": "gt", "values": [20]}],
        },
    )
    assert set(by_status) == {"shipped"}
    assert by_status["shipped"]["Orders.revenue"] == 40.0
    assert by_status["shipped"]["Orders.count"] == 2
