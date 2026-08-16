"""cubepy-native platform routes (governance), mounted under ``/cubepy``.

Distinct from the cube.js-compatible surface (``/cubejs-api``): these endpoints
serve the metrics-platform story — catalog, lineage, impact analysis.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from cubepy.catalog import build_catalog, impact, lineage
from cubepy.schema.registry import registry
from cubepy.security.auth import security_context
from cubepy.security.context import SecurityContext
from cubepy.security.permissions import PermissionBuilder

router = APIRouter(dependencies=[Depends(security_context)])


def _visible_cubes(ctx: SecurityContext) -> list:
    """Permission-filtered cube list (same visibility rules as /cubejs-api/v1/meta)."""
    return [c for c in registry.all() if PermissionBuilder.cube_visible(c, ctx)]


@router.get("/v1/catalog")
async def catalog(ctx: SecurityContext = Depends(security_context)) -> dict[str, Any]:
    """Governed metric catalog: owner/tags/status + per-member lineage."""
    return build_catalog(_visible_cubes(ctx))


@router.get("/v1/lineage")
async def lineage_route(
    table: str | None = None,
    column: str | None = None,
    ctx: SecurityContext = Depends(security_context),
) -> dict[str, Any]:
    """Flat lineage graph; with ?table=[&column=] returns impact analysis."""
    if table:
        return impact(table, column, _visible_cubes(ctx))
    return lineage(_visible_cubes(ctx))
