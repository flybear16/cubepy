"""One-shot demo launcher: real FastAPI app + real local Redis + a seeded
testcontainers Postgres, on http://127.0.0.1:8765.

Run:  uv run python run_server.py
Stop: Ctrl+C  (container + engine + redis are torn down).
"""

from __future__ import annotations

from pathlib import Path

import redis.asyncio as redis_asyncio
import uvicorn
from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import create_async_engine
from testcontainers.community.postgres import PostgresContainer

from cubepy.api.app import create_app
from cubepy.cache.redis_cache import RedisCache
from cubepy.config import settings
from cubepy.orchestrator.executor import AsyncEngineExecutor
from cubepy.orchestrator.orchestrator import QueryOrchestrator
from cubepy.samples.orders_schema import register_samples

SEED = Path(__file__).resolve().parent / "cubepy" / "samples" / "seed.sql"
PORT = 8765


def main() -> None:
    register_samples()

    container = PostgresContainer("postgres:16-alpine")
    container.start()
    sync_url = container.get_connection_url().replace("+psycopg2", "+psycopg")

    seed_engine = create_engine(sync_url)
    try:
        with seed_engine.begin() as conn:
            conn.exec_driver_sql(SEED.read_text())
    finally:
        seed_engine.dispose()

    async_url = sync_url.replace("+psycopg", "+asyncpg")
    engine = create_async_engine(async_url, pool_pre_ping=True)
    redis_client = redis_asyncio.from_url(settings.redis_url)

    orch = QueryOrchestrator(
        RedisCache(redis_client), AsyncEngineExecutor(engine), settings=settings
    )
    app = create_app(orchestrator=orch)

    print(f"\n[cubepy] Postgres : {async_url}")
    print(f"[cubepy] Redis    : {settings.redis_url}")
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
