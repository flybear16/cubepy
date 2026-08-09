# 自主取用数平台架构设计

> 基于 JimuChatBI 设计理念 + cube123 现有技术栈
> 调研时间：2026-08-10
> 版本：v1.0

---

## 一、项目定位

### 1.1 核心目标

构建一个**企业级自主取用数平台**，让业务人员通过自然语言直接获取可信数据，无需依赖数据团队。

### 1.2 与 JimuChatBI 的关系

| 维度 | JimuChatBI | 本平台（自主取用数） |
|------|------------|----------------------|
| 定位 | 对话式 BI 模块 | 自主取用数平台 |
| 技术栈 | Spring Boot JAR | Cube + OpenMetadata + dbt + PostgreSQL |
| 部署 | 嵌入积木报表 | 独立部署 |
| 核心理念 | 建模优先 | 建模优先（借鉴） |
| 扩展方向 | 固定产品 | 可扩展 Agent 架构 |

### 1.3 借鉴 JimuChatBI 的核心设计

1. **建模优先** — 先定义口径再问数，确保数字可信
2. **三层权限** — 访问授权 / 行级 / 列级
3. **术语映射** — 业务口语对齐精确口径
4. **数据域** — 封闭语义边界，控制问数范围
5. **AI 描述** — 表/字段描述提升问数准确率

---

## 二、整体架构

### 2.1 架构图

```
┌─────────────────────────────────────────────────────────────────┐
│                        用户交互层                                │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐       │
│  │ 对话 UI  │  │ 仪表盘   │  │ 报表导出  │  │ API 接口 │       │
│  └────┬─────┘  └────┬─────┘  └────┬─────┘  └────┬─────┘       │
│       │              │              │              │             │
└───────┼──────────────┼──────────────┼──────────────┼────────────┘
        │              │              │              │
        ▼              ▼              ▼              ▼
┌─────────────────────────────────────────────────────────────────┐
│                      AI 对话层（Agent）                          │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │  自然语言 → 意图解析 → 语义路由 → Cube 查询生成 → 结果解读  │  │
│  └──────────────────────────────────────────────────────────┘  │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐      │
│  │ 术语注入 │  │ 权限注入 │  │ 上下文管理│  │ 多轮对话 │      │
│  └──────────┘  └──────────┘  └──────────┘  └──────────┘      │
└─────────────────────────────┬───────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                     语义建模层（数据域）                           │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐      │
│  │ 数据域   │  │ 表注册   │  │ 术语管理  │  │ 权限管理 │      │
│  │ (Domain) │  │ (Tables) │  │ (Terms)  │  │ (RBAC)  │      │
│  └──────────┘  └──────────┘  └──────────┘  └──────────┘      │
└─────────────────────────────┬───────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                      语义层（Cube）                              │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │  Cube Schema（指标定义、维度、关系）                         │  │
│  │  Cube API（/v1/load、/v1/meta）                           │  │
│  │  Cube Store（预聚合、缓存）                                │  │
│  └──────────────────────────────────────────────────────────┘  │
└─────────────────────────────┬───────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                     元数据层（OpenMetadata）                      │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐      │
│  │ 表元数据 │  │ 血缘关系 │  │ 数据质量  │  │ 术语字典 │      │
│  └──────────┘  └──────────┘  └──────────┘  └──────────┘      │
└─────────────────────────────┬───────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                      ETL 层（dbt）                               │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │  RAW → STAGING → INTERMEDIATE → MARTS                    │  │
│  │  dbt seed / run / test                                   │  │
│  └──────────────────────────────────────────────────────────┘  │
└─────────────────────────────┬───────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                      存储层（PostgreSQL）                         │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │  业务数据 + 平台元数据 + 权限数据 + 对话记录               │  │
│  └──────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
```

### 2.2 五层架构说明

| 层级 | 名称 | 职责 | 核心组件 |
|------|------|------|----------|
| L1 | 用户交互层 | 用户界面与操作入口 | 对话 UI、仪表盘、API |
| L2 | AI 对话层 | 自然语言理解与查询生成 | Agent、LLM、Prompt |
| L3 | 语义建模层 | 数据域、口径、权限管理 | 数据域、术语、RBAC |
| L4 | 语义层 | 指标定义与查询执行 | Cube API、Cube Store |
| L5 | 数据层 | 数据存储与 ETL | OpenMetadata、dbt、PostgreSQL |

