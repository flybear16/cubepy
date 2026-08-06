"""Unit tests for the permission layer (G004)."""

from __future__ import annotations

from collections.abc import Iterator

import jwt as pyjwt
import pytest
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient

from cubepy.schema.loader import cube, dimension, measure
from cubepy.schema.meta import CubeMeta, MeasureType
from cubepy.schema.registry import registry
from cubepy.security.auth import security_context
from cubepy.security.context import SecurityContext, create_token
from cubepy.security.permissions import PermissionBuilder

SECRET = "test-secret"


@pytest.fixture(autouse=True)
def _isolate_registry() -> Iterator[None]:
    registry.clear()
    yield
    registry.clear()


def _ctx(**kw: object) -> SecurityContext:
    return SecurityContext(
        user_id=str(kw.get("user_id", "")),
        role=str(kw.get("role", "viewer")),
        department=str(kw.get("department", "")),
        tenant_id=str(kw.get("tenant_id", "")),
    )


# --- SecurityContext.from_jwt -------------------------------------------------

def test_from_jwt_maps_known_claims_and_keeps_custom() -> None:
    token = create_token(
        {"sub": "u1", "role": "admin", "dept": "sales", "tid": "t9", "region": "APAC"},
        secret=SECRET,
    )
    ctx = SecurityContext.from_jwt(token, secret=SECRET)
    assert ctx.user_id == "u1"
    assert ctx.role == "admin"
    assert ctx.department == "sales"
    assert ctx.tenant_id == "t9"
    assert ctx.claims["region"] == "APAC"


def test_from_jwt_invalid_token_raises() -> None:
    with pytest.raises(pyjwt.PyJWTError):
        SecurityContext.from_jwt("not.a.jwt", secret=SECRET)


# --- PermissionBuilder.apply_row_level ---------------------------------------

def _orders_cube() -> CubeMeta:
    @cube(
        "Orders",
        "SELECT * FROM orders",
        security_context={"check_permission": lambda ctx: [f"Orders.tenant_id = {ctx.tenant_id}"]},
    )
    class _O:
        count = measure(None, MeasureType.COUNT)

    return registry.get("Orders")


def test_rls_check_permission_fragments_included() -> None:
    meta = _orders_cube()
    ctx = _ctx(role="admin", tenant_id="42")
    conds = PermissionBuilder.apply_row_level(meta, ctx)
    assert "Orders.tenant_id = 42" in conds


def test_rls_viewer_and_manager_defaults() -> None:
    meta = _orders_cube()
    viewer = PermissionBuilder.apply_row_level(meta, _ctx(role="viewer", user_id="u7"))
    assert any("Orders.user_id = 'u7'" in c for c in viewer)

    manager = PermissionBuilder.apply_row_level(meta, _ctx(role="manager", department="sales"))
    assert any("Orders.department = 'sales'" in c for c in manager)

    admin = PermissionBuilder.apply_row_level(meta, _ctx(role="admin"))
    assert all("Orders.user_id" not in c and "Orders.department" not in c for c in admin)


def test_rls_quotes_escaped_to_prevent_injection() -> None:
    @cube("Orders", "SELECT * FROM orders")
    class _O2:
        count = measure(None, MeasureType.COUNT)

    meta = registry.get("Orders")
    conds = PermissionBuilder.apply_row_level(
        meta, _ctx(role="manager", department="x' OR '1'='1")
    )
    joined = " AND ".join(conds)
    assert "OR '1'='1'" not in joined
    assert "''" in joined  # escaped


# --- PermissionBuilder.filter_fields -----------------------------------------

def test_filter_fields_honours_shown() -> None:
    @cube("Orders", "SELECT * FROM orders")
    class _O3:
        revenue = measure("amount", MeasureType.SUM, shown=lambda ctx: ctx.role == "admin")
        public = measure("amount", MeasureType.SUM)
        secret_dim = dimension("secret", shown=lambda ctx: ctx.role == "admin")
        public_dim = dimension("status")

    meta = registry.get("Orders")
    m_admin, d_admin, _ = PermissionBuilder.filter_fields(meta, _ctx(role="admin"))
    assert {x.name for x in m_admin} == {"revenue", "public"}
    assert {x.name for x in d_admin} == {"secret_dim", "public_dim"}

    m_viewer, d_viewer, _ = PermissionBuilder.filter_fields(meta, _ctx(role="viewer"))
    assert {x.name for x in m_viewer} == {"public"}
    assert {x.name for x in d_viewer} == {"public_dim"}


def test_cube_visibility() -> None:
    @cube("Secret", "SELECT * FROM s", shown=lambda ctx: ctx.role == "admin")
    class _S:
        count = measure(None, MeasureType.COUNT)

    meta = registry.get("Secret")
    assert PermissionBuilder.cube_visible(meta, _ctx(role="admin")) is True
    assert PermissionBuilder.cube_visible(meta, _ctx(role="viewer")) is False


# --- FastAPI dependency -------------------------------------------------------

def _app() -> FastAPI:
    app = FastAPI()

    @app.get("/whoami")
    async def whoami(ctx: SecurityContext = Depends(security_context)) -> dict[str, str]:
        return {"role": ctx.role, "user_id": ctx.user_id}

    return app


async def test_dep_rejects_missing_and_bad_token() -> None:
    app = _app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        assert (await ac.get("/whoami")).status_code == 401
        r = await ac.get("/whoami", headers={"Authorization": "Bearer garbage"})
        assert r.status_code == 401


async def test_dep_accepts_valid_bearer() -> None:
    app = _app()
    token = create_token({"sub": "u1", "role": "manager"}, secret="dev-secret-change-me")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        r = await ac.get("/whoami", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    assert r.json() == {"role": "manager", "user_id": "u1"}
