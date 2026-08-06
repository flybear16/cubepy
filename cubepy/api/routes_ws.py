"""WebSocket subscribe endpoint, mirroring cube.js's /cubejs-api/v1/subscribe.

Protocol (docs/06 §6):
  1. client sends ``{"authorization": "Bearer <jwt>"}``
  2. client sends ``{"method": "subscribe", "messageId": "1",
                      "params": {"query": {...}, "refreshKey": {"every": 30}}}``
  3. server polls the orchestrator every ``every`` seconds and pushes
     ``{"messageId", "data", "annotation", "lastRefreshTime"}`` only when the
     result hash changes.
  4. client cancels with ``{"method": "unsubscribe", "messageId": "1"}`` or
     ``{"unsubscribe": "1"}``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from pydantic import ValidationError

from cubepy.config import settings
from cubepy.orchestrator.orchestrator import QueryOrchestrator
from cubepy.security.context import SecurityContext
from cubepy.sqlgen.query import Query

logger = logging.getLogger("cubepy.ws")

router = APIRouter()


def _data_hash(data: list[dict[str, Any]]) -> str:
    return hashlib.sha256(
        json.dumps(data, sort_keys=True, default=str).encode()
    ).hexdigest()


async def _poll(
    orch: QueryOrchestrator,
    ctx: SecurityContext,
    query: Query,
    every: float,
    message_id: str,
    websocket: WebSocket,
    send_lock: asyncio.Lock,
) -> None:
    last_hash: str | None = None
    while True:
        try:
            # Subscribe polls fresh each tick (bypasses result cache) so changes
            # are detected rather than masked by a warm cache entry.
            envelope = await orch.load(query, ctx, use_cache=False)
        except (ValueError, ValidationError, KeyError) as exc:
            async with send_lock:
                await websocket.send_json(
                    {"messageId": message_id, "type": "error", "error": str(exc)}
                )
            return
        current = _data_hash(envelope["data"])
        if current != last_hash:
            async with send_lock:
                await websocket.send_json(
                    {
                        "messageId": message_id,
                        "data": envelope["data"],
                        "annotation": envelope["annotation"],
                        "lastRefreshTime": envelope["lastRefreshTime"],
                    }
                )
            last_hash = current
        await asyncio.sleep(every)


@router.websocket("/v1/subscribe")
async def subscribe(websocket: WebSocket) -> None:
    await websocket.accept()

    try:
        auth_msg = await asyncio.wait_for(websocket.receive_json(), timeout=10)
    except (TimeoutError, WebSocketDisconnect, ValueError):
        await websocket.close(code=4401)
        return

    token = str(auth_msg.get("authorization", "")).removeprefix("Bearer ").strip()
    try:
        ctx = SecurityContext.from_jwt(
            token, secret=settings.jwt_secret, algorithm=settings.jwt_algorithm
        )
    except Exception:
        await websocket.close(code=4401)
        return

    orch: QueryOrchestrator = websocket.app.state.orchestrator
    subs: dict[str, asyncio.Task[None]] = {}
    send_lock = asyncio.Lock()

    try:
        while True:
            msg = await websocket.receive_json()
            method = msg.get("method")
            if method == "subscribe":
                message_id = str(msg.get("messageId"))
                params = msg.get("params") or {}
                try:
                    query = Query.parse(params.get("query") or {})
                except (ValidationError, ValueError) as exc:
                    async with send_lock:
                        await websocket.send_json(
                            {"messageId": message_id, "type": "error", "error": str(exc)}
                        )
                    continue
                every = float(
                    (params.get("refreshKey") or {}).get("every")
                    or settings.default_refresh_every
                )
                if message_id in subs:
                    subs[message_id].cancel()
                subs[message_id] = asyncio.create_task(
                    _poll(orch, ctx, query, every, message_id, websocket, send_lock)
                )
            elif method == "unsubscribe" or "unsubscribe" in msg:
                key = str(msg.get("messageId") or msg.get("unsubscribe"))
                task = subs.pop(key, None)
                if task is not None:
                    task.cancel()
    except WebSocketDisconnect:
        pass
    finally:
        for task in subs.values():
            task.cancel()
        for task in subs.values():
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