---

## 三、核心模块设计

### 3.1 AI 对话层（Agent）

借鉴 JimuChatBI 的对话流程，设计 Agent 处理链：

```
用户提问
  → ① 意图解析（LLM）
  → ② 敏感词检查
  → ③ 数据域路由（选择最相关的域）
  → ④ A 层：访问授权检查
  → ⑤ 术语注入（业务口语 → 精确口径）
  → ⑥ Cube 查询生成（LLM）
  → ⑦ B 层：行级权限过滤（WHERE 条件注入）
  → ⑧ 执行 Cube 查询
  → ⑨ C 层：列级权限（隐藏/脱敏）
  → ⑩ 结果解读（LLM 生成洞察）
  → ⑪ 返回结果（表格 + 图表 + 洞察）
```

### 3.2 语义建模层（数据域）

借鉴 JimuChatBI 的数据域概念，设计封闭语义边界：

#### 3.2.1 数据域（Domain）

```yaml
domain:
  name: "销售分析域"
  description: "销售相关数据的语义边界"
  data_source: "cube"
  tables:
    - name: "orders"
      alias: "订单"
      fields:
        - name: "order_id"
          type: "dimension"
          alias: "订单ID"
        - name: "amount"
          type: "measure"
          alias: "订单金额"
          aggregation: "SUM"
        - name: "created_at"
          type: "dimension"
          alias: "创建时间"
          time_grain: "day"
  joins:
    - left: "orders"
      right: "customers"
      on: "orders.customer_id = customers.id"
  terms:
    - name: "核心客户"
      synonyms: ["VIP客户", "大客户"]
      filter: "customer_level IN ('gold', 'platinum')"
  permissions:
    access:
      roles: ["sales_manager", "analyst"]
    row_level:
      - role: "regional_manager"
        field: "region_id"
        operator: "="
        dynamic: "sys_org_code"
    column_level:
      - role: "analyst"
        field: "salary"
        action: "hide"
```

#### 3.2.2 术语管理

| 类型 | 说明 | 示例 |
|------|------|------|
| 仅释义 | 名词解释，不改口径 | "核心客户 = VIP客户 + 大客户" |
| 时间口径 | 指定时间范围 | "本月 = 当前自然月" |
| 数据筛选 | 追加 WHERE 条件 | "核心险种 = 寿险/健康险" |
| 指标映射 | 业务指标 → Cube 指标 | "GMV = orders.total_amount" |

### 3.3 三层权限体系

借鉴 JimuChatBI 的三层权限模型：

#### A 层：访问授权

```sql
-- 控制用户是否能使用该数据域
SELECT * FROM domain_permissions
WHERE domain_id = ? AND (role_id IN (?) OR user_id = ?)
```

#### B 层：行级数据权限

```sql
-- 动态注入 WHERE 条件
-- 示例：区域经理只能看本区域数据
WHERE region_id = ${sys_org_code}
```

#### C 层：列级权限 / 脱敏

```python
# 结果后处理
def apply_column_permissions(result, user_roles, field_permissions):
    for field in result.columns:
        permission = get_permission(field, user_roles, field_permissions)
        if permission.action == "hide":
            result.drop(field)
        elif permission.action == "mask":
            result[field] = "***"
    return result
```

### 3.4 Cube 语义层集成

利用 Cube 已有的语义层能力：

```javascript
// cube/schema/orders.js
cube(`Orders`, {
  sql: `SELECT * FROM dws_order_di`,
  
  dimensions: {
    id: {
      sql: `id`,
      type: `number`,
      primary_key: true
    },
    status: {
      sql: `status`,
      type: `string`
    },
    created_at: {
      sql: `created_at`,
      type: `time`
    }
  },
  
  measures: {
    count: {
      type: `count`
    },
    total_amount: {
      sql: `amount`,
      type: `sum`
    },
    avg_amount: {
      sql: `amount`,
      type: `avg`
    }
  },
  
  joins: {
    Customers: {
      sql: `${CUBE}.customer_id = ${Customers}.id`,
      relationship: `belongsTo`
    }
  }
});
```

Agent 生成 Cube 查询：

