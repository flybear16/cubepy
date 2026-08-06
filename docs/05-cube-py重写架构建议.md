# Python 重写 Cube.js：架构建议

## 核心模块拆解

```
Cube.js 架构：
┌─────────────────────────────────────────┐
│  1. Schema 层（语义模型）                 │
│     - Cube 定义（measures/dimensions/joins）│
│     - YAML/JS 声明式                    │
├─────────────────────────────────────────┤
│  2. 查询引擎（Query Orchestrator）        │
│     - 前端查询 → SQL 生成               │
│     - 多数据源适配                       │
│     - 预聚合路由（命中预聚合还是原始表）     │
├─────────────────────────────────────────┤
│  3. 权限层（Security Context）           │
│     - 行级/字段级权限                    │
│     - 认证中间件                        │
├─────────────────────────────────────────┤
│  4. 预聚合（Pre-aggregation）            │
│     - 自动构建/刷新聚合表                 │
│     - 查询路由到预聚合                   │
├─────────────────────────────────────────┤
│  5. 缓存层                              │
│     - 查询结果缓存（内存/Redis）          │
│     - 按刷新策略失效                     │
├─────────────────────────────────────────┤
│  6. API 层                             │
│     - REST / GraphQL                   │
│     - WebSocket 订阅推送                │
└─────────────────────────────────────────┘
```

## 技术选型

| 模块 | 推荐方案 | 理由 |
|------|---------|------|
| **Web 框架** | FastAPI | 原生 async、自带 OpenAPI、WebSocket 支持 |
| **ORM/SQL** | SQLAlchemy 2.0 | 多数据源、SQL 生成、表达式构建 |
| **Schema 定义** | Pydantic + YAML/Python decorator | 声明式定义 Cube 模型 |
| **缓存** | Redis (aioredis) | 成熟、快、和 Cube.js 对齐 |
| **WebSocket** | FastAPI WebSocket | 订阅推送 |
| **GraphQL** | Strawberry | Python 生态最好的 GraphQL 库 |
| **任务调度** | APScheduler / Celery | 预聚合定时刷新 |
| **配置管理** | Pydantic Settings | 环境变量 + 配置文件 |

## 架构设计

```
┌──────────────────────────────────────────────────┐
│                  py-cube 架构                     │
│                                                  │
│  ┌──────────┐  ┌──────────┐  ┌───────────────┐ │
│  │ REST API │  │ GraphQL  │  │ WebSocket 订阅 │ │
│  │ (FastAPI)│  │(Strawberry)│  │ (FastAPI WS) │ │
│  └────┬─────┘  └────┬─────┘  └──────┬────────┘ │
│       └──────────────┴───────────────┘          │
│                      ↓                           │
│           ┌──────────────────────┐               │
│           │   查询编排器          │               │
│           │  QueryOrchestrator   │               │
│           └──────────┬───────────┘               │
│                      ↓                           │
│    ┌─────────┬──────┴──────┬──────────┐         │
│    ↓         ↓             ↓          ↓         │
│ ┌─────────┐ ┌─────────┐ ┌────────┐ ┌────────┐  │
│ │ 权限层   │ │ 预聚合   │ │ 缓存层  │ │ Schema │  │
│ │Security │ │PreAgg   │ │ Cache  │ │Registry│  │
│ └─────────┘ └─────────┘ └────────┘ └────────┘  │
│                      ↓                           │
│           ┌──────────────────────┐               │
│           │   SQL 生成器          │               │
│           │  (SQLAlchemy Core)   │               │
│           └──────────┬───────────┘               │
│                      ↓                           │
│           ┌──────────────────────┐               │
│           │   多数据源适配层       │               │
│           │  PG/MySQL/Hologres   │               │
│           └──────────────────────┘               │
└──────────────────────────────────────────────────┘
```

## 核心模块实现

### 1. Schema 定义层

