"""WebSocket subscribe integration tests (G008)."""

from __future__ import annotations

from collections.abc import Iterator

import fakeredis
import pytest
from fastapi import FastAPI
from sqlalchemy.sql.elements import TextClause
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from cubepy.api.app import create_app
from cubepy.cache.redis_cache import RedisCache
from cubepy.config import settings
from cubepy.orchestrator.orchestrator import QueryOrchestrator
from cubepy.schema.loader import cube, measure
from cubepy.schema.meta import MeasureType
from cubepy.schema.registry import registry
from cubepy.security.context import create_token


class _FixedExecutor:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows

    async def execute(self, stmt: TextClause) -> list[dict]:
        return [dict(r) for r in self.rows]


class _ChangingExecutor:
    """Returns each successive row-set, holding the last one forever."""

    def __init__(self, sequence: list[list[dict]]) -> None:
        self.sequence = sequence
        self.i = 0

    async def execute(self, stmt: TextClause) -> list[dict]:
        rows = self.sequence[min(self.i, len(self.sequence) - 1)]
        self.i += 1
        return [dict(r) for r in rows]


@pytest.fixture(autouse=True)
def _orders() -> Iterator[None]:
    registry.clear()

    @cube("Orders", "orders")
    class _O:
        revenue = measure("amount", MeasureType.SUM)

    yield
    registry.clear()


def _app(executor: object) -> FastAPI:
    orch = QueryOrchestrator(
        RedisCache(fakeredis.FakeAsyncRedis()), executor, settings=settings  # type: ignore[arg-type]
    )
    return create_app(orchestrator=orch)


def _token(role: str = "admin") -> str:
    return create_token({"sub": "u1", "role": role}, secret=settings.jwt_secret)


def test_ws_bad_auth_is_closed() -> None:
    app = _app(_FixedExecutor([{"Orders.revenue": 1}]))
    with TestClient(app) as client:
        with client.websocket_connect("/cubejs-api/v1/subscribe") as ws:
            ws.send_json({"authorization": "not-a-jwt"})
            with pytest.raises((WebSocketDisconnect, Exception)):
                ws.receive_json()


def test_ws_subscribe_first_push() -> None:
    app = _app(_FixedExecutor([{"Orders.revenue": 42}]))
    with TestClient(app) as client:
        with client.websocket_connect("/cubejs-api/v1/subscribe") as ws:
            ws.send_json({"authorization": f"Bearer {_token()}"})
            ws.send_json(
                {
                    "method": "subscribe",
                    "messageId": "1",
                    "params": {
                        "query": {"measures": ["Orders.revenue"]},
                        "refreshKey": {"every": 0.05},
                    },
                }
            )
            msg = ws.receive_json()
    assert msg["messageId"] == "1"
    assert msg["data"] == [{"Orders.revenue": 42}]
    assert msg["annotation"]["measures"]["Orders.revenue"]["type"] == "sum"


def test_ws_pushes_only_on_change() -> None:
    # Executor returns A, A, B -> client should see exactly two pushes: A then B.
    app = _app(
        _ChangingExecutor(
            [[{"Orders.revenue": 1}], [{"Orders.revenue": 1}], [{"Orders.revenue": 2}]]
        )
    )
    with TestClient(app) as client:
        with client.websocket_connect("/cubejs-api/v1/subscribe") as ws:
            ws.send_json({"authorization": f"Bearer {_token()}"})
            ws.send_json(
                {
                    "method": "subscribe",
                    "messageId": "9",
                    "params": {
                        "query": {"measures": ["Orders.revenue"]},
                        "refreshKey": {"every": 0.05},
                    },
                }
            )
            first = ws.receive_json()
            second = ws.receive_json()
    assert first["data"] == [{"Orders.revenue": 1}]
    assert second["data"] == [{"Orders.revenue": 2}]