```json
{
  "measures": ["Orders.total_amount"],
  "dimensions": ["Orders.status"],
  "timeDimensions": [{
    "dimension": "Orders.created_at",
    "granularity": "month"
  }],
  "filters": [{
    "member": "Orders.region_id",
    "operator": "equals",
    "values": ["${sys_org_code}"]
  }]
}
```

---

## 四、数据模型设计

### 4.1 平台元数据表

```sql
-- 数据域
CREATE TABLE data_domains (
    id SERIAL PRIMARY KEY,
    name VARCHAR(100) NOT NULL,
    description TEXT,
    data_source VARCHAR(50),
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

-- 数据域-表注册
CREATE TABLE domain_tables (
    id SERIAL PRIMARY KEY,
    domain_id INT REFERENCES data_domains(id),
    table_name VARCHAR(100),
    alias VARCHAR(100),
    description TEXT,
    visible BOOLEAN DEFAULT true,
    created_at TIMESTAMP DEFAULT NOW()
);

-- 字段定义
CREATE TABLE domain_fields (
    id SERIAL PRIMARY KEY,
    table_id INT REFERENCES domain_tables(id),
    field_name VARCHAR(100),
    alias VARCHAR(100),
    field_type VARCHAR(20),  -- dimension, measure
    aggregation VARCHAR(20), -- SUM, AVG, MAX, MIN, COUNT
    description TEXT,
    enabled BOOLEAN DEFAULT true
);

-- 术语
CREATE TABLE domain_terms (
    id SERIAL PRIMARY KEY,
    domain_id INT REFERENCES data_domains(id),
    name VARCHAR(100),
    synonyms TEXT[],
    description TEXT,
    term_type VARCHAR(20),  -- definition, time_filter, data_filter
    filter_expression TEXT,
    created_at TIMESTAMP DEFAULT NOW()
);
```

### 4.2 权限数据表

```sql
-- 数据域访问授权
CREATE TABLE domain_access (
    id SERIAL PRIMARY KEY,
    domain_id INT REFERENCES data_domains(id),
    role_id INT,
    user_id INT,
    created_at TIMESTAMP DEFAULT NOW()
);

-- 行级权限规则
CREATE TABLE row_permissions (
    id SERIAL PRIMARY KEY,
    domain_id INT REFERENCES data_domains(id),
    role_id INT,
    field_name VARCHAR(100),
    operator VARCHAR(10),  -- =, !=, >, <, like, in
    value VARCHAR(200),
    dynamic_var VARCHAR(50), -- sys_user_code, sys_org_code, etc.
    created_at TIMESTAMP DEFAULT NOW()
);

-- 列级权限规则
CREATE TABLE column_permissions (
    id SERIAL PRIMARY KEY,
    domain_id INT REFERENCES data_domains(id),
    role_id INT,
    field_name VARCHAR(100),
    action VARCHAR(20),  -- hide, mask
    created_at TIMESTAMP DEFAULT NOW()
);

-- 字段敏感度
CREATE TABLE field_sensitivity (
    id SERIAL PRIMARY KEY,
    table_id INT REFERENCES domain_tables(id),
    field_name VARCHAR(100),
    level VARCHAR(10),  -- high, medium, low
    created_at TIMESTAMP DEFAULT NOW()
);
```

### 4.3 对话记录表

```sql
-- 对话会话
CREATE TABLE chat_sessions (
    id SERIAL PRIMARY KEY,
    user_id INT,
    domain_id INT REFERENCES data_domains(id),
    created_at TIMESTAMP DEFAULT NOW()
);

-- 对话消息
CREATE TABLE chat_messages (
    id SERIAL PRIMARY KEY,
    session_id INT REFERENCES chat_sessions(id),
    role VARCHAR(20),  -- user, assistant
    content TEXT,
    sql_query TEXT,
    result_data JSONB,
    created_at TIMESTAMP DEFAULT NOW()
);
```

---

## 五、技术选型

### 5.1 核心组件

| 组件 | 选型 | 版本 | 说明 |
|------|------|------|------|
| **语义层** | Cube | latest | 指标定义、查询引擎、预聚合 |
| **元数据** | OpenMetadata | latest | 表元数据、血缘、数据质量 |
| **ETL** | dbt + dbt-postgres | latest | 数据转换、分层建模 |
| **存储** | PostgreSQL | 15+ | 业务数据 + 平台元数据 |
| **AI** | LLM API | - | GPT-4o / Claude / DeepSeek |
| **前端** | Next.js | 14+ | 对话 UI、管理后台 |
| **后端** | Node.js / Python | - | Agent 服务、API 网关 |

