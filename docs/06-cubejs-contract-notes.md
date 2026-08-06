# 06 — Cube.js Contract Notes (porting reference)

Pinned from `cube.js/packages/{cubejs-schema-compiler,cubejs-api-gateway,cubejs-query-orchestrator}`
to keep CubePy faithful. Cross-checked against `docs/05` Python sketches. File:line refs are from
the cloned shallow snapshot (commit may differ but symbols are stable).

## 1. Query object (request body of `/load`, WS `subscribe`/`load` `params.query`)

```jsonc
{
  "measures": ["Orders.revenue", "Orders.count"],
  "dimensions": ["Orders.status", "Users.country"],
  "timeDimensions": [
    { "dimension": "Orders.createdAt", "granularity": "day",
      "dateRange": ["2026-01-01", "2026-02-01"] }   // or "last 7 days", "this month"
  ],
  "filters": [
    { "member": "Orders.status", "operator": "equals", "values": ["shipped","paid"] },
    { "or": [ { "member":"Users.country","operator":"equals","values":["CN"]},
              { "member":"Users.country","operator":"equals","values":["JP"]} ] }
  ],
  "segments": ["Orders.activeOrders"],
  "order": { "Orders.createdAt": "desc" },   // also: [["Orders.revenue","desc"]]
  "limit": 100,
  "offset": 0,
  "timezone": "UTC"
}
```

Member path is always `CubeName.memberName`.

## 2. Filter operators → SQL (Postgres dialect)

Source: `cubejs-schema-compiler/src/adapter/BaseQuery.js:4665-4682` (`filters` template table).
Public operator name (camelCase) → generated SQL:

| operator | SQL |
|---|---|
| `equals` | `col = value` (+`OR col IS NULL` when value can be null) |
| `notEquals` | `col <> value` |
| `in` | `col IN (v1, v2, …)` |
| `notIn` | `col NOT IN (v1, v2, …)` |
| `gt` / `gte` / `lt` / `lte` | `col >` / `>=` / `<` / `<= value` |
| `contains` | `col LIKE '%value%'` |
| `notContains` | `col NOT LIKE '%value%'` |
| `startsWith` | `col LIKE 'value%'` |
| `endsWith` | `col LIKE '%value'` |
| `set` | `col IS NOT NULL` |
| `notSet` | `col IS NULL` |
| `inDateRange` | `col >= from AND col <= to` |
| `notInDateRange` | `col < from OR col > to` |
| `beforeDate` | `col < value` |
| `afterDate` | `col > value` |
| `measureFilter` | applied as a HAVING filter on the measure (not WHERE) |

Filters combine with AND by default; `{or: [...]}` and `{and:[...]}` nest (composite filter).

## 3. Measures → SQL (PG)

Source: `PostgresQuery.ts`, `BaseQuery.js:3801-3884`.

| measure type | SQL |
|---|---|
| `sum` | `SUM(col)` |
| `count` (no sql) | `COUNT(*)` |
| `count` (sql given) | `COUNT(col)` |
| `countDistinct` | `COUNT(DISTINCT col)` |
| `countDistinctApprox` | `round(hll_cardinality(hll_add_agg(hll_hash_any(col))))` |
| `avg` / `min` / `max` | `AVG(col)` / `MIN(col)` / `MAX(col)` |
| `filteredMeasure` | `SUM(CASE WHEN <measureFilter> THEN col ELSE 0 END)` (approx; full impl aggregates) |

Param placeholders are `$1, $2, …` (PG `PostgresParamAllocator`). CubePy will use SQLAlchemy
bound params (`:param` / compile literal binds for the `/sql` endpoint).

## 4. Dimensions & timeDimensions (PG)

- Dimension types: `time`, `string`, `number`, `boolean`, `geo`, `array`, `primaryKey`.
- Granularity → `date_trunc('<unit>', dim)` where unit ∈ `second,minute,hour,day,week,month,quarter,year`
  (`PostgresQuery.ts:5-33`). Week/month/quarter are passed through to PG's `date_trunc`.
- Timezone conversion (if `query.timezone` set): `(dim::timestamptz AT TIME ZONE 'tz')`.
- `dateRange` accepts: `[startISO, endISO]`, single ISO date, or relative keywords
  (`today`, `yesterday`, `last 7 days`, `this month`, `last month`, `this quarter`,
  `this year`, `last N days` …). CubePy resolves keywords to concrete `[start,end]` server-side.

