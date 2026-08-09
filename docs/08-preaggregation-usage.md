# 08 — 预聚合使用指南

CubePy 的预聚合（pre-aggregation）采用 **同库物化 rollup + aggregate-navigation** 路线（见 [07](./07-预聚合方案调研.md)）：
把可加度量（SUM/COUNT）按维度 + 时间粒度提前算成一张 rollup 表，查询时由匹配器（matcher）决定是否改写到 rollup，
命中则对 rollup 列再聚合（粒度上卷，无损），未命中或失败则**安全回退**到基础 cube。

## 1. 声明一个 rollup

在 `@cube` 上声明 `pre_aggregations` 与（当存在 RLS 时）`security_columns`：

```python
from cubepy.schema.loader import cube, dimension, measure
from cubepy.schema.meta import MeasureType, PreAggregation

@cube(
    "Orders",
    "orders",
    security_context={"check_permission": lambda ctx: [f"orders.tenant_id = {ctx.tenant_id}"]},
    security_columns=("tenant_id",),  # RLS 引用的列，rollup 必须物化
    pre_aggregations=(
        PreAggregation(
            name="daily",
            measures=("Orders.revenue", "Orders.count"),
            dimensions=("Orders.status",),
            time_dimension="Orders.created_at",
            granularity="day",
            refresh_key={"every": 300},       # 秒，调度器刷新间隔
            security_columns=("tenant_id",),  # 必须覆盖 cube.security_columns
        ),
    ),
)
class _Orders:
    revenue = measure("amount", MeasureType.SUM)
    count = measure(None, MeasureType.COUNT)
    status = dimension("status", "string")
    created_at = dimension("created_at", "time")
```

YAML 等价写法用 camelCase：`preAggregations` / `securityColumns` / `timeDimension`。

加载时会校验（`cubepy/schema/validators.py`）：rollup 度量必须可加（SUM/COUNT）；
`time_dimension` 必须是 time 类型维度；维度/度量引用必须存在；RLS cube 的 rollup 必须覆盖其 `security_columns`。

**列模型**（rollup 表的列名约定，build 与改写两端必须一致）：

- 维度 / 时间 / 安全列 → 以其物理 `member.sql` 命名（如 `status`、`tenant_id`、`created_at`），
  这样 RLS 片段 `Orders.tenant_id` 折叠到别名 `orders.tenant_id` 后能直接命中 rollup 列；
- 度量列 → 以 `measure.name` 命名并加引号（度量名可能是保留字如 `count`），存放已聚合值。

## 2. 启用

默认**关闭**。开启需设置环境变量（前缀 `CUBEPY_`）：

| 变量 | 默认 | 含义 |
|------|------|------|
| `CUBEPY_PREAGG_ENABLED` | `false` | 开启查询路由到 rollup |
| `CUBEPY_PREAGG_REFRESH_ON_START` | `true` | 启动时先全量 build 一次 rollup，再交给定时刷新 |

开启后，FastAPI lifespan 会启动 `PreAggScheduler`（APScheduler `AsyncIOScheduler`）：
为每个 rollup 注册一个 interval job，间隔取其 `refresh_key.every`（无则 `CUBEPY_DEFAULT_REFRESH_EVERY`）。
build 产出 `DROP TABLE IF EXISTS` + `CREATE TABLE cubepy_rollup_{cube}_{name} AS SELECT ...`，
并在 `SET TIME ZONE 'UTC'` 会话下执行（见下）。

## 3. 匹配规则（fail-closed）

`PreAggRouter.match`（`cubepy/orchestrator/preagg.py`）在以下任一条件不满足时返回 `None` → 走基础 cube：

- 存在已认证的 `security_context`（RLS 正确性依赖它）；
- 查询不含 segment；
- 仅引用一个 cube（无 join）；
- 存在一个 rollup：时间维度一致、`granularity` 能上卷（day rollup 可答 month 查询；反之不可）、
  查询 `timezone` 为 UTC 或未设；
- 查询的度量/维度被 rollup 覆盖，且度量可加（SUM/COUNT）；
- RLS 列覆盖：`rollup.security_columns ⊇ cube.security_columns`。

**MVP 固定 UTC 时间锚点（G1）**：`date_trunc` 不带 tz 参数，`date_range` 边界渲染为 `+00:00`。
若会话时区非 UTC，存储的 day-bucket 与查询边界会错位、丢行。故 build 与改写查询都在
`SET TIME ZONE 'UTC'` 下执行；非 UTC `timezone` 的查询直接不命中（回退基础 cube）。

## 4. 可观测

- 响应信封的 `usedPreAggregations`（命中时为 `[{"tableName": "cubepy_rollup_orders_daily"}]`，否则 `[]`）；
- 日志：命中 `INFO`（route 名）、未命中 `DEBUG`（具体是哪条 guard 拦下）、回退 `WARNING`；
- 计数器：`orchestrator.preagg_counters` → `{"hits": n, "misses": n, "fallbacks": n}`（仅在 `preagg_enabled` 时统计）。

## 5. 限制度（MVP）

- **仅 SUM/COUNT** 可加度量；COUNT_DISTINCT / CALCULATED / RUNNING_TOTAL 等不进 rollup，查询时自动回退基础 cube；
- **单 cube、无 join**；多 cube 查询不命中；
- **UTC only**；非 UTC `timezone` 不命中；
- **同库存储**：rollup 表建在源库（Postgres / DuckDB），不做跨库或独立 OLAP；
- **全量刷新**：每次 build 为 `DROP` + `CREATE AS SELECT`，无增量；
- **不自动迁移源表**：`security_columns` 对应的物理列必须已存在于源表；
- ClickHouse / Hologres / Snowflake 等优先用 DB 原生物化视图（见 [07](./07-预聚合方案调研.md)「何时不做」）。
