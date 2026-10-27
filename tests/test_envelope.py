"""Envelope wire-format tests（#5/#6，2026-09-05 双引擎对拍契约）。

对拍实锤的真 Cube.js 形态：
- 数值 measure → 字符串（"26574.50"/"28"）；float 保留（仅 DuckDB/测试桩）
- 时间维度值 → '%Y-%m-%dT%H:%M:%S.mmm'（UTC 无后缀）；带粒度回双键 dim + dim.<granularity>
- 布尔/字符串维度 → 原生 JSON 类型不动
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from cubepy.orchestrator.envelope import _numeric_wire, _time_wire, build_envelope
from cubepy.schema.registry import registry
from cubepy.sqlgen.query import Query


def setup_function() -> None:
    registry.clear()
    from cubepy.schema.loader import cube, dimension, measure
    from cubepy.schema.meta import MeasureType

    @cube("Orders", "orders")
    class _O:
        gmv = measure("amount", MeasureType.SUM)
        region = dimension("region", "string")
        orderDate = dimension("dt", "time")


# --- _numeric_wire -------------------------------------------------------------


def test_numeric_wire_decimal_and_int_to_str() -> None:
    assert _numeric_wire(Decimal("26574.50")) == "26574.50"
    assert _numeric_wire(Decimal("949.0892857142857143")) == "949.0892857142857143"
    assert _numeric_wire(Decimal("0E-20")) == "0.00000000000000000000"  # 定点展开，禁科学计数法
    assert _numeric_wire(28) == "28"
    assert _numeric_wire(None) is None
    assert _numeric_wire(True) is True  # bool 是 int 子类，先排除


def test_numeric_wire_float_passes_through() -> None:
    # float 只出现在 DuckDB/测试桩；产线 numeric 走 Decimal
    assert _numeric_wire(40.0) == 40.0


# --- _time_wire ----------------------------------------------------------------


def test_time_wire_formats_datetime_utc() -> None:
    dt = datetime(2026, 6, 1, 0, 0, 0, tzinfo=UTC)
    assert _time_wire(dt) == "2026-06-01T00:00:00.000"


def test_time_wire_converts_offset_to_utc() -> None:
    dt = datetime(2026, 6, 1, 16, 0, 0, tzinfo=UTC) - timedelta(hours=8)  # 非零偏移时区→UTC 折算
    assert _time_wire(dt) == "2026-06-01T08:00:00.000"


def test_time_wire_date_and_string() -> None:
    from datetime import date

    assert _time_wire(date(2026, 6, 30)) == "2026-06-30T00:00:00.000"
    assert _time_wire("2026-06-01 00:00:00") == "2026-06-01T00:00:00.000"
    assert _time_wire("not-a-date") == "not-a-date"  # 容错原样


# --- build_envelope rows（集成）--------------------------------------------------


def test_envelope_time_dimension_dual_keys_and_numeric_str() -> None:
    q = Query.parse(
        {
            "measures": ["Orders.gmv"],
            "timeDimensions": [{"dimension": "Orders.orderDate", "granularity": "month"}],
        }
    )
    rows = [{"Orders.gmv": Decimal("26574.50"), "Orders.orderDate": datetime(2026, 6, 1)}]
    env = build_envelope(q, rows)
    assert env["data"] == [
        {
            "Orders.gmv": "26574.50",
            "Orders.orderDate": "2026-06-01T00:00:00.000",
            "Orders.orderDate.month": "2026-06-01T00:00:00.000",
        }
    ]


def test_envelope_time_dimension_without_granularity_single_key() -> None:
    q = Query.parse({"measures": ["Orders.gmv"], "dimensions": ["Orders.orderDate"]})
    rows = [{"Orders.gmv": Decimal("1.00"), "Orders.orderDate": datetime(2026, 6, 30)}]
    env = build_envelope(q, rows)
    assert env["data"] == [{"Orders.gmv": "1.00", "Orders.orderDate": "2026-06-30T00:00:00.000"}]


def test_envelope_leaves_plain_dimensions_alone() -> None:
    q = Query.parse({"measures": ["Orders.gmv"], "dimensions": ["Orders.region"]})
    rows = [{"Orders.gmv": Decimal("1.00"), "Orders.region": "华东"}]
    assert build_envelope(q, rows)["data"] == [{"Orders.gmv": "1.00", "Orders.region": "华东"}]
