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


def sql_str(value: Any) -> str:
    """Render a Python value as a SQL string literal with escaped single quotes.

    Public so schema callbacks (``check_permission``, ``shown``) can safely
    interpolate JWT claims into SQL fragments — claims are signed-but-untrusted,
    so never splice them raw.
    """
    text = "" if value is None else str(value)
    return "'" + text.replace("'", "''") + "'"


def require_claims(ctx: SecurityContext, *fields: str) -> None:
    """RLS fail-closed helper: assert the named SecurityContext fields are non-empty.

    RLS 回调在拼 SQL 片段前调用；任一字段为空即抛 ``ValueError`` → API 层转 400。
    没有它，缺失的 claim 会以空串注入 WHERE（``tenant_id = ''``），把「未授权」
    变成 SQL 执行期 500——语义错且泄漏内部错误细节。用法::

        def _rls(ctx):
            require_claims(ctx, "tenant_id")
            return [f"dwdorders.tenant_id = {sql_str(ctx.tenant_id)}"]
    """
    missing = [f for f in fields if not getattr(ctx, f, "")]
    if missing:
        raise ValueError(
            f"RLS 缺少必填 security claim：{', '.join(missing)}（租户隔离 fail-closed，缺失即拒绝）"
        )


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

        # Role-based convenience defaults (docs/05). These are additive, but a
        # default may only fire when the cube DECLARES the column it filters on
        # (``security_columns``) — blindly appending `user_id =` to a cube
        # without that column crashes every viewer query (caught live by the
        # trade-domain acceptance run: `dwdorders.user_id` does not exist).
        if ctx.role == "viewer" and "user_id" in cube.security_columns:
            conditions.append(f"{cube.name}.user_id = {sql_str(ctx.user_id)}")
        elif ctx.role == "manager" and "department" in cube.security_columns:
            conditions.append(f"{cube.name}.department = {sql_str(ctx.department)}")

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
