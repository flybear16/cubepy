"""Phase 1 (T1.3): load-time validator rejects declared-but-unsatisfiable rollups."""

from __future__ import annotations

import pytest

from cubepy.schema.loader import cube, dimension, measure
from cubepy.schema.meta import MeasureType, PreAggregation
from cubepy.schema.registry import registry
from cubepy.schema.validators import SchemaError


@pytest.fixture(autouse=True)
def _clear_registry():
    registry.clear()
    yield
    registry.clear()


def _orders(
    *,
    pa: PreAggregation | None = None,
    security_columns: tuple[str, ...] = (),
    extra_pa: tuple[PreAggregation, ...] = (),
    security_context=None,
):
    paggs = list(extra_pa) + ([pa] if pa else [])
    deco = cube(
        "Orders",
        "orders",
        pre_aggregations=paggs or None,
        security_columns=security_columns or None,
        security_context=security_context,
    )

    @deco
    class _Orders:
        revenue = measure("amount", MeasureType.SUM)
        cnt = measure(None, MeasureType.COUNT)
        avg_val = measure("amount", MeasureType.AVG)
        status = dimension("status", "string")
        created_at = dimension("created_at", "time")

    return registry.get("Orders")


def _daily(**kw) -> PreAggregation:
    base = dict(
        name="daily",
        measures=("Orders.revenue",),
        dimensions=("Orders.status",),
        time_dimension="Orders.created_at",
        granularity="day",
    )
    base.update(kw)
    return PreAggregation(**base)


def test_valid_rollup_passes():
    meta = _orders(pa=_daily())  # no exception
    assert meta.pre_aggregations[0].name == "daily"


def test_rejects_unknown_measure():
    with pytest.raises(SchemaError, match="measure"):
        _orders(pa=_daily(measures=("Orders.nope",)))


def test_rejects_unknown_dimension():
    with pytest.raises(SchemaError, match="dimension"):
        _orders(pa=_daily(dimensions=("Orders.nope",)))


def test_rejects_time_dimension_not_on_cube():
    with pytest.raises(SchemaError, match="time"):
        _orders(pa=_daily(time_dimension="Orders.not_a_dim"))


def test_rejects_non_additive_measure():
    # AVG is not re-aggregatable from a rollup -> reject at load.
    with pytest.raises(SchemaError, match="additive"):
        _orders(pa=_daily(measures=("Orders.avg_val",)))


def test_rejects_duplicate_rollup_name():
    with pytest.raises(SchemaError, match="name"):
        _orders(pa=_daily(), extra_pa=(_daily(),))


def test_rejects_security_context_without_security_columns():
    with pytest.raises(SchemaError, match="security_columns"):
        _orders(
            pa=_daily(security_columns=("tenant_id",)),
            security_context={"check_permission": lambda ctx: ["orders.tenant_id = 'x'"]},
            security_columns=(),  # declared callback but no security_columns
        )


def test_allows_security_context_with_declared_security_columns():
    meta = _orders(
        pa=_daily(security_columns=("tenant_id",)),
        security_context={"check_permission": lambda ctx: ["orders.tenant_id = 'x'"]},
        security_columns=("tenant_id",),
    )
    assert meta.security_columns == ("tenant_id",)
