# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概览

CubePy 是 [Cube.js](https://github.com/cube-js/cube) 语义层的 Python 重写，面向 **分析语义层 + 权限层 + API 层**，针对 Hologres/Postgres 后端调优（Hologres 兼容 PG 线协议，走 asyncpg 驱动）。

`cube.js/` 目录是上游 Cube.js 的 shallow clone 参考副本（已被 `.gitignore`），**不是本项目源码**。移植契约记录在 `docs/06-cubejs-contract-notes.md`；架构与调研文档在 `docs/`。不要修改 `cube.js/`，需要核对行为时只读它。

**预聚合层**：实装的「同库物化 rollup + 聚合导航」MVP（见 `docs/07`、`docs/08`）。默认关闭（`CUBEPY_PREAGG_ENABLED=false`），开启后 `PreAggRouter.match(query, ctx)` 做 fail-closed 路由——同时满足单 cube、SUM/COUNT 可加、UTC、粒度可上卷、RLS 列覆盖才走 rollup 表，否则或异常时透明回退 base cube。刷新由 `scheduler.py` 的 per-rollup asyncio 定时任务（`refresh_key.every`）驱动。Hologres 动态表在引擎内维护聚合，应用层 rollup 是可选加速而非必需（见 `docs/02`、`docs/05`）。

## 常用命令

```bash
# 环境（uv 管理，Python >=3.11）
uv sync --extra dev

# 测试
uv run pytest                       # 全量（含 testcontainers PG 集成测试）
uv run pytest -m "not integration"  # 跳过需要 Postgres 容器的测试
uv run pytest tests/test_sqlgen.py::test_xxx -v   # 跑单个测试
uv run pytest tests/test_sqlgen.py -k "keyword"   # 按名筛选

# Lint / 格式
uv run ruff check .
uv run ruff format .

# 运行服务（生产式：lifespan 内自建 engine + Redis）
uv run uvicorn cubepy.api.app:app --reload   # http://127.0.0.1:8000/docs

# 一键 demo：testcontainers PG（自动 seed）+ 本地 Redis，端口 8765
uv run python run_server.py
```

集成测试默认用 testcontainers 拉 `postgres:16-alpine`；也可用 `CUBEPY_TEST_PG_DSN=postgresql+asyncpg://...` 指向本地 PG。

## 架构（分层 + 请求流）

单一请求流贯穿以下分层，**理解这条链就理解了项目**：

```
HTTP/WS/GraphQL 请求
  └─ security_context (FastAPI dep)     JWT → SecurityContext
     └─ QueryOrchestrator.load()
        ├─ Redis 缓存查找（key 按 ctx 作用域）
        ├─ refreshKey 探针（源数据新鲜度签名）
        ├─ SQLBuilder(query, ctx).build()  ← 注入行级安全 WHERE
        └─ QueryExecutor.execute()          → SQL → envelope
```

- **`schema/`** — 语义模型 DSL。`@cube` 装饰器 + YAML loader（`loader.py`），编译为 frozen dataclass（`meta.py`: `CubeMeta`/`Measure`/`Dimension`/`Join`），注册到进程级单例 `registry`（`registry.py`）。**依赖方向铁律：schema 永远不 import security**（`shown`/`check_permission` 回调把 ctx 当 opaque 对象接收）。
- **`security/`** — `context.py` 的 `SecurityContext`（frozen Pydantic 模型，从 JWT 解码，保留 `claims` 供自定义断言）。`permissions.py` 的 `PermissionBuilder`：`apply_row_level` 返回 RLS WHERE 片段，`filter_fields`/`cube_visible` 处理字段级 `shown` 可见性。`auth.py` 的 `security_context` 是 FastAPI 依赖（支持 HS256/RS256）。
- **`sqlgen/`** — `builder.py` 的 `SQLBuilder` 把 `Query` + ctx 转成 SQLAlchemy `text()`（`query.py` 是 cube.js 风格 query 的 Pydantic 解析）。`operators.py` 是完整 filter 操作符表，`date_range.py` 处理 time dimension。
- **`orchestrator/`** — `orchestrator.py` 编排 缓存 → 预聚合路由 → 执行 → 缓存写入；含冷查询去重（in-flight Future）与 refreshKey 探针。`executor.py` 提供 async/sync 两种执行器。`preagg.py` 是 fail-closed 聚合导航 matcher（命中返回 `RollupRoute`），配合 `sqlgen/rollup.py` 的 `RollupBuilder` 改写、`rollup_builder.py` 的幂等 CTAS、`scheduler.py` 的定时刷新。
- **`api/`** — REST（`routes_rest.py`：`/cubejs-api/v1/{load,sql,meta,subscribe}`）、WS subscribe（`routes_ws.py`）、GraphQL（`graphql.py`，Strawberry）。`app.py` 是应用工厂，测试可直接注入 orchestrator 绕过真实 engine/Redis。
- **`cache/`** — `redis.asyncio` 封装（`redis_cache.py`）。**`client/`** — 异步客户端 SDK。

## 关键约定（非显而易见，跨多文件）

- **缓存 key 按 ctx 作用域**（tenant_id/user_id/role 参与哈希），行级安全绝不跨身份共享。改 ctx 结构等于让所有缓存失效。
- **Cube 别名一律小写**。Postgres 把未加引号标识符折叠为小写，所以作者写的 `Orders.user_id` / `Users.id` 引用能一致解析。改这套规则要同步改 builder。
- **SQL 有两套信任等级**：作者 SQL（member 的 `sql`、join `sql`、segment `sql`、RLS 片段）是可信的，直接拼进 SQL；filter 的**取值**永远是绑定参数，绝不插值。插值进 RLS 的 JWT claim 会做单引号转义防注入。这是与 cube.js 一致的信任模型。
- **不可见成员 fail closed**：请求了 `shown(ctx)` 为假的成员，builder 抛 `ValueError`，API 层映射为 HTTP 400。
- **Executor 按 DSN scheme 自动选择**：asyncpg/aiosqlite 走 `AsyncEngineExecutor`；DuckDB（无 async 方言，`duckdb-engine` 是同步）走 `SyncEngineExecutor`，在 worker 线程执行以让 async orchestrator 驱动。
- **成员名即声明的属性名**，不做自动 camelCase。
- `registry` 是全局单例 —— 测试间用 `registry.clear()` 重置，否则会串状态。

## 配置

`CUBEPY_` 前缀的环境变量（pydantic-settings，`config.py`）。关键项：`CUBEPY_JWT_SECRET`（生产必改）、`CUBEPY_JWT_ALGORITHM`（HS256 或 RS256，后者需 `CUBEPY_JWT_PUBLIC_KEY`）、`CUBEPY_PG_DSN`、`CUBEPY_DB_DSN`（任意 SQLAlchemy URL，如 DuckDB，覆盖 PG）、`CUBEPY_REDIS_URL`。`.env.example` 是模板。

## 定义 Cube（DSL 两种形态）

装饰器形态（全功能，支持 `shown`/`check_permission` 回调）与 YAML 形态（声明式，无回调）见 `schema/loader.py` 顶部 docstring 和 README。样本 schema 在 `cubepy/samples/`。
