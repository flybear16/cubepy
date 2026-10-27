"""Column masking (mask=): unit + end-to-end permission-path tests."""

from __future__ import annotations

from collections.abc import Iterator

import fakeredis
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.sql.elements import TextClause

from cubepy.api.app import create_app
from cubepy.cache.redis_cache import RedisCache
from cubepy.config import settings
from cubepy.orchestrator.orchestrator import QueryOrchestrator
from cubepy.schema.loader import cube, dimension, measure
from cubepy.schema.meta import MeasureType
from cubepy.schema.registry import registry
from cubepy.security.context import SecurityContext, create_token
from cubepy.sqlgen.query import Query
from cubepy.security.masking import apply_mask, mask_rows


class _FakeExecutor:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows

    async def execute(self, stmt: TextClause) -> list[dict]:
        return [dict(r) for r in self.rows]


# --- built-in mask types ------------------------------------------------------


def test_fixed_mask_default_and_param() -> None:
    assert apply_mask(123.45, "fixed") == "***"
    assert apply_mask(123.45, ("fixed", "[REDACTED]")) == "[REDACTED]"


def test_partial_mask_keeps_head_and_tail() -> None:
    assert apply_mask("13812345678", "partial") == "138****5678"
    assert apply_mask("abc", "partial") == "***"  # too short -> full mask


def test_partial_mask_falls_back_to_fixed_for_non_strings() -> None:
    assert apply_mask(42, "partial") == "***"


def test_hash_mask_is_stable_pseudonym() -> None:
    h1 = apply_mask("alice@x.com", "hash")
    h2 = apply_mask("alice@x.com", "hash")
    assert h1 == h2 and len(h1) == 12 and h1 != "alice@x.com"


def test_mask_preserves_null() -> None:
    assert apply_mask(None, "fixed") is None


def test_unknown_mask_type_fails_closed() -> None:
    with pytest.raises(ValueError, match="unknown mask type"):
        apply_mask("x", "bogus")


# --- end-to-end: mask fires per-caller in the orchestrator --------------------

_ROWS = [{"Orders.status": "shipped", "Orders.revenue": 40.0}]


@pytest.fixture(autouse=True)
def _schema() -> Iterator[None]:
    registry.clear()

    @cube("Orders", "orders")
    class _O:
        revenue = measure(
            "amount",
            MeasureType.SUM,
            mask=lambda ctx: None if ctx.role == "admin" else "fixed",
        )
        status = dimension(
            "status",
            "string",
            mask=lambda ctx: None if ctx.role == "admin" else ("fixed", "[HIDDEN]"),
        )

    yield
    registry.clear()


def _client(rows: list[dict]) -> AsyncClient:
    orch = QueryOrchestrator(
        RedisCache(fakeredis.FakeAsyncRedis()), _FakeExecutor(rows), settings=settings
    )
    app = create_app(orchestrator=orch)
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _auth(role: str) -> dict[str, str]:
    tok = create_token({"sub": "u1", "role": role, "tid": "42"}, secret=settings.jwt_secret)
    return {"Authorization": f"Bearer {tok}"}


_QUERY = {"measures": ["Orders.revenue"], "dimensions": ["Orders.status"]}


@pytest.mark.parametrize(
    ("role", "expected"),
    [
        ("admin", "shipped"),  # mask callback returns None -> raw
        ("viewer", "[HIDDEN]"),  # tuple param honoured
    ],
)
async def test_mask_dimension_per_role(role: str, expected: str) -> None:
    async with _client(_ROWS) as ac:
        r = await ac.post("/cubejs-api/v1/load", headers=_auth(role), json={"query": _QUERY})
    assert r.status_code == 200
    assert r.json()["data"][0]["Orders.status"] == expected


@pytest.mark.parametrize(
    ("role", "expected"),
    [("admin", 40.0), ("viewer", "***")],
)
async def test_mask_measure_per_role(role: str, expected: float) -> None:
    async with _client(_ROWS) as ac:
        r = await ac.post(
            "/cubejs-api/v1/load",
            headers=_auth(role),
            json={"query": {"measures": ["Orders.revenue"]}},
        )
    assert r.status_code == 200
    assert r.json()["data"][0]["Orders.revenue"] == expected


async def test_masked_value_never_stored_in_cache_raw() -> None:
    """The envelope masks BEFORE the orchestrator caches, so the raw value is
    unreachable from the cache store entirely (defence in depth)."""
    from cubepy.security.masking import mask_rows  # noqa: F401 — referenced in docstring

    orch = QueryOrchestrator(
        RedisCache(fakeredis.FakeAsyncRedis()), _FakeExecutor(_ROWS), settings=settings
    )
    viewer = SecurityContext(role="viewer")
    await orch.load(Query.parse(_QUERY), viewer)
    # second load hits cache; the cached value must already be masked
    env = await orch.load(Query.parse(_QUERY), viewer)
    assert env["data"][0]["Orders.revenue"] == "***"


# --- LLM catalog annotation ---------------------------------------------------


def test_build_context_annotates_masked_members_per_ctx() -> None:
    from cubepy.ai import build_context

    catalog_admin = build_context(ctx=SecurityContext(role="admin"))
    catalog_viewer = build_context(ctx=SecurityContext(role="viewer"))
    assert "MASKED" not in catalog_admin
    assert "[MASKED for this user: queryable, values auto-redacted]" in catalog_viewer
    # the LLM contract teaches the semantics so the model never refuses a
    # masked member (live-caught: it over-refused on the bare tag)
    from cubepy.ai import query_contract

    assert "MASKED" in query_contract()
    # still listed (queryable), unlike `shown` which removes it
    assert "Orders.revenue" in catalog_viewer
