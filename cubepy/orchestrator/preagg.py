"""Pre-aggregation router.

CubePy skips pre-aggregation: with Hologres as the backing store, dynamic
tables maintain aggregates inside the database engine (see ``docs/02``), so an
application-level pre-agg layer is redundant. This router is a no-op seam so a
real implementation can be plugged in later without touching the orchestrator.
"""

from __future__ import annotations

import logging

from cubepy.sqlgen.query import Query

logger = logging.getLogger("cubepy.preagg")


class PreAggRouter:
    def match(self, query: Query) -> None:  # noqa: ARG002
        logger.debug(
            "pre-agg routing skipped — backing store handles aggregation "
            "(see docs/02); falling through to base cube"
        )
        return None


router = PreAggRouter()