### 5.2 借鉴 vs 自研

| 能力 | 来源 | 说明 |
|------|------|------|
| 建模优先理念 | JimuChatBI | 直接借鉴，核心设计 |
| 三层权限模型 | JimuChatBI | 直接借鉴，安全护栏 |
| 术语管理 | JimuChatBI | 直接借鉴，业务对齐 |
| 数据域概念 | JimuChatBI | 直接借鉴，语义边界 |
| 语义层查询 | Cube | 已有，复用 |
| 元数据管理 | OpenMetadata | 已有，复用 |
| ETL 管道 | dbt | 已有，复用 |
| AI 对话 | 自研 | 基于 Agent 架构 |
| 前端 UI | 自研 | 参考 JimuChatBI 交互 |

---

## 六、Agent 设计

### 6.1 Agent 工具集

```python
tools = [
    {
        "name": "get_data_domains",
        "description": "获取用户可访问的数据域列表",
    },
    {
        "name": "get_domain_tables",
        "description": "获取数据域内的表和字段定义",
    },
    {
        "name": "get_domain_terms",
        "description": "获取数据域内的术语定义",
    },
    {
        "name": "get_row_permissions",
        "description": "获取用户的行级权限规则",
    },
    {
        "name": "get_column_permissions",
        "description": "获取用户的列级权限规则",
    },
    {
        "name": "query_cube",
        "description": "执行 Cube 查询并返回结果",
    },
    {
        "name": "generate_chart",
        "description": "根据数据生成可视化图表",
    }
]
```

### 6.2 Prompt 模板

```
你是一个数据分析助手。请根据用户的自然语言问题，生成 Cube 查询并返回结果。

## 数据域
{domain_name}

## 可用表和字段
{tables_and_fields}

## 术语定义
{terms}

## 行级权限
{row_permissions}

## 用户信息
- 用户ID: {user_id}
- 角色: {roles}
- 部门: {org_code}

## 规则
1. 只能查询数据域内已注册且启用的表
2. 将业务术语映射到精确口径
3. 根据行级权限生成 WHERE 条件
4. 生成 Cube 查询格式的 JSON
5. 返回结果时生成一句话洞察

## 用户问题
{user_question}
```

---

## 七、部署架构

### 7.1 Docker Compose

```yaml
version: '3.8'

services:
  # 语义层
  cube-api:
    image: cubejs/cube:latest
    ports:
      - "4000:4000"
    environment:
      CUBEJS_DB_TYPE: postgres
      CUBEJS_DB_HOST: postgres
      CUBEJS_DB_NAME: cube123
      CUBEJS_DB_USER: ${DB_USER}
      CUBEJS_DB_PASS: ${DB_PASS}
      CUBEJS_API_SECRET: ${CUBE_SECRET}
    volumes:
      - ./cube/schema:/cube/schema
      - ./cube/rollup:/cube/rollup

  cube-store:
    image: cubejs/cubestore:latest
    ports:
      - "3030:3030"
      - "3031:3031"

  # 元数据
  openmetadata:
    image: openmetadata/openmetadata-server:latest
    ports:
      - "8585:8585"
    environment:
      DATABASE_HOST: postgres
      DATABASE_PORT: 5432
      DATABASE_USER: ${DB_USER}
      DATABASE_PASSWORD: ${DB_PASS}
      DATABASE_NAME: openmetadata

  # 数据库
  postgres:
    image: postgres:15
    ports:
      - "5432:5432"
    environment:
      POSTGRES_DB: cube123
      POSTGRES_USER: ${DB_USER}
      POSTGRES_PASSWORD: ${DB_PASS}
    volumes:
      - pgdata:/var/lib/postgresql/data

  # Agent 服务
  agent:
    build: ./agent
    ports:
      - "8000:8000"
    environment:
      CUBE_API_URL: http://cube-api:4000
      OPENMETADATA_URL: http://openmetadata:8585
      LLM_API_KEY: ${LLM_API_KEY}
      LLM_MODEL: ${LLM_MODEL:-gpt-4o}
    depends_on:
      - cube-api
      - openmetadata

  # 前端
  frontend:
    build: ./frontend
    ports:
      - "3000:80"
    depends_on:
      - agent

volumes:
  pgdata:
```

