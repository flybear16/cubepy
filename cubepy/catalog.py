"""Metric catalog, lineage and impact analysis (metrics-platform P0.1).

Builds a governed catalog from registered cubes:

* source-table extraction from cube SQL (``SELECT * FROM posts`` -> ``posts``)
* column-level lineage for every member (best-effort identifier scan)
* impact analysis: "I'm changing ``posts.author_id`` — which members break?"

Lineage is heuristic: it parses the cube/member SQL for identifiers and maps
them to physical columns. Generated schemas (autodml ``--format cubepy``) yield
exact lineage because member SQL is the bare column name.
"""

from __future__ import annotations

import re
from typing import Any

from cubepy.schema.meta import CubeMeta, CubeMeta as _C, Measure, Dimension
from cubepy.schema.registry import SchemaRegistry

# --- SQL identifier heuristics -------------------------------------------------

_KEYWORDS = frozenset(
    """select from where and or not null is as on join left right inner outer full
    cross group by order limit offset having distinct case when then else end
    union all asc desc between in like ilike exists cast coalesce greatest least
    count sum avg min max true false interval current_date now extract""".split()
)

_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_FUNC_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\s*\(")
_FROM_RE = re.compile(
    r"\bfrom\s+([A-Za-z_][\w.$]*|\"[^\"]+\"|`[^`]+`)", re.IGNORECASE
)


def table_of(cube_sql: str) -> str | None:
    """Best-effort source table of a cube SQL (first FROM target, else the text)."""
    if not cube_sql or not cube_sql.strip():
        return None
    s = cube_sql.strip().rstrip(";")
    m = _FROM_RE.search(s)
    if m:
        return m.group(1).strip('"`').lower()
    if re.fullmatch(r"[A-Za-z_][\w.$]*", s):
        return s.lower()
    return None


def columns_ref(sql: str | None) -> list[str]:
    """Column identifiers referenced by a member SQL expression (best effort).

    Skips SQL keywords, function names, and ``{sibling}`` measure refs.
    """
    if not sql:
        return []
    funcs = {m.group(1).lower() for m in _FUNC_RE.finditer(sql)}
    body = re.sub(r"\{[A-Za-z_]\w*\}", " ", sql)  # sibling-measure refs
    out: list[str] = []
    for ident in _IDENT_RE.findall(body):
        low = ident.lower()
        if low in _KEYWORDS or low in funcs:
            continue
        if ident not in out:
            out.append(ident)
    return out


def _same_table(a: str | None, b: str) -> bool:
    """``public.posts`` matches ``posts`` (case-insensitive, suffix aware)."""
    if not a:
        return False
    a, b = a.lower(), b.lower()
    return a == b or a.endswith("." + b) or b.endswith("." + a)


# --- catalog -------------------------------------------------------------------


def _member_entry(cube: CubeMeta, kind: str, m: Measure | Dimension) -> dict[str, Any]:
    sql = getattr(m, "sql", None)
    return {
        "name": m.name,
        "path": f"{cube.name}.{m.name}",
        "kind": kind,
        "type": str(m.type),
        **({"sql": sql} if sql is not None else {}),
        "columns": columns_ref(sql),
        "owner": m.owner,
        "tags": list(m.tags),
        "status": m.status,
        **({"description": m.description} if m.description else {}),
    }


def build_catalog(cubes: list[CubeMeta] | SchemaRegistry | None = None) -> dict[str, Any]:
    """Governed catalog: cubes -> members with owner/tags/status + lineage."""
    if cubes is None:
        from cubepy.schema.registry import registry
        cubes = registry.all()
    elif isinstance(cubes, SchemaRegistry):
        cubes = cubes.all()

    out: list[dict[str, Any]] = []
    for c in cubes:
        table = table_of(c.sql)
        cols: set[str] = set()
        members = (
            [_member_entry(c, "measure", m) for m in c.measures]
            + [_member_entry(c, "dimension", d) for d in c.dimensions]
        )
        for mem in members:
            cols.update(mem["columns"])
        joins = [
            {
                "target": target,
                "relationship": str(j.relationship),
                "sql": j.sql,
                "columns": columns_ref(j.sql),
            }
            for target, j in c.joins.items()
        ]
        for j in joins:
            cols.update(
                col.split(".")[-1] for col in columns_ref(j["sql"])
            )
        out.append(
            {
                "name": c.name,
                "sql": c.sql,
                "table": table,
                "owner": c.owner,
                "tags": list(c.tags),
                "status": c.status,
                **({"description": c.description} if c.description else {}),
                "members": members,
                "joins": joins,
                "lineage": {"table": table, "columns": sorted(cols)},
            }
        )
    return {"cubes": out}


def lineage(cubes: list[CubeMeta] | SchemaRegistry | None = None) -> dict[str, Any]:
    """Flat lineage graph: member -> table.columns edges + join edges."""
    cat = build_catalog(cubes)
    edges: list[dict[str, Any]] = []
    for cube in cat["cubes"]:
        table = cube["table"] or f"({cube['name']}?)"
        for mem in cube["members"]:
            for col in mem["columns"]:
                edges.append(
                    {"from": mem["path"], "to": f"{table}.{col}", "kind": "column"}
                )
            if not mem["columns"]:
                edges.append({"from": mem["path"], "to": table, "kind": "table"})
        for j in cube["joins"]:
            edges.append(
                {"from": f"{cube['name']}->joins->{j['target']}", "to": j["sql"],
                 "kind": "join"}
            )
    return {"edges": edges}


def impact(
    table: str,
    column: str | None = None,
    cubes: list[CubeMeta] | SchemaRegistry | None = None,
) -> dict[str, Any]:
    """Impact analysis: which members/joins break if ``table[.column]`` changes."""
    cat = build_catalog(cubes)
    hits: list[dict[str, Any]] = []
    for cube in cat["cubes"]:
        if not _same_table(cube["table"], table):
            continue
        reason_tbl = f"cube {cube['name']!r} reads table {table!r}"
        for mem in cube["members"]:
            if column is None or column in mem["columns"]:
                why = (
                    reason_tbl
                    if column is None
                    else f"member references {table}.{column}"
                )
                hits.append(
                    {"cube": cube["name"], "member": mem["path"],
                     "kind": mem["kind"], "reason": why, "status": mem["status"]}
                )
        if column is not None:
            tbl = table.lower().split(".")[-1]
            col = column.lower()
            for j in cube["joins"]:
                qualified = [
                    c for c in j["columns"]
                    if c.lower() == f"{tbl}.{col}" or c.lower() == col
                ]
                if qualified:
                    hits.append(
                        {"cube": cube["name"], "join": j["target"],
                         "kind": "join", "reason": f"join ON references {qualified[0]}",
                         "status": "active"}
                    )
    return {"table": table, **({"column": column} if column else {}), "impacted": hits}
