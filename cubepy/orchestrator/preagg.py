"""Pre-aggregation aggregate-navigation matcher.

Given a parsed ``Query`` and the requester's security context, decide whether any
declared rollup can answer it. Every guard is **fail-closed**: on any doubt the
matcher returns ``None`` and the orchestrator falls through to the base cube.

Guards (mirrors the approved plan §5):
  * a non-None authenticated ``ctx`` is required (RLS correctness depends on it);
  * exactly one cube referenced across measures / dimensions / time dimensions;
  * a matching rollup exists whose ``time_dimension`` equals the query's and whose
    ``granularity`` rolls up to the requested granularity (day -> month OK,
    month -> day rejected);
  * ``query.timezone`` is UTC or unset (MVP pins UTC bucketing — see plan §5/G1);
  * the query's measures/dimensions are covered by the rollup;
  * every queried measure is additive (SUM/COUNT only — defence in depth, the loader
    already validated the rollup's own measures);
  * if the cube has a ``security_context`` (RLS active), the rollup materialises every
    declared ``security_columns`` so RLS predicates can be replayed on the rollup.

The matcher never inspects RLS callback SQL (opaque by design); it relies on the
cube's declared ``security_columns`` plus the load-time cross-tenant test (plan §4).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from cubepy.schema.meta import MeasureType
from cubepy.schema.registry import registry
from cubepy.sqlgen.query import Query

if TYPE_CHECKING:
    from cubepy.schema.meta import CubeMeta, PreAggregation

logger = logging.getLogger("cubepy.preagg")

# Re-aggregatable measures: a rollup of SUM/COUNT can be summed again losslessly.
_ADDITIVE = frozenset({MeasureType.SUM, MeasureType.COUNT})

# Roll-up lattice: a rollup at a lower rank can answer a query at an equal/higher rank.
# (A day rollup answers a week/month/year query; a month rollup cannot answer a day one.)
_GRANULARITY_RANK = {
    "second": 0,
    "minute": 1,
    "hour": 2,
    "day": 3,
    "week": 4,
    "month": 5,
    "quarter": 6,
    "year": 7,
}


@dataclass(frozen=True)
class RollupRoute:
    """A chosen rollup the orchestrator can query instead of the base cube."""

    table_name: str
    cube: str
    measures: tuple[str, ...]
    dimensions: tuple[str, ...]
    time_dimension: str
    granularity: str
    security_columns: tuple[str, ...]


def _member(path: str) -> str:
    return path.rsplit(".", 1)[-1]


class PreAggRouter:
    def match(self, query: Query, ctx: Any = None) -> RollupRoute | None:
        if ctx is None:
            # No authenticated context -> cannot vouch for RLS correctness -> fail closed.
            logger.debug("pre-agg miss: no security context")
            return None

        # Segments emit opaque WHERE fragments that may reference base columns the
        # rollup doesn't materialise; without inspecting segment SQL we can't verify,
        # so fail closed.
        if query.segments:
            logger.debug("pre-agg miss: query uses segments")
            return None

        cube_names = self._referenced_cubes(query)
        if len(cube_names) != 1:
            # Zero cubes (nothing queried) or a multi-cube join -> no single rollup fits.
            logger.debug("pre-agg miss: references %d cubes", len(cube_names))
            return None

        meta = registry.get(next(iter(cube_names)))
        for pa in meta.pre_aggregations:
            route, reason = self._try_rollup(meta, pa, query)
            if route is not None:
                logger.info("pre-agg hit: rollup %s.%s", meta.name, pa.name)
                return route
            logger.debug("pre-agg miss: rollup %s.%s — %s", meta.name, pa.name, reason)
        return None

    @staticmethod
    def _referenced_cubes(query: Query) -> set[str]:
        paths: list[str] = list(query.measures) + list(query.dimensions)
        paths.extend(td.dimension for td in query.timeDimensions)
        return {p.split(".", 1)[0] for p in paths if "." in p}

    def _try_rollup(
        self, meta: CubeMeta, pa: PreAggregation, query: Query
    ) -> tuple[RollupRoute | None, str]:
        """Return (route, reason). A non-None route implies reason is unused."""
        if pa.time_dimension is None or pa.granularity is None:
            return None, "rollup has no time bucketing"

        # Time dimension must match (a rollup bucketed on created_at can't serve a
        # query grouped on a different time column). A query with no time dimension
        # is an all-time aggregate and is served by summing across all buckets.
        if query.timeDimensions:
            qtd = query.timeDimensions[0]
            if qtd.dimension != pa.time_dimension:
                return None, "time dimension mismatch"
            # granularity=None means the base builder groups at raw timestamp
            # precision (full), which a bucketed rollup cannot reproduce.
            if qtd.granularity is None:
                return None, "query groups at raw timestamp precision"
            if not self._rolls_up(pa.granularity, qtd.granularity):
                return None, f"{pa.granularity} rollup cannot serve {qtd.granularity}"

        # MVP pins UTC bucketing; a non-UTC session would silently re-bucket (plan §5/G1).
        if query.timezone not in (None, "UTC"):
            return None, f"timezone {query.timezone!r} not supported (MVP: UTC only)"

        # Coverage: the rollup must materialise every requested measure & dimension.
        if not set(query.measures) <= set(pa.measures):
            return None, "query measures not covered by rollup"
        if not set(query.dimensions) <= set(pa.dimensions):
            return None, "query dimensions not covered by rollup"

        # Additivity (defence in depth): queried measures must be SUM/COUNT so that
        # re-aggregating the rollup is lossless. The loader already validated the
        # rollup's own measures, but a measure resolved here comes from the query.
        for m_path in query.measures:
            try:
                mdef = meta.measure(_member(m_path))
            except KeyError:
                return None, f"measure {m_path!r} not found"
            if mdef.type not in _ADDITIVE:
                return None, f"measure {m_path!r} is non-additive ({mdef.type})"

        # RLS column coverage: if the cube enforces row-level security, the rollup
        # must carry every declared security column or the predicate can't be replayed.
        if meta.security_context is not None and not set(meta.security_columns) <= set(
            pa.security_columns
        ):
            return None, "rollup lacks RLS security columns"

        return RollupRoute(
            table_name=f"cubepy_rollup_{meta.name.lower()}_{pa.name}",
            cube=meta.name,
            measures=pa.measures,
            dimensions=pa.dimensions,
            time_dimension=pa.time_dimension,
            granularity=pa.granularity,
            security_columns=pa.security_columns,
        ), ""

    @staticmethod
    def _rolls_up(rollup_granularity: str, query_granularity: str | None) -> bool:
        if query_granularity is None:
            return True
        r = _GRANULARITY_RANK.get(rollup_granularity)
        q = _GRANULARITY_RANK.get(query_granularity)
        if r is None or q is None:
            return False
        return r <= q


router = PreAggRouter()
