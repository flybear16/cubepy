# py-om 设计方案：用 Python 重写 OpenMetadata（精简治理内核）

> 日期：2026-08-11 | 作者：cube123 项目组 | 状态：设计已确认，待出实施计划
> 关联文档：`03-architecture-design.md`（四层架构）· `21-data-asset-management.md`（资产运营）· `13-rbac-multi-tenancy.md`（权限）· `09-modern-data-stack-*.md`（现代数据栈）

---

## 0. 一句话定位

**py-om = cube123 的精简 Python 元数据 + 治理内核，替代 OpenMetadata。**

它不是 OpenMetadata 的完整翻版，而是一个**聚焦治理定制**的精简层：只拥有「治理字段」（中文名 / owner / 状态 / 审批 / 权限），技术元数据（列 / schema / DQ）从 DataWorks / Cube / Superset **活读**，不重建、不存副本。

---

## 1. 背景与驱动

cube123 四层架构里，OpenMetadata 是「上下文层」（数据目录 / 血缘 / 数据质量 / 权限目录 / MCP）。当前痛点：

- **驱动（核心）**：深度定制中文化 + 治理流程（审批 / 生命周期），upstream 不接受 PR，Java 后端每改一处都要搏斗。
- **次级**：JVM 运维负担（4C8G 起步）、UI 英文化、与栈内其他工具权限不统一。

结论：不重写整个 OM（~30 万行 Java + ~100 实体 + ES/MySQL 双存储 + React UI），而是造一个**只覆盖 cube123 实际使用面**的 Python 治理内核。

---

## 2. 关键决策记录（brainstorming 过程产出）

| # | 决策 | 选择 | 理由 |
|---|---|---|---|
| D1 | 重写范围 | **自有契约，只 cube123 消费** | 不继承 OM 的 100 实体，工作量从人年压到人月 |
| D2 | MVP 能力裁剪 | 目录 + RBAC + 治理工作流；**砍 DQ 引擎、砍血缘、砍术语表/标签/profiling/ingestion/使用度** | DQ 用 DataWorks 数据质量；血缘 Phase 2；其余 YAGNI |
| D3 | catalog 来源 | **活读源系统，不重建** | DataWorks/Cube/Superset 自己存技术元数据；py-om 只存治理字段 |
| D4 | 执行层 | **DataWorks**（替代 dbt） | importer 从 DataWorks OpenAPI 拉 |
| D5 | 认证 | **自建账号 + JWT**（由 FastapiAdmin 内置 RBAC/JWT 提供） | 最简，不依赖外部 IdP |
| D6 | 审批通知 | **钉钉群机器人 webhook（加签）**，动作在 py-om 后台完成 | 不用飞书；钉钉只做通知通道 |
| D7 | 基础框架 | **FastapiAdmin**（fastapiadmin/FastapiAdmin v3.0.0） | 全栈 Vue3+FastAPI 脚手架，白送 RBAC/CRUD 代码生成/定时任务/审计/部署 |
| D8 | 门户形态 | **A：Vue3 后台即门户**，靠 RBAC 数据级权限分视图 | 最懒；不另建前端 |
| D9 | DataWorks 接入 | **有 OpenAPI AK/SK 权限**，importer 自动拉 | 已确认 |

---

## 3. MVP 范围

**IN（必做）：**
- 数据资产目录（5 类资产统一建模：DataWorks 表/节点、Cube 指标、Superset 报表、API、订阅）
- RBAC（菜单 / 按钮 / 数据级，FastapiAdmin 内置）
- 治理工作流：审批（创建 / 发布 / 下线 / 改 owner / 申请权限）+ 生命周期 FSM
- Source Importers：DataWorks / Cube / Superset，APScheduler 定时同步
- DQ 状态展示（只读，从 DataWorks 数据质量活读 + 懒缓存）
- 钉钉审批通知（群机器人 webhook 加签）
- MCP Server（给 cube123 Agent：search/get/审批申请/权限校验）
- Vue3 中文化后台（FastapiAdmin 自带 + 自定义审批中心 / 资产看板）

**OUT（不做 / 后续）：**
- 血缘（Phase 2 回归）
- DQ 引擎（用 DataWorks 数据质量，py-om 只展示）
- 业务术语表 / 标签分类 / 自动 profiling / ingestion 连接器框架 / 使用度分析
- 独立业务门户（走 A 方案，不另建）

---

## 4. 总体架构

