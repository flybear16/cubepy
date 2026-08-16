"""LLM-ready context builders (metrics-platform AI interface).

Provider-agnostic helpers for text-to-Query: give any LLM the compact member
catalog + the Cube Query contract, get back a valid query JSON. No LLM SDK is
bundled — wire your own (OpenAI, DashScope, …)::

    from cubepy.ai import system_prompt
    from cubepy.sqlgen.query import Query

    messages = [
        {"role": "system", "content": system_prompt()},
        {"role": "user", "content": "上个月每个客户的下单金额？"},
    ]
    ... your LLM call ...
    q = Query.parse(json.loads(llm_json_output))  # validates members exist
"""

from __future__ import annotations

from typing import Any

from cubepy.schema.meta import CubeMeta
from cubepy.schema.registry import SchemaRegistry

__all__ = ["build_context", "query_contract", "system_prompt", "members_index"]

QUERY_CONTRACT = """\
Query JSON contract (return ONLY this JSON, no prose):
{
  "measures": ["CubeName.measureName", ...],        // aggregates; >=1 recommended
  "dimensions": ["CubeName.dimensionName", ...],    // group-bys
  "timeDimensions": [{"dimension": "CubeName.timeDim",
                      "granularity": "day|week|month|year",
                      "dateRange": ["YYYY-MM-DD", "YYYY-MM-DD"]
                      | "last N days" | "this month" | ...}],
  "filters": [{"member": "CubeName.memberName",
               "operator": "equals|notEquals|contains|notContains|gt|gte|lt|lte|\
afterDate|beforeDate|set|notSet|inDateRange|notInDateRange",
               "values": [...]} | {"or": [...]} | {"and": [...]}],
  "segments": ["CubeName.segmentName"],
  "order": {"CubeName.memberName": "asc"|"desc"},
  "limit": 100
}
Rules:
- Use ONLY member paths listed in the catalog below; never invent names.
- Joins are automatic: any cube referenced by measures/dimensions is joined
  along declared relationships — no join config needed.
- Relative dateRanges ("last 30 days") are supported; timezone default UTC."""


def _cubes(cubes: list[CubeMeta] | SchemaRegistry | None) -> list[CubeMeta]:
    if cubes is None:
        from cubepy.schema.registry import registry
        return registry.all()
    if isinstance(cubes, SchemaRegistry):
        return cubes.all()
    return list(cubes)


def build_context(cubes: list[CubeMeta] | SchemaRegistry | None = None) -> str:
    """Compact markdown catalog of members + relationships for LLM prompts."""
    lines: list[str] = ["## Catalog"]
    for c in _cubes(cubes):
        lines.append(f"### {c.name}" + (f" — {c.description}" if c.description else ""))
        for m in c.measures:
            if m.status == "deprecated":
                continue
            extra = f" ({m.description})" if m.description else ""
            lines.append(f"- measure: {c.name}.{m.name} [{m.type}]{extra}")
        for d in c.dimensions:
            if d.status == "deprecated":
                continue
            extra = f" ({d.description})" if d.description else ""
            pk = " primary key" if d.primary_key else ""
            lines.append(f"- dimension: {c.name}.{d.name} [{d.type}]{pk}{extra}")
        for s in c.segments:
            lines.append(f"- segment: {c.name}.{s.name}")
        for tgt, j in c.joins.items():
            lines.append(f"- relationship: {c.name} {j.relationship.value} {tgt}")
    return "\n".join(lines)


def query_contract() -> str:
    return QUERY_CONTRACT


def system_prompt(cubes: list[CubeMeta] | SchemaRegistry | None = None) -> str:
    """Drop-in system prompt: contract + catalog + example."""
    return (
        "You translate business questions into Cube Query JSON for a semantic "
        "layer.\n\n" + QUERY_CONTRACT + "\n\n" + build_context(cubes)
        + '\n\nExample — "每个客户的总销售额，按金额降序，前10":\n'
        '{"measures":["Orders.revenue"],"dimensions":["Customers.name"],'
        '"order":{"Orders.revenue":"desc"},"limit":10}'
    )


def members_index(cubes: list[CubeMeta] | SchemaRegistry | None = None) -> list[str]:
    """All valid member paths — use to validate LLM output before Query.parse."""
    out: list[str] = []
    for c in _cubes(cubes):
        out += [f"{c.name}.{m.name}" for m in c.measures if m.status != "deprecated"]
        out += [f"{c.name}.{d.name}" for d in c.dimensions if d.status != "deprecated"]
        out += [f"{c.name}.{s.name}" for s in c.segments]
    return out
