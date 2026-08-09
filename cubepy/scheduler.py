"""Scheduled refresh of declared pre-aggregations (plan §6).

One APScheduler interval job per rollup; the interval is the rollup's
``refresh_key.every`` (seconds), falling back to ``default_refresh_every``.
``start(build_on_start=True)`` builds every rollup once before the periodic
schedule takes over, so a freshly-started server serves correct rollup data
without waiting for the first interval.
"""

from __future__ import annotations

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from cubepy.orchestrator.executor import QueryExecutor
from cubepy.orchestrator.rollup_builder import RollupBuilderService
from cubepy.schema.meta import PreAggregation
from cubepy.schema.registry import registry

logger = logging.getLogger("cubepy.scheduler")


def _declared_rollups() -> list[tuple]:
    """Every (cube, pre_aggregation) pair in the registry."""
    return [
        (cube, pa)
        for cube in registry.all()
        for pa in cube.pre_aggregations
    ]


class PreAggScheduler:
    def __init__(self, executor: QueryExecutor, *, default_every: int = 30) -> None:
        self._service = RollupBuilderService(executor)
        self._default_every = default_every
        self._sched = AsyncIOScheduler()

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
            self._sched.add_job(
                self._service.build,
                args=[cube, pa],
                trigger=IntervalTrigger(seconds=self._every(pa)),
                id=f"{cube.name}.{pa.name}",
                replace_existing=True,
            )
        self._sched.start()

    def shutdown(self) -> None:
        if self._sched.running:
            self._sched.shutdown(wait=False)

    def _every(self, pa: PreAggregation) -> int:
        refresh = pa.refresh_key or {}
        raw = refresh.get("every")
        try:
            n = int(raw) if raw is not None else self._default_every
        except (TypeError, ValueError):
            return self._default_every
        return max(n, 1)
