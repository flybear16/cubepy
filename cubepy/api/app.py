"""FastAPI application factory.

Wires the async DB engine, Redis cache, and QueryOrchestrator on startup and
mounts the REST router under ``/cubejs-api``. WebSocket (G008) and GraphQL
(G009) routers are added by their modules.

Tests inject an ``orchestrator`` directly to bypass the real engine/Redis.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import redis.asyncio as redis_asyncio
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncEngine

from cubepy.api.graphql import build_graphql_router
from cubepy.api.routes_rest import router as rest_router
from cubepy.api.routes_ws import router as ws_router
from cubepy.cache.redis_cache import RedisCache
from cubepy.config import settings
from cubepy.orchestrator.executor import make_engine_and_executor
from cubepy.orchestrator.orchestrator import QueryOrchestrator
from cubepy.scheduler import PreAggScheduler


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    engine: AsyncEngine | Engine | None = None
    is_async_engine = True
    redis_client: redis_asyncio.Redis | None = None
    scheduler: PreAggScheduler | None = None
    if app.state.orchestrator is None:
        dsn = settings.db_dsn or settings.pg_dsn
        engine, executor, is_async_engine = make_engine_and_executor(dsn)
        redis_client = redis_asyncio.from_url(settings.redis_url)
        app.state.orchestrator = QueryOrchestrator(
            RedisCache(redis_client),
            executor,
            settings=settings,
        )
        app.state.engine = engine
        app.state.redis = redis_client
        if settings.preagg_enabled:
            scheduler = PreAggScheduler(
                executor, default_every=settings.default_refresh_every
            )
            await scheduler.start()
            app.state.scheduler = scheduler
    yield
    if scheduler is not None:
        scheduler.shutdown()
    if engine is not None:
        if is_async_engine:
            await engine.dispose()  # type: ignore[func-returns-value]
        else:
            engine.dispose()
    if redis_client is not None:
        await redis_client.aclose()


def create_app(orchestrator: QueryOrchestrator | None = None) -> FastAPI:
    app = FastAPI(title="CubePy API", version="0.1.0", lifespan=lifespan)
    app.state.orchestrator = orchestrator

    @app.get("/readyz")
    async def readyz() -> JSONResponse:
        components: dict[str, str] = {}

        engine: AsyncEngine | None = getattr(app.state, "engine", None)
        if engine is not None:
            try:
                async with engine.connect() as conn:
                    await conn.execute(text("SELECT 1"))
                components["db"] = "up"
            except Exception:  # noqa: BLE001 — any failure means down
                components["db"] = "down"
        else:
            components["db"] = "unknown"

        redis_client: redis_asyncio.Redis | None = getattr(app.state, "redis", None)
        if redis_client is not None:
            try:
                await redis_client.ping()
                components["redis"] = "up"
            except Exception:  # noqa: BLE001
                components["redis"] = "down"
        else:
            components["redis"] = "unknown"

        healthy = all(v != "down" for v in components.values())
        return JSONResponse(
            {"status": "ok" if healthy else "degraded", "components": components},
            status_code=200 if healthy else 503,
        )

    app.include_router(rest_router, prefix="/cubejs-api")
    app.include_router(ws_router, prefix="/cubejs-api")
    app.include_router(build_graphql_router(), prefix="/cubejs-api/graphql")

    from cubepy.api.routes_platform import router as platform_router
    app.include_router(platform_router, prefix="/cubepy")
    return app


app = create_app()
