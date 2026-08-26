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

from datetime import UTC, datetime

from cubepy.schema.meta import CubeMeta
from cubepy.schema.registry import SchemaRegistry

__all__ = ["build_context", "glossary_prompt", "query_contract", "system_prompt", "members_index"]

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
- Return ONLY the dimensions the question asks for. Do NOT add a time dimension
  unless the question asks for a trend/period breakdown — "最近7天各渠道订单数"
  means FILTER by time, group by channel only.
- Joins are automatic: any cube referenced by measures/dimensions is joined
  along declared relationships — no join config needed.
- Relative dateRanges ("last 30 days") are supported; timezone default UTC.
- If the question cannot be answered from the catalog (no relevant members,
  or it is not a data question), return {"notAnswerable": true, "reason":
  "<one short sentence>"} instead of a query."""


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


def glossary_prompt(glossary: dict[str, str] | None = None) -> str:
    """Business-term glossary section for the system prompt (F-E1.3).

    ``glossary`` maps a business phrase to its precise meaning (member path +
    口径). Hardcoded per domain for the M2 POC — M3 lifts it into config.
    """
    if not glossary:
        return ""
    lines = ["## Glossary (business term -> precise meaning, prefer these mappings)"]
    lines += [f"- {term}: {meaning}" for term, meaning in glossary.items()]
    return "\n".join(lines)


def system_prompt(
    cubes: list[CubeMeta] | SchemaRegistry | None = None,
    glossary: dict[str, str] | None = None,
) -> str:
    """Drop-in system prompt: date anchor + contract + catalog + glossary + example.

    The date anchor is load-bearing: without it the model resolves month-only
    references ("8月") against its training-cutoff year and the query silently
    returns empty (caught live by the M2 real-LLM acceptance run).
    """
    today = datetime.now(UTC).date().isoformat()
    parts = [
        "You translate business questions into Cube Query JSON for a semantic layer.",
        f"Today's date is {today} (UTC). Resolve any absolute or month/quarter-"
        'only time reference (e.g. "8月", "Q3") against THIS date\'s year, '
        'or prefer a relative dateRange ("this month", "last 30 days").',
        QUERY_CONTRACT,
        build_context(cubes),
        glossary_prompt(glossary),
        '\nExample — "每个客户的总销售额，按金额降序，前10":\n'
        '{"measures":["Orders.revenue"],"dimensions":["Customers.name"],'
        '"order":{"Orders.revenue":"desc"},"limit":10}',
    ]
    return "\n\n".join(p for p in parts if p)


def members_index(cubes: list[CubeMeta] | SchemaRegistry | None = None) -> list[str]:
    """All valid member paths — use to validate LLM output before Query.parse."""
    out: list[str] = []
    for c in _cubes(cubes):
        out += [f"{c.name}.{m.name}" for m in c.measures if m.status != "deprecated"]
        out += [f"{c.name}.{d.name}" for d in c.dimensions if d.status != "deprecated"]
        out += [f"{c.name}.{s.name}" for s in c.segments]
    return out
