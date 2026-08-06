"""Scaffold smoke test: app boots, /readyz responds, all subpackages import."""

from httpx import ASGITransport, AsyncClient

from cubepy.api.app import app


async def test_readyz() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        r = await ac.get("/readyz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_subpackages_import() -> None:
    import cubepy.api  # noqa: F401
    import cubepy.cache  # noqa: F401
    import cubepy.config  # noqa: F401
    import cubepy.orchestrator  # noqa: F401
    import cubepy.schema  # noqa: F401
    import cubepy.security  # noqa: F401
    import cubepy.sqlgen  # noqa: F401
    from cubepy.config import settings

    assert settings.cache_ttl_seconds > 0
