"""Schema diff with breaking-change detection (metrics-platform P0.2).

Compares two cube schema YAML files and classifies every change:

* ``breaking``   — removed cube/member/join, changed type, changed member SQL
                   (口径变更), changed join relationship/SQL
* ``warning``    — detected rename (removed + added with same SQL+type),
                   status -> deprecated
* ``info``       — added cube/member/join, description/owner/tags changed

CLI (also wired as the ``cubepy-diff`` console script)::

    python -m cubepy.diff old.yml new.yml [--check] [--format json]

``--check`` exits 1 when any breaking change exists — use it in CI:

    cubepy-diff schemas/main.yml schemas/pr.yml --check
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cubepy.schema.loader import parse_cube_file
from cubepy.schema.meta import CubeMeta

__all__ = ["Change", "diff_cubes", "diff_yaml_files", "main"]


@dataclass(frozen=True)
class Change:
    severity: str  # breaking | warning | info
    kind: str
    cube: str
    member: str | None
    detail: str

    def __str__(self) -> str:
        loc = self.cube if not self.member else f"{self.cube}.{self.member}"
        return f"[{self.severity:8s}] {self.kind:22s} {loc}: {self.detail}"


def _members(cube: CubeMeta) -> dict[str, tuple[str, Any]]:
    """path-less member name -> (kind, member)."""
    out: dict[str, tuple[str, Any]] = {}
    for m in cube.measures:
        out[m.name] = ("measure", m)
    for d in cube.dimensions:
        out[d.name] = ("dimension", d)
    for s in cube.segments:
        out[s.name] = ("segment", s)
    return out


def diff_cubes(old: list[CubeMeta], new: list[CubeMeta]) -> list[Change]:
    changes: list[Change] = []
    old_by = {c.name: c for c in old}
    new_by = {c.name: c for c in new}

    for name in sorted(old_by.keys() - new_by.keys()):
        changes.append(Change("breaking", "cube-removed", name, None, "cube removed"))
    for name in sorted(new_by.keys() - old_by.keys()):
        changes.append(Change("info", "cube-added", name, None, "cube added"))

    for name in sorted(old_by.keys() & new_by.keys()):
        oc, nc = old_by[name], new_by[name]

        # cube-level source change
        if oc.sql != nc.sql:
            changes.append(
                Change("breaking", "cube-sql-changed", name, None,
                       f"source changed: {oc.sql!r} -> {nc.sql!r}")
            )
        if oc.status != nc.status:
            sev = "warning" if nc.status == "deprecated" else "info"
            changes.append(
                Change(sev, "status-changed", name, None,
                       f"{oc.status} -> {nc.status}")
            )

        om, nm = _members(oc), _members(nc)

        removed = sorted(om.keys() - nm.keys())
        added = sorted(nm.keys() - om.keys())

        # rename detection: removed member reappears with identical sql+type
        added_sig = {(str(nm[a][1].type), getattr(nm[a][1], "sql", None)): a for a in added}
        for r in removed:
            sig = (str(om[r][1].type), getattr(om[r][1], "sql", None))
            twin = added_sig.get(sig)
            if twin is not None:
                changes.append(
                    Change("warning", "member-renamed", name, r,
                           f"likely renamed to {twin!r} (same type+sql) — consumers using the old path break")
                )
                added_sig.pop(sig, None)
                added = [a for a in added if a != twin]
            else:
                changes.append(
                    Change("breaking", "member-removed", name, r,
                           f"{om[r][0]} removed")
                )

        for a in added:
            changes.append(
                Change("info", "member-added", name, a, f"{nm[a][0]} added")
            )

        for m in sorted(om.keys() & nm.keys()):
            o, n = om[m][1], nm[m][1]
            # Segments have no ``type``; only measures/dimensions are typed.
            otype, ntype = getattr(o, "type", None), getattr(n, "type", None)
            if otype is not None and ntype is not None and str(otype) != str(ntype):
                changes.append(
                    Change("breaking", "type-changed", name, m,
                           f"{o.type} -> {n.type}")
                )
            osql, nsql = getattr(o, "sql", None), getattr(n, "sql", None)
            if osql != nsql:
                changes.append(
                    Change("breaking", "sql-changed", name, m,
                           f"口径 changed: {osql!r} -> {nsql!r}")
                )
            if getattr(o, "status", "active") != getattr(n, "status", "active"):
                sev = "warning" if getattr(n, "status", "") == "deprecated" else "info"
                changes.append(
                    Change(sev, "status-changed", name, m,
                           f"{o.status} -> {n.status}")
                )

        # joins
        for tgt in sorted(oc.joins.keys() - nc.joins.keys()):
            changes.append(
                Change("breaking", "join-removed", name, None,
                       f"join to {tgt!r} removed")
            )
        for tgt in sorted(nc.joins.keys() - oc.joins.keys()):
            changes.append(
                Change("info", "join-added", name, None, f"join to {tgt!r} added")
            )
        for tgt in sorted(oc.joins.keys() & nc.joins.keys()):
            oj, nj = oc.joins[tgt], nc.joins[tgt]
            if str(oj.relationship) != str(nj.relationship) or oj.sql != nj.sql:
                changes.append(
                    Change("breaking", "join-changed", name, None,
                           f"join {tgt!r}: {oj.relationship.value} {oj.sql!r} -> "
                           f"{nj.relationship.value} {nj.sql!r}")
                )

    return changes


def diff_yaml_files(old_path: str | Path, new_path: str | Path) -> list[Change]:
    return diff_cubes(parse_cube_file(old_path), parse_cube_file(new_path))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="cubepy-diff",
        description="Diff two cubepy schema YAML files; exit 1 on breaking changes with --check.",
    )
    ap.add_argument("old", help="baseline schema YAML")
    ap.add_argument("new", help="candidate schema YAML")
    ap.add_argument("--check", action="store_true",
                    help="exit 1 if any breaking change is found (CI mode)")
    ap.add_argument("--format", choices=["text", "json"], default="text")
    args = ap.parse_args(argv)

    changes = diff_yaml_files(args.old, args.new)
    breaking = [c for c in changes if c.severity == "breaking"]

    if args.format == "json":
        print(json.dumps(
            {"changes": [c.__dict__ for c in changes], "breaking": len(breaking)},
            ensure_ascii=False, indent=2,
        ))
    else:
        for c in changes:
            print(c)
        summary = f"{len(changes)} change(s), {len(breaking)} breaking"
        print(summary)

    if args.check and breaking:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())  # pragma: no cover - exercised via `python -m cubepy.diff` in tests
