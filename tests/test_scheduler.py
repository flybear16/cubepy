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
