"""Phase 3 (T3.2): PreAggScheduler — build-all on start, one interval job per rollup."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine

from cubepy.orchestrator.executor import SyncEngineExecutor
from cubepy.scheduler import PreAggScheduler
from cubepy.schema.loader import cube, dimension, measure
from cubepy.schema.meta import MeasureType, PreAggregation
from cubepy.schema.registry import registry

pytest.importorskip("duckdb_engine")


@pytest.fixture(autouse=True)
def _clean() -> Iterator[None]:
    registry.clear()
    yield
    registry.clear()


async def test_build_all_creates_every_rollup(tmp_path) -> None:
    eng = create_engine(f"duckdb:///{tmp_path}/sched.duckdb")
    with eng.begin() as conn:
        conn.exec_driver_sql(
            "CREATE TABLE orders (id INTEGER, amount DOUBLE, status VARCHAR, "
            "created_at TIMESTAMP)"
        )
        conn.exec_driver_sql(
            "INSERT INTO orders VALUES (1, 10, 'shipped', '2026-08-01 10:00:00')"
        )

    @cube(
        "Orders",
        "orders",
        pre_aggregations=[
            PreAggregation(
                "daily",
                ("Orders.revenue",),
                ("Orders.status",),
                "Orders.created_at",
                "day",
            )
        ],
    )
    class _O:
        revenue = measure("amount", MeasureType.SUM)
        status = dimension("status", "string")
        created_at = dimension("created_at", "time")

    sched = PreAggScheduler(SyncEngineExecutor(eng))
    built = await sched.build_all()
    assert built == ["cubepy_rollup_orders_daily"]
    with eng.connect() as conn:
        n = conn.exec_driver_sql("SELECT COUNT(*) FROM cubepy_rollup_orders_daily").scalar()
        assert n == 1
    sched.shutdown()


async def test_start_schedules_one_job_per_rollup_with_refresh_every() -> None:
    @cube(
        "Orders",
        "orders",
        pre_aggregations=[
            PreAggregation(
                "daily", ("Orders.revenue",), ("Orders.status",), "Orders.created_at", "day",
                refresh_key={"every": 120},
            ),
            PreAggregation(
                "hourly", ("Orders.revenue",), (), "Orders.created_at", "hour",
            ),
        ],
    )
    class _O:
        revenue = measure("amount", MeasureType.SUM)
        status = dimension("status", "string")
        created_at = dimension("created_at", "time")

    # Fake executor: build_on_start=False means build is never called, so a bare
    # object suffices — we only assert job registration here.
    sched = PreAggScheduler(object())  # type: ignore[arg-type]
    await sched.start(build_on_start=False)
    try:
        assert set(sched._tasks) == {"Orders.daily", "Orders.hourly"}
        # Declared refresh_key.every honoured; missing every falls back to default.
        assert sched._intervals["Orders.daily"] == 120
        assert sched._intervals["Orders.hourly"] == sched._default_every
    finally:
        sched.shutdown()


async def test_start_with_no_rollups_is_a_noop() -> None:
    sched = PreAggScheduler(object())  # type: ignore[arg-type]
    await sched.start(build_on_start=False)  # must not raise
    sched.shutdown()


async def test_refresh_loop_ticks_and_recovers_after_failure(monkeypatch) -> None:
    """The per-rollup sleep loop refreshes on every tick and logs+retries failures."""
    import asyncio

    import cubepy.scheduler as sched_mod

    calls: list[str] = []

    class _Stub:
        async def build(self, cube, pa):
            calls.append(pa.name)
            if len(calls) == 1:
                raise RuntimeError("boom")

    @cube(
        "Orders",
        "orders",
        pre_aggregations=[
            PreAggregation("daily", ("Orders.revenue",), (), "Orders.created_at", "day")
        ],
    )
    class _O:
        revenue = measure("amount", MeasureType.SUM)
        created_at = dimension("created_at", "time")

    real_sleep = asyncio.sleep

    async def _fast_sleep(_seconds: float) -> None:
        await real_sleep(0)  # yield without waiting the real interval

    monkeypatch.setattr(sched_mod.asyncio, "sleep", _fast_sleep)

    sched = PreAggScheduler(object())  # type: ignore[arg-type]
    sched._service = _Stub()
    cube_meta = registry.get("Orders")
    pa = cube_meta.pre_aggregations[0]

    task = asyncio.create_task(sched._loop(cube_meta, pa))
    for _ in range(500):
        if len(calls) >= 2:  # tick 1 failed, tick 2 succeeded
            break
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert len(calls) >= 2  # the failure was logged, the loop kept going
