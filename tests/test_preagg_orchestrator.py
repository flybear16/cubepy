"""Phase 2 (T2.2): orchestrator routes to a rollup when enabled, and falls back
transparently on rollup failure (table missing / broken / KeyboardInterrupt)."""

from __future__ import annotations

from collections.abc import Iterator

import fakeredis
import pytest
from sqlalchemy.sql.elements import TextClause

from cubepy.cache.redis_cache import RedisCache
from cubepy.config import Settings
from cubepy.orchestrator.orchestrator import QueryOrchestrator
from cubepy.schema.loader import cube, dimension, measure
from cubepy.schema.meta import MeasureType, PreAggregation
from cubepy.schema.registry import registry
from cubepy.security.context import SecurityContext
from cubepy.sqlgen.query import Query


class _RecordingExecutor:
    """Records the executed statement; execute_with_session can raise on demand."""

    def __init__(self, rows: list[dict], *, session_exc: BaseException | None = None) -> None:
        self.rows = rows
        self.last_stmt: TextClause | None = None
        self.session_sql: str | None = None
        self.session_exc = session_exc
        self.exec_calls = 0

    async def execute(self, stmt: TextClause) -> list[dict]:
        self.exec_calls += 1
        self.last_stmt = stmt
        return [dict(r) for r in self.rows]

    async def execute_with_session(
        self, stmt: TextClause, session_sql: str | None = None
    ) -> list[dict]:
        if self.session_exc is not None:
            raise self.session_exc
        self.last_stmt = stmt
        self.session_sql = session_sql
        return [dict(r) for r in self.rows]


@pytest.fixture(autouse=True)
def _orders_with_rollup() -> Iterator[None]:
    registry.clear()

    @cube(
        "Orders",
        "orders",
        pre_aggregations=[
            PreAggregation(
                "daily",
                ("Orders.revenue", "Orders.count"),
                ("Orders.status",),
                "Orders.created_at",
                "day",
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


def _ctx() -> SecurityContext:
    return SecurityContext(role="admin", tenant_id="42")


def _orch(rows: list[dict], *, session_exc: BaseException | None = None, enabled: bool = True):
    exe = _RecordingExecutor(rows, session_exc=session_exc)
    orch = QueryOrchestrator(
        RedisCache(fakeredis.FakeAsyncRedis()), exe, settings=Settings(preagg_enabled=enabled)
    )
    return orch, exe


_QUERY = {"measures": ["Orders.revenue"], "dimensions": ["Orders.status"]}


async def test_enabled_routes_to_rollup_and_sets_utc_session() -> None:
    orch, exe = _orch([{"Orders.revenue": 99.0}])
    env = await orch.load(Query.parse(_QUERY), _ctx())
    assert exe.last_stmt is not None
    assert "cubepy_rollup_orders_daily" in str(exe.last_stmt)
    assert exe.session_sql == "SET TIME ZONE 'UTC'"
    assert env["usedPreAggregations"] == [{"tableName": "cubepy_rollup_orders_daily"}]
    assert env["data"] == [{"Orders.revenue": 99.0}]


async def test_rollup_failure_falls_back_to_base() -> None:
    # Rollup execute raises (e.g. table not built) -> orchestrator falls back to the
    # base cube, empties usedPreAggregations, and never raises.
    orch, exe = _orch(
        [{"Orders.revenue": 7.0}], session_exc=RuntimeError("rollup table missing")
    )
    env = await orch.load(Query.parse(_QUERY), _ctx())
    assert exe.exec_calls == 1  # base path executed exactly once
    assert exe.last_stmt is not None
    assert "cubepy_rollup_orders_daily" not in str(exe.last_stmt)
    assert env["usedPreAggregations"] == []
    assert env["data"] == [{"Orders.revenue": 7.0}]


async def test_keyboard_interrupt_not_swallowed() -> None:
    orch, _ = _orch([], session_exc=KeyboardInterrupt())
    with pytest.raises(KeyboardInterrupt):
        await orch.load(Query.parse(_QUERY), _ctx())


async def test_disabled_never_routes_to_rollup() -> None:
    orch, exe = _orch([{"Orders.revenue": 1.0}], enabled=False)
    env = await orch.load(Query.parse(_QUERY), _ctx())
    assert exe.session_sql is None  # execute_with_session never called
    assert exe.exec_calls == 1
    assert env["usedPreAggregations"] == []


class _PlainExecutor:
    """No execute_with_session: exercises the orchestrator's plain-execute path."""

    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self.last_stmt: TextClause | None = None

    async def execute(self, stmt: TextClause) -> list[dict]:
        self.last_stmt = stmt
        return [dict(r) for r in self.rows]


async def test_enabled_routes_to_rollup_with_plain_executor() -> None:
    exe = _PlainExecutor([{"Orders.revenue": 5.0}])
    orch = QueryOrchestrator(
        RedisCache(fakeredis.FakeAsyncRedis()),
        exe,
        settings=Settings(preagg_enabled=True),
    )
    env = await orch.load(Query.parse(_QUERY), _ctx())
    assert "cubepy_rollup_orders_daily" in str(exe.last_stmt)
    assert env["usedPreAggregations"] == [{"tableName": "cubepy_rollup_orders_daily"}]
    assert env["data"] == [{"Orders.revenue": 5.0}]
