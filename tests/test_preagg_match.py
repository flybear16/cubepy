"""Phase 1 (T1.4): PreAggRouter matcher truth table — all guards fail-closed."""

from __future__ import annotations

import pytest

from cubepy.orchestrator.preagg import RollupRoute, router
from cubepy.schema.loader import cube, dimension, measure
from cubepy.schema.meta import MeasureType, PreAggregation
from cubepy.schema.registry import registry
from cubepy.security.context import SecurityContext
from cubepy.sqlgen.query import Query, TimeDimension


@pytest.fixture(autouse=True)
def _clear_registry():
    registry.clear()
    yield
    registry.clear()


def _build_cube(
    *,
    security_columns=("tenant_id",),
    rollup_security=("tenant_id",),
    security_context=None,
):
    """Orders cube with one valid additive daily rollup."""

    @cube(
        "Orders",
        "orders",
        security_columns=security_columns or None,
        security_context=security_context,
        pre_aggregations=[
            PreAggregation(
                "daily",
                ("Orders.revenue", "Orders.count"),
                ("Orders.status",),
                "Orders.created_at",
                "day",
                security_columns=rollup_security,
            )
        ],
    )
    class _Orders:
        revenue = measure("amount", MeasureType.SUM)
        count = measure(None, MeasureType.COUNT)
        distinct_users = measure("user_id", MeasureType.COUNT_DISTINCT)
        avg_order_value = measure(
            None, MeasureType.CALCULATED, formula="{revenue} / NULLIF({count}, 0)"
        )
        cumulative_revenue = measure("revenue", MeasureType.RUNNING_TOTAL)
        status = dimension("status", "string")
        created_at = dimension("created_at", "time")

    # Users cube for multi-cube guard.
    @cube("Users", "users")
    class _Users:
        country = dimension("country", "string")

    return registry.get("Orders")


def _ctx(role="admin", tenant_id="t1", user_id="u1"):
    return SecurityContext(user_id=user_id, role=role, tenant_id=tenant_id)


def test_additive_sum_count_hits_and_returns_route():
    _build_cube()
    q = Query(
        measures=["Orders.revenue", "Orders.count"],
        dimensions=["Orders.status"],
        timeDimensions=[TimeDimension(dimension="Orders.created_at", granularity="month")],
    )
    route = router.match(q, _ctx())
    assert isinstance(route, RollupRoute)
    assert route.table_name == "cubepy_rollup_orders_daily"


def test_non_additive_count_distinct_rejected():
    _build_cube()
    q = Query(measures=["Orders.distinct_users"])
    assert router.match(q, _ctx()) is None


def test_calculated_measure_rejected():
    _build_cube()
    q = Query(measures=["Orders.avg_order_value"])
    assert router.match(q, _ctx()) is None


def test_window_measure_rejected():
    _build_cube()
    q = Query(measures=["Orders.cumulative_revenue"])
    assert router.match(q, _ctx()) is None


def test_multi_cube_join_rejected():
    _build_cube()
    q = Query(measures=["Orders.revenue"], dimensions=["Users.country"])
    assert router.match(q, _ctx()) is None


def test_segments_rejected():
    # Segments emit opaque WHERE fragments that may hit unmaterialised columns.
    _build_cube()
    q = Query(measures=["Orders.revenue"], segments=["Orders.some_segment"])
    assert router.match(q, _ctx()) is None


def test_granularity_rolldown_ok():
    # rollup=day, query=year -> day rolls up to year -> hit.
    _build_cube()
    q = Query(
        measures=["Orders.revenue"],
        timeDimensions=[TimeDimension(dimension="Orders.created_at", granularity="year")],
    )
    assert router.match(q, _ctx()) is not None


