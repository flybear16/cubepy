"""REST routes mirroring cube.js's API gateway (/cubejs-api/v1/*)."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ValidationError

from cubepy.api.deps import get_orchestrator
from cubepy.config import settings
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


class SubscribeRequest(BaseModel):
    query: dict[str, Any]
    refreshKey: dict[str, Any] = {}
    timeout: float | None = None


def _data_hash(data: list[dict[str, Any]]) -> str:
    return hashlib.sha256(json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()


def _member_view(
    name: str,
    sql: str | None,
    mtype: str,
    title: str | None,
    desc: str | None,
    fmt: str | None = None,
    drill_members: tuple[str, ...] = (),
) -> dict[str, Any]:
    out: dict[str, Any] = {"name": name, "type": mtype}
    if sql is not None:
        out["sql"] = sql
    if title is not None:
        out["title"] = title
    if desc is not None:
        out["description"] = desc
    if fmt is not None:
        out["format"] = fmt
    if drill_members:
        out["drillMembers"] = list(drill_members)
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


@router.post("/v1/subscribe")
async def subscribe(
    body: SubscribeRequest,
    ctx: SecurityContext = Depends(security_context),
    orch: QueryOrchestrator = Depends(get_orchestrator),
) -> dict[str, Any]:
    """HTTP long-poll subscribe: returns once the result hash changes, or the
    latest result on timeout. Polling interval is ``refreshKey.every``."""
    try:
        query = Query.parse(body.query)
        envelope = await orch.load(query, ctx, use_cache=False)
    except (ValidationError, ValueError, KeyError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    every = float(body.refreshKey.get("every") or settings.default_refresh_every)
    max_wait = float(body.timeout if body.timeout is not None else settings.default_refresh_every)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max_wait

    last_hash = _data_hash(envelope["data"])
    while loop.time() < deadline:
        await asyncio.sleep(every)
        envelope = await orch.load(query, ctx, use_cache=False)
        current = _data_hash(envelope["data"])
        if current != last_hash:
            return envelope
        last_hash = current
    return envelope


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
                    _member_view(
                        m.name, m.sql, str(m.type), m.title, m.description,
                        m.format, m.drill_members,
                    )
                    for m in measures
                ],
                "dimensions": [
                    _member_view(
                        d.name, d.sql, str(d.type), d.title, d.description,
                        d.format, d.drill_members,
                    )
                    for d in dimensions
                ],
                "segments": [
                    {"name": s.name, "sql": s.sql} for s in segments
                ],
            }
        )
    return {"cubes": cubes}

