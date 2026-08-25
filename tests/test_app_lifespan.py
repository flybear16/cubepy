"""App factory: real lifespan startup/shutdown + readyz component probes.

``create_app(orchestrator=None)`` builds its own engine/Redis/scheduler in the
lifespan — the path API tests normally bypass by injecting an orchestrator.

These tests drive the app through ``lifespan_context`` + ASGITransport (not
TestClient): the portal thread TestClient uses is only partially traced by
coverage.py, while ASGITransport runs everything in the test's event loop.
"""

from __future__ import annotations

from types import SimpleNamespace

import fakeredis
import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from starlette.requests import Request

import cubepy.api.app as app_module
from cubepy.api.app import create_app
from cubepy.api.deps import get_orchestrator, get_settings
from cubepy.config import settings
from cubepy.schema.registry import registry


def test_get_settings_returns_singleton() -> None:
    assert get_settings() is settings


def test_get_orchestrator_uninitialized_raises_503() -> None:
    scope = {
        "type": "http",
        "app": SimpleNamespace(state=SimpleNamespace(orchestrator=None)),
    }
    with pytest.raises(HTTPException) as ei:
        get_orchestrator(Request(scope))
    assert ei.value.status_code == 503


def _patch_redis(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        app_module.redis_asyncio,
        "from_url",
        lambda url, **kw: fakeredis.FakeAsyncRedis(),
    )


async def _readyz(monkeypatch: pytest.MonkeyPatch, dsn: str, preagg: bool):
    registry.clear()
    monkeypatch.setattr(settings, "db_dsn", dsn)
    monkeypatch.setattr(settings, "preagg_enabled", preagg)
    _patch_redis(monkeypatch)

    app = create_app()
    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            r = await ac.get("/readyz")
    return r


async def test_lifespan_sync_engine_duckdb(monkeypatch: pytest.MonkeyPatch) -> None:
    r = await _readyz(monkeypatch, "duckdb:///:memory:", preagg=False)
    # A sync engine can't run the async-readyz probe -> db down; redis (fake) is up.
    assert r.status_code == 503
    assert r.json()["components"] == {"db": "down", "redis": "up"}


async def test_lifespan_async_engine_real_pg(
    pg_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Full startup: async engine + scheduler (preagg on) + readyz db/redis up."""
    r = await _readyz(monkeypatch, pg_dsn, preagg=True)
    assert r.status_code == 200, r.text
    assert r.json()["components"] == {"db": "up", "redis": "up"}
