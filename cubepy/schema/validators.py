"""Load-time schema validation for pre-aggregations.

Called by the loader before ``registry.register`` so that a declared-but-unsatisfiable
rollup fails fast at startup instead of silently never matching (or, worse, matching
and producing wrong results). All checks are conservative: anything ambiguous raises
``SchemaError``.
"""

from __future__ import annotations

from cubepy.schema.meta import CubeMeta, DimensionType, MeasureType

# Only SUM/COUNT are re-aggregatable from a rollup (sum-of-sums / sum-of-counts).
# AVG/MIN/MAX/COUNT_DISTINCT lose information; CALCULATED/window depend on rows.
ADDITIVE_MEASURES = frozenset({MeasureType.SUM, MeasureType.COUNT})


class SchemaError(Exception):
    """Raised when a cube's declared schema (esp. pre-aggregations) is unsatisfiable."""


def _member(path: str) -> str:
    """``Orders.revenue`` -> ``revenue``; ``revenue`` -> ``revenue``."""
    return path.rsplit(".", 1)[-1]


def validate_cube(meta: CubeMeta) -> None:
    """Validate ``meta``'s measures/dimensions/pre-aggregations. Raises ``SchemaError``."""
    measure_names = {m.name for m in meta.measures}
    dimension_names = {d.name for d in meta.dimensions}
    time_dimension_names = {
        d.name for d in meta.dimensions if d.type == DimensionType.TIME
    }

    # RLS column-coverage contract: when a cube uses row-level security AND declares
    # rollups, it must also list security_columns — otherwise a rollup's coverage check
    # (rollup.security_columns ⊇ cube.security_columns) is trivially true and an
    # under-covering rollup would silently leak across tenants. RLS-on-base-table cubes
    # without rollups are unaffected.
    if (
        meta.security_context is not None
        and meta.pre_aggregations
        and not meta.security_columns
    ):
        raise SchemaError(
            f"cube {meta.name!r}: security_context is set with pre-aggregations but "
            "security_columns is empty; declare the columns RLS predicates filter on"
        )

    seen_rollup_names: set[str] = set()
    for pa in meta.pre_aggregations:
        if pa.name in seen_rollup_names:
            raise SchemaError(
                f"cube {meta.name!r}: duplicate pre-aggregation name {pa.name!r}"
            )
        seen_rollup_names.add(pa.name)

        for m_path in pa.measures:
            mname = _member(m_path)
            if mname not in measure_names:
                raise SchemaError(
                    f"cube {meta.name!r}: pre-agg {pa.name!r} references "
                    f"unknown measure {m_path!r}"
                )
            mtype = meta.measure(mname).type
            if mtype not in ADDITIVE_MEASURES:
                raise SchemaError(
                    f"cube {meta.name!r}: pre-agg {pa.name!r} uses non-additive measure "
                    f"{m_path!r} ({mtype}); only SUM/COUNT are re-aggregatable from a rollup"
                )

        for d_path in pa.dimensions:
            if _member(d_path) not in dimension_names:
                raise SchemaError(
                    f"cube {meta.name!r}: pre-agg {pa.name!r} references "
                    f"unknown dimension {d_path!r}"
                )

        if pa.time_dimension is not None and _member(pa.time_dimension) not in time_dimension_names:
            raise SchemaError(
                f"cube {meta.name!r}: pre-agg {pa.name!r} time_dimension "
                f"{pa.time_dimension!r} is not a time dimension on this cube"
            )
