"""P0.3 — metric unit tests: fixture engine + assert_query incl. cross-cube join."""

import pytest

from cubepy.schema.loader import load_cube_file
from cubepy.schema.registry import registry
from cubepy.testing import assert_query, fixture_engine, render_query

duckdb_engine = pytest.importorskip("duckdb_engine")


SCHEMA = """
cubes:
  - name: orders
    sql: SELECT * FROM orders
    measures:
      - {name: total_revenue, sql: amount, type: sum}
      - {name: order_count, type: count}
      - {name: avg_amount, sql: amount, type: avg}
    dimensions:
      - {name: status, sql: status, type: string}
      - {name: id, sql: id, type: number, primaryKey: true}
    joins:
      customers: {relationship: belongsTo, sql: "orders.customer_id = customers.id"}
  - name: customers
    sql: SELECT * FROM customers
    dimensions:
      - {name: id, sql: id, type: number, primaryKey: true}
      - {name: name, sql: name, type: string}
"""

ROWS = {
    "orders": [
        {"id": 1, "customer_id": 10, "amount": 100, "status": "paid"},
        {"id": 2, "customer_id": 10, "amount": 50, "status": "paid"},
        {"id": 3, "customer_id": 20, "amount": 30, "status": "shipped"},
    ],
    "customers": [
        {"id": 10, "name": "alice"},
        {"id": 20, "name": "bob"},
    ],
}


@pytest.fixture(autouse=True)
def _setup():
    registry.clear()
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as td:
        f = Path(td) / "shop.yml"
        f.write_text(SCHEMA, encoding="utf-8")
        load_cube_file(f)
        yield


def test_simple_aggregate():
    eng = fixture_engine(ROWS)
    assert_query(
        {"measures": ["orders.total_revenue"]},
        [{"orders.total_revenue": 180}],
        engine=eng,
    )
    assert_query(
        {"measures": ["orders.order_count"]},
        [{"orders.order_count": 3}],
        engine=eng,
    )


def test_group_by():
    eng = fixture_engine(ROWS)
    assert_query(
        {"measures": ["orders.total_revenue"], "dimensions": ["orders.status"]},
        [
            {"orders.status": "paid", "orders.total_revenue": 150},
            {"orders.status": "shipped", "orders.total_revenue": 30},
        ],
        engine=eng,
    )


def test_cross_cube_join():
    eng = fixture_engine(ROWS)
    assert_query(
        {
            "measures": ["orders.total_revenue"],
            "dimensions": ["customers.name"],
            "order": {"orders.total_revenue": "desc"},
        },
        [
            {"customers.name": "alice", "orders.total_revenue": 150},
            {"customers.name": "bob", "orders.total_revenue": 30},
        ],
        engine=eng,
        sort_by="customers.name",
    )


def test_failure_message_includes_sql():
    eng = fixture_engine(ROWS)
    with pytest.raises(AssertionError) as ei:
        assert_query(
            {"measures": ["orders.total_revenue"]},
            [{"orders.total_revenue": 999}],
            engine=eng,
        )
    msg = str(ei.value)
    assert "SELECT" in msg and "999" in msg


def test_render_query_smoke():
    sql = str(render_query({"measures": ["orders.order_count"]}))
    assert "count(*)" in sql.lower()


def test_fixture_engine_empty_table_raises():
    with pytest.raises(ValueError, match="at least one row"):
        fixture_engine({"t": []})


def test_harness_type_helpers():
    # _json_safe: Decimal / datetime / date -> JSON-safe values; everything else
    # passes through unchanged.
    from datetime import date, datetime
    from decimal import Decimal

    from cubepy import testing as T

    assert T._json_safe(Decimal("1.5")) == 1.5
    assert T._json_safe(datetime(2026, 8, 1, 10, 0)) == "2026-08-01T10:00:00"
    assert T._json_safe(date(2026, 8, 1)) == "2026-08-01"
    assert T._json_safe("x") == "x"

    # _col_type: empty / boolean / int / float / fallback.
    assert T._col_type([]) == "VARCHAR"
    assert T._col_type([True, False]) == "BOOLEAN"
    assert T._col_type([1, 2, 3]) == "BIGINT"
    assert T._col_type([1.5, 2.0]) == "DOUBLE"
    assert T._col_type(["a", "b"]) == "VARCHAR"
