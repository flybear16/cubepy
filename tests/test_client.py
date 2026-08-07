"""Client SDK REST tests (G021). Drives the app in-process via ASGITransport."""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator

import fakeredis
import pytest
import uvicorn
from httpx import ASGITransport
from sqlalchemy.sql.elements import TextClause

from cubepy.api.app import create_app
from cubepy.cache.redis_cache import RedisCache
from cubepy.client import CubePyClient
from cubepy.config import settings
from cubepy.orchestrator.orchestrator import QueryOrchestrator
from cubepy.schema.loader import cube, measure
from cubepy.schema.meta import MeasureType
from cubepy.schema.registry import registry
from cubepy.security.context import create_token


class _FakeExecutor:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows

    async def execute(self, stmt: TextClause) -> list[dict]:
        return [dict(r) for r in self.rows]


@pytest.fixture(autouse=True)
def _orders() -> Iterator[None]:
    registry.clear()

    @cube("Orders", "orders")
    class _O:
        revenue = measure("amount", MeasureType.SUM)

    yield
    registry.clear()


def _token() -> str:
    return create_token({"sub": "u1", "role": "admin", "tid": "42"}, secret=settings.jwt_secret)


def _client(rows: list[dict]) -> CubePyClient:
    orch = QueryOrchestrator(
        RedisCache(fakeredis.FakeAsyncRedis()), _FakeExecutor(rows), settings=settings
    )
    app = create_app(orchestrator=orch)
    return CubePyClient("http://test", token=_token(), transport=ASGITransport(app=app))


async def test_client_load_returns_envelope() -> None:
    async with _client([{"Orders.revenue": 40.0}]) as c:
        env = await c.load({"measures": ["Orders.revenue"]})
    assert env["data"] == [{"Orders.revenue": 40.0}]
    assert env["annotation"]["measures"]["Orders.revenue"]["type"] == "sum"


async def test_client_meta_and_sql() -> None:
    async with _client([]) as c:
        meta = await c.meta()
        sql = await c.sql({"measures": ["Orders.revenue"]})
    assert meta["cubes"][0]["name"] == "Orders"
    assert "sum(amount)" in sql["sql"][0]["sql"]


async def test_client_token_factory() -> None:
    orch = QueryOrchestrator(
        RedisCache(fakeredis.FakeAsyncRedis()),
        _FakeExecutor([{"Orders.revenue": 1.0}]),
        settings=settings,
    )
    app = create_app(orchestrator=orch)

    async def factory() -> str:
        return _token()

    async with CubePyClient("http://test", token_factory=factory, transport=ASGITransport(app=app)) as c:
        env = await c.load({"measures": ["Orders.revenue"]})
    assert env["data"] == [{"Orders.revenue": 1.0}]


def test_client_requires_token_or_factory() -> None:
    with pytest.raises(ValueError):
        CubePyClient("http://test")


# --- WebSocket subscribe (real uvicorn server) -------------------------------


class _StatefulExecutor:
    def __init__(self, sequence: list[list[dict]]) -> None:
        self.sequence = sequence
        self.i = 0

    async def execute(self, stmt: TextClause) -> list[dict]:
        rows = self.sequence[min(self.i, len(self.sequence) - 1)]
        self.i += 1
        return [dict(r) for r in rows]


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wait_for_port(port: int, timeout: float = 10.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.05)
    raise RuntimeError(f"server on port {port} did not start")


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> Iterator[int]:
    # localhost connections must not be routed through a host SOCKS/HTTP proxy.
    for var in (
        "ALL_PROXY", "HTTPS_PROXY", "HTTP_PROXY",
        "all_proxy", "https_proxy", "http_proxy",
    ):
        monkeypatch.delenv(var, raising=False)

    port = _free_port()
    orch = QueryOrchestrator(
        RedisCache(fakeredis.FakeAsyncRedis()),
        _StatefulExecutor(
            [[{"Orders.revenue": 1.0}], [{"Orders.revenue": 1.0}], [{"Orders.revenue": 2.0}]]
        ),
        settings=settings,
    )
    app = create_app(orchestrator=orch)
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    instance = uvicorn.Server(config)
    thread = threading.Thread(target=instance.run, daemon=True)
    thread.start()
    _wait_for_port(port)
    yield port
    instance.should_exit = True
    thread.join(timeout=5)


async def test_client_subscribe_yields_pushes(server: int) -> None:
    port = server
    async with CubePyClient(f"http://127.0.0.1:{port}", token=_token()) as c:
        gen = c.subscribe({"measures": ["Orders.revenue"]}, every=0.05)
        first = await gen.__anext__()
        second = await gen.__anext__()
        await gen.aclose()
    assert first["data"] == [{"Orders.revenue": 1.0}]
    assert second["data"] == [{"Orders.revenue": 2.0}]
