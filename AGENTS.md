# AGENTS.md

CubePy 是 Cube.js 语义层的 Python 重写（分析语义层 + 权限层 + API 层），面向 Hologres/Postgres 后端。
深度架构文档在 `CLAUDE.md`（改核心代码前先读），Cube.js 移植契约在 `docs/06-cubejs-contract-notes.md`。

## 目录与边界

- `cubepy/` — 源码。请求流：`api/` → `security/`（JWT→SecurityContext）→ `orchestrator/`（缓存/预聚合路由/执行）→ `sqlgen/`（SQL 生成，RLS 注入）
- **铁律：`schema/` 永不 import `security/`**（`shown`/`check_permission` 回调把 ctx 当 opaque 对象）
- `cubepy/llm.py` + `api/routes_ask.py` — AI 问数层：prompt 可见性 = `cube_visible(ctx)` 过滤，与执行共用同一 SecurityContext；LLM 输出永远按不可信输入处理（`members_index` 白名单 + `Query.parse` 双校验，fail-closed）
- `cube.js/` — 上游 shallow clone 参考副本（gitignored），**只读，绝不修改**
- `.omc/` — 本地编排状态（gitignored）：计划/结果文档在 `.omc/plans/`，跑批脚本在 `.omc/scripts/`
- `samples/` — 两个 demo 域：Orders（玩具）与 trade（电商 mock，`trade_data.py` 确定性生成 60k 数据）

## 常用命令

```bash
uv sync --extra dev                      # 环境（uv 管理，Python >=3.11）
uv run pytest                            # 全量 277 个测试（含 testcontainers PG 集成）
uv run pytest -m "not integration"       # 跳过容器测试
uv run pytest tests/test_sqlgen.py -k "keyword" -v
uv run ruff check . && uv run ruff format .
uv run python run_server.py [trade]      # demo 服务 :8765（trade = 电商 mock 域 + AI 问数）
cubepy-diff old.yml new.yml --check      # schema 变更 breaking 检测（CI 门禁）
```

## 非显而易见的坑（跨多文件约定）

- **RLS 字符串引用小写 cube 别名，不是表名**：`check_permission` 返回的 SQL 原生注入 WHERE，cube `DwdOrders` 的别名是 `dwdorders`。表名≠别名时写表名直接 SQL 报错
- **时间维度同时进 `dimensions` 和 `timeDimensions`**：sqlgen 只渲染 timeDimensions 一份（granularity + dateRange 优先），有回归测试锁定
- **缓存 key 按 ctx 作用域**（tenant/user/role 参与哈希）；`registry` 是全局单例，测试间必须 `registry.clear()`
- **SQL 两套信任等级**：作者 SQL（member/join/RLS 片段）可信直接拼；filter 取值永远绑定参数。JWT claim 插值必须过 `security.permissions.sql_str` 转义
- **Executor 按 DSN scheme 自动选**：asyncpg/aiosqlite 走 async；DuckDB 走 sync + worker 线程
- 不可见成员 fail-closed：`shown(ctx)` 为假 → builder 抛 ValueError → API 400
- 成员名即声明的属性名（无自动 camelCase）；cube 别名一律小写（PG 大小写折叠）

## 测试与环境

- 集成测试默认 testcontainers 拉 `postgres:16-alpine`；`CUBEPY_TEST_PG_DSN` 指向真库；**`CUBEPY_TEST_HOLOGRES_EMU=1` 用无 FK/PK 的 Hologres 形状 seed**（`seed_hologres.sql`）
- coverage 门槛 `fail_under = 95`（pyproject）
- `FakeLLM`（`llm.py`）是确定性 LLM 替身，responder 同步/异步都可；无 key 的 demo 自动降级到它
- 环境变量 `CUBEPY_` 前缀（`config.py`）；`.env` 已 gitignore，含借用的 DeepSeek key，绝不入库

## 运营脉络（改方向前先读）

`.omc/plans/` 下有 M1（Hologres smoke，PG 模拟）/M2（AI 问数层，真 LLM 5/5）/M3（mock pilot 10/10）的计划与验收结果；
CEO 级决策记录在 `~/.gstack/projects/flybear16-cubepy/ceo-plans/`。当前状态：cubepy 内部特性冻结，精力在 AI 问数层与真实域接入。