## 5. REST routes (`cubejs-api-gateway/src/gateway.ts`)

basePath = `/cubejs-api` (configurable). Auth via `Authorization: Bearer <jwt>`.

| Method | Path | Body | Returns |
|---|---|---|---|
| GET/POST | `/cubejs-api/v1/load` | `{query, queryType:'multi'|'regular', ...}` | `{data, annotation, sql?, usedPreAgregations, refreshKeyMatches, lastRefreshTime}` |
| GET/POST | `/cubejs-api/v1/sql` | `{query}` | `{sql: [{sql, ...}]}` |
| GET | `/cubejs-api/v1/meta` | — | `{cubes:[{name,measures[],dimensions[],segments[]}]}` |
| GET/POST | `/cubejs-api/v1/subscribe` | `{query, ...}` | long-poll variant (we implement WS instead) |
| GET | `/readyz` | — | health |

### Response envelope keys
- `data`: array of row dicts, keys are the member paths (and time-dim label for timeDimensions).
- `annotation`: `{measures:{path:{type,...}}, dimensions:{...}, timeDimensions:{...}}`.
- `lastRefreshTime`: ISO timestamp of last cache refresh.
- `usedPreAgregations`: array (empty when pre-agg skipped — our default).
- `refreshKeyMatches`: bool/array.

## 6. WebSocket subscribe protocol (`ws/message-schema.ts`)

Frame sequence:
1. Client sends **auth**: `{"authorization":"<jwt>"}` (strict, no extras).
2. Client sends **method** message (discriminated on `method`):
   ```jsonc
   { "method":"subscribe", "messageId":"1", "requestId":"r1",
     "params":{ "query":{...}, "queryType":"regular", "cache":true } }
   ```
   Also valid methods: `load`, `sql`, `dry-run`, `meta`, `unsubscribe`.
3. Server pushes **result** messages keyed by `messageId` when data changes:
   ```jsonc
   { "messageId":"1", "data":[...], "annotation":{...}, "lastRefreshTime":"..." }
   ```
4. Client cancels: `{"unsubscribe":"1"}` or `{"method":"unsubscribe","messageId":"1",...}`.

`refreshKey.every` (seconds) drives server-side polling; push only when the result hash changes
(matching `docs/04` "服务端轮询 + 变了才推"). `messageId` ≤ 16 chars OR int; `requestId` ≤ 64 chars.

## 7. GraphQL (high-level, `graphql.ts`)

- Root `Query` exposes `cube`/`cubes` field(s) taking `where`, `orderBy`, `limit`, plus the
  selected members as fields. Result object carries `annotation` + `lastRefreshTime` alongside data.
- CubePy (Strawberry) will expose `Query.cubes(where, orderBy, limit) -> [Cube]` where `Cube` is a
  dynamic resolver mapping requested members; reuse the orchestrator + security context. Full type
  fidelity (auto-generated per cube) is Phase 3; MVP uses a generic member resolver.

## 8. Permission model (`docs/03` + schema-compiler)

- `securityContext` on a cube provides `checkPermission(authContext) -> string[]` returning raw
  SQL WHERE fragments, OR’d/AND’d into the base query. Template tokens `${CUBE}`, `${Other}` are
  substituted with the cube’s SQL alias / joined cube alias.
- `shown(authContext, cube) -> bool` on cube/measure/dimension/segment controls visibility — a
  hidden member is dropped from SELECT (and the cube itself hidden from `/meta` if cube-level false).
- Auth: JWT (HS256 by default), decoded in middleware → `SecurityContext{user_id, role, dept, tenant_id}`.

## 9. What CubePy will deliberately NOT replicate (this pass)
- **Pre-aggregation** build/refresh/match — `PreAggRouter.match()` returns `None` + logs; the
  orchestrator always routes to the base cube. (`docs/02`,`docs/05` justify this for Hologres.)
- **Multi-stage / window / calculated measures** beyond sum/count/countDistinct/avg/min/max.
- **SQL passthrough (CubeSQL/Tesseract)** — out of scope; we speak REST/GraphQL/WS only.
- **Drivers beyond Postgres** (Hologres speaks the PG wire protocol).
