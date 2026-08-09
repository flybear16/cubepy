"""Query executors over SQLAlchemy engines.

``AsyncEngineExecutor`` covers async DBAPIs (asyncpg, aiosqlite). DuckDB has no
async SQLAlchemy dialect (``duckdb-engine`` is sync), so ``SyncEngineExecutor``
runs the sync execute in a worker thread so the async orchestrator can drive it.
``make_engine_and_executor`` picks the right pair from the DSN scheme.
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Protocol

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.sql.elements import TextClause


def _json_safe(value: Any) -> Any:
    """Coerce DB types into JSON-serialisable Python values (Decimal, dates)."""
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def _rows(result: Any) -> list[dict[str, Any]]:
    return [{k: _json_safe(v) for k, v in row.items()} for row in result.mappings().all()]


class QueryExecutor(Protocol):
    async def execute(self, stmt: TextClause) -> list[dict[str, Any]]: ...


class AsyncEngineExecutor:
    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine

    async def execute(self, stmt: TextClause) -> list[dict[str, Any]]:
        async with self.engine.connect() as conn:
            result = await conn.execute(stmt)
            return _rows(result)

    async def execute_with_session(
        self, stmt: TextClause, session_sql: str | None = None
    ) -> list[dict[str, Any]]:
        """Run an optional session SQL (e.g. ``SET TIME ZONE 'UTC'``) on the same
        connection before the statement. Used for rollup queries whose UTC bucketing
        must not drift with the session timezone (plan §5/G1).
        """
        async with self.engine.connect() as conn:
            if session_sql:
                await conn.execute(text(session_sql))
            result = await conn.execute(stmt)
            # Materialise rows BEFORE commit — commit() closes the result, so a
            # SELECT read after it raises ResourceClosedError. DDL returns no rows.
            out = _rows(result) if result.returns_rows else []
            # Commit: the build path runs transactional DDL (DROP/CREATE TABLE),
            # which otherwise rolls back on connection close. A no-op for reads.
            await conn.commit()
            return out


class SyncEngineExecutor:
    """Runs a sync SQLAlchemy engine's execute in a worker thread.

    For dialects without an async DBAPI (e.g. DuckDB via duckdb-engine).
    """

    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    async def execute(self, stmt: TextClause) -> list[dict[str, Any]]:
        def _run() -> list[dict[str, Any]]:
            with self.engine.connect() as conn:
                result = conn.execute(stmt)
                return _rows(result)

        return await asyncio.to_thread(_run)

    async def execute_with_session(
        self, stmt: TextClause, session_sql: str | None = None
    ) -> list[dict[str, Any]]:
        def _run() -> list[dict[str, Any]]:
            with self.engine.connect() as conn:
                if session_sql:
                    conn.execute(text(session_sql))
                result = conn.execute(stmt)
                out = _rows(result) if result.returns_rows else []
                conn.commit()  # see AsyncEngineExecutor: persist transactional DDL.
                return out

        return await asyncio.to_thread(_run)


def make_engine_and_executor(
    dsn: str,
) -> tuple[AsyncEngine | Engine, QueryExecutor, bool]:
    """Build an engine + matching executor for a SQLAlchemy DSN.

    Returns ``(engine, executor, is_async)``. DuckDB (and other sync-only
    dialects) use a sync engine wrapped in ``SyncEngineExecutor``; everything
    else (asyncpg, aiosqlite) uses ``AsyncEngineExecutor``.
    """
    if dsn.startswith("duckdb"):
        engine: AsyncEngine | Engine = create_engine(dsn)
        return engine, SyncEngineExecutor(engine), False
    engine = create_async_engine(dsn, pool_pre_ping=True)
    return engine, AsyncEngineExecutor(engine), True

