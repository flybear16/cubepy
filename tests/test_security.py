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


def test_from_jwt_rs256_accepts_and_rejects() -> None:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv_pem = priv.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    pub_pem = (
        priv.public_key()
        .public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )

    token = pyjwt.encode({"sub": "u1", "role": "admin"}, priv_pem, algorithm="RS256")
    ctx = SecurityContext.from_jwt(token, secret=pub_pem, algorithm="RS256")
    assert ctx.user_id == "u1"
    assert ctx.role == "admin"

    # A different keypair must reject the token.
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    other_pub = (
        other.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    with pytest.raises(pyjwt.PyJWTError):
        SecurityContext.from_jwt(token, secret=other_pub, algorithm="RS256")


async def test_auth_rs256_without_public_key_returns_500(monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi import HTTPException

    from cubepy.security import auth

    monkeypatch.setattr(auth.settings, "jwt_algorithm", "RS256")
    monkeypatch.setattr(auth.settings, "jwt_public_key", None)
    with pytest.raises(HTTPException) as exc:
        await auth.security_context(authorization="Bearer some-token")
    assert exc.value.status_code == 500


# --- PermissionBuilder.apply_row_level ---------------------------------------


def _orders_cube() -> CubeMeta:
    @cube(
        "Orders",
        "SELECT * FROM orders",
        security_context={"check_permission": lambda ctx: [f"Orders.tenant_id = {ctx.tenant_id}"]},
        # role convenience defaults only fire on cubes that declare these
        security_columns=("user_id", "department"),
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
    @cube("Orders", "SELECT * FROM orders", security_columns=("department",))
    class _O2:
        count = measure(None, MeasureType.COUNT)

    meta = registry.get("Orders")
    conds = PermissionBuilder.apply_row_level(meta, _ctx(role="manager", department="x' OR '1'='1"))
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
    # settings.jwt_secret (not the hardcoded default): a local .env overriding
    # CUBEPY_JWT_SECRET must not break this test.
    from cubepy.config import settings

    token = create_token({"sub": "u1", "role": "manager"}, secret=settings.jwt_secret)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        r = await ac.get("/whoami", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    assert r.json() == {"role": "manager", "user_id": "u1"}


def test_rls_defaults_skip_cubes_without_declared_columns() -> None:
    """A cube without user_id/department must NOT get the role defaults
    appended — live acceptance caught `dwdorders.user_id` crashing every
    viewer query on the trade domain."""
    @cube(
        "TradeOrders",
        "SELECT * FROM dwd_orders",
        security_context={"check_permission": lambda ctx: [f"orders.tenant_id = {ctx.tenant_id}"]},
        security_columns=("tenant_id",),
    )
    class _T:
        count = measure(None, MeasureType.COUNT)

    meta = registry.get("TradeOrders")
    conds = PermissionBuilder.apply_row_level(meta, _ctx(role="viewer", user_id="u7", tenant_id="42"))
    assert conds == ["orders.tenant_id = 42"]  # check_permission only, no default