### 7.2 服务端口规划

| 服务 | 端口 | 说明 |
|------|------|------|
| Frontend | 3000 | 用户界面 |
| Agent API | 8000 | 对话接口 |
| Cube API | 4000 | 语义查询 |
| Cube Store | 3030/3031 | 预聚合存储 |
| OpenMetadata | 8585 | 元数据管理 |
| PostgreSQL | 5432 | 数据存储 |

---

## 八、实施路线

### Phase 1：基础搭建（2 周）

- [ ] PostgreSQL 数据库初始化
- [ ] Cube 语义层配置（复用现有 schema）
- [ ] OpenMetadata 元数据同步
- [ ] 平台元数据表创建

### Phase 2：语义建模（2 周）

- [ ] 数据域管理 CRUD
- [ ] 表注册与字段配置
- [ ] 术语管理
- [ ] 权限管理（A/B/C 三层）

### Phase 3：AI 对话（3 周）

- [ ] Agent 服务搭建
- [ ] LLM 集成（GPT-4o / Claude）
- [ ] Cube 查询生成
- [ ] 权限注入
- [ ] 结果解读与洞察

### Phase 4：前端 UI（2 周）

- [ ] 对话界面
- [ ] 管理后台（数据域、术语、权限）
- [ ] 结果展示（表格、图表）
- [ ] SQL 查看

### Phase 5：优化上线（1 周）

- [ ] 性能优化（Cube 预聚合）
- [ ] 安全加固
- [ ] 文档完善
- [ ] 用户培训

**总周期：约 10 周**

---

## 九、与 JimuChatBI 的差异化

| 维度 | JimuChatBI | 本平台 |
|------|------------|--------|
| **部署方式** | JAR 嵌入 | 独立部署 |
| **语义层** | 自研 | Cube（开源、可扩展） |
| **元数据** | 无 | OpenMetadata（血缘、DQ） |
| **ETL** | 无 | dbt（分层建模） |
| **扩展性** | 固定产品 | 可扩展 Agent 架构 |
| **AI 模型** | 依赖积木报表 | 自由选择（GPT/Claude/DeepSeek） |
| **多租户** | 支持 | 支持 |
| **开源** | 商业产品 | 可开源/可商用 |

---

## 十、风险与对策

| 风险 | 影响 | 对策 |
|------|------|------|
| LLM 生成 SQL 不准确 | 查询结果错误 | 建模优先 + 术语映射 + 人工校验 |
| 权限绕过 | 数据泄露 | 三层权限自动执行 + 审计日志 |
| 性能问题 | 查询慢 | Cube 预聚合 + 查询缓存 |
| 模型成本 | API 费用高 | 选择性价比模型 + 缓存策略 |
| 用户学习成本 | 使用率低 | 简化交互 + 推荐问题 + 培训 |

---

## 十一、总结

本平台借鉴 JimuChatBI 的核心设计理念，结合 cube123 现有技术栈，构建一个**建模优先、安全可控、可扩展**的自主取用数平台。

**核心优势：**

1. **建模优先** — 先定义口径再问数，确保数字可信
2. **三层权限** — 访问授权 / 行级 / 列级，企业级安全
3. **语义层复用** — Cube 已有，直接复用
4. **可扩展架构** — Agent 架构支持多模型、多数据源
5. **元数据治理** — OpenMetadata 提供血缘、DQ 能力

**适用场景：**

- 企业内部业务自助取数
- 数据分析师快速验证口径
- 管理层实时数据洞察
- 跨部门数据共享

---

## 附录 A：参考资源

| 资源 | 链接 |
|------|------|
| JimuChatBI 官方文档 | https://help.jimureport.com/chat2bi |
| Cube 官方文档 | https://cube.dev/docs |
| OpenMetadata 文档 | https://docs.open-metadata.org |
| dbt 文档 | https://docs.getdbt.com |
| cube123 项目文档 | cube123/docs/ |

---

*文档版本：v1.0*
*创建时间：2026-08-10*
*作者：阿拉蕾*