```
┌───────────────────────────────────────────────────────┐
│  Vue3 + Element-Plus 中文化后台（FastapiAdmin 自带）     │
│   ├─ 工作台 / 数据分析（自定义看板：资产健康/僵尸资产）    │
│   ├─ 数据资产目录（代码生成器出 CRUD）                    │
│   ├─ 审批中心 / 生命周期看板（自定义页面 + 按钮动作）      │
│   ├─ 系统管理（用户/角色/菜单/部门/字典/公告，自带）       │
│   ├─ 定时任务（APScheduler，自带，挂 importer）          │
│   └─ 操作日志审计（自带，= 治理审计的一部分）              │
├───────────────────────────────────────────────────────┤
│  FastAPI 自定义路由  +  MCP Server（旁路，给 cube123 Agent）│
├───────────────────────────────────────────────────────┤
│  py-om 治理内核（差异部分，全自定义）                      │
│   ├─ Governance Service（审批 FSM + 生命周期 FSM）        │
│   ├─ Source Importers（DW / Cube / Superset）             │
│   └─ Notification Service（钉钉群机器人 webhook，加签）    │
├───────────────────────────────────────────────────────┤
│  FastapiAdmin 地基：SQLAlchemy 2.0 + Pydantic + Alembic  │
│  Postgres（已有）+ Redis（新增）+ RBAC/JWT（内置）         │
└───────────────────────────────────────────────────────┘
        ↓ read-through          ↓ 钉钉 webhook
  DataWorks / Cube / Superset      钉钉群
```

**分层职责：**
- **FastapiAdmin 地基**：提供 Web 框架、ORM、RBAC/JWT、Vue3 后台、代码生成器、APScheduler、操作日志、部署。
- **py-om 治理内核**：自定义数据模型 + 治理业务逻辑（FSM）+ 跨工具同步 + 钉钉通知 + MCP。
- **源系统**：DataWorks / Cube / Superset 各自是技术元数据与 DQ 的真相源，py-om 活读不副本。

---

## 5. 技术选型

| 模块 | 选型 | 来源 |
|---|---|---|
| Web 框架 | FastAPI | FastapiAdmin 内置 |
| ORM / 迁移 | SQLAlchemy 2.0 + Pydantic 2.0 + Alembic | FastapiAdmin 内置 |
| 数据库 | **Postgres 14+**（复用 cube123 现有） | FastapiAdmin 支持 PG/MySQL，选 PG |
| 缓存 | **Redis 6/7**（新增） | FastapiAdmin 必需；quality_cache 可放此 |
| 前端 | Vue3 + TS + Vite + Pinia + Element-Plus | FastapiAdmin 内置，原生中文 |
| 认证 / 权限 | RBAC 三级（菜单/按钮/数据级）+ JWT | FastapiAdmin 内置 |
| 定时任务 | APScheduler | FastapiAdmin 内置，挂 importer |
| 审计 | 操作日志 | FastapiAdmin 内置 |
| 代码生成 | 选表 → 出前后端代码 | FastapiAdmin 内置 |
| IM 通知 | 钉钉群机器人 webhook（加签） | 自研 Notification Service |
| Agent 接入 | MCP Server（stdio/SSE，旁路） | 自研 |
| 部署 | Docker + Nginx + SSL | FastapiAdmin 内置 |
| Python / Node | Python ≥ 3.12，Node ≥ 20 + pnpm | FastapiAdmin 要求；包管理用 uv |

---

## 6. 数据模型

> 自定义业务表写为 SQLAlchemy model → 跑 FastapiAdmin 代码生成器 → Vue3 CRUD 页面 + 接口自动出。审批/生命周期等流程业务在生成代码之上手写 service。

### 6.1 资产目录

**`asset`**（统一 5 类资产，单表 + type + source_system）
| 字段 | 说明 |
|---|---|
| id | 主键 |
| source_system | dataworks / cube / superset / api / subscription |
| source_ref | 源系统内的 ID（DW 表 fqn / Cube cube 名 / Superset dashboard id） |
| fqn | py-om 内全限定名（`model.dataworks.fct_order_daily`） |
| name | 业务名（中文） |
| type | model / metric / dashboard / api / subscription |
| description | 业务描述（中文） |
| owner / team / business_domain | 责任人 / 团队 / 业务域 |
| status | draft / active / deprecated / archived |
| custom_properties | jsonb，扩展字段 |
| created_at / updated_at | 时间戳 |

**`asset_version`** — 版本快照（asset_id, version, snapshot jsonb, changed_by, changed_at），治理审计。

