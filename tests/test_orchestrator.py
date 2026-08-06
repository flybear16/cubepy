"""Unit tests for the query orchestrator (G006). Uses fakeredis + a fake executor."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator

import fakeredis
import pytest
from sqlalchemy.sql.elements import TextClause

from cubepy.cache.redis_cache import RedisCache
from cubepy.config import settings
from cubepy.orchestrator.executor import QueryExecutor
from cubepy.orchestrator.orchestrator import QueryOrchestrator, make_cache_key
from cubepy.schema.loader import cube, dimension, measure
from cubepy.schema.meta import MeasureType
from cubepy.schema.registry import registry
from cubepy.security.context import SecurityContext
from cubepy.sqlgen.query import Query


class FakeExecutor:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self.calls = 0
        self.last_stmt: TextClause | None = None

    async def execute(self, stmt: TextClause) -> list[dict]:
        self.calls += 1
        self.last_stmt = stmt
        return [dict(r) for r in self.rows]


@pytest.fixture(autouse=True)
def _orders() -> Iterator[None]:
    registry.clear()

    @cube("Orders", "orders", refresh_key={"every": 120})
    class _O:
        revenue = measure("amount", MeasureType.SUM)
        count = measure(None, MeasureType.COUNT)
        status = dimension("status", "string")

    yield
    registry.clear()


def _ctx(tenant_id: str = "42", role: str = "admin") -> SecurityContext:
    return SecurityContext(role=role, tenant_id=tenant_id)


def _orch(rows: list[dict]) -> tuple[QueryOrchestrator, FakeExecutor]:
    cache = RedisCache(fakeredis.FakeAsyncRedis())
    exe = FakeExecutor(rows)
    return QueryOrchestrator(cache, exe, settings=settings), exe


async def test_cache_miss_then_hit() -> None:
    orch, exe = _orch([{"Orders.revenue": 100, "Orders.count": 5}])
    query = Query.parse({"measures": ["Orders.revenue", "Orders.count"]})

    r1 = await orch.load(query, _ctx())
    assert r1["data"] == [{"Orders.revenue": 100, "Orders.count": 5}]
    assert exe.calls == 1
    assert r1["annotation"]["measures"]["Orders.revenue"]["type"] == "sum"
    assert r1["usedPreAggregations"] == []

    r2 = await orch.load(query, _ctx())
    assert exe.calls == 1  # served from cache, no new execution
    assert r2["data"] == r1["data"]
    assert r2["lastRefreshTime"] == r1["lastRefreshTime"]


async def test_cache_key_scoped_by_security_context() -> None:
    orch, exe = _orch([{"Orders.revenue": 1}])
    query = Query.parse({"measures": ["Orders.revenue"]})

    await orch.load(query, _ctx(tenant_id="42"))
    await orch.load(query, _ctx(tenant_id="99"))  # different tenant -> RLS re-scope

    assert exe.calls == 2
    assert make_cache_key(query, _ctx(tenant_id="42")) != make_cache_key(
        query, _ctx(tenant_id="99")
    )


async def test_executor_receives_parameterised_stmt() -> None:
    orch, exe = _orch([{"Orders.count": 1}])
    query = Query.parse(
        {
            "measures": ["Orders.count"],
            "filters": [{"member": "Orders.status", "operator": "equals", "values": ["x"]}],
        }
    )
    await orch.load(query, _ctx())
    assert exe.last_stmt is not None
    # The compiled statement carries the bound parameter value.
    rendered = str(exe.last_stmt.compile(compile_kwargs={"literal_binds": True}))
    assert "status = 'x'" in rendered


async def test_executor_protocol_satisfied() -> None:
    # FakeExecutor is a structural QueryExecutor; static check via isinstance-ish usage.
    exe: QueryExecutor = FakeExecutor([])
    assert hasattr(exe, "execute")


class _ProbeExecutor:
    """Returns a probe row when the SQL mentions updated_at, else a data row."""

    def __init__(self) -> None:
        self.probe_val = "v1"
        self.data_calls = 0

    async def execute(self, stmt: TextClause) -> list[dict]:
        if "updated_at" in str(stmt):
            return [{"max": self.probe_val}]
        self.data_calls += 1
        return [{"Orders.revenue": 100.0}]


async def test_refresh_key_sql_probe_invalidates_on_change() -> None:
    registry.clear()

    @cube(
        "Orders",
        "orders",
        refresh_key={"sql": "SELECT MAX(updated_at) FROM orders", "updateWindow": 0},
    )
    class _O:
        revenue = measure("amount", MeasureType.SUM)

    orch = QueryOrchestrator(
        RedisCache(fakeredis.FakeAsyncRedis()), _ProbeExecutor(), settings=settings
    )
    query = Query.parse({"measures": ["Orders.revenue"]})
    ctx = _ctx()

    await orch.load(query, ctx)            # miss -> exec (1), cache + probe sig1
    await orch.load(query, ctx)            # probe unchanged -> cache hit
    assert orch.executor.data_calls == 1  # type: ignore[attr-defined]

    orch.executor.probe_val = "v2"  # type: ignore[attr-defined]  # source data changed
    await orch.load(query, ctx)            # probe changed -> invalidate -> re-exec
    assert orch.executor.data_calls == 2  # type: ignore[attr-defined]


class _SlowExecutor:
    def __init__(self) -> None:
        self.calls = 0

    async def execute(self, stmt: TextClause) -> list[dict]:
        self.calls += 1
        await asyncio.sleep(0.05)  # widen the window so callers overlap
        return [{"Orders.revenue": 100.0}]


async def test_concurrent_identical_queries_dedup_to_one_execution() -> None:
    registry.clear()

    @cube("Orders", "orders")
    class _O:
        revenue = measure("amount", MeasureType.SUM)

    orch = QueryOrchestrator(
        RedisCache(fakeredis.FakeAsyncRedis()), _SlowExecutor(), settings=settings
    )
    query = Query.parse({"measures": ["Orders.revenue"]})
    ctx = _ctx()

    results = await asyncio.gather(*(orch.load(query, ctx) for _ in range(5)))
    assert orch.executor.calls == 1  # type: ignore[attr-defined]
    assert all(r == results[0] for r in results)
