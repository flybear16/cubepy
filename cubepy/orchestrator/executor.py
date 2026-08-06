"""Async query executor over a SQLAlchemy 2.0 ``AsyncEngine``."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.sql.elements import TextClause


def _json_safe(value: Any) -> Any:
    """Coerce DB types into JSON-serialisable Python values (Decimal, dates)."""
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


class QueryExecutor(Protocol):
    async def execute(self, stmt: TextClause) -> list[dict[str, Any]]: ...


class AsyncEngineExecutor:
    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine

    async def execute(self, stmt: TextClause) -> list[dict[str, Any]]:
        async with self.engine.connect() as conn:
            result = await conn.execute(stmt)
            return [
                {k: _json_safe(v) for k, v in row.items()}
                for row in result.mappings().all()
            ]
