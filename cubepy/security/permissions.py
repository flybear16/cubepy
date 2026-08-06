"""Permission enforcement: row-level security + field-level visibility.

Mirrors cube.js's ``securityContext.checkPermission`` (returns raw SQL WHERE
fragments) and ``shown`` callbacks (member visibility). See ``docs/03``.

Trust boundary: the SQL fragments come from (a) the schema developer via
``check_permission`` and (b) role-based defaults interpolating JWT claims.
JWTs are signed, so claims are trusted-but-escaped: interpolated values are
single-quote-escaped to prevent injection from a claim value.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from cubepy.schema.meta import CubeMeta, Dimension, Measure, Segment
from cubepy.security.context import SecurityContext


def _sql_str(value: Any) -> str:
    """Render a Python value as a SQL string literal with escaped single quotes."""
    text = "" if value is None else str(value)
    return "'" + text.replace("'", "''") + "'"


def _visible(shown: Callable[[Any], bool] | None, ctx: SecurityContext) -> bool:
    return shown is None or bool(shown(ctx))


class PermissionBuilder:
    @staticmethod
    def apply_row_level(cube: CubeMeta, ctx: SecurityContext) -> list[str]:
        """WHERE fragments to AND-join into the base query."""
        conditions: list[str] = []

        if cube.security_context is not None:
            result = cube.security_context(ctx)
            if isinstance(result, list):
                conditions.extend(result)

        # Role-based convenience defaults (docs/05). These are additive.
        if ctx.role == "viewer":
            conditions.append(f"{cube.name}.user_id = {_sql_str(ctx.user_id)}")
        elif ctx.role == "manager":
            conditions.append(f"{cube.name}.department = {_sql_str(ctx.department)}")

        return conditions

    @staticmethod
    def filter_fields(
        cube: CubeMeta, ctx: SecurityContext
    ) -> tuple[tuple[Measure, ...], tuple[Dimension, ...], tuple[Segment, ...]]:
        measures = tuple(m for m in cube.measures if _visible(m.shown, ctx))
        dimensions = tuple(d for d in cube.dimensions if _visible(d.shown, ctx))
        segments = tuple(s for s in cube.segments if _visible(s.shown, ctx))
        return measures, dimensions, segments

    @staticmethod
    def cube_visible(cube: CubeMeta, ctx: SecurityContext) -> bool:
        return _visible(cube.shown, ctx)