```python
from pydantic import BaseModel
from typing import Optional
from enum import Enum

class CubeMeta(BaseModel):
    name: str
    sql: str
    joins: dict = {}
    measures: dict = {}
    dimensions: dict = {}
    security_context: Optional[dict] = None

def cube(name: str, sql: str, **kwargs):
    def decorator(cls):
        meta = CubeMeta(
            name=name,
            sql=sql,
            joins=kwargs.get('joins', {}),
            measures=_extract_measures(cls),
            dimensions=_extract_dimensions(cls),
            security_context=kwargs.get('security_context'),
        )
        SchemaRegistry.register(meta)
        return cls
    return decorator

class MeasureType(Enum):
    SUM = "sum"
    COUNT = "count"
    AVG = "avg"
    MIN = "min"
    MAX = "max"

def measure(sql: str, mtype: MeasureType, shown=None):
    return {"sql": sql, "type": mtype.value, "shown": shown}

def dimension(sql: str, dtype: str = "string", shown=None):
    return {"sql": sql, "type": dtype, "shown": shown}
```

```python
@cube(
    name="Orders",
    sql="SELECT * FROM orders",
    joins={
        "Users": {"relationship": "belongsTo", "sql": "Orders.user_id = Users.id"},
    },
    security_context={
        "check_permission": lambda ctx: [f"Orders.tenant_id = {ctx.tenant_id}"],
    },
)
class OrdersCube:
    revenue = measure(sql="amount", mtype=MeasureType.SUM)
    profit = measure(
        sql="profit",
        mtype=MeasureType.SUM,
        shown=lambda ctx: ctx.role == "admin",
    )
    count = measure(sql="1", mtype=MeasureType.COUNT)
    category = dimension(sql="category", dtype="string")
    status = dimension(sql="status", dtype="string")
    created_at = dimension(sql="created_at", dtype="time")
```

### 2. 权限层

```python
from dataclasses import dataclass

@dataclass
class SecurityContext:
    user_id: str
    role: str
    department: str = ""
    tenant_id: str = ""
    extra: dict = None

    @classmethod
    def from_jwt(cls, token: str) -> "SecurityContext":
        import jwt
        payload = jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
        return cls(
            user_id=payload["sub"],
            role=payload["role"],
            department=payload.get("dept", ""),
            tenant_id=payload.get("tid", ""),
        )


class PermissionBuilder:
    @staticmethod
    def apply_row_level(cube_meta: CubeMeta, ctx: SecurityContext) -> list[str]:
        conditions = []
        if cube_meta.security_context:
            check_fn = cube_meta.security_context.get("check_permission")
            if check_fn:
                result = check_fn(ctx)
                if isinstance(result, list):
                    conditions.extend(result)

        if ctx.role == "viewer":
            conditions.append(f"{cube_meta.name}.user_id = '{ctx.user_id}'")
        elif ctx.role == "manager":
            conditions.append(f"{cube_meta.name}.department = '{ctx.department}'")

        return conditions

    @staticmethod
    def filter_fields(cube_meta: CubeMeta, ctx: SecurityContext) -> dict:
        visible = {"measures": {}, "dimensions": {}}
        for name, m in cube_meta.measures.items():
            if m["shown"] is None or m["shown"](ctx):
                visible["measures"][name] = m
        for name, d in cube_meta.dimensions.items():
            if d["shown"] is None or d["shown"](ctx):
                visible["dimensions"][name] = d
        return visible
```

### 3. SQL 生成器

```python
from sqlalchemy import select, func, and_
from sqlalchemy.sql.elements import literal_column

class SQLBuilder:
    def __init__(self, cube_meta: CubeMeta, dialect="postgresql"):
        self.cube = cube_meta
        self.dialect = dialect

    def build(self, query: dict, ctx: SecurityContext) -> str:
        base_table = literal_column(self.cube.sql).self_group()
        select_cols = []
        group_cols = []

        for measure in query.get("measures", []):
            m_def = self.cube.measures[measure.split(".")[1]]
            agg = self._get_agg(m_def)
            select_cols.append(agg.label(measure))

        for dim in query.get("dimensions", []):
            d_def = self.cube.dimensions[dim.split(".")[1]]
            col = literal_column(d_def["sql"]).label(dim)
            select_cols.append(col)
            group_cols.append(col)

        conditions = PermissionBuilder.apply_row_level(self.cube, ctx)
        conditions.extend(self._parse_filters(query.get("filters", [])))
        conditions.extend(self._parse_time(query.get("time_dimensions", [])))

        stmt = select(*select_cols)
        if group_cols:
            stmt = stmt.group_by(*group_cols)
        if conditions:
            stmt = stmt.where(and_(*[literal_column(c) for c in conditions]))
        stmt = stmt.select_from(base_table)

        return str(stmt.compile(compile_kwargs={"literal_binds": True}))
```

