# Cube.js 订阅（Subscribe）机制

## 是什么

Cube.js 的订阅是指 **实时数据刷新**——当后端数据变了，前端自动收到更新，不需要手动刷新或轮询。

```
传统：前端发请求 → 等响应 → 手动再发请求拿最新数据
订阅：前端订阅一次 → 数据变了 → Cube.js 主动推给前端
```

## 工作原理

```
前端 subscribe()
    ↓ WebSocket 连接
Cube.js Server
    ↓ 定期执行原始查询（轮询间隔可配）
    ↓ 结果变了？
    ├─ 没变 → 什么都不做
    └─ 变了 → 推送新结果给前端
    ↓
前端自动更新
```

底层实现是「服务端轮询 + WebSocket 推送」，不是真正的 CDC/流式订阅。

## 前端代码示例

### React

```jsx
import { useCubeQuery } from '@cubejs-client/react';

function Dashboard() {
  const { resultSet, isLoading, error } = useCubeQuery(
    {
      measures: ['Orders.revenue'],
      timeDimensions: [{
        dimension: 'Orders.createdAt',
        granularity: 'day',
        dateRange: 'last 7 days',
      }],
    },
    {
      subscribe: true,
      refreshKey: { every: 30 },
    }
  );

  if (isLoading) return <div>Loading...</div>;
  return <Chart data={resultSet.chartPivot()} />;
}
```

### 原生 WebSocket

```js
cubeApi.subscribe(
  { measures: ['Orders.revenue'] },
  { refreshKey: { every: 60 } },
  (error, resultSet) => {
    if (error) { console.error(error); return; }
    console.log('收到更新:', resultSet.rawData());
  }
);
```

## 和轮询的区别

| 维度 | 前端轮询 | Cube.js 订阅 |
|------|---------|-------------|
| **发起方** | 前端定时发请求 | 前端订阅一次，服务端定期查 |
| **连接** | 每次 HTTP 请求 | 一个 WebSocket 长连接 |
| **数据刷新** | 整个查询重新走一遍 | 服务端对比结果，变了才推 |
| **多客户端** | N 个客户端 = N 倍查询压力 | 服务端可共享查询缓存 |
| **资源消耗** | 前端+后端都消耗 | 主要在服务端 |

## 订阅 + 预聚合配合

```
前端 subscribe（每 30 秒检查）
    ↓
Cube.js 检查预聚合缓存
    ├─ 缓存命中 → 直接对比缓存结果，变了就推（不碰数据库）
    └─ 缓存未命中 → 查数据库 → 构建/更新缓存 → 推送
```

## 如果底层是 Hologres 动态表

```
前端 subscribe（每 30 秒）
    ↓
Cube.js 查询 → 打到 Hologres 动态表（已经是预聚合好的）
    ↓
动态表数据变了 → Cube.js 查到新结果 → WebSocket 推给前端
```

数据层秒级更新 + API 层 30 秒推送，体验已经很流畅。
