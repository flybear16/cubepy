"""Column masking: rewrite result VALUES for callers without clearance.

Declaration (mirrors ``shown``)::

    measure("amount", MeasureType.SUM,
            mask=lambda ctx: None if ctx.role == "admin" else "fixed")

Callback contract: return ``None`` to show raw values, a built-in type name, or
``(type_name, param)`` for a parameterised mask. Built-ins:

- ``fixed``   -> ``***`` (param: replacement string)
- ``partial`` -> keep first 3 + last 4 chars, mask the middle (strings; other
                types fall through to ``fixed``)
- ``hash``    -> first 12 hex chars of sha256 (stable pseudonymisation)

Applied where results leave the engine (``build_envelope``), so REST / WS /
GraphQL / ask all inherit it. Masked members stay QUERYABLE and LISTED —
unlike ``shown`` (invisible), masking is "visible but redacted". The LLM
catalog annotates masked members so the insight layer does not narrate
redacted values as real ones.
"""

from __future__ import annotations

import hashlib
from typing import Any

from cubepy.schema.meta import Dimension, Measure
from cubepy.schema.registry import resolve_member
from cubepy.sqlgen.query import Query

MaskSpec = "str | tuple[str, Any] | None"


def apply_mask(value: Any, spec: MaskSpec) -> Any:
    """Apply one mask spec to a single value."""
    kind = spec
    param: Any = None
    if isinstance(spec, tuple):
        kind, param = spec[0], spec[1]
    if value is None:
        return value  # NULL stays NULL under any mask
    if kind == "fixed":
        return param if param is not None else "***"
    if kind == "partial":
        text = str(value)
        if len(text) <= 7:  # too short to partial-mask safely
            return "***"
        return f"{text[:3]}{'*' * (len(text) - 7)}{text[-4:]}"
    if kind == "hash":
        return hashlib.sha256(str(value).encode()).hexdigest()[:12]
    raise ValueError(f"unknown mask type {kind!r}")


def _member_mask(member: Measure | Dimension, ctx: Any) -> MaskSpec:
    if member.mask is None:
        return None
    return member.mask(ctx)


def _path_mask(path: str, ctx: Any) -> MaskSpec:
    """Resolve the mask spec for a result-column path (member path)."""
    try:
        _cube, _kind, member = resolve_member(path)
    except KeyError:
        return None
    return _member_mask(member, ctx)


def mask_rows(query: Query, rows: list[dict[str, Any]], ctx: Any) -> list[dict[str, Any]]:
    """Rewrite result values for members whose ``mask`` callback fires.

    Column keys map 1:1 to member paths (the envelope contract). Paths with no
    active mask pass through untouched; empty result sets short-circuit.
    """
    if not rows:
        return rows
    specs: dict[str, MaskSpec] = {}
    for path in [*query.measures, *query.dimensions]:
        spec = _path_mask(path, ctx)
        if spec is not None:
            specs[path] = spec
    for td in query.timeDimensions:
        spec = _path_mask(td.dimension, ctx)
        if spec is not None:
            specs.setdefault(td.dimension, spec)
    if not specs:
        return rows
    masked: list[dict[str, Any]] = []
    for row in rows:
        out = dict(row)
        for path, spec in specs.items():
            if path in out:
                out[path] = apply_mask(out[path], spec)
        masked.append(out)
    return masked
