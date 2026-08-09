"""RollupBuilderService: materialise a declared pre-aggregation into a table.

Generates a ``CREATE TABLE cubepy_rollup_{cube}_{name} AS SELECT ...`` whose column
model matches what :class:`cubepy.sqlgen.rollup.RollupBuilder` reads:

  * dimension / time / security columns named after their physical SQL (so filters,
    RLS fragments and ``date_trunc`` use the same expressions as the base query);
  * measure columns named after the measure (quoted), holding the already-aggregated
    value.

Both the build and the later rollup query run under ``SET TIME ZONE 'UTC'`` so the
stored day-buckets and the query-time bounds compare consistently (plan §5/G1).

The build is idempotent (``DROP TABLE IF EXISTS`` then ``CREATE``) so a refresh just
replaces the table.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text

from cubepy.orchestrator.executor import QueryExecutor
from cubepy.schema.meta import CubeMeta, PreAggregation
from cubepy.security.context import SecurityContext
from cubepy.sqlgen.builder import SQLBuilder
from cubepy.sqlgen.query import Query

# Build runs as a privileged system operation; measure ``shown`` callbacks are not
# evaluated (the rollup materialises every declared measure regardless of viewer).
_BUILD_CTX = SecurityContext(role="admin")


class RollupBuilderService:
    def __init__(self, executor: QueryExecutor) -> None:
        self.executor = executor

    async def build(self, cube: CubeMeta, pa: PreAggregation) -> str:
        """Create/refresh the rollup table for ``pa`` and return its name."""
        table = f"cubepy_rollup_{cube.name.lower()}_{pa.name}"
        alias = cube.name.lower()

        select_items: list[str] = []
        group_items: list[str] = []

        for path in pa.dimensions:
            dim = cube.dimension(path.rsplit(".", 1)[-1])
            select_items.append(f"{alias}.{dim.sql} AS {dim.sql}")
            group_items.append(f"{alias}.{dim.sql}")

        for col in pa.security_columns:
            select_items.append(f"{alias}.{col} AS {col}")
            group_items.append(f"{alias}.{col}")

        if pa.time_dimension is not None and pa.granularity is not None:
            td = cube.dimension(pa.time_dimension.rsplit(".", 1)[-1])
            bucket = f"date_trunc('{pa.granularity}', {alias}.{td.sql})"
            select_items.append(f"{bucket} AS {td.sql}")
            group_items.append(bucket)

        for path in pa.measures:
            measure = cube.measure(path.rsplit(".", 1)[-1])
            select_items.append(f'{self._measure_ctas_sql(cube, measure)} AS "{measure.name}"')

        base = cube.sql.strip()
        base_sql = f"({base})" if base.lower().startswith(("select", "with")) else base
        select = ",\n       ".join(select_items)
        group_by = ", ".join(group_items)
        create_sql = (
            f"CREATE TABLE {table} AS\n"
            f"SELECT {select}\n"
            f"FROM {base_sql} AS {alias}\n"
            f"GROUP BY {group_by}"
        )

        run = getattr(self.executor, "execute_with_session", None)
        if run is None:  # pragma: no cover - real executors always have it
            raise TypeError("executor must support execute_with_session to build rollups")
        # Idempotent refresh: drop then recreate under a UTC-pinned session.
        await run(text(f"DROP TABLE IF EXISTS {table}"), "SET TIME ZONE 'UTC'")
        await run(text(create_sql), "SET TIME ZONE 'UTC'")
        return table

    def _measure_ctas_sql(self, cube: CubeMeta, measure: Any) -> str:
        """Reuse SQLBuilder's aggregate rendering, binding filter constants as literals
        (measure filters are static author config, so literals are safe in the CTAS)."""
        builder = SQLBuilder(Query(measures=[]), _BUILD_CTX)
        builder._cubes[cube.name.lower()] = cube  # let _resolve_member find the cube
        expr = builder._measure_sql(measure)
        if not builder._params.values:
            return expr
        return str(
            text(expr)
            .bindparams(**builder._params.values)
            .compile(compile_kwargs={"literal_binds": True})
        )
