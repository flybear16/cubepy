# Ultragoal Brief: CubePy (Python rewrite of Cube.js)

## Goal
Download Cube.js source, analyze it alongside the project docs, and implement a working
Python port ("CubePy") that replaces ~70–90% of Cube.js core for a Hologres/Postgres backend.

## Decisions (confirmed with user)
- **Scope: Phase 1 + Phase 2** (per README `docs/`). Phase 1 = Schema + Permission + SQL gen +
  REST API + PG/Hologres adapter. Phase 2 = WebSocket subscribe + Strawberry GraphQL + Redis cache.
- **Database: real Postgres** for integration tests. Postgres 16 is running on localhost:5432;
  Docker available as fallback (testcontainers).
- **Pre-aggregation: SKIPPED.** `docs/02` + `docs/05` argue Hologres dynamic tables replace
  Cube.js pre-agg at the DB layer. We keep a `PreAggRouter` *abstraction* that no-ops + logs,
  so a real impl can plug in later (YAGNI now, but seam preserved).
- **Tooling**: uv (project + venv), FastAPI, SQLAlchemy 2.0 (async), Pydantic v2 + pydantic-settings,
  redis (aioredis via redis-py async), strawberry-graphql, APScheduler, psycopg[async] or asyncpg,
  pytest + pytest-asyncio. Target PG/Hologres dialect.

## Reference material
- Project docs: `docs/01..05` (architecture, Hologres, permission, subscribe, rewrite spec).
- Cube.js source (shallow clone): `cube.js/packages/` — primary: `cubejs-schema-compiler`,
  `cubejs-api-gateway`, `cubejs-query-orchestrator`. Cross-check fidelity, don't port JSverbatim.
- The docs already contain a near-complete Python sketch (doc 05); treat it as the spec,
  refine against cube.js source for edge cases (filter operators, response envelopes).

## Non-goals (this pass)
- Pre-aggregation build/refresh (stubbed).
- Phase 3: Superset integration, multi-datasource beyond PG, frontend SDK, PyPI publish.
- RBAC beyond securityContext (no role table / permission DB).

## Out of scope but noted
- Real Hologres DSN not provided; PG-compatible SQL is sufficient (Hologres speaks PG wire).