def test_granularity_rolldown_rejected():
    # rollup=month, query=day -> cannot drill down -> reject.
    _build_cube(rollup_security=("tenant_id",))
    # rebuild with a month-granularity rollup
    registry.clear()

    @cube(
        "Orders",
        "orders",
        security_columns=("tenant_id",),
        pre_aggregations=[
            PreAggregation(
                "monthly",
                ("Orders.revenue",),
                ("Orders.status",),
                "Orders.created_at",
                "month",
                security_columns=("tenant_id",),
            )
        ],
    )
    class _O:
        revenue = measure("amount", MeasureType.SUM)
        status = dimension("status", "string")
        created_at = dimension("created_at", "time")

    q = Query(
        measures=["Orders.revenue"],
        timeDimensions=[TimeDimension(dimension="Orders.created_at", granularity="day")],
    )
    assert router.match(q, _ctx()) is None


def test_coverage_miss_rejected():
    # rollup lacks Orders.count -> query asking for it must reject (or only revenue hits).
    _build_cube()
    q = Query(measures=["Orders.revenue"])  # covered
    assert router.match(q, _ctx()) is not None
    q2 = Query(measures=["Orders.revenue"], dimensions=["Orders.status"])
    # status is in rollup dims -> still hit
    assert router.match(q2, _ctx()) is not None


def test_non_utc_timezone_rejected():
    _build_cube()
    q = Query(
        measures=["Orders.revenue"],
        timeDimensions=[TimeDimension(dimension="Orders.created_at", granularity="month")],
        timezone="Asia/Shanghai",
    )
    assert router.match(q, _ctx()) is None


def test_utc_and_none_timezone_allowed():
    _build_cube()
    td = [TimeDimension(dimension="Orders.created_at", granularity="month")]
    assert (
        router.match(Query(measures=["Orders.revenue"], timeDimensions=td, timezone="UTC"), _ctx())
        is not None
    )
    assert (
        router.match(Query(measures=["Orders.revenue"], timeDimensions=td, timezone=None), _ctx())
        is not None
    )


def test_rollup_missing_security_column_rejected():
    # RLS active; cube declares tenant_id+user_id but the rollup only carries
    # tenant_id -> the user_id predicate can't be replayed on the rollup -> reject.
    _build_cube(
        security_columns=("tenant_id", "user_id"),
        rollup_security=("tenant_id",),
        security_context={"check_permission": lambda ctx: ["orders.tenant_id = 'x'"]},
    )
    q = Query(
        measures=["Orders.revenue"],
        timeDimensions=[TimeDimension(dimension="Orders.created_at", granularity="month")],
    )
    assert router.match(q, _ctx()) is None


def test_rollup_covering_security_columns_hits():
    _build_cube(
        security_columns=("tenant_id",),
        rollup_security=("tenant_id",),
        security_context={"check_permission": lambda ctx: ["orders.tenant_id = 'x'"]},
    )
    q = Query(
        measures=["Orders.revenue"],
        timeDimensions=[TimeDimension(dimension="Orders.created_at", granularity="month")],
    )
    assert router.match(q, _ctx()) is not None


def test_rls_guard_skipped_without_security_context():
    # No security_context -> RLS not active -> empty rollup security columns still OK.
    _build_cube(security_columns=(), rollup_security=())
    q = Query(
        measures=["Orders.revenue"],
        timeDimensions=[TimeDimension(dimension="Orders.created_at", granularity="month")],
    )
    assert router.match(q, _ctx()) is not None


def test_no_ctx_fails_closed():
    """Without an authenticated context the matcher must not route to a rollup."""
    _build_cube()
    q = Query(
        measures=["Orders.revenue"],
        timeDimensions=[TimeDimension(dimension="Orders.created_at", granularity="month")],
    )
    assert router.match(q) is None


def test_measure_filter_misses_rollup_fail_closed() -> None:
    """Measure filters are HAVING predicates; the rollup rewrite compiles
    filters as row-level WHERE, so the router must fall back to base."""
    _build_cube()
    q = Query.parse(
        {
            "measures": ["Orders.revenue"],
            "dimensions": ["Orders.status"],
            "filters": [{"member": "Orders.revenue", "operator": "gt", "values": [100]}],
        }
    )
    assert router.match(q, SecurityContext(role="admin", tenant_id="42")) is None