**`asset_quality_cache`** — asset_id, source, status, last_checked。带 TTL 的懒缓存，仅用于列表徽标；详情页活读源系统（也可整体放 Redis）。

### 6.2 治理工作流

**`approval_request`** — asset_id, type（创建/发布/下线/改 owner/申请权限）, requester, payload jsonb, status（pending/approved/rejected/cancelled）, approver_role, created_at, decided_at。

**`approval_decision`** — request_id, approver, decision, comment, decided_at。

> 生命周期审计**复用 FastapiAdmin 操作日志**（status 迁移、owner 变更等自动留痕），不另建 `lifecycle_event`；若有强追溯需求再补精简版。

### 6.3 框架内置（不建）

user / role / menu / dept / dict / config / 公告 / 操作日志 / 定时任务 —— 全部 FastapiAdmin 自带。

### 6.4 配置

钉钉 webhook（url + secret + 模板）放 FastapiAdmin「参数配置 / 字典」模块，不单建表。MVP 单机器人。

---

## 7. 模块与关键流

### 7.1 RBAC 求值
FastapiAdmin 内置三级权限：菜单（能否看到菜单项）/ 按钮（能否点发布、审批）/ 数据级（只能看本业务域资产）。py-om 的资产按 `business_domain` + `team` 做数据级隔离。

### 7.2 审批流（治理核心，IM 无关）
```
申请（前端 / Agent via MCP）→ approval_request(pending)
  → Notification Service 发钉钉 webhook（加签 + markdown 模板）→ 钉钉群
  → 审批人点深链回 py-om 后台 → 在审批中心点「通过 / 拒绝」
  → approval_decision + 副作用（status 迁移 / role_user 写入）+ 操作日志留痕
  →（可选）结果再钉钉通知申请人
```
MVP：钉钉仅通知，动作在后台。Phase 2：钉钉交互卡片（直接在钉钉里点通过/拒绝，回调 py-om）。

### 7.3 生命周期 FSM
`draft → active → deprecated → archived`
- draft→active：需「发布审批」通过
- active→deprecated：需「下线审批」通过
- deprecated→archived：归档（管理员）
- 非法跳变直接拒；合法迁移用 transitions 配置约束
- 每次迁移写操作日志

### 7.4 Source Importers（APScheduler 定时，如每小时）
- **DataWorks importer**：DataWorks OpenAPI（AK/SK）→ 拉表 / 节点 / DQ 状态 → upsert `asset(source=dataworks)`（新资产入 `draft`）+ 刷 `asset_quality_cache`
- **Cube importer**：Cube REST `/meta` → 指标 → upsert `asset(source=cube)`
- **Superset importer**：Superset REST → dashboards → upsert `asset(source=superset)`
- **py-om 拥有治理记录，源系统拥有技术真相** —— 无 stale 副本。失败不阻塞主服务，记录 retry + 钉钉告警。

### 7.5 DQ 展示
列表徽标读 `asset_quality_cache`；详情页活读 DataWorks 数据质量模块（通过 OpenAPI）。py-om 不建 DQ 引擎、不存测试定义。

### 7.6 MCP Server（旁路，给 cube123 Agent）
tools：`search_asset` / `get_asset` / `list_my_approvals` / `request_approval` / `check_permission`。复用同一套 SQLAlchemy model + Governance Service。写操作守 CLAUDE.md「建议 → 人工确认 → 执行」：`request_approval` 只创建审批单，不直接变更资产。

---

## 8. 错误处理与安全

- 所有治理写操作（发布 / 下线 / 改 owner / 审批）**必须经审批流**，Agent 自然语言不能直接变更。
- RBAC 拒绝 → 403 + 中文提示 + 操作日志。
- Importer 失败不阻塞主服务，记录 retry + 钉钉告警。
- 钉钉 webhook 加签（HMAC-SHA256 + timestamp），防伪造。
- DataWorks AK/SK 存于环境变量 / FastapiAdmin 配置加密项，不入代码、不入日志。
- 全部治理变更有操作日志可追溯。

---

## 9. 测试

遵循 `rules/common/testing.md` 的 80% 覆盖目标，外加 ponytail 的「非平凡逻辑留一个可跑 check」：

- **单测**：生命周期 FSM 合法 / 非法迁移；审批状态机；钉钉加签与 payload 模板；importer 的源系统响应 → asset 映射。
- **集成**：REST 端点 + Postgres + Redis（复用 cube123 mock PG 或 testcontainers）。
- **一个 `demo()` 自检**：建资产 → 申请发布 → 模拟审批通过 → 验证 status 迁移 + 操作日志落库 + 钉钉 webhook 被调用（mock）。
- Importer 用源系统响应 fixture，不依赖真实 DataWorks（CI 友好）。

