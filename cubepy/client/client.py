"""Async CubePy REST + WebSocket client."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import httpx
import websockets

TokenFactory = Callable[[], Awaitable[str]]


def _to_ws_url(base_url: str) -> str:
    return base_url.replace("http://", "ws://").replace("https://", "wss://")


class CubePyClient:
    """Async client for a CubePy / Cube.js REST API.

    Example::

        async with CubePyClient("http://localhost:8765", token=jwt) as c:
            result = await c.load({"measures": ["Orders.revenue"]})
            print(result["data"])

    Pass ``transport=httpx.ASGITransport(app=...)`` to drive the server
    in-process (used by the test suite, no open port required).
    """

    def __init__(
        self,
        base_url: str,
        *,
        token: str | None = None,
        token_factory: TokenFactory | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 30.0,
    ) -> None:
        if token is None and token_factory is None:
            raise ValueError("CubePyClient requires a token or a token_factory")
        self._token = token
        self._token_factory = token_factory
        self.base_url = base_url.rstrip("/")
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            transport=transport,
            timeout=timeout,
        )

    async def __aenter__(self) -> CubePyClient:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _headers(self) -> dict[str, str]:
        token = self._token
        if token is None and self._token_factory is not None:
            token = await self._token_factory()
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    async def load(self, query: dict[str, Any], *, query_type: str = "regular") -> dict[str, Any]:
        """Run a query; returns the full envelope (data, annotation, ...)."""
        r = await self._http.post(
            "/cubejs-api/v1/load",
            headers=await self._headers(),
            json={"query": query, "queryType": query_type},
        )
        r.raise_for_status()
        return r.json()

    async def meta(self) -> dict[str, Any]:
        r = await self._http.get("/cubejs-api/v1/meta", headers=await self._headers())
        r.raise_for_status()
        return r.json()

    async def sql(self, query: dict[str, Any]) -> dict[str, Any]:
        r = await self._http.post(
            "/cubejs-api/v1/sql",
            headers=await self._headers(),
            json={"query": query},
        )
        r.raise_for_status()
        return r.json()

    async def subscribe(
        self,
        query: dict[str, Any],
        *,
        every: float = 30.0,
    ) -> AsyncIterator[dict[str, Any]]:
        """Async iterator over subscribe pushes (``{data, annotation, ...}``).

        Implements the WS protocol: auth frame -> ``{method:subscribe}`` ->
        yield each server push keyed by ``messageId``. Closing the generator
        (``break`` / ``aclose``) sends unsubscribe and tears the socket down.
        """
        ws_url = _to_ws_url(self.base_url) + "/cubejs-api/v1/subscribe"
        token = self._token
        if token is None and self._token_factory is not None:
            token = await self._token_factory()
        async with websockets.connect(ws_url) as ws:
            await ws.send(json.dumps({"authorization": f"Bearer {token}"}))
            await ws.send(
                json.dumps(
                    {
                        "method": "subscribe",
                        "messageId": "1",
                        "params": {"query": query, "refreshKey": {"every": every}},
                    }
                )
            )
            async for raw in ws:
                yield json.loads(raw)
