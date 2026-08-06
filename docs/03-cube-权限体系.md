# Cube.js 权限体系

## 核心机制：Security Context（安全上下文）

Cube.js 的权限不是传统 RBAC，而是**基于查询上下文动态注入过滤条件**。核心思路：用户请求进来时，Cube.js 根据用户身份注入 `securityContext`，然后在 Schema 层用这个 context 动态过滤数据。

## 完整链路

```
用户请求（带 JWT Token）
    ↓
认证中间件 → 解析 Token → 提取用户信息
    ↓
注入 securityContext → { userId, role, department, tenantId, ... }
    ↓
Schema 层 → 根据 securityContext 动态生成 SQL WHERE 条件
    ↓
查询结果 → 用户只能看到自己有权的数据
```

## 第一步：认证中间件（验证身份）

```js
// cube.js 配置
module.exports = {
  checkAuth: async (req, authorizationToken) => {
    const token = authorizationToken.replace('Bearer ', '');
    const decoded = jwt.verify(token, process.env.JWT_SECRET);
    return {
      userId: decoded.userId,
      role: decoded.role,
      department: decoded.department,
      tenantId: decoded.tenantId,
    };
  },
};
```

## 第二步：Schema 层权限控制（核心）

### 1. 行级权限（Row-Level Security）

```js
cube(`Orders`, {
  sql: `SELECT * FROM orders`,

  joins: {
    Users: {
      relationship: `belongsTo`,
      sql: `${CUBE}.user_id = ${Users}.id`,
    },
  },

  measures: {
    count: { type: `count` },
    totalAmount: { sql: `amount`, type: `sum` },
  },

  dimensions: {
    id: { sql: `id`, type: `number` },
    status: { sql: `status`, type: `string` },
    department: { sql: `department`, type: `string` },
  },

  securityContext: {
    checkPermission: (authContext, injectedParams) => {
      return [`${CUBE}.user_id = ${authContext.userId}`];
    },
  },
});
```

### 2. 基于角色的权限

```js
cube(`Orders`, {
  sql: `SELECT * FROM orders`,

  measures: {
    count: { type: `count` },
    revenue: {
      sql: `amount`, type: `sum`,
      shown: (authContext) => ['manager', 'admin'].includes(authContext.role),
    },
    profit: {
      sql: `profit`, type: `sum`,
      shown: (authContext) => authContext.role === 'admin',
    },
  },

  dimensions: {
    department: {
      sql: `department`, type: `string`,
      shown: (authContext) => authContext.role === 'admin',
    },
  },

  securityContext: {
    checkPermission: (authContext) => {
      const conditions = [];
      if (authContext.role === 'viewer') {
        conditions.push(`${CUBE}.user_id = ${authContext.userId}`);
      } else if (authContext.role === 'manager') {
        conditions.push(`${CUBE}.department = '${authContext.department}'`);
      }
      return conditions;
    },
  },
});
```

### 3. 多租户隔离

```js
cube(`Orders`, {
  sql: `SELECT * FROM orders`,
  securityContext: {
    checkPermission: (authContext) => {
      return [`${CUBE}.tenant_id = ${authContext.tenantId}`];
    },
  },
});
```

## 权限控制的几个层面

| 层面 | 控制什么 | 怎么做 |
|------|---------|--------|
| **Cube 级** | 整个数据模型能不能被访问 | `shown: (ctx) => ctx.role === 'admin'` |
| **Measure/Dimension 级** | 某个指标/维度能不能看 | `shown` 回调函数 |
| **行级（RLS）** | 能看到哪些行 | `securityContext.checkPermission` 返回 WHERE 条件 |
| **API 级** | 能不能调查询/写入 API | 中间件层拦截 |

## 和数据库 RLS 的对比

| 维度 | Cube.js 权限 | PostgreSQL/Hologres RLS |
|------|-------------|------------------------|
| **配置位置** | 应用层（JS Schema） | 数据库层（SQL Policy） |
| **灵活性** | 高（JS 逻辑，复杂判断） | 中（SQL 表达式） |
| **绕过风险** | 直连 DB 就绕过了 | 无法绕过（DB 强制执行） |
| **多数据源** | ✅ 统一权限层 | ❌ 每个库各自配 |
| **前端集成** | ✅ 自动隐藏无权限字段 | ❌ 前端不知道 |

## Cube.js 开源情况

Cube.js 完全开源，权限层也包含在内：

```
GitHub: https://github.com/cube-js/cube
License: Apache 2.0
Star: 18k+
```

社区版（免费开源）包含：语义层、securityContext 行级权限、shown 回调字段级权限、认证中间件、多数据源、REST/GraphQL API、预聚合、缓存层。

付费版（Cube Cloud）主要省运维：托管部署、APM 监控、自动扩缩容、Workbook、企业 SSO。
