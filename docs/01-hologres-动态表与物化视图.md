# Hologres 调研笔记：动态表与物化视图

## 背景

调研 Hologres（Holo）的核心特性——动态表（Dynamic Table）和物化视图（Materialized View），梳理两者的定位差异、适用场景，以及与 ODPS（MaxCompute）传统流水线加工的区别。最后以电商 GMV 实时大屏为例，给出具体用法。

---

## 一、物化视图

物化视图的本质是预计算的数据快照，将复杂查询（GROUP BY / JOIN 等）的结果物理存储下来，查询时直接读取结果，避免重复计算。

### 适用场景

查询加速是物化视图最核心的价值。当报表或 BI 场景中有固定的聚合维度（如「每日 UV/PV」「各品类销售额」），且底层表变更不频繁时，物化视图可以大幅缩短查询响应时间。它适合读多写少、数据量适中的场景，刷新成本可控。

### 核心特点

- **刷新方式**：手动或定时刷新（REFRESH MATERIALIZED VIEW），刷新时通常整表重算（全量），部分场景支持增量
- **存储占用**：等于结果集大小
- **定位**：查询加速缓存

---

## 二、动态表

动态表是 Hologres 特有的概念，更接近一个自动维护的中间表。底层可以对接实时数据流，支持自动增量刷新，适合需要低延迟产出的场景。

### 适用场景

当源数据持续写入（如实时订单流、CDC 数据），需要近实时地维护加工结果时，动态表是比物化视图更合适的选择。典型用途包括实时/近实时分析、CDC 数据消费、大规模数据的分层加工链路（ODS → DWD → DWS → ADS）。

### 核心特点

- **自动刷新**：支持按时间间隔或数据变更触发，引擎自动增量维护
- **依赖链**：可以被其他动态表依赖，形成 DAG 加工链，引擎自动感知上下游
- **底层存储**：真实的 Hologres 内部表，有独立的存储和索引
- **数据新鲜度**：秒至分钟级

---

## 三、核心对比

| 维度 | 物化视图 | 动态表 |
|------|---------|--------|
| 定位 | 查询加速缓存 | 数据加工产物 |
| 刷新方式 | 手动/定时全量 | 自动增量/定时，支持触发 |
| 数据新鲜度 | 取决于刷新频率 | 秒至分钟级近实时 |
| 依赖链 | 一般单层 | 可多层 DAG 依赖 |
| 数据源 | 本库表 | 内/外部表、MaxCompute、实时流 |
| 典型场景 | BI 报表加速 | 实时数仓分层加工 |

一句话总结：物化视图是「算好存着，查得快」，适合读多写少的报表加速；动态表是「自动维护的加工层」，适合实时数仓里 ODS → ADS 的流水线加工。实际数仓架构中两者经常配合使用——动态表负责实时分层加工，物化视图负责最终查询加速。

---

## 四、与 ODPS 流水线加工的区别

ODPS（MaxCompute）流水线加工和 Hologres 动态表流水线虽然都是数据分层加工，但底层机制完全不同。

### ODPS 流水线

ODPS 的本质是离线计算引擎加任务调度系统。数据是「算完就结束」的批处理，依赖 DataWorks 调度节点 DAG 串联——上游任务跑完，下游任务才开始。典型的时效性是 T+1（天或小时级），每次刷新都是全量或分区重算，计算资源按任务量计费。适合 PB 级离线批量 ETL。

### Hologres 动态表流水线

Hologres 动态表的本质是实时数据库内的自动增量维护。数据写入即触发更新，定义好 SQL 和刷新策略后引擎自动维护，不需要写调度。上下游依赖引擎自动感知，计算只处理变更部分（增量），数据延迟可达分钟甚至秒级。适合 TB 至 PB 级实时分析，运维复杂度低。

### 关键差异

| 维度 | ODPS 流水线 | Hologres 动态表 |
|------|------------|----------------|
| 时效性 | T+1（天/小时级） | 近实时（分钟/秒级） |
| 驱动方式 | 调度系统触发 SQL 任务 | 数据写入自动触发增量刷新 |
| 计算量 | 全量/分区重算 | 增量计算（只算变化部分） |
| 调度依赖 | DataWorks DAG 手动编排 | 引擎自动感知上下游依赖 |
| 查询时机 | 需等调度跑完才能查 | 随时可查，数据持续更新 |
| 运维复杂度 | 高（调度、重跑、补数据） | 低（定义好策略自动跑） |

