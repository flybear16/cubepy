"""Hardcoded business glossary for the sample Orders domain (M2 POC).

Maps business phrases to precise member paths + 口径. Injected into the ask
layer's system prompt (F-E1.3: term hallucination defense). M3 replaces this
with a per-domain configurable glossary.
"""

SAMPLE_GLOSSARY: dict[str, str] = {
    "收入": "Orders.revenue（订单金额 SUM）",
    "销售额": 'Orders.revenue（同"收入"）',
    "订单数": "Orders.count（订单条数 COUNT）",
    "客单价": "Orders.revenue / Orders.count",
    "已发货": 'Orders.status equals "shipped"',
    "待支付": 'Orders.status equals "pending"',
    "大额订单": "Orders.amount gt 50",
}
