"""Mock e-commerce trade domain (M3 pilot 素材).

数仓风格的三表：事实表 ``dwd_orders``（RLS 按 tenant 隔离）+ 共享维表
``dim_customer`` / ``dim_product``（无租户列，跨租户共享的参考数据，
与真实 Hologres 数仓一致）。数据由 ``.omc/scripts/m3_pilot.py`` 用
generate_series 确定性灌入（同参数必得同数据）。

注册：``register_trade_schema()``；术语表：``samples/glossary_trade.py``。
"""

from __future__ import annotations

from cubepy.schema.loader import cube, dimension, measure
from cubepy.schema.meta import MeasureType
from cubepy.schema.registry import registry
from cubepy.security.permissions import sql_str


def register_trade_schema() -> None:
    @cube(
        "DwdOrders",
        "dwd_orders",
        joins={
            "DimCustomer": {
                "relationship": "belongsTo",
                "sql": "DwdOrders.customer_id = DimCustomer.id",
            },
            "DimProduct": {
                "relationship": "belongsTo",
                "sql": "DwdOrders.product_id = DimProduct.id",
            },
        },
        security_context={
            # RLS strings are injected raw into WHERE — reference the table
            # ALIAS (= lowercased cube name), not the physical table name.
            "check_permission": lambda ctx: [
                f"dwdorders.tenant_id = {sql_str(ctx.tenant_id)}"
            ]
        },
        security_columns=("tenant_id",),
    )
    class _DwdOrders:
        gmv = measure("gmv", MeasureType.SUM)
        pay_amount = measure("pay_amount", MeasureType.SUM)
        refund_amount = measure("refund_amount", MeasureType.SUM)
        order_cnt = measure(None, MeasureType.COUNT)
        aov = measure(
            None, MeasureType.CALCULATED, formula="{gmv} / NULLIF({order_cnt}, 0)"
        )
        region = dimension("region", "string")
        channel = dimension("channel", "string")
        category = dimension("category", "string")
        status = dimension("status", "string")
        is_new = dimension("is_new", "number")
        created_at = dimension("created_at", "time")

    @cube("DimCustomer", "dim_customer")
    class _DimCustomer:
        id = dimension("id", "number", primary_key=True)
        level = dimension("level", "string")

    @cube("DimProduct", "dim_product")
    class _DimProduct:
        id = dimension("id", "number", primary_key=True)
        brand = dimension("brand", "string")

    assert "DwdOrders" in registry and "DimProduct" in registry
