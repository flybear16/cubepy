# Cube.js 预聚合 vs Hologres 物化视图/动态表

## Cube.js 预聚合在干什么

Cube.js 的预聚合（Pre-aggregation）本质是：**API 层自己做了一层缓存**——把用户查询按维度组合预先算好，存在 Redis/MySQL/Postgres 里，下次同样查询直接返回缓存结果。

```
用户查询 → Cube.js REST/GraphQL API
              ↓
         检查预聚合是否命中？
         ├─ 命中 → 直接返回缓存（快）
         └─ 未命中 → 原始 SQL 打到数据库（慢）→ 触发预聚合构建
```

## 如果底层是 Hologres，怎么做更好？

**答案是：用动态表 > 物化视图 > Cube.js 预聚合**

### 动态表方案（最优）

```sql
-- 直接在 Hologres 里建动态表，自动维护聚合结果
CREATE DYNAMIC TABLE dws_sales_daily
REFRESH FAST
NEXT = '5 minutes'
AS
SELECT
    product_category,
    date_trunc('day', order_time) AS dt,
    region,
    SUM(amount) AS revenue,
    COUNT(DISTINCT user_id) AS unique_users
FROM ods_orders
GROUP BY product_category, date_trunc('day', order_time), region;
```

用户查询直接打 Hologres，不走 Cube.js 预聚合：

```sql
-- 查询直接命中动态表，5 分钟内新鲜度，毫秒级响应
SELECT product_category, SUM(revenue), SUM(unique_users)
FROM dws_sales_daily
WHERE dt >= '2026-08-01' AND region = '华东'
GROUP BY product_category;
```

### 三者对比

| 维度 | Cube.js 预聚合 | Hologres 物化视图 | Hologres 动态表 |
|------|---------------|-------------------|----------------|
| **聚合维护方** | Cube.js（应用层） | Hologres 引擎 | Hologres 引擎 |
| **数据新鲜度** | 取决于 refresh 工具调度 | 取决于手动刷新 | 分钟/秒级自动增量 |
| **多维查询** | 按预定义组合命中 | 按固定聚合查 | 按固定聚合查 |
| **缓存存储** | Redis/MySQL（额外存储） | Hologres 内部表 | Hologres 内部表 |
| **未命中回退** | 原始 SQL 打源库（慢） | 不存在，直接查视图 | 不存在，直接查动态表 |
| **运维复杂度** | 高（Cube + Redis + 调度） | 低 | 最低 |
| **架构层数** | 多一层（App → Cube → DB） | 少一层（App → DB） | 少一层 |

### 为什么动态表更适合

**Cube.js 预聚合的痛点**：

- 需要提前定义哪些维度组合要预聚合（dimension combinations），维度多了组合爆炸
- 未命中预聚合的查询回退到原始查询，可能很慢
- 额外维护 Cube.js 服务 + Redis，架构复杂度增加
- 数据更新后要触发 rebuild，调度策略不好搞

**Hologres 动态表直接解决**：

- 聚合在数据库引擎层自动维护，增量更新，不用应用层操心
- 查询永远命中（动态表就是真实表，不存在"缓存未命中"）
- 数据新鲜度分钟级，不需要应用层调度
- 少一层架构，直接 SQL 查询，毫秒级返回

## 什么时候还该用 Cube.js？

1. **多数据源**：聚合需要跨 MySQL + PG + Elasticsearch，Cube.js 能统一语义层
2. **行级权限/多租户**：Cube.js 有成熟的 security context
3. **前端集成**：Cube.js 的 React/Vue SDK 对前端更友好
4. **API 层限流/监控**：需要独立的 API 网关做查询审计和限流

## 最佳实践：混合方案

```
前端 → Cube.js（语义层 + API + 权限 + 前端 SDK）
          ↓
         不开预聚合，或只对极高频查询开
          ↓
      Hologres 动态表（聚合在 DB 层自动维护）
          ↓
      ODS 实时表（原始数据）
```

**一句话总结**：如果底层是 Hologres，预聚合那层可以省了，Cube.js 退回去做语义层和 API 层就好。
