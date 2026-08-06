"""REST routes mirroring cube.js's API gateway (/cubejs-api/v1/*)."""

from __future__ import annotations

import asyncio
import copy
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ValidationError

from cubepy.api.deps import get_orchestrator
from cubepy.orchestrator.orchestrator import QueryOrchestrator
from cubepy.schema.registry import registry
from cubepy.security.auth import security_context
from cubepy.security.context import SecurityContext
from cubepy.security.permissions import PermissionBuilder
from cubepy.sqlgen.builder import SQLBuilder
from cubepy.sqlgen.query import Query

router = APIRouter(dependencies=[Depends(security_context)])


class LoadRequest(BaseModel):
    query: dict[str, Any] | list[Any]
    queryType: str = "regular"


class SqlRequest(BaseModel):
    query: dict[str, Any]


def _member_view(name: str, sql: str | None, mtype: str, title: str | None, desc: str | None) -> dict[str, Any]:
    out: dict[str, Any] = {"name": name, "type": mtype}
    if sql is not None:
        out["sql"] = sql
    if title is not None:
        out["title"] = title
    if desc is not None:
        out["description"] = desc
    return out


def _with_range(base: dict[str, Any], date_range: Any) -> dict[str, Any]:
    """Clone a query, setting the first timeDimension's dateRange to ``date_range``."""
    q = copy.deepcopy(base)
    tds = q.get("timeDimensions") or []
    if tds:
        tds[0]["dateRange"] = date_range
    return q


@router.post("/v1/load")
async def load(
    body: LoadRequest,
    ctx: SecurityContext = Depends(security_context),
    orch: QueryOrchestrator = Depends(get_orchestrator),
) -> dict[str, Any]:
    try:
        if body.queryType == "multi":
            queries = body.query if isinstance(body.query, list) else [body.query]
            envs = await asyncio.gather(
                *(orch.load(Query.parse(q), ctx) for q in queries)
            )
            return {"data": list(envs)}

        if body.queryType == "compareDateRange":
            base = body.query if isinstance(body.query, dict) else {}
            tds = base.get("timeDimensions") or []
            ranges = tds[0].get("dateRange") if tds else None
            if not ranges or not isinstance(ranges, list):
                raise ValueError(
                    "compareDateRange requires timeDimensions[0].dateRange as a list of ranges"
                )
            subs = [_with_range(base, r) for r in ranges]
            envs = await asyncio.gather(
                *(orch.load(Query.parse(q), ctx) for q in subs)
            )
            return {"data": list(envs)}

        # regular single query
        query = Query.parse(body.query if isinstance(body.query, dict) else {})
        return await orch.load(query, ctx)
    except (ValidationError, ValueError, KeyError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/v1/sql")
async def sql(
    body: SqlRequest,
    ctx: SecurityContext = Depends(security_context),
) -> dict[str, Any]:
    try:
        query = Query.parse(body.query)
        stmt = SQLBuilder(query, ctx).render_literal()
    except (ValidationError, ValueError, KeyError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"sql": [{"sql": stmt}]}


@router.get("/v1/meta")
async def meta(ctx: SecurityContext = Depends(security_context)) -> dict[str, Any]:
    cubes: list[dict[str, Any]] = []
    for cube in registry.all():
        if not PermissionBuilder.cube_visible(cube, ctx):
            continue
        measures, dimensions, segments = PermissionBuilder.filter_fields(cube, ctx)
        cubes.append(
            {
                "name": cube.name,
                "sql": cube.sql,
                "measures": [
                    _member_view(m.name, m.sql, str(m.type), m.title, m.description)
                    for m in measures
                ],
                "dimensions": [
                    _member_view(d.name, d.sql, str(d.type), d.title, d.description)
                    for d in dimensions
                ],
                "segments": [
                    {"name": s.name, "sql": s.sql} for s in segments
                ],
            }
        )
    return {"cubes": cubes}
