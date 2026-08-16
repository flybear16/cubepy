# CubePy

Python implementation of an analytical **semantic layer + permission layer + API layer**,
modelled on [Cube.js](https://github.com/cube-js/cube) and tuned for Hologres/Postgres backends.

> CubePy is an independent project, not affiliated with, sponsored by, or endorsed by
> Cube Dev, Inc. See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) for licensing and
> trademark attribution.

Cube.js source (for cross-checking the contract) is vendored under `cube.js/` (shallow clone,
gitignored). Porting notes live in `docs/06-cubejs-contract-notes.md`.

## Status

| Layer | State |
|---|---|
| Schema (measures/dimensions/joins/segments, decorator + YAML) | ✅ |
| Permission (SecurityContext, JWT, RLS via `check_permission`, field-level `shown`) | ✅ |
| SQL generator (SQLAlchemy 2.0 `text`, full filter-operator table, time dimensions, joins) | ✅ |
| Query orchestrator (Redis cache, ctx-scoped cache key, in-flight dedupe, refreshKey probe) | ✅ |
| Pre-aggregation (rollup materialisation + fail-closed navigation + scheduled refresh) | ✅ MVP (off by default) |
| REST (`/cubejs-api/v1/{load,sql,meta,subscribe}` + `/readyz`) | ✅ |
| WebSocket subscribe (`/cubejs-api/v1/subscribe`, hash-change push) | ✅ |
| GraphQL (Strawberry, `/cubejs-api/graphql`) | ✅ |
| Async client SDK (`client/`, REST + WS subscribe) | ✅ |
| DuckDB data source (sync engine on a worker thread) | ✅ |
| Real Postgres integration tests (testcontainers) | ✅ |

**Pre-aggregation** is a same-DB materialised-rollup + aggregate-navigation MVP
(see `docs/07`, `docs/08`), off by default (`CUBEPY_PREAGG_ENABLED=false`). When enabled,
`PreAggRouter.match(query, ctx)` routes **fail-closed** — single cube, additive
SUM/COUNT measures, UTC timezone, granularity roll-up, RLS column coverage — and the
orchestrator transparently falls back to the base cube on any miss or error. Refresh is a
per-rollup asyncio task in `scheduler.py` driven by `refresh_key.every` (no scheduler
dependency). Hologres dynamic tables already maintain aggregates inside the engine, so the
app-level rollup layer is an optional accelerator rather than a requirement
(see `docs/02`, `docs/05`).

## Stack

FastAPI · SQLAlchemy 2.0 (async, asyncpg) · Pydantic v2 · Redis (`redis.asyncio`) ·
Strawberry · uv · pytest/testcontainers. Executors: asyncpg/aiosqlite run on an async
engine; DuckDB (no async dialect) runs on a sync engine in a worker thread.

## How a query flows

```
HTTP / WS / GraphQL
  └─ security_context (FastAPI dependency)   JWT → SecurityContext
     └─ QueryOrchestrator.load()
        ├─ Redis cache lookup (key scoped to the security context)
        ├─ refreshKey probe (source-data freshness signature)
        ├─ PreAggRouter.match()              rollup rewrite, or base cube
        ├─ SQLBuilder(query, ctx).build()    injects the RLS WHERE
        └─ QueryExecutor.execute()           SQL → envelope
```

## Run

```bash
uv sync --extra dev
export CUBEPY_JWT_SECRET=...                 # required in prod
export CUBEPY_PG_DSN=postgresql+asyncpg://...# Hologres speaks the PG wire protocol
export CUBEPY_REDIS_URL=redis://localhost:6379/0
uv run uvicorn cubepy.api.app:app --reload   # http://127.0.0.1:8000/docs
```

One-shot demo (testcontainers Postgres, auto-seeded, + local Redis) on port 8765:

```bash
uv run python run_server.py
```

## Define a cube

```python
from cubepy.schema.loader import cube, measure, dimension
from cubepy.schema.meta import MeasureType, PreAggregation

@cube("Orders", "orders",
      joins={"Users": {"relationship": "belongsTo", "sql": "Orders.user_id = Users.id"}},
      security_context={"check_permission": lambda ctx: [f"orders.tenant_id = {ctx.tenant_id}"]},
      security_columns=("tenant_id",),
      pre_aggregations=(
          PreAggregation(
              "daily", ("Orders.revenue", "Orders.count"), ("Orders.status",),
              "Orders.created_at", "day",
              refresh_key={"every": 300}, security_columns=("tenant_id",),
          ),
      ))
class OrdersCube:
    revenue  = measure("amount", MeasureType.SUM)
    count    = measure(None, MeasureType.COUNT)
    status   = dimension("status", "string")
    created  = dimension("created_at", "time")
```

Member names are the declared attribute names (no auto-camelCase). Cube aliases are
lowercased so Postgres's case-folding makes `Orders.user_id` / `Users.id` resolve consistently.
Full pre-aggregation usage guide: `docs/08-preaggregation-usage.md`.

## Query

```bash
# REST /load
curl -X POST localhost:8000/cubejs-api/v1/load \
  -H "Authorization: Bearer $JWT" -H "Content-Type: application/json" \
  -d '{"query":{"measures":["Orders.revenue"],"dimensions":["Orders.status"]}}'
```

WebSocket subscribe and GraphQL (`load(query: JSON)`) follow the cube.js shapes — see
`docs/06-cubejs-contract-notes.md` for the exact protocol.

## Test

```bash
uv run pytest                       # unit + integration (testcontainers Postgres)
uv run pytest -m "not integration"  # skip the container tests
CUBEPY_TEST_PG_DSN=... uv run pytest  # use a real/local Postgres instead of a container
```

## Docs

- [01 - Hologres 动态表与物化视图](docs/01-hologres-动态表与物化视图.md)
- [02 - Cube.js 预聚合 vs Hologres](docs/02-cube-hologres-预聚合对比.md)
- [03 - Cube.js 权限体系](docs/03-cube-权限体系.md)
- [04 - Cube.js 订阅机制](docs/04-cube-订阅机制.md)
- [05 - Py 重写架构建议](docs/05-cube-py重写架构建议.md)
- [06 - Cube.js Contract Notes (porting reference)](docs/06-cubejs-contract-notes.md)
- [07 - 预聚合方案调研](docs/07-预聚合方案调研.md)
- [08 - 预聚合使用指南](docs/08-preaggregation-usage.md)

## License

MIT