### 4. API 层

```python
from fastapi import FastAPI, Depends, WebSocket
from fastapi.security import HTTPBearer

app = FastAPI(title="PyCube API")
security = HTTPBearer()

@app.post("/api/v1/load")
async def load(body: QueryRequest, ctx: SecurityContext = Depends(get_security_context)):
    query = body.dict()
    cache_key = make_cache_key(query, ctx)
    cached = await redis.get(cache_key)
    if cached:
        return {"data": json.loads(cached), "from_cache": True}

    pre_agg = PreAggRouter.match(query)
    if pre_agg:
        sql = SQLBuilder(pre_agg.cube_meta).build(query, ctx, pre_agg=True)
    else:
        cube_meta = SchemaRegistry.get(query["cube"])
        sql = SQLBuilder(cube_meta).build(query, ctx)

    result = await db.execute(sql)
    await redis.setex(cache_key, TTL_300, json.dumps(result))
    return {"data": result, "sql": sql, "from_cache": False}


@app.websocket("/api/v1/subscribe")
async def subscribe(ws: WebSocket):
    await ws.accept()
    query = await ws.receive_json()
    ctx = SecurityContext.from_jwt(query["token"])
    last_hash = None

    while True:
        result = await execute_query(query, ctx)
        result_hash = hash(json.dumps(result, sort_keys=True))
        if result_hash != last_hash:
            await ws.send_json({"data": result, "refreshed": True})
            last_hash = result_hash
        await asyncio.sleep(query.get("refresh_interval", 30))
```

### 5. 预聚合

```python
from apscheduler.schedulers.asyncio import AsyncIOScheduler

class PreAggregationManager:
    def __init__(self):
        self.aggregations: list[PreAggDef] = []
        self.scheduler = AsyncIOScheduler()

    def register(self, agg_def: PreAggDef):
        self.aggregations.append(agg_def)
        self.scheduler.add_job(
            self._refresh, "interval",
            seconds=agg_def.refresh_every,
            args=[agg_def], id=agg_def.name,
        )

    async def _refresh(self, agg_def: PreAggDef):
        sql = agg_def.build_refresh_sql()
        await db.execute(f"CREATE TABLE IF NOT EXISTS {agg_def.table_name} AS {sql}")
        await db.execute(f"TRUNCATE {agg_def.table_name}; INSERT INTO {agg_def.table_name} {sql}")

    def match(self, query: dict) -> PreAggDef | None:
        for agg in self.aggregations:
            if agg.matches(query):
                return agg
        return None
```

## 开发优先级

```
Phase 1（MVP，2-3 周）          Phase 2（1-2 周）           Phase 3（持续）
┌─────────────────────┐     ┌──────────────────┐     ┌─────────────────┐
│ Schema 定义层       │     │ 预聚合            │     │ Superset 集成    │
│ 权限层（行级+字段）  │     │ WebSocket 订阅    │     │ 多数据源         │
│ SQL 生成器          │     │ 缓存层优化        │     │ 更多 measure     │
│ REST API（/load）   │     │ GraphQL          │     │ 前端 SDK         │
│ PG/Hologres 适配    │     │ 监控/日志         │     │ PyPI 发布        │
└─────────────────────┘     └──────────────────┘     └─────────────────┘
```

Phase 1 最关键——Schema + 权限 + SQL 生成 + 基本查询 API 跑通，就能替代 Cube.js 70% 的核心功能。

## 和 Superset 的关系

- **方案 A**：PyCube 独立运行，Superset 直接连 Hologres，各管各的
- **方案 B（推荐）**：PyCube 作为查询中间件，Superset 和前端都走 PyCube API，权限统一
- **方案 C**：把 PyCube 做成 Superset 的插件，最紧密但最复杂

## 一句话总结

> Cube.js 核心就 4 个模块：Schema 定义 + 权限注入 + SQL 生成 + 查询编排。用 FastAPI + SQLAlchemy 2.0 + Pydantic 重写，MVP 两三周能出。和 Superset 配合，PyCube 做查询代理 + 权限层，Superset 做 BI 可视化，是 Python 生态最干净的架构。
