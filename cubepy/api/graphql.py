"""GraphQL surface (Strawberry).

Two surfaces share one schema:
  * ``load(query: JSON)`` — generic fallback (G009), always available.
  * ``<cube>(where, orderBy, limit) { <member> ... }`` — per-cube typed fields
    (G012): the member selection drives the underlying query, so clients can ask
    ``{ orders(limit: 10) { revenue status } }`` and get typed rows.

Per-cube node types are generated dynamically from the registry at schema-build
time. ``build_graphql_router()`` must be called *after* cubes are registered.
"""

from typing import Any

import strawberry
from fastapi import Depends
from strawberry.fastapi import GraphQLRouter
from strawberry.scalars import JSON
from strawberry.types import Info

from cubepy.api.deps import get_orchestrator
from cubepy.orchestrator.orchestrator import QueryOrchestrator
from cubepy.schema.registry import registry
from cubepy.security.auth import security_context
from cubepy.security.context import SecurityContext
from cubepy.sqlgen.query import Query as QueryModel

# --- generic load() fallback --------------------------------------------------


@strawberry.type
class QueryResult:
    data: list[JSON]
    last_refresh_time: str


# --- dynamic per-cube types ---------------------------------------------------


def _member_resolver(cube_name: str, member_name: str) -> Any:
    def resolver(self: Any) -> JSON:
        return self.row.get(f"{cube_name}.{member_name}")

    return resolver


def _build_node_type(cube_name: str, members: list[str]) -> type:
    namespace = {
        name: strawberry.field(resolver=_member_resolver(cube_name, name))
        for name in members
    }
    return strawberry.type(type(f"{cube_name}Node", (), namespace))


def _instantiate(node_type: type, row: dict[str, Any]) -> Any:
    """Node fields are resolver-based; the row is attached as plain state."""
    obj = node_type()
    obj.row = row  # type: ignore[attr-defined]
    return obj


def _selected_members(info: Info[Any, Any]) -> list[str]:
    """Member field names selected under the cube field."""
    try:
        field = info.selected_fields[0]
    except (AttributeError, IndexError):
        return []
    return [getattr(s, "name", "") for s in getattr(field, "selections", [])]


def _split_members(cube_name: str, members: list[str]) -> tuple[list[str], list[str]]:
    cube = registry.get(cube_name)
    measure_names = {m.name for m in cube.measures}
    dimension_names = {d.name for d in cube.dimensions}
    measures = [f"{cube_name}.{m}" for m in members if m in measure_names]
    dimensions = [f"{cube_name}.{d}" for d in members if d in dimension_names]
    return measures, dimensions


def _make_cube_resolver(cube_name: str, node_type: type) -> Any:
    async def resolver(
        root: Any,
        info: Info[Any, Any],
        where: JSON | None = None,
        order_by: JSON | None = None,
        limit: int | None = None,
    ) -> list[node_type]:
        ctx: SecurityContext = info.context["ctx"]
        orch: QueryOrchestrator = info.context["orchestrator"]
        members = _selected_members(info)
        measures, dimensions = _split_members(cube_name, members)
        query: dict[str, Any] = {"measures": measures, "dimensions": dimensions}
        if where:
            query["filters"] = where if isinstance(where, list) else [where]
        if order_by:
            query["order"] = order_by
        if limit is not None:
            query["limit"] = limit
        envelope = await orch.load(QueryModel.parse(query), ctx)
        return [_instantiate(node_type, row) for row in envelope["data"]]

    return resolver


def _build_query_type() -> type:
    fields: dict[str, Any] = {}

    async def load(root: Any, info: Info[Any, Any], query: JSON) -> QueryResult:
        ctx: SecurityContext = info.context["ctx"]
        orch: QueryOrchestrator = info.context["orchestrator"]
        parsed = QueryModel.parse(query if isinstance(query, dict) else dict(query))
        envelope = await orch.load(parsed, ctx)
        return QueryResult(
            data=envelope["data"], last_refresh_time=envelope["lastRefreshTime"]
        )

    fields["load"] = strawberry.field(resolver=load)

    for cube in registry.all():
        members = [m.name for m in cube.measures] + [d.name for d in cube.dimensions]
        node_type = _build_node_type(cube.name, members)
        fields[cube.name.lower()] = strawberry.field(
            resolver=_make_cube_resolver(cube.name, node_type)
        )
    return strawberry.type(type("RootQuery", (), fields))


def context_getter(
    ctx: SecurityContext = Depends(security_context),
    orch: QueryOrchestrator = Depends(get_orchestrator),
) -> dict[str, Any]:
    return {"ctx": ctx, "orchestrator": orch}


def build_graphql_router() -> GraphQLRouter:
    schema = strawberry.Schema(query=_build_query_type())
    return GraphQLRouter(schema, context_getter=context_getter)
