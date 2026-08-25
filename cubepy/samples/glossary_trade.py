"""E-commerce trade glossary (M3 pilot mock 素材).

业务口径 → 精确 member 映射。通过 ``CUBEPY_ASK_GLOSSARY=cubepy.samples.
glossary_trade.TRADE_GLOSSARY`` 注入 ask 层（M2 时承诺的 config lift）。
真实业务域到位后按同结构替换即可。
"""

TRADE_GLOSSARY: dict[str, str] = {
    "GMV": "DwdOrders.gmv（下单金额 SUM，含未支付）",
    "支付金额": "DwdOrders.pay_amount（实际支付 SUM）",
    "客单价": "DwdOrders.aov（GMV / 订单数）",
    "订单数": "DwdOrders.order_cnt（订单条数 COUNT）",
    "退款金额": "DwdOrders.refund_amount（仅退款状态订单的支付额）",
    "新客": 'DwdOrders.is_new equals 1（首单用户）',
    "老客": "DwdOrders.is_new equals 0",
    "APP渠道": 'DwdOrders.channel equals "app"',
    "小程序渠道": 'DwdOrders.channel equals "miniapp"',
    "品牌": "DimProduct.brand（订单关联商品的品牌）",
    # 维度值归一（M3 pilot 实测发现：用户说"华东区"，库里是"华东"——
    # 术语表就是低基数字典典值归一的第一道防线）
    "华东区": 'DwdOrders.region equals "华东"',
    "华南区": 'DwdOrders.region equals "华南"',
    "华北区": 'DwdOrders.region equals "华北"',
}