---

## 10. 迁移（从 OpenMetadata 1.13.1）

- 一次性脚本读 OM REST（`/api/v1/tables` 等）→ 映射到 `asset`（只取 cube123 用的字段，丢 glossary / tag / profiling）。
- 短期并存：`config/mcp-servers.jsonc` 先双挂 OM + py-om，验证 py-om 覆盖后切单。
- 用户 / 角色从 OM 导入到 FastapiAdmin user/role；DataWorks / Cube / Superset 资产由 importer 首跑补齐。

---

## 11. 风险

| # | 风险 | 严重度 | 对策 |
|---|---|:---:|---|
| 1 | **FastapiAdmin 是社区脚手架（v3.0.0）** | ⚠️⚠️ | Phase 0 先跑通脚手架烟测；adopt 其全部约定（结构/菜单/字典） |
| 2 | **新增 Redis 依赖** | ⚠️ | cube123 现无 Redis，部署需加（docker-compose 一行） |
| 3 | **代码生成器生成的 CRUD 是基础形态** | ⚠️ | 审批 / 生命周期流程业务手写 service + 自定义 Vue3 页 |
| 4 | **DataWorks OpenAPI 版本 / edition 差异** | ⚠️ | importer 失败降级为手工注册；AK/SK 权限已确认有 |
| 5 | **无血缘** | ⚠️ | Agent 暂丢影响分析，Phase 2 补 `lineage_edge` + 递归 CTE |
| 6 | **amis→Vue3 学习成本 / Element-Plus 定制** | ⚠️ | 团队需熟 Vue3；自定义审批中心页有工作量 |
| 7 | **OM 双存储复杂度** | — | 已规避（不引入 ES，单 Postgres + Redis） |

---

## 12. 分阶段路线图

### Phase 0：脚手架烟测（2-3 天）
- clone FastapiAdmin → Postgres + Redis + Vue3 后台跑通
- 登录 / RBAC / 代码生成器出一张表 CRUD 验证可用
- **Go/No-Go 门**：框架可用则进 Phase 1；不可用则回退评估（如 fastapi-amis-admin 或自建）

### Phase 1：MVP（~3 周）
- asset + asset_version + approval 模型（代码生成器出 CRUD）
- DataWorks / Cube / Superset importer（挂 APScheduler）
- 审批 FSM + 生命周期 FSM（自定义 service + Vue3 审批中心）
- DQ-from-DataWorks 展示（活读 + quality_cache）
- 钉钉群机器人通知（加签）
- MCP Server（5 个 tools）
- 工作台资产看板（健康 / 僵尸资产）
- **闭环验收**：DataWorks 自动发现表 → 补中文名 / owner → 申请发布 → 钉钉通知 → 后台审批 → 发布 + 操作日志留痕

### Phase 2：按需扩展
- 钉钉交互卡片（回调 py-om）
- api / subscription 资产纳入
- **血缘回归**（`lineage_edge` + 递归 CTE + 影响分析）
- 使用度分析（doc-21 资产运营层）
- 术语表 / 标签分类
- 独立业务门户（若 A 方案体验不足，切 B）

### Phase 3：自动化运营
- 僵尸资产自动下线流程
- 跨工具血缘（Superset 报表 → Cube 指标 → DataWorks 模型 → 源）
- Agno Agent 深度集成（可选）

---

## 13. 与现有文档的关系

| 文档 | 关系 |
|---|---|
| `03-architecture-design.md` | py-om 替换其中「OpenMetadata 上下文层」 |
| `21-data-asset-management.md` | py-om 是其资产目录基座；使用度 / 健康分在 Phase 2 |
| `13-rbac-multi-tenancy.md` | RBAC 改由 FastapiAdmin 三级权限承载 |
| `09-modern-data-stack-*.md` | 执行层 dbt → DataWorks（D4） |
| `20-data-api-gateway-metric-market.md` | api 类资产后续纳入注册表 |
| `CLAUDE.md` | OM 1.13.1 替换为 py-om；MCP 配置改挂 py-om |

---

## 14. 后续动作

1. 本文档落盘 + commit
2. 转 **writing-plans** 出 Phase 0 + Phase 1 的逐步实施计划（任务粒度、验收标准、依赖顺序）
3. Phase 0 烟测通过后再细化 Phase 2/3

---

> 维护人：cube123 项目组 | 最后更新：2026-08-11