---

## 五、实战示例：电商 GMV 实时大屏

### 场景设定

电商平台要做一个实时 GMV 大屏：每秒上千笔订单，需要展示各品类实时销售额、各省份 TOP 买家、最近 1 小时趋势。

### ODPS 方案（T+1）

凌晨 2:00 DataWorks 调度启动，依次执行：清洗订单表 → JOIN 商品表 → 按品类聚合 → 按省份聚合 → 生成 ADS 报表。凌晨 4:30 全部跑完后导出到 QuickBI 或大屏。问题很明显——上午 10 点的大促数据看不到，只能等明天凌晨，大屏永远展示昨天的数。

### Hologres 动态表方案（近实时）

实时订单流通过 Flink 或 DataWorks DataHub 秒级写入 ODS 层（ods_order_rt），然后由动态表自动分层加工。

```sql
-- DWD 层：JOIN 商品维度表（自动增量刷新，每 1 分钟）
CREATE DYNAMIC TABLE dwd_order_detail_rt
REFRESH FAST
NEXT = '1 minute'
AS
SELECT
    o.order_id,
    o.user_id,
    o.amount,
    p.category,
    p.product_name,
    o.city,
    o.create_time
FROM ods_order_rt o
JOIN dim_product p ON o.product_id = p.product_id;
```

```sql
-- DWS 层：按品类实时聚合（自动刷新，每 2 分钟）
CREATE DYNAMIC TABLE dws_gmv_category_rt
REFRESH FAST
NEXT = '2 minutes'
AS
SELECT
    category,
    COUNT(*)          AS order_cnt,
    SUM(amount)       AS total_gmv,
    COUNT(DISTINCT user_id) AS buyer_cnt
FROM dwd_order_detail_rt
WHERE create_time >= now() - interval '1 hour'
GROUP BY category;
```

大屏直接查询动态表，数据始终是最新的（2 分钟前），而不是昨天的。

---

## 六、历史 + 实时一起查

实际场景中，大屏通常需要展示「最近 7 天」的趋势——包括历史数据（ODPS 算好的）和今天实时数据（动态表维护的）。核心方法是 UNION ALL。

### 直接 UNION ALL 查询

```sql
SELECT '历史' AS source, ds, category, total_gmv, buyer_cnt
FROM dws_gmv_category_his
WHERE ds >= '20260730' AND ds <= '20260804'

UNION ALL

SELECT '今日' AS source,
       to_char(now(), 'YYYYMMDD') AS ds,
       category, total_gmv, buyer_cnt
FROM dws_gmv_category_rt;
```

### 建合并视图（推荐）

工程上建议一次性建好合并视图，大屏只查这一个视图，不用关心数据来源。

```sql
CREATE VIEW ads_gmv_7day AS
SELECT ds, category, total_gmv, buyer_cnt
FROM dws_gmv_category_his
WHERE ds >= to_char(now() - interval '7 days', 'YYYYMMDD')

UNION ALL

SELECT to_char(now(), 'YYYYMMDD') AS ds,
       category, total_gmv, buyer_cnt
FROM dws_gmv_category_rt;
```

```sql
-- 大屏 SQL 只有一行
SELECT * FROM ads_gmv_7day ORDER BY ds, total_gmv DESC;
```

### 大屏背后的完整查询逻辑

顶部 KPI 卡片：今日实时 GMV 从动态表查，昨日全量 GMV 从历史表查，两者相除算环比。品类排行直接从动态表 ORDER BY DESC LIMIT 10。7 天趋势查合并视图。各省 TOP 从最细粒度的动态表按天聚合。

---

## 七、一体化数仓标准姿势

阿里云数仓的标准做法是 ODPS 和 Hologres 结合使用：ODPS 负责历史全量加工（大表 JOIN、复杂回溯），算好后同步到 Hologres；Hologres 负责当天实时增量加工和服务层毫秒级查询。最终 ADS 层同时包含历史和实时数据，对外提供统一查询服务。

一句话概括：ODPS 把昨天算清楚，Hologres 把今天实时算出来，大屏一起查。
