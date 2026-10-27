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
from cubepy.security.permissions import require_claims, sql_str


def _orders_rls(ctx) -> list[str]:
    """租户 RLS：fail-closed（require_claims 缺 claim 即 400），别名用小写 cube 名。"""
    require_claims(ctx, "tenant_id")
    return [f"dwdorders.tenant_id = {sql_str(ctx.tenant_id)}"]


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
            # Fail-closed：租户 claim 缺失/为空直接拒绝（400），
            # 绝不注入空串让 SQL 执行期才炸成 500。
            "check_permission": lambda ctx: _orders_rls(ctx),
        },
        security_columns=("tenant_id",),
    )
    class _DwdOrders:
        gmv = measure("gmv", MeasureType.SUM, title="成交总额(GMV)")
        pay_amount = measure("pay_amount", MeasureType.SUM, title="支付金额")
        refund_amount = measure("refund_amount", MeasureType.SUM, title="退款金额")
        order_cnt = measure(None, MeasureType.COUNT, title="订单数")
        aov = measure(
            None, MeasureType.CALCULATED, formula="{gmv} / NULLIF({order_cnt}, 0)",
            title="客单价",
        )
        region = dimension("region", "string", title="地区")
        channel = dimension("channel", "string", title="渠道")
        category = dimension("category", "string", title="品类")
        status = dimension("status", "string", title="订单状态")
        is_new = dimension("is_new", "number", title="是否新客")
        created_at = dimension("created_at", "time", title="下单时间")

    @cube("DimCustomer", "dim_customer")
    class _DimCustomer:
        id = dimension("id", "number", primary_key=True)
        level = dimension("level", "string")

    @cube("DimProduct", "dim_product")
    class _DimProduct:
        id = dimension("id", "number", primary_key=True)
        brand = dimension("brand", "string")

    assert "DwdOrders" in registry and "DimProduct" in registry
