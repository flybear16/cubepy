"""Pre-agg hardening: edge-case branches not exercised by the phase-1/2/3 suites.

Targets the fail-closed guards in ``PreAggRouter``, the filter / visibility /
tail paths in ``RollupBuilder``, the failure-isolation + refresh-every paths in
``PreAggScheduler``, and the filtered-measure CTAS path in ``RollupBuilderService``.

The defence-in-depth cases (rollup listing a measure the loader would reject) register
a raw ``CubeMeta`` directly to bypass load-time validation — that is the only way to
reach the matcher's additivity / not-found branches, which exist precisely to guard a
rollup registered without validation.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine

from cubepy.orchestrator.executor import SyncEngineExecutor
from cubepy.orchestrator.preagg import PreAggRouter, RollupRoute, router
from cubepy.orchestrator.rollup_builder import RollupBuilderService
from cubepy.scheduler import PreAggScheduler
from cubepy.schema.loader import cube, dimension, measure
from cubepy.schema.meta import (
    CubeMeta,
    Dimension,
    DimensionType,
    Measure,
    MeasureType,
    PreAggregation,
)
from cubepy.schema.registry import registry
from cubepy.security.context import SecurityContext
from cubepy.sqlgen.query import Query, TimeDimension
from cubepy.sqlgen.rollup import RollupBuilder

NOW = datetime(2026, 8, 15, 12, 0, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _clear_registry():
    registry.clear()
    yield
    registry.clear()


def _ctx(role="admin", tenant_id="t1", user_id="u1"):
    return SecurityContext(user_id=user_id, role=role, tenant_id=tenant_id)


def _route(cube="Orders"):
    return RollupRoute(
        table_name=f"cubepy_rollup_{cube.lower()}_daily",
        cube=cube,
        measures=("Orders.revenue",),
        dimensions=("Orders.status",),
        time_dimension="Orders.created_at",
        granularity="day",
        security_columns=(),
    )


def _daily_cube(*, status_shown=None, cube_shown=None):
    """Orders with one additive daily rollup over (revenue, status, created_at)."""

    @cube(
        "Orders",
        "orders",
        shown=cube_shown,
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
        status = dimension("status", "string", shown=status_shown)
        created_at = dimension("created_at", "time")

    return registry.get("Orders")


# --------------------------------------------------------------------------- #
# PreAggRouter: fail-closed guards (preagg.py missing branches)
# --------------------------------------------------------------------------- #
def test_rollup_without_time_bucketing_skipped():
    # pa.time_dimension / granularity both None -> "rollup has no time bucketing".
    @cube("Orders", "orders", pre_aggregations=[PreAggregation("notime", ("Orders.revenue",))])
    class _O:
        revenue = measure("amount", MeasureType.SUM)
        status = dimension("status", "string")
        created_at = dimension("created_at", "time")

    q = Query(
        measures=["Orders.revenue"],
        timeDimensions=[TimeDimension(dimension="Orders.created_at", granularity="month")],
    )
    assert router.match(q, _ctx()) is None


def test_time_dimension_mismatch_rejected():
    # Rollup bucketed on created_at; query groups a different time dim.
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
        updated_at = dimension("updated_at", "time")

    q = Query(
        measures=["Orders.revenue"],
        timeDimensions=[TimeDimension(dimension="Orders.updated_at", granularity="month")],
    )
    assert router.match(q, _ctx()) is None


def test_raw_timestamp_precision_rejected():
    _daily_cube()
    q = Query(
        measures=["Orders.revenue"],
        timeDimensions=[TimeDimension(dimension="Orders.created_at", granularity=None)],
    )
    assert router.match(q, _ctx()) is None


def test_query_dimension_not_covered_rejected():
    # region is a real dimension but not materialised in the rollup.
    @cube(
        "Orders",
        "orders",
        pre_aggregations=[
            PreAggregation("daily", ("Orders.revenue",), ("Orders.status",), "Orders.created_at", "day")
        ],
    )
    class _O:
        revenue = measure("amount", MeasureType.SUM)
        status = dimension("status", "string")
        region = dimension("region", "string")
        created_at = dimension("created_at", "time")

    q = Query(measures=["Orders.revenue"], dimensions=["Orders.region"])
    assert router.match(q, _ctx()) is None


def test_rolls_up_lattice_edges():
    # _rolls_up with a None query granularity (all-time) -> True.
    assert PreAggRouter._rolls_up("day", None) is True
    # Unknown query granularity -> False.
    assert PreAggRouter._rolls_up("day", "fortnight") is False
    # Unknown rollup granularity -> False.
    assert PreAggRouter._rolls_up("eon", "day") is False
    # Equal rank -> True.
    assert PreAggRouter._rolls_up("day", "day") is True


def test_rollup_measure_not_on_cube_rejected():
    # Defence-in-depth: rollup lists a measure the cube doesn't define. Only
    # reachable by bypassing load-time validation (raw CubeMeta).
    meta = CubeMeta(
        name="Orders",
        sql="orders",
        measures=(Measure(name="revenue", sql="amount", type=MeasureType.SUM),),
        dimensions=(Dimension(name="created_at", sql="created_at", type=DimensionType.TIME),),
        pre_aggregations=(
            PreAggregation("daily", ("Orders.ghost",), (), "Orders.created_at", "day"),
        ),
    )
    registry.register(meta)
    q = Query(
        measures=["Orders.ghost"],
        timeDimensions=[TimeDimension(dimension="Orders.created_at", granularity="month")],
    )
    assert router.match(q, _ctx()) is None


def test_rollup_non_additive_measure_rejected():
    # Defence-in-depth: an AVG measure snuck into a rollup (loader would reject).
    meta = CubeMeta(
        name="Orders",
        sql="orders",
        measures=(
            Measure(name="avg_x", sql="amount", type=MeasureType.AVG),
            Measure(name="revenue", sql="amount", type=MeasureType.SUM),
        ),
        dimensions=(Dimension(name="created_at", sql="created_at", type=DimensionType.TIME),),
        pre_aggregations=(
            PreAggregation("daily", ("Orders.avg_x",), (), "Orders.created_at", "day"),
        ),
    )
    registry.register(meta)
    q = Query(
        measures=["Orders.avg_x"],
        timeDimensions=[TimeDimension(dimension="Orders.created_at", granularity="month")],
    )
    assert router.match(q, _ctx()) is None


# --------------------------------------------------------------------------- #
# RollupBuilder: visibility / filter / tail branches (rollup.py missing)
# --------------------------------------------------------------------------- #
def test_rollup_render_literal():
    _daily_cube()
    q = Query(measures=["Orders.revenue"], dimensions=["Orders.status"])
    sql = RollupBuilder(q, _ctx(), _route(), now=NOW).render_literal()
    assert "cubepy_rollup_orders_daily" in sql
    assert '"Orders.revenue"' in sql


def test_rollup_hidden_dimension_fails_closed():
    _daily_cube(status_shown=lambda ctx: False)
    q = Query(measures=["Orders.revenue"], dimensions=["Orders.status"])
    with pytest.raises(ValueError):
        RollupBuilder(q, _ctx(), _route()).build()


def test_rollup_cube_not_visible_fails_closed():
    _daily_cube(cube_shown=lambda ctx: False)
    q = Query(measures=["Orders.revenue"])
    with pytest.raises(ValueError):
        RollupBuilder(q, _ctx(), _route()).build()


def test_rollup_filter_or_branch():
    _daily_cube()
    q = Query(
        measures=["Orders.revenue"],
        dimensions=["Orders.status"],
        filters=[
            {
                "or": [
                    {"member": "Orders.status", "operator": "equals", "values": ["shipped"]},
                    {"member": "Orders.status", "operator": "equals", "values": ["pending"]},
                ]
            }
        ],
    )
    sql = RollupBuilder(q, _ctx(), _route(), now=NOW).render_literal()
    assert " OR " in sql


def test_rollup_filter_and_branch():
    _daily_cube()
    q = Query(
        measures=["Orders.revenue"],
        dimensions=["Orders.status"],
        filters=[
            {
                "and": [
                    {"member": "Orders.status", "operator": "equals", "values": ["shipped"]},
                    {"member": "Orders.status", "operator": "notEquals", "values": ["x"]},
                ]
            }
        ],
    )
    sql = RollupBuilder(q, _ctx(), _route(), now=NOW).render_literal()
    assert " AND " in sql


def test_rollup_filter_missing_member_rejected():
    _daily_cube()
    q = Query(
        measures=["Orders.revenue"],
        dimensions=["Orders.status"],
        filters=[{"member": None, "operator": None, "values": []}],
    )
    with pytest.raises(ValueError):
        RollupBuilder(q, _ctx(), _route()).build()


def test_rollup_filter_unknown_operator_rejected():
    _daily_cube()
    q = Query(
        measures=["Orders.revenue"],
        dimensions=["Orders.status"],
        filters=[{"member": "Orders.status", "operator": "wibble", "values": ["x"]}],
    )
    with pytest.raises(ValueError):
        RollupBuilder(q, _ctx(), _route()).build()


def test_rollup_filter_in_date_range():
    _daily_cube()
    q = Query(
        measures=["Orders.revenue"],
        dimensions=["Orders.status"],
        filters=[
            {
                "member": "Orders.created_at",
                "operator": "inDateRange",
                "values": ["2026-08-01", "2026-08-31"],
            }
        ],
    )
    sql = RollupBuilder(q, _ctx(), _route(), now=NOW).render_literal()
    assert ">=" in sql and "<=" in sql


def test_rollup_filter_before_after_date():
    _daily_cube()
    q = Query(
        measures=["Orders.revenue"],
        dimensions=["Orders.status"],
        filters=[{"member": "Orders.created_at", "operator": "beforeDate", "values": ["2026-08-15"]}],
    )
    sql = RollupBuilder(q, _ctx(), _route(), now=NOW).render_literal()
    # beforeDate renders a strict < bound against the resolved UTC start-of-day.
    assert "created_at <" in sql and "2026-08-15" in sql


def test_rollup_tail_order_limit_offset():
    _daily_cube()
    q = Query(
        measures=["Orders.revenue"],
        dimensions=["Orders.status"],
        order=[["Orders.revenue", "desc"]],
        limit=10,
        offset=5,
    )
    sql = RollupBuilder(q, _ctx(), _route(), now=NOW).render_literal()
    assert "ORDER BY" in sql and "LIMIT 10" in sql and "OFFSET 5" in sql


# --------------------------------------------------------------------------- #
# PreAggScheduler: failure isolation + build_on_start + refresh-every fallback
# --------------------------------------------------------------------------- #
class _BrokenExecutor:
    async def execute_with_session(self, stmt, session_sql=None):
        raise RuntimeError("boom")


async def test_build_all_isolates_single_rollup_failure():
    @cube(
        "Orders",
        "orders",
        pre_aggregations=[
            PreAggregation("daily", ("Orders.revenue",), ("Orders.status",), "Orders.created_at", "day")
        ],
    )
    class _O:
        revenue = measure("amount", MeasureType.SUM)
        status = dimension("status", "string")
        created_at = dimension("created_at", "time")

    sched = PreAggScheduler(_BrokenExecutor())
    built = await sched.build_all()  # one rollup raises -> logged, skipped, not re-raised
    assert built == []
    sched.shutdown()


async def test_start_builds_on_start(tmp_path):
    eng = create_engine(f"duckdb:///{tmp_path}/edge_start.duckdb")
    with eng.begin() as conn:
        conn.exec_driver_sql(
            "CREATE TABLE orders (id INTEGER, amount DOUBLE, status VARCHAR, created_at TIMESTAMP)"
        )
        conn.exec_driver_sql("INSERT INTO orders VALUES (1, 10, 'shipped', '2026-08-01 10:00:00')")

    @cube(
        "Orders",
        "orders",
        pre_aggregations=[
            PreAggregation("daily", ("Orders.revenue",), ("Orders.status",), "Orders.created_at", "day")
        ],
    )
    class _O:
        revenue = measure("amount", MeasureType.SUM)
        status = dimension("status", "string")
        created_at = dimension("created_at", "time")

    sched = PreAggScheduler(SyncEngineExecutor(eng))
    await sched.start(build_on_start=True)
    try:
        with eng.connect() as conn:
            assert conn.exec_driver_sql("SELECT COUNT(*) FROM cubepy_rollup_orders_daily").scalar() == 1
        assert sched._sched.running
    finally:
        sched.shutdown()


async def test_refresh_every_invalid_value_falls_back_to_default():
    @cube(
        "Orders",
        "orders",
        pre_aggregations=[
            PreAggregation(
                "daily", ("Orders.revenue",), (), "Orders.created_at", "day",
                refresh_key={"every": "not-a-number"},
            )
        ],
    )
    class _O:
        revenue = measure("amount", MeasureType.SUM)
        created_at = dimension("created_at", "time")

    sched = PreAggScheduler(object(), default_every=42)  # type: ignore[arg-type]
    await sched.start(build_on_start=False)
    try:
        jobs = {j.id: j for j in sched._sched.get_jobs()}
        assert jobs["Orders.daily"].trigger.interval.total_seconds() == 42
    finally:
        sched.shutdown()


# --------------------------------------------------------------------------- #
# RollupBuilderService: filtered-measure CTAS literal-binds path
# --------------------------------------------------------------------------- #
async def test_build_with_filtered_measure_renders_literal_binds(tmp_path):
    eng = create_engine(f"duckdb:///{tmp_path}/edge_filt.duckdb")
    with eng.begin() as conn:
        conn.exec_driver_sql(
            "CREATE TABLE orders (id INTEGER, amount DOUBLE, status VARCHAR, created_at TIMESTAMP)"
        )
        conn.exec_driver_sql("INSERT INTO orders VALUES (1, 10, 'shipped', '2026-08-01 10:00:00')")

    @cube(
        "Orders",
        "orders",
        pre_aggregations=[
            PreAggregation(
                "daily", ("Orders.filtered_revenue",), ("Orders.status",), "Orders.created_at", "day"
            )
        ],
    )
    class _O:
        filtered_revenue = measure(
            "amount",
            MeasureType.SUM,
            filters=({"member": "Orders.status", "operator": "equals", "values": ["shipped"]},),
        )
        status = dimension("status", "string")
        created_at = dimension("created_at", "time")

    table = await RollupBuilderService(SyncEngineExecutor(eng)).build(
        registry.get("Orders"), registry.get("Orders").pre_aggregations[0]
    )
    assert table == "cubepy_rollup_orders_daily"
    with eng.connect() as conn:
        # The single shipped row passes the filter -> revenue 10.
        row = conn.exec_driver_sql(
            'SELECT "filtered_revenue" FROM cubepy_rollup_orders_daily'
        ).one()
        assert row[0] == 10.0
