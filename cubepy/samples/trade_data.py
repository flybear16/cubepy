"""Deterministic mock data for the trade domain (M3 素材).

Same parameters -> same data, every time (``generate_series`` arithmetic, no
random). Shared by the M3 pilot runner (``.omc/scripts/m3_pilot.py``) and
``run_server.py trade`` mode, so the batch acceptance set and the interactive
demo always talk to an identical database.

数据 mock 定案（2026-08-26）：真实业务数据暂不可得，mock 即 M3 验收口径。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import sqlalchemy as sa

_SEED_SQL = Path(__file__).with_name("seed_trade.sql")


def _statements(sql_text: str) -> list[str]:
    """Same comment-stripping + split as tests/conftest.py."""
    stripped = "\n".join(line.split("--", 1)[0] for line in sql_text.splitlines())
    return [s for s in (part.strip() for part in stripped.split(";")) if s]


def generate_trade_data(
    conn: sa.Connection,
    n_orders: int = 60_000,
    n_customers: int = 5_000,
    n_products: int = 300,
    start: date = date(2026, 1, 1),
) -> int:
    """Create the trade tables and load deterministic mock rows.

    Dates span ``start`` .. today so relative ranges ("近30天") are never empty.
    Returns the number of orders loaded.
    """
    days = (date.today() - start).days + 1
    for stmt in _statements(_SEED_SQL.read_text()):
        conn.exec_driver_sql(stmt)
    conn.exec_driver_sql(f"""
        INSERT INTO dim_customer
        SELECT g, (ARRAY['普通','银卡','金卡','黑卡'])[1 + g %% 4]
        FROM generate_series(1, {n_customers}) g
    """)
    conn.exec_driver_sql(f"""
        INSERT INTO dim_product
        SELECT g, '品牌' || (1 + g %% 30) FROM generate_series(1, {n_products}) g
    """)
    conn.exec_driver_sql(f"""
        INSERT INTO dwd_orders
        SELECT g,
               1 + g %% {n_customers},
               1 + g %% {n_products},
               ROUND((g %% 900 + 20)::numeric / 10, 2) + ROUND((g %% 50)::numeric / 10, 2),
               ROUND((g %% 900 + 20)::numeric / 10, 2),
               CASE WHEN (1 + g %% 4) = 4
                    THEN ROUND((g %% 900 + 20)::numeric / 10, 2) ELSE 0 END,
               (ARRAY['paid','shipped','completed','refunded'])[1 + g %% 4],
               (ARRAY['华东','华北','华南','西南','东北','西北'])[1 + g %% 6],
               (ARRAY['app','miniapp','web','offline'])[1 + g %% 4],
               (ARRAY['女装','男装','数码','家居','美妆'])[1 + g %% 5],
               CASE WHEN g %% 3 = 0 THEN 1 ELSE 0 END,
               TIMESTAMP '2026-01-01' + (g %% {days}) * INTERVAL '1 day'
                   + (g %% 86400) * INTERVAL '1 second',
               CASE WHEN (1 + g %% {n_customers}) %% 20 = 0 THEN 99 ELSE 42 END
        FROM generate_series(1, {n_orders}) g
    """)
    return int(conn.execute(sa.text("SELECT count(*) FROM dwd_orders")).scalar() or 0)
