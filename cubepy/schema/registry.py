"""Process-wide registry of compiled cubes.

A module-level singleton ``registry`` is used by the ``@cube`` decorator and the
YAML loader. Tests call ``registry.clear()`` between cases.
"""

from __future__ import annotations

from typing import Any

from cubepy.schema.meta import CubeMeta


class SchemaRegistry:
    def __init__(self) -> None:
        self._cubes: dict[str, CubeMeta] = {}

    def register(self, meta: CubeMeta) -> None:
        self._cubes[meta.name] = meta

    def get(self, name: str) -> CubeMeta:
        try:
            return self._cubes[name]
        except KeyError:
            raise KeyError(f"cube {name!r} not registered") from None

    def all(self) -> list[CubeMeta]:
        return list(self._cubes.values())

    def names(self) -> list[str]:
        return list(self._cubes)

    def __contains__(self, name: object) -> bool:
        return name in self._cubes

    def clear(self) -> None:
        self._cubes.clear()


registry = SchemaRegistry()


def resolve_member(path: str) -> tuple[CubeMeta, str, Any]:
    """Resolve a ``CubeName.member`` path to (cube, kind, member_def)."""
    cube_name, member_name = path.split(".", 1)
    cube = registry.get(cube_name)
    if member_name in {m.name for m in cube.measures}:
        return cube, "measure", cube.measure(member_name)
    if member_name in {d.name for d in cube.dimensions}:
        return cube, "dimension", cube.dimension(member_name)
    if member_name in {s.name for s in cube.segments}:
        return cube, "segment", cube.segment(member_name)
    raise KeyError(f"member {path!r} not found in cube {cube_name!r}")

