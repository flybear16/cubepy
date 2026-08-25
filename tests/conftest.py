"""Shared pytest fixtures.

``pg_dsn`` (session-scoped) provisions a real Postgres via testcontainers and
seeds the sample schema. Falls back to ``$CUBEPY_TEST_PG_DSN`` if set. Skips
the whole integration module when neither is available.

``CUBEPY_TEST_HOLOGRES_EMU=1`` seeds the Hologres-constrained shape instead
(no FK / no PK, see ``seed_hologres.sql``) — used by the M1 smoke plan to
validate the application-layer assumptions before a real Hologres instance
is available.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa

_SAMPLES = Path(__file__).resolve().parent.parent / "cubepy" / "samples"
_SEED_SQL = _SAMPLES / "seed.sql"
_SEED_HOLOGRES_SQL = _SAMPLES / "seed_hologres.sql"


def _statements(sql_text: str) -> list[str]:
    """Split a seed script into individual statements.

    Hologres limits multi-statement execution in a single round trip, so the
    seed path executes statement-by-statement. ``--`` comments are stripped
    first (they may contain ``;``), then a naive ``;`` split — safe here since
    the seed scripts carry no ``;`` inside string literals.
    """
    stripped = "\n".join(line.split("--", 1)[0] for line in sql_text.splitlines())
    return [s for s in (part.strip() for part in stripped.split(";")) if s]


def _seed(url: str) -> None:
    # Normalise to the psycopg3 driver (psycopg2 is not installed).
    sync = url.replace("+asyncpg", "+psycopg").replace("+psycopg2", "+psycopg")
    seed_file = _SEED_HOLOGRES_SQL if os.environ.get("CUBEPY_TEST_HOLOGRES_EMU") == "1" else _SEED_SQL
    engine = sa.create_engine(sync)
    try:
        with engine.begin() as conn:
            for stmt in _statements(seed_file.read_text()):
                conn.exec_driver_sql(stmt)
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


@pytest.fixture
def pg_reseed(pg_dsn) -> str:
    """Reset the shared PG DB to the canonical seed (function-scoped).

    The ``pg_dsn`` fixture seeds once per session; some tests mutate ``orders``
    without restoring it. Tests that assert exact seed values depend on a known
    baseline, so they re-seed via this fixture before running.
    """
    _seed(pg_dsn)
    return pg_dsn
