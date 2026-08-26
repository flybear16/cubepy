"""One-shot demo launcher: real FastAPI app + real local Redis + a seeded
testcontainers Postgres, on http://127.0.0.1:8765.

Run:  uv run python run_server.py
Stop: Ctrl+C  (container + engine + redis are torn down).

Ask layer (M2): set CUBEPY_LLM_API_KEY to wire the real OpenAI-compatible
LLM (DeepSeek by default); without a key the demo mounts a deterministic
FakeLLM so /cubepy/v1/ask still answers the seeded sample questions.
"""

from __future__ import annotations

import json
from pathlib import Path

import redis.asyncio as redis_asyncio
import uvicorn
from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import create_async_engine
from testcontainers.community.postgres import PostgresContainer

from cubepy.api.app import create_app
from cubepy.cache.redis_cache import RedisCache
from cubepy.config import settings
from cubepy.llm import FakeLLM, OpenAICompatibleLLM
from cubepy.orchestrator.executor import AsyncEngineExecutor
from cubepy.orchestrator.orchestrator import QueryOrchestrator
from cubepy.samples.orders_schema import register_samples

SEED = Path(__file__).resolve().parent / "cubepy" / "samples" / "seed.sql"
PORT = 8765

_FAKE_RULES = {
    "收入": {"measures": ["Orders.revenue"]},
    "销售额": {"measures": ["Orders.revenue"]},
    "订单数": {"measures": ["Orders.count"]},
    "状态": {
        "measures": ["Orders.revenue", "Orders.count"],
        "dimensions": ["Orders.status"],
    },
}


def _demo_llm() -> FakeLLM | OpenAICompatibleLLM:
    """Real LLM when a key is present; keyword-matching FakeLLM otherwise."""
    if settings.llm_api_key:
        return OpenAICompatibleLLM()
    print("[cubepy] ask layer: no CUBEPY_LLM_API_KEY -> FakeLLM demo mode")

    async def responder(messages: list[dict[str, str]]) -> str:
        question = messages[-1]["content"]
        if "数据解读助手" in messages[0]["content"]:  # LLM#2: interpret
            return "（FakeLLM 演示模式）见下方数据。"
        for keyword, query in _FAKE_RULES.items():  # LLM#1: NL -> query JSON
            if keyword in question:
                return json.dumps(query, ensure_ascii=False)
        return json.dumps(
            {"notAnswerable": True, "reason": "演示模式只认收入/销售额/订单数/状态"},
            ensure_ascii=False,
        )

    return FakeLLM(responder=responder)


def main() -> None:
    import sys

    trade_mode = len(sys.argv) > 1 and sys.argv[1] == "trade"
    n_orders = 0

    container = PostgresContainer("postgres:16-alpine")
    container.start()
    sync_url = container.get_connection_url().replace("+psycopg2", "+psycopg")

    seed_engine = create_engine(sync_url)
    try:
        with seed_engine.begin() as conn:
            if trade_mode:
                # M3 mock 环境：电商交易域（60k 确定性数据，PG 模拟 Hologres 形状）。
                # 术语表切换到 TRADE_GLOSSARY，/cubepy/v1/ask 即可自由问数。
                from cubepy.samples.trade_data import generate_trade_data
                from cubepy.samples.trade_schema import register_trade_schema

                n_orders = generate_trade_data(conn)
                register_trade_schema()
                settings.ask_glossary = "cubepy.samples.glossary_trade.TRADE_GLOSSARY"
            else:
                register_samples()
                conn.exec_driver_sql(SEED.read_text())
    finally:
        seed_engine.dispose()

    async_url = sync_url.replace("+psycopg", "+asyncpg")
    engine = create_async_engine(async_url, pool_pre_ping=True)
    redis_client = redis_asyncio.from_url(settings.redis_url)

    orch = QueryOrchestrator(
        RedisCache(redis_client), AsyncEngineExecutor(engine), settings=settings
    )
    app = create_app(orchestrator=orch, llm=_demo_llm())

    mode = f"trade mock ({n_orders} orders, M3)" if trade_mode else "orders demo"
    print(f"\n[cubepy] Mode     : {mode}")
    print(f"[cubepy] Postgres : {async_url}")
    print(f"[cubepy] Redis    : {settings.redis_url}")
    print("[cubepy] Ask      : POST /cubepy/v1/ask  (FakeLLM demo or CUBEPY_LLM_*)")
    print(f"[cubepy] Listening: http://127.0.0.1:{PORT}  (docs at /docs)\n")

    try:
        uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
    finally:
        import asyncio

        asyncio.run(engine.dispose())
        asyncio.run(redis_client.aclose())
        container.stop()


if __name__ == "__main__":
    main()
