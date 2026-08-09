"""Phase 1 (T1.1 + T1.2): PreAggregation model + CubeMeta fields + loader support."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest
import yaml

from cubepy.schema.loader import cube, dimension, load_cube_file, measure
from cubepy.schema.meta import (
    CubeMeta,
    Dimension,
    DimensionType,
    Measure,
    MeasureType,
    PreAggregation,
)
from cubepy.schema.registry import registry


@pytest.fixture(autouse=True)
def _clear_registry():
    registry.clear()
    yield
    registry.clear()


def test_preaggregation_is_frozen_dataclass():
    pa = PreAggregation(
        name="daily",
        measures=("Orders.revenue",),
        dimensions=("Orders.status",),
        time_dimension="Orders.created_at",
        granularity="day",
        security_columns=("tenant_id",),
    )
    assert pa.name == "daily"
    assert pa.measures == ("Orders.revenue",)
    assert pa.security_columns == ("tenant_id",)
    with pytest.raises(dataclasses.FrozenInstanceError):
        pa.name = "x"  # type: ignore[misc]


def test_cubemeta_defaults_empty_preagg_fields():
    meta = CubeMeta(name="X", sql="x")
    assert meta.pre_aggregations == ()
    assert meta.security_columns == ()


def test_cube_decorator_carries_preagg_and_security_columns():
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
                security_columns=("tenant_id",),
            )
        ],
        security_columns=("tenant_id",),
    )
    class _Orders:
        revenue = measure("amount", MeasureType.SUM)
        status = dimension("status", "string")
        created_at = dimension("created_at", "time")

    meta = registry.get("Orders")
    assert meta.security_columns == ("tenant_id",)
    assert len(meta.pre_aggregations) == 1
    assert meta.pre_aggregations[0].granularity == "day"


def test_yaml_loader_carries_preagg_and_security_columns(tmp_path: Path):
    content = {
        "cubes": [
            {
                "name": "Orders",
                "sql": "orders",
                "measures": [{"name": "revenue", "sql": "amount", "type": "sum"}],
                "dimensions": [
                    {"name": "status", "sql": "status", "type": "string"},
                    {"name": "created_at", "sql": "created_at", "type": "time"},
                ],
                "securityColumns": ["tenant_id"],
                "preAggregations": [
                    {
                        "name": "daily",
                        "measures": ["Orders.revenue"],
                        "dimensions": ["Orders.status"],
                        "timeDimension": "Orders.created_at",
                        "granularity": "day",
                        "securityColumns": ["tenant_id"],
                    }
                ],
            }
        ]
    }
    p = tmp_path / "cubes.yaml"
    p.write_text(yaml.safe_dump(content), encoding="utf-8")

    load_cube_file(p)
    meta = registry.get("Orders")
    assert meta.security_columns == ("tenant_id",)
    assert meta.pre_aggregations[0].time_dimension == "Orders.created_at"
    assert meta.pre_aggregations[0].security_columns == ("tenant_id",)


def test_existing_cube_fields_unchanged():
    """Adding pre_aggregations/security_columns must not disturb existing members."""
    meta = CubeMeta(
        name="Orders",
        sql="orders",
        measures=(Measure(name="revenue", sql="amount", type=MeasureType.SUM),),
        dimensions=(Dimension(name="status", sql="status", type=DimensionType.STRING),),
    )
    assert meta.measure("revenue").type == MeasureType.SUM
    assert meta.pre_aggregations == ()
