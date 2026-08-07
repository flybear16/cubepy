"""Sample semantic schema (Orders + Users) for demos and integration tests.

Register via :func:`register_samples`. Cube names are the table names; member
``sql`` strings are bare columns resolved against the (lowercased) cube alias.
``security_context.check_permission`` injects a tenant_id row filter.
"""

from __future__ import annotations

from cubepy.schema.loader import cube, dimension, measure
from cubepy.schema.meta import MeasureType
from cubepy.schema.registry import registry


def register_samples() -> None:
    @cube(
        "Orders",
        "orders",
        joins={"Users": {"relationship": "belongsTo", "sql": "Orders.user_id = Users.id"}},
        security_context={
            "check_permission": lambda ctx: [f"orders.tenant_id = {ctx.tenant_id}"]
        },
        refresh_key={"every": 60},
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

    @cube(
        "Users",
        "users",
        security_context={
            "check_permission": lambda ctx: [f"users.tenant_id = {ctx.tenant_id}"]
        },
    )
    class _Users:
        country = dimension("country", "string")

    # Touch so linters don't flag the local classes as unused.
    assert "Orders" in registry and "Users" in registry
