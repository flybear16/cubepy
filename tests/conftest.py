"""Shared pytest fixtures.

``pg_dsn`` (session-scoped) provisions a real Postgres via testcontainers and
seeds the sample schema. Falls back to ``$CUBEPY_TEST_PG_DSN`` if set. Skips
the whole integration module when neither is available.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa

_SEED_SQL = Path(__file__).resolve().parent.parent / "cubepy" / "samples" / "seed.sql"


def _seed(url: str) -> None:
    # Normalise to the psycopg3 driver (psycopg2 is not installed).
    sync = url.replace("+asyncpg", "+psycopg").replace("+psycopg2", "+psycopg")
    engine = sa.create_engine(sync)
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql(_SEED_SQL.read_text())
    finally:
        engine.dispose()


@pytest.fixture(scope="session")
def pg_dsn() -> Iterator[str]:
    env_dsn = os.environ.get("CUBEPY_TEST_PG_DSN")
    if env_dsn:
        sync = env_dsn.replace("+asyncpg", "+psycopg")
        _seed(sync)
        return env_dsn

    try:
        from testcontainers.community.postgres import PostgresContainer
    except ImportError:
        pytest.skip("testcontainers not installed; set CUBEPY_TEST_PG_DSN to run PG tests")
        return ""  # unreachable

    container = PostgresContainer("postgres:16-alpine")
    try:
        container.start()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"postgres container unavailable: {exc}")
        return ""  # unreachable

    sync_url = container.get_connection_url()
    _seed(sync_url)
    async_url = sync_url.replace("+psycopg2", "+asyncpg").replace("+psycopg", "+asyncpg")
    yield async_url
    container.stop()
