"""Scheduled refresh of declared pre-aggregations (plan §6).

One asyncio task per rollup: optionally build every rollup once on start, then
refresh each on its ``refresh_key.every`` interval (falling back to
``default_refresh_every``). No scheduler dependency — a plain sleep loop is
enough for a handful of rollups.
"""

from __future__ import annotations

import asyncio
import logging

from cubepy.orchestrator.executor import QueryExecutor
from cubepy.orchestrator.rollup_builder import RollupBuilderService
from cubepy.schema.meta import CubeMeta, PreAggregation
from cubepy.schema.registry import registry

logger = logging.getLogger("cubepy.scheduler")


def _declared_rollups() -> list[tuple[CubeMeta, PreAggregation]]:
    """Every (cube, pre_aggregation) pair in the registry."""
    return [(cube, pa) for cube in registry.all() for pa in cube.pre_aggregations]


class PreAggScheduler:
    def __init__(self, executor: QueryExecutor, *, default_every: int = 30) -> None:
        self._service = RollupBuilderService(executor)
        self._default_every = default_every
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._intervals: dict[str, int] = {}
        self.running = False

    async def build_all(self) -> list[str]:
        """Build/refresh every declared rollup once; return the table names.

        A failure on one rollup is logged and skipped — one broken rollup must
        not block the others (the orchestrator falls back to the base cube for it).
        """
        built: list[str] = []
        for cube, pa in _declared_rollups():
            try:
                table = await self._service.build(cube, pa)
                built.append(table)
                logger.info("built rollup %s", table)
            except Exception:  # noqa: BLE001 — one rollup failing must not stop the rest
                logger.warning(
                    "rollup build failed for %s.%s", cube.name, pa.name, exc_info=True
                )
        return built

    async def start(self, *, build_on_start: bool = True) -> None:
        if build_on_start:
            await self.build_all()
        for cube, pa in _declared_rollups():
            rid = f"{cube.name}.{pa.name}"
            self._intervals[rid] = self._every(pa)
            self._tasks[rid] = asyncio.create_task(self._loop(cube, pa))
        self.running = True

    async def _loop(self, cube: CubeMeta, pa: PreAggregation) -> None:
        # ponytail: per-rollup sleep loop. Switch to APScheduler if we need
        # missed-run policy, jitter, or cross-restart persistence.
        every = self._every(pa)
        while True:
            await asyncio.sleep(every)
            try:
                await self._service.build(cube, pa)
            except Exception:  # noqa: BLE001 — refresh failure logs, retries next tick
                logger.warning(
                    "rollup refresh failed for %s.%s", cube.name, pa.name, exc_info=True
                )

    def shutdown(self) -> None:
        for task in self._tasks.values():
            task.cancel()
        self._tasks.clear()
        self._intervals.clear()
        self.running = False

    def _every(self, pa: PreAggregation) -> int:
        refresh = pa.refresh_key or {}
        raw = refresh.get("every")
        try:
            n = int(raw) if raw is not None else self._default_every
        except (TypeError, ValueError):
            return self._default_every
        return max(n, 1)
