"""Unit tests for the schema layer (G003)."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from cubepy.schema.loader import (
    cube,
    dimension,
    load_cube_file,
    measure,
    segment,
)
from cubepy.schema.meta import DimensionType, MeasureType, RelationshipType
from cubepy.schema.registry import registry


@pytest.fixture(autouse=True)
def _isolate_registry() -> Iterator[None]:
    registry.clear()
    yield
    registry.clear()


def test_decorator_extracts_members_in_order() -> None:
    @cube("Orders", "SELECT * FROM orders")
    class Orders:
        revenue = measure("amount", MeasureType.SUM)
        count = measure(None, MeasureType.COUNT)
        status = dimension("status", "string")
        created_at = dimension("created_at", "time")

    meta = registry.get("Orders")
    assert [m.name for m in meta.measures] == ["revenue", "count"]
    assert [d.name for d in meta.dimensions] == ["status", "created_at"]
    assert meta.measure("revenue").type is MeasureType.SUM
    assert meta.measure("count").sql is None
    assert meta.dimension("created_at").type is DimensionType.TIME


def test_joins_and_segments() -> None:
    @cube(
        "Orders",
        "SELECT * FROM orders",
        joins={"Users": {"relationship": "belongsTo", "sql": "Orders.user_id = Users.id"}},
    )
    class Orders:
        count = measure(None, MeasureType.COUNT)
        active = segment("status = 'active'")

    meta = registry.get("Orders")
    join = meta.joins["Users"]
    assert join.relationship is RelationshipType.BELONGS_TO
    assert join.sql == "Orders.user_id = Users.id"
    assert meta.segment("active").sql == "status = 'active'"


def test_shown_callback_and_security_context() -> None:
    @cube(
        "Orders",
        "SELECT * FROM orders",
        security_context={
            "check_permission": lambda ctx: [f"Orders.tenant_id = {ctx['tenant_id']}"]
        },
    )
    class Orders:
        revenue = measure("amount", MeasureType.SUM, shown=lambda ctx: ctx["role"] == "admin")
        public = measure("amount", MeasureType.SUM)

    meta = registry.get("Orders")
    admin = {"role": "admin", "tenant_id": 42}
    viewer = {"role": "viewer", "tenant_id": 7}
    assert meta.measure("revenue").shown(admin) is True
    assert meta.measure("revenue").shown(viewer) is False
    assert meta.measure("public").shown is None
    assert meta.security_context(admin) == ["Orders.tenant_id = 42"]


def test_measure_type_accepts_str_and_rejects_unknown() -> None:
    assert measure("x", "sum").type is MeasureType.SUM
    with pytest.raises(ValueError):
        measure("x", "bogus")


def test_yaml_loader(tmp_path) -> None:
    f = tmp_path / "cubes.yaml"
    f.write_text(
        """
cubes:
  - name: Orders
    sql: SELECT * FROM orders
    joins:
      Users: {relationship: belongsTo, sql: "Orders.user_id = Users.id"}
    measures:
      - {name: revenue, sql: amount, type: sum}
      - {name: count, type: count}
    dimensions:
      - {name: status, sql: status, type: string}
    segments:
      - {name: active, sql: "status = 'active'"}
"""
    )
    metas = load_cube_file(f)
    assert len(metas) == 1
    meta = registry.get("Orders")
    assert meta.measure("count").sql is None
    assert meta.measure("revenue").type is MeasureType.SUM
    assert meta.dimension("status").type is DimensionType.STRING
    assert meta.joins["Users"].relationship is RelationshipType.BELONGS_TO


def test_registry_missing_raises_keyerror() -> None:
    with pytest.raises(KeyError):
        registry.get("DoesNotExist")


def test_duplicate_name_overwrites() -> None:
    @cube("Orders", "SELECT * FROM orders1")
    class _O1:
        count = measure(None, MeasureType.COUNT)

    @cube("Orders", "SELECT * FROM orders2")
    class _O2:
        count = measure(None, MeasureType.COUNT)

    assert registry.get("Orders").sql == "SELECT * FROM orders2"


def test_resolve_member_all_kinds() -> None:
    from cubepy.schema.registry import resolve_member

    registry.clear()

    @cube("Orders", "orders")
    class _O:
        c = measure(None, MeasureType.COUNT)
        status = dimension("status", "string")
        active = segment("status = 'active'")

    assert resolve_member("Orders.c")[1] == "measure"
    assert resolve_member("Orders.status")[1] == "dimension"
    assert resolve_member("Orders.active")[1] == "segment"
    with pytest.raises(KeyError):
        resolve_member("Orders.nope")


def test_yaml_bad_join_spec_raises(tmp_path) -> None:
    f = tmp_path / "c.yaml"
    f.write_text(
        "cubes:\n  - name: Orders\n    sql: orders\n    joins:\n        Users: 123\n"
        "    measures: [{name: c, type: count}]"
    )
    with pytest.raises(TypeError):
        load_cube_file(f)


def test_load_cube_dir_counts_files(tmp_path) -> None:
    from cubepy.schema.loader import load_cube_dir

    (tmp_path / "a.yaml").write_text(
        "cubes:\n  - {name: A, sql: a, measures: [{name: c, type: count}]}"
    )
    (tmp_path / "b.yml").write_text(
        "cubes:\n  - {name: B, sql: b, measures: [{name: c, type: count}]}"
    )
    assert load_cube_dir(tmp_path) == 2


def test_missing_member_lookup_raises_keyerror() -> None:
    @cube("Orders", "orders")
    class _O:
        c = measure(None, MeasureType.COUNT)
        status = dimension("status", "string")
        active = segment("status = 'active'")

    meta = registry.get("Orders")
    with pytest.raises(KeyError):
        meta.dimension("nope")
    with pytest.raises(KeyError):
        meta.segment("nope")


def test_registry_names() -> None:
    @cube("Orders", "orders")
    class _O:
        c = measure(None, MeasureType.COUNT)

    @cube("Users", "users")
    class _U:
        c = measure(None, MeasureType.COUNT)

    assert set(registry.names()) == {"Orders", "Users"}


def test_join_instance_accepted_in_decorator() -> None:
    from cubepy.schema.meta import Join

    @cube(
        "Orders",
        "orders",
        joins={
            "Users": Join(
                relationship=RelationshipType.BELONGS_TO,
                sql="Orders.user_id = Users.id",
            )
        },
    )
    class _O:
        c = measure(None, MeasureType.COUNT)

    join = registry.get("Orders").joins["Users"]
    assert join.relationship is RelationshipType.BELONGS_TO
    assert join.sql == "Orders.user_id = Users.id"
