"""GraphQL integration tests (G009)."""

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
from cubepy.schema.loader import cube, measure
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

    yield
    registry.clear()


def _client(rows: list[dict]) -> AsyncClient:
    orch = QueryOrchestrator(
        RedisCache(fakeredis.FakeAsyncRedis()), _FakeExecutor(rows), settings=settings
    )
    app = create_app(orchestrator=orch)
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _auth() -> dict[str, str]:
    token = create_token({"sub": "u1", "role": "admin"}, secret=settings.jwt_secret)
    return {"Authorization": f"Bearer {token}"}


_QUERY = """
query Load($q: JSON!) {
  load(query: $q) {
    data
    lastRefreshTime
  }
}
"""


async def test_graphql_load_returns_typed_rows() -> None:
    async with _client([{"Orders.revenue": 99.0}]) as ac:
        r = await ac.post(
            "/cubejs-api/graphql",
            headers={**_auth(), "Content-Type": "application/json"},
            json={"query": _QUERY, "variables": {"q": {"measures": ["Orders.revenue"]}}},
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["data"]["load"]["data"] == [{"Orders.revenue": 99.0}]
    assert body["data"]["load"]["lastRefreshTime"]


async def test_graphql_requires_auth() -> None:
    async with _client([]) as ac:
        r = await ac.post(
            "/cubejs-api/graphql",
            json={"query": _QUERY, "variables": {"q": {"measures": ["Orders.revenue"]}}},
        )
    assert r.status_code in (401, 400)


async def test_graphql_dynamic_per_cube_field() -> None:
    async with _client([{"Orders.revenue": 7.0}]) as ac:
        r = await ac.post(
            "/cubejs-api/graphql",
            headers={**_auth(), "Content-Type": "application/json"},
            json={"query": "{ orders(limit: 2) { revenue } }"},
        )
    assert r.status_code == 200, r.text
    assert r.json()["data"]["orders"] == [{"revenue": 7.0}]
