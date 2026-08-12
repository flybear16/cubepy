"""QueryOrchestrator: cache lookup -> pre-agg route -> execute -> cache set.

The cache key is scoped by the security context (tenant/user/role) so that
row-level security is never shared across identities.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from datetime import datetime
from typing import Any

from sqlalchemy import text

from cubepy.cache.redis_cache import RedisCache
from cubepy.config import Settings
from cubepy.orchestrator.envelope import build_envelope
from cubepy.orchestrator.executor import QueryExecutor
from cubepy.orchestrator.preagg import router as preagg_router
from cubepy.security.context import SecurityContext
from cubepy.sqlgen.builder import SQLBuilder
from cubepy.sqlgen.query import Query
from cubepy.sqlgen.rollup import RollupBuilder

logger = logging.getLogger("cubepy.orchestrator")


def make_cache_key(query: Query, ctx: SecurityContext) -> str:
    payload = {
        "query": query.model_dump(mode="json"),
        "tenant_id": ctx.tenant_id,
        "user_id": ctx.user_id,
        "role": ctx.role,
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode()
    ).hexdigest()
    return f"cubepy:q:{digest}"


def _primary_cube_name(query: Query) -> str | None:
    for path in [*query.measures, *query.dimensions]:
        if "." in path:
            return path.split(".", 1)[0]
    for td in query.timeDimensions:
        if "." in td.dimension:
            return td.dimension.split(".", 1)[0]
    return None


class QueryOrchestrator:
    def __init__(
        self,
        cache: RedisCache,
        executor: QueryExecutor,
        *,
        settings: Settings,
    ) -> None:
        self.cache = cache
        self.executor = executor
        self.settings = settings
        # cache_key -> Future, so N concurrent cold identical queries execute once.
        self._inflight: dict[str, asyncio.Future[dict[str, Any]]] = {}

    async def load(
        self,
        query: Query,
        ctx: SecurityContext,
        *,
        now: datetime | None = None,
        use_cache: bool = True,
    ) -> dict[str, Any]:
        key = make_cache_key(query, ctx)
        probe_sig: str | None = None

        if use_cache:
            probe_sig = await self._probe_signature(_primary_cube_name(query))
            cached = await self.cache.get(key)
            if cached is not None:
                if probe_sig is None:
                    logger.debug("cache hit %s", key)
                    return cached
                stored_sig = await self.cache.get(f"{key}:probe")
                if stored_sig == probe_sig:
                    logger.debug("cache hit %s (probe unchanged)", key)
                    return cached
                logger.debug("probe changed for %s; invalidating", key)
                await self.cache.delete(key)

            # Dedup: if an identical cold query is in flight, await its result.
            existing = self._inflight.get(key)
            if existing is not None:
                logger.debug("dedup: awaiting in-flight %s", key)
                return await existing

        # Either non-cached (subscribe) or the first cold caller: execute once.
        fut: asyncio.Future[dict[str, Any]] | None = None
        if use_cache:
            fut = asyncio.get_running_loop().create_future()
            self._inflight[key] = fut

        try:
            envelope = await self._execute(query, ctx, now)
            if use_cache:
                ttl = self._ttl_seconds(query)
                await self.cache.setex(key, ttl, envelope)
                if probe_sig is not None:
                    await self.cache.setex(f"{key}:probe", ttl, probe_sig)
                logger.debug("cached %s for %ss", key, ttl)
            if fut is not None and not fut.done():
                fut.set_result(envelope)
            return envelope
        except BaseException as exc:
            if fut is not None and not fut.done():
                fut.set_exception(exc)
            raise
        finally:
            if fut is not None:
                self._inflight.pop(key, None)

    async def _execute(
        self, query: Query, ctx: SecurityContext, now: datetime | None
    ) -> dict[str, Any]:
        route = preagg_router.match(query, ctx)
        if route is not None and self.settings.preagg_enabled:
            try:
                stmt = RollupBuilder(query, ctx, route, now=now).build()
                # Real engines pin the session to UTC so day-buckets don't drift with
                # the session timezone (plan §5/G1). Test fakes lack this method and
                # fall through to plain execute.
                run_with_session = getattr(self.executor, "execute_with_session", None)
                if run_with_session is not None:
                    rows = await run_with_session(stmt, "SET TIME ZONE 'UTC'")
                else:
                    rows = await self.executor.execute(stmt)
                logger.info("pre-agg routed to %s", route.table_name)
                return build_envelope(
                    query,
                    rows,
                    now=now,
                    used_pre_aggregations=[{"tableName": route.table_name}],
                )
            except Exception:
                # Rollup missing/stale/broken -> transparent fallback to the base
                # cube. BaseException (KeyboardInterrupt/SystemExit) propagates.
                logger.warning(
                    "pre-agg %s failed; falling back to base cube",
                    route.table_name,
                    exc_info=True,
                )
        builder = SQLBuilder(query, ctx, now=now)
        rows = await self.executor.execute(builder.build())
        return build_envelope(query, rows, now=now)

    async def _probe_signature(self, cube_name: str | None) -> str | None:
        """refreshKey.sql probe: a short-TTL signature of source freshness.

        Returns None when the primary cube has no ``refresh_key.sql`` (no probing).
        The probe result is itself cached for ``updateWindow`` seconds so cache
        hits don't each cost a DB round-trip; ``updateWindow <= 0`` forces a fresh
        probe on every check.
        """
        if not cube_name or cube_name not in _registry():
            return None
        cube = _registry().get(cube_name)
        refresh = cube.refresh_key or {}
        probe_sql = refresh.get("sql")
        if not probe_sql:
            return None

        update_window_raw = refresh.get("updateWindow")
        update_window = int(update_window_raw) if update_window_raw is not None else 10

        probe_key = f"cubepy:probe:{cube_name}"
        if update_window > 0:
            cached_sig = await self.cache.get(probe_key)
            if cached_sig is not None:
                return str(cached_sig)

        rows = await self.executor.execute(text(probe_sql))
        sig = hashlib.sha256(
            json.dumps(rows, sort_keys=True, default=str).encode()
        ).hexdigest()
        if update_window > 0:
            await self.cache.setex(probe_key, update_window, sig)
        return sig

    def _ttl_seconds(self, query: Query) -> int:
        # Per-cube refresh_key.every override (seconds), else default TTL.
        every = self._cube_refresh_every(_primary_cube_name(query))
        return every if every is not None else self.settings.cache_ttl_seconds

    def _cube_refresh_every(self, cube_name: str | None) -> int | None:
        if not cube_name:
            return None
        from cubepy.schema.registry import registry

        cube = registry.get(cube_name) if cube_name in registry else None
        refresh = cube.refresh_key if cube else None
        if refresh and refresh.get("every") is not None:
            try:
                return int(refresh["every"])
            except (TypeError, ValueError):
                return None
        return None


def _registry() -> Any:
    # Late import avoids a circular dependency at module load.
    from cubepy.schema.registry import registry

    return registry
