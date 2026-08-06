"""REST API integration tests (G007). Uses an injected orchestrator (fake executor + fakeredis)."""

from __future__ import annotations

from collections.abc import Iterator

import fakeredis
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.sql.elements import TextClause

from cubepy.api.app import create_app
from cubepy.cache.redis_cache import RedisCache
from cubepy.config import settings
from cubepy.orchestrator.orchestrator import QueryOrchestrator
from cubepy.schema.loader import cube, dimension, measure
from cubepy.schema.meta import MeasureType
from cubepy.schema.registry import registry
from cubepy.security.context import create_token


class _FakeExecutor:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows

    async def execute(self, stmt: TextClause) -> list[dict]:
        return [dict(r) for r in self.rows]


@pytest.fixture(autouse=True)
def _orders() -> Iterator[None]:
    registry.clear()

    @cube("Orders", "orders")
    class _O:
        revenue = measure("amount", MeasureType.SUM)
        count = measure(None, MeasureType.COUNT)
        secret = measure("amount", MeasureType.SUM, shown=lambda ctx: ctx.role == "admin")
        status = dimension("status", "string")

    yield
    registry.clear()


def _client(rows: list[dict]) -> AsyncClient:
    orch = QueryOrchestrator(
        RedisCache(fakeredis.FakeAsyncRedis()), _FakeExecutor(rows), settings=settings
    )
    app = create_app(orchestrator=orch)
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _auth(role: str = "admin") -> dict[str, str]:
    token = create_token({"sub": "u1", "role": role}, secret=settings.jwt_secret)
    return {"Authorization": f"Bearer {token}"}


async def test_readyz_open() -> None:
    async with _client([]) as ac:
        r = await ac.get("/readyz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


async def test_load_requires_auth() -> None:
    async with _client([]) as ac:
        assert (await ac.post("/cubejs-api/v1/load", json={"query": {}})).status_code == 401


async def test_load_returns_envelope() -> None:
    async with _client([{"Orders.revenue": 40.0, "Orders.count": 2, "Orders.status": "shipped"}]) as ac:
        r = await ac.post(
            "/cubejs-api/v1/load",
            headers=_auth(),
            json={"query": {"measures": ["Orders.revenue"], "dimensions": ["Orders.status"]}},
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["data"][0]["Orders.revenue"] == 40.0
    assert body["annotation"]["measures"]["Orders.revenue"]["type"] == "sum"
    assert body["usedPreAggregations"] == []
    assert "lastRefreshTime" in body


async def test_load_multi() -> None:
    async with _client([{"Orders.revenue": 1.0}]) as ac:
        r = await ac.post(
            "/cubejs-api/v1/load",
            headers=_auth(),
            json={
                "queryType": "multi",
                "query": [{"measures": ["Orders.revenue"]}, {"measures": ["Orders.count"]}],
            },
        )
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert len(data) == 2
    assert data[0]["data"] == [{"Orders.revenue": 1.0}]


async def test_sql_endpoint() -> None:
    async with _client([]) as ac:
        r = await ac.post(
            "/cubejs-api/v1/sql",
            headers=_auth(),
            json={"query": {"measures": ["Orders.revenue"]}},
        )
    assert r.status_code == 200
    sql = r.json()["sql"][0]["sql"]
    assert "sum(amount)" in sql
    assert 'AS "Orders.revenue"' in sql


async def test_meta_lists_visible_members() -> None:
    async with _client([]) as ac:
        r = await ac.get("/cubejs-api/v1/meta", headers=_auth(role="viewer"))
    assert r.status_code == 200
    cubes = {c["name"]: c for c in r.json()["cubes"]}
    assert "Orders" in cubes
    names = {m["name"] for m in cubes["Orders"]["measures"]}
    assert "revenue" in names
    assert "secret" not in names  # viewer cannot see the admin-only measure


async def test_hidden_member_returns_400() -> None:
    async with _client([]) as ac:
        r = await ac.post(
            "/cubejs-api/v1/load",
            headers=_auth(role="viewer"),
            json={"query": {"measures": ["Orders.secret"]}},
        )
    assert r.status_code == 400


async def test_bad_query_returns_400() -> None:
    async with _client([]) as ac:
        r = await ac.post(
            "/cubejs-api/v1/load",
            headers=_auth(),
            json={"query": {"measures": ["Orders.nonexistent"]}},
        )
    assert r.status_code == 400
