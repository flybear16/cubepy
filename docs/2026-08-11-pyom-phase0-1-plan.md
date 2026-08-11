# py-om Phase 0 + Phase 1 Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 在 FastapiAdmin 脚手架之上，搭起 py-om——cube123 的精简 Python 元数据 + 治理内核，替代 OpenMetadata，覆盖 Phase 1 MVP（数据目录 + RBAC + 审批/生命周期 + DataWorks/Cube/Superset importer + 钉钉通知 + MCP）。

**Architecture:** FastapiAdmin（Vue3+Element-Plus + FastAPI + SQLAlchemy 2.0 async + Postgres + Redis）作地基，复用其 RBAC/代码生成器/APScheduler/操作日志。py-om 在 `backend/app/api/v1/module_om/` 下加自定义治理模块，活读 DataWorks/Cube/Superset 的技术元数据（不副本），旁路一个 MCP Server 给 cube123 Agent。

**Tech Stack:** Python ≥3.12 (uv) · FastAPI · SQLAlchemy 2.0 async + asyncpg · Pydantic 2.0 · Alembic · Postgres 14+ · Redis 7 · Vue3 + Element-Plus · APScheduler · pytest/pytest-asyncio/httpx · 钉钉群机器人 webhook · MCP SDK

**Design doc:** `cube123/docs/plans/2026-08-11-openmetadata-python-rewrite-design.md`（已确认）

---

## ⚠️ 读前必读：FastapiAdmin 约定的不确定性

FastapiAdmin v3.0.0 的 README/backend README 只给了**文件夹分层**，没给以下细节：
- SQLAlchemy `Base` 类的**确切 import 路径和名字**
- RBAC 在路由上**怎么强制**（装饰器还是 Dependency，名字是啥）
- 新菜单/按钮/数据权限**怎么 seed**（`app/scripts/` 里格式）
- APScheduler job **怎么用代码注册**（`app/module_task/` 里格式）
- 代码生成器的**确切输出**

这些**只能 clone 后读源码确认**。所以 **Phase 0 的核心产出是一份 cheatsheet**：`docs/plans/fastapiadmin-cheatsheet.md`，Phase 1 全程引用它。本计划里用 `{占位符}` 表示这些待确认项，执行时从 cheatsheet 替换：

| 占位符 | 含义 | Phase 0 任务锁定 |
|---|---|---|
| `{BaseClass}` | SQLAlchemy declarative Base 的 import 路径 | Task 0.3 |
| `{rbac_dep}` | 路由 RBAC 强制的 Dependency/装饰器 | Task 0.3 |
| `{menu_seed}` | 菜单/权限 seed 的写法 | Task 0.3 |
| `{register_router}` | 业务路由挂到 v1 的写法 | Task 0.3 |
| `{register_job}` | APScheduler job 注册写法 | Task 0.3 |
| `{resp_ok}/{resp_err}` | `app/common/response.py` 的统一响应封装 | Task 0.3 |

**已确认的目录约定（直接用）：**
```
backend/app/api/v1/module_<name>/
  ├─ controller.py   # HTTP 层
  ├─ service.py      # 业务逻辑（py-om 重点手写这里）
  ├─ crud.py         # DB 操作（代码生成器出）
  ├─ model.py        # SQLAlchemy ORM
  ├─ schema.py       # Pydantic
  └─ param.py        # 请求参数模型
backend/app/module_task/      # APScheduler 定时任务
backend/app/scripts/          # 初始化脚本和种子数据
backend/app/common/response.py# 统一响应
backend/app/core/validator.py # DateStr/TimeStr/DateTimeStr
backend/env/.env.dev          # 环境配置
backend/main.py               # 入口：uv run main.py run --env=dev
```

**Alembic 命令（确认）：** 改了 model 后 `uv run main.py revision --env=dev` → `uv run main.py upgrade --env=dev`。首次启动自动建表+种子，无需手动。

**仓库布局：** py-om 是 cube123 的一个**子目录**：`cube123/pyom/` 下放 FastapiAdmin（clone 进来或 submodule）。本计划路径以 `pyom/` 为根（即 clone 后的 FastapiAdmin 根）。设环境变量 `$PYOM` = `cube123/pyom`。

---

## Phase 0 — 脚手架烟测 + 约定发现（2-3 天，Go/No-Go 门）

> **目的：** 证明 FastapiAdmin 可用 + 产出 cheatsheet + 跑通一个自定义模块全链路。**跑不通则回退评估**（备选：fastapi-amis-admin 或自建 FastAPI+Vue3）。

### Task 0.1: clone FastapiAdmin + 起全栈

**Files:**
- Create: `cube123/pyom/`（FastapiAdmin 根）

**Step 1: clone**
```bash
cd cube123
git clone https://github.com/fastapiadmin/FastapiAdmin.git pyom
cd pyom
```
Expected: `pyom/backend/`、`pyom/frontend/web/` 存在。

**Step 2: 配置后端 env**
```bash
cp backend/env/.env.dev.example backend/env/.env.dev
```
编辑 `backend/env/.env.dev`：DB 指向 cube123 现有 Postgres（或新建 `pyom` 库），Redis 指向本机 `localhost:6379`。

**Step 3: 起依赖（Postgres + Redis）**
```bash
# 复用现有 Postgres；Redis 起一个容器
docker run -d --name pyom-redis -p 6379:6379 redis:7
```

**Step 4: 装后端依赖 + 首启（自动建表+种子）**
```bash
cd backend
uv sync
uv run main.py run --env=dev
```
Expected: 日志显示「自动初始化数据库表与基础数据」成功，API 起在预期端口。

**Step 5: 起前端**
```bash
cd ../frontend/web
pnpm install
pnpm run dev
```
Expected: 浏览器 `http://127.0.0.1:5173` 打开登录页，`admin/123456` 登录成功，看到「工作台/系统管理/监控管理」等菜单。

**Step 6: Commit**
```bash
cd cube123
# pyom 作为子目录纳入 cube123（或按你们的 submodule 偏好）
git add pyom/            # 若太大，改用 .gitignore + 单独说明
git commit -m "chore(pyom): scaffold FastapiAdmin v3.0.0"
```

> 注：FastapiAdmin 自带 .git，是否纳入 cube123 主仓由你们定。MVP 建议先纳入子目录（去掉内层 .git），简化依赖管理。

---

### Task 0.2: 跑代码生成器，记录输出结构

**Step 1: 登录后台 → 开发工具 → 代码生成**

**Step 2: 建一张临时表测试**
```sql
-- 在 pyom 库执行
CREATE TABLE pyom_smoke (
  id BIGSERIAL PRIMARY KEY,
  name VARCHAR(100) NOT NULL,
  created_at TIMESTAMPTZ DEFAULT now()
);
```

**Step 3: 在代码生成器里选 `pyom_smoke` 表 → 生成**

**Step 4: 把生成的文件落到 `pyom_smoke` 模块，记录生成的全部文件路径**
Expected: 生成物至少包含 `controller.py / service.py / crud.py / model.py / schema.py / param.py` + 前端 Vue 页面 + 菜单 SQL。把生成物的文件树抄进 cheatsheet。

**Step 5: 验证生成的 CRUD 可用** —— 在前端能看到 `pyom_smoke` 菜单，能增删改查一条记录。

**Step 6: Commit**
```bash
git add pyom/backend/app/api/v1/ pyom/frontend/web/src/views/
git commit -m "chore(pyom): record codegen output structure (smoke)"
```

---

### Task 0.3: 读源码，锁定 5 个占位符 → 写 cheatsheet（关键产出）

**Files:**
- Create: `cube123/docs/plans/fastapiadmin-cheatsheet.md`

**Step 1: 锁定 `{BaseClass}`** —— grep `backend/app/` 找 SQLAlchemy declarative Base 定义（如 `class Base(DeclarativeBase)`），记下 import 路径。

**Step 2: 锁定 `{rbac_dep}`** —— 读 `backend/app/api/v1/module_system/sys_user/controller.py`，看登录用户依赖、权限校验依赖的确切名字（如 `Depends(get_current_user)`、`require_perms('sys:user:add')` 之类）。

**Step 3: 锁定 `{menu_seed}`** —— 读 `backend/app/scripts/` 下初始化脚本，看菜单/权限怎么插入（表名、字段、外键到角色）。

**Step 4: 锁定 `{register_router}`** —— 看 `backend/app/api/v1/` 的 `__init__.py` 或路由聚合处，看业务 module 的 router 怎么挂上去。

**Step 5: 锁定 `{register_job}`** —— 读 `backend/app/module_task/`，看 APScheduler job 怎么用代码声明（vs 后台 UI 配置）。确认「代码注册 job」可行（importer 需要代码注册，不是 UI 配）。

**Step 6: 锁定 `{resp_ok}/{resp_err}`** —— 读 `backend/app/common/response.py`，记下统一响应函数签名。

**Step 7: 写 cheatsheet** —— 把上述 5 项 + Task 0.2 的代码生成器输出树 + model/schema/crud 模板范例，整理进 `fastapiadmin-cheatsheet.md`。这份文档是 Phase 1 的「地基规范」。

**Step 8: Commit**
```bash
git add cube123/docs/plans/fastapiadmin-cheatsheet.md
git commit -m "docs(pyom): FastapiAdmin conventions cheatsheet"
```

---

### Task 0.4: 端到端验证一个自定义模块（证明环路通）

> 用 cheatsheet 手写一个最小自定义模块，不靠代码生成器，证明「自定义 model + service + 路由 + RBAC + 菜单」全链路可走通。

**Files:**
- Create: `pyom/backend/app/api/v1/module_om/hello/`（model/schema/crud/service/controller）
- Modify: 菜单 seed、路由聚合（按 cheatsheet）

**Step 1: 写 model** —— `model.py` 定义 `Hello` 表（id, msg），用 `{BaseClass}`。

**Step 2: 生成并应用迁移**
```bash
uv run main.py revision --env=dev
uv run main.py upgrade --env=dev
```

**Step 3: 写 schema/crud/service/controller** —— controller 暴露 `GET /om/hello`，用 `{rbac_dep}` 加权限，返回 `{resp_ok(...)}`。

**Step 4: 注册路由** —— 按 `{register_router}` 挂到 v1。

**Step 5: seed 菜单** —— 按 `{menu_seed}` 插一条「py-om > Hello」菜单 + 按钮权限。

**Step 6: 验证** —— 前端刷新看到菜单，调 `GET /om/hello` 返回数据，无权限用户被拦。

**Step 7: Commit**
```bash
git add pyom/backend/app/api/v1/module_om/
git commit -m "feat(pyom): smoke custom module end-to-end"
```

---

### Task 0.5: Go/No-Go 决策

**验收（全绿才 Go）：**
- [ ] 全栈起得来（Postgres+Redis+backend+Vue 前端），admin 能登录
- [ ] 代码生成器能用，输出结构已记录
- [ ] cheatsheet 5 个占位符全部锁定
- [ ] 自定义模块全链路通（model→RBAC→菜单→前端可见）
- [ ] APScheduler 能用代码注册 job（importer 的前提）

**No-Go 处置：** 若 APScheduler 不能代码注册 job、或 RBAC/菜单机制过于反人类、或框架跑不起来 → 停止，回退评估 fastapi-amis-admin 或自建（FastAPI + Vue3 + 手写 RBAC）。把决策记录追加到设计文档 `## 11. 风险`。

---

## Phase 1 — MVP（~3 周）

> 依赖顺序：**测试基建 → asset 模型 → importer → governance FSM → 钉钉通知 → 审批中心 UI → MCP → 工作台看板 → 迁移脚本**。每个模块 TDD：先写失败测试 → 实现 → 过测 → commit。CRUD 骨架用代码生成器出，**业务逻辑（service）手写并测**。

### Task 1.0: 测试基建

**Files:**
- Modify: `pyom/backend/pyproject.toml`（加测试依赖）
- Create: `pyom/backend/tests/conftest.py`
- Create: `pyom/backend/tests/README.md`

**Step 1: 加依赖**
```bash
cd pyom/backend
uv add --dev pytest pytest-asyncio httpx pytest-cov
```

**Step 2: 写 conftest（异步 DB fixture，每测回滚）**
```python
# tests/conftest.py
import pytest, pytest_asyncio
from httpx import AsyncClient, ASGITransport
# from app.<...> import app  # 按 cheatsheet 找到 FastAPI app 实例
from app.core.db import async_session  # 按 cheatsheet 调整 import

@pytest_asyncio.fixture
async def db():
    async with async_session() as session:
        async with session.begin():
            yield session
            await session.rollback()

@pytest_asyncio.fixture
async def client(db):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
```
> import 路径按 Task 0.3 cheatsheet 校正。

**Step 3: 配 pytest**
```toml
# pyproject.toml [tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
```

**Step 4: 冒烟测试**
```python
# tests/test_smoke.py
async def test_smoke(client):
    assert client is not None
```
Run: `uv run pytest -v` → PASS。

**Step 5: Commit**
```bash
git add pyom/backend/pyproject.toml pyom/backend/tests/
git commit -m "test(pyom): pytest async harness + smoke"
```

---

### Task 1.1: 配置项（DataWorks / Cube / Superset / 钉钉）

**Files:**
- Modify: `pyom/backend/env/.env.dev`
- Create: `pyom/backend/app/config/sources.py`（或按 cheatsheet 的 config 位置）

**Step 1: 加配置**
```bash
# .env.dev 追加
DATAWORKS_AK=xxx
DATAWORKS_SK=xxx
DATAWORKS_REGION=cn-xxx
DATAWORKS_PROJECT=xxx
CUBE_API_URL=http://localhost:4000/cubejs-api/v1
CUBE_API_TOKEN=xxx
SUPERSET_API_URL=http://localhost:8088/api/v1
SUPERSET_TOKEN=xxx
DINGTALK_WEBHOOK=https://oapi.dingtalk.com/robot/send?access_token=xxx
DINGTALK_SECRET=SECxxx
```

**Step 2: 写配置加载（Pydantic Settings）**
```python
# app/config/sources.py
from pydantic_settings import BaseSettings

class SourceSettings(BaseSettings):
    dataworks_ak: str = ""
    dataworks_sk: str = ""
    dataworks_region: str = ""
    dataworks_project: str = ""
    cube_api_url: str = ""
    cube_api_token: str = ""
    superset_api_url: str = ""
    superset_token: str = ""
    dingtalk_webhook: str = ""
    dingtalk_secret: str = ""
    class Config:
        env_file = "env/.env.dev"

sources = SourceSettings()
```

**Step 3: 测配置加载**
```python
# tests/test_config.py
def test_sources_load():
    from app.config.sources import sources
    assert sources.dataworks_region  # 非空
```
Run → PASS。

**Step 4: Commit**
```bash
git add pyom/backend/app/config/sources.py pyom/backend/env/.env.dev
git commit -m "feat(pyom): source + dingtalk config"
```
> ⚠️ `.env.dev` 含密钥，确认 `.gitignore` 已忽略；只 commit 模板 `.env.dev.example`。

---

### Task 1.2: asset 模型 + version

**Files:**
- Create: `pyom/backend/app/api/v1/module_om/asset/model.py`
- Create: `pyom/backend/app/api/v1/module_om/asset/schema.py`
- Test: `pyom/backend/tests/om/test_asset_model.py`

**Step 1: 写失败测试**
```python
# tests/om/test_asset_model.py
import pytest

@pytest.mark.asyncio
async def test_create_asset(db):
    from app.api.v1.module_om.asset.model import Asset
    a = Asset(source_system="dataworks", source_ref="dw.fct_order",
              fqn="model.dataworks.fct_order", name="订单事实表",
              type="model", status="draft", business_domain="交易")
    db.add(a)
    await db.flush()
    assert a.id is not None
    assert a.status == "draft"

def test_status_transitions_invalid():
    from app.api.v1.module_om.asset.model import is_valid_transition
    assert is_valid_transition("draft", "active") is True
    assert is_valid_transition("draft", "archived") is False  # 跳变
    assert is_valid_transition("active", "deprecated") is True
```

**Step 2: Run → FAIL（模块未定义）**
```bash
uv run pytest tests/om/test_asset_model.py -v
```

**Step 3: 写 model**
```python
# app/api/v1/module_om/asset/model.py
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy import String, Integer, JSON
from datetime import datetime
from app.core.db import Base  # {BaseClass} — 按 cheatsheet 校正

VALID_TRANSITIONS = {
    ("draft", "active"), ("active", "deprecated"),
    ("deprecated", "archived"), ("archived", "active"),  # 允许重新激活
}

def is_valid_transition(frm: str, to: str) -> bool:
    return (frm, to) in VALID_TRANSITIONS

class Asset(Base):
    __tablename__ = "pyom_asset"
    id: Mapped[int] = mapped_column(primary_key=True)
    source_system: Mapped[str] = mapped_column(String(32), index=True)
    source_ref: Mapped[str] = mapped_column(String(255), index=True)
    fqn: Mapped[str] = mapped_column(String(255), unique=True)
    name: Mapped[str] = mapped_column(String(255))
    type: Mapped[str] = mapped_column(String(32))  # model/metric/dashboard/api/subscription
    description: Mapped[str | None] = mapped_column(String(2000))
    owner: Mapped[str | None] = mapped_column(String(100))
    team: Mapped[str | None] = mapped_column(String(100))
    business_domain: Mapped[str | None] = mapped_column(String(100), index=True)
    status: Mapped[str] = mapped_column(String(32), default="draft", index=True)
    custom_properties: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=datetime.utcnow, onupdate=datetime.utcnow)

class AssetVersion(Base):
    __tablename__ = "pyom_asset_version"
    id: Mapped[int] = mapped_column(primary_key=True)
    asset_id: Mapped[int] = mapped_column(index=True)
    version: Mapped[int]
    snapshot: Mapped[dict] = mapped_column(JSON)
    changed_by: Mapped[str | None] = mapped_column(String(100))
    changed_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)
```

**Step 4: 迁移**
```bash
uv run main.py revision --env=dev
uv run main.py upgrade --env=dev
```

**Step 5: Run → PASS**
```bash
uv run pytest tests/om/test_asset_model.py -v
```

**Step 6: schema + 代码生成器出 CRUD/controller/Vue** —— 用 Task 0.2 流程对 `pyom_asset` 跑代码生成器，落到 `module_om/asset/`；手写 `schema.py` 补中文校验消息。

**Step 7: seed 菜单** —— 「数据资产」一级菜单 + 「资产目录」子菜单 + 增删改查按钮权限（按 `{menu_seed}`）。

**Step 8: 验证** —— 前端能 CRUD 资产；无权限用户看不到。

**Step 9: Commit**
```bash
git add pyom/backend/app/api/v1/module_om/asset/
git commit -m "feat(pyom): asset catalog model + CRUD"
```

---

### Task 1.3: asset_quality_cache 模型

**Files:** `module_om/asset/model.py`（追加）、`tests/om/test_quality_cache.py`

**Step 1: 测试**
```python
async def test_quality_cache_upsert(db):
    from app.api.v1.module_om.asset.model import AssetQualityCache
    c = AssetQualityCache(asset_id=1, source="dataworks", status="pass", last_checked=datetime.utcnow())
    db.add(c); await db.flush()
    assert c.id is not None
```

**Step 2: 加 model** —— `AssetQualityCache(asset_id, source, status, last_checked)`。

**Step 3: 迁移 + 跑测 + commit** —— `feat(pyom): asset quality cache`。

---

### Task 1.4: Source Importer 抽象 + DataWorks importer

**Files:**
- Create: `pyom/backend/app/api/v1/module_om/importer/base.py`
- Create: `pyom/backend/app/api/v1/module_om/importer/dataworks.py`
- Test: `pyom/backend/tests/om/importer/test_dataworks.py`

**Step 1: 写 base + 失败测试（用 fixture，不打真实 API）**
```python
# base.py
from abc import ABC, abstractmethod

class SourceImporter(ABC):
    @abstractmethod
    async def list_assets(self) -> list[dict]:
        """返回 [{source_ref, name, type, extra}, ...]"""

    async def sync(self, db) -> dict:
        """同步到 pyom_asset；新资产入 draft。返回 {created, updated}."""
        created = updated = 0
        for item in await self.list_assets():
            # upsert by (source_system, source_ref)
            ...  # 见实现
        return {"created": created, "updated": updated}
```

```python
# tests/om/importer/test_dataworks.py
import pytest
from unittest.mock import AsyncMock

@pytest.mark.asyncio
async def test_dataworks_sync_creates_draft_assets(db, monkeypatch):
    from app.api.v1.module_om.importer.dataworks import DataWorksImporter
    imp = DataWorksImporter()
    monkeypatch.setattr(imp, "list_assets",
        AsyncMock(return_value=[{"source_ref":"dw.t1","name":"t1","type":"model"}]))
    result = await imp.sync(db)
    assert result["created"] == 1
    from app.api.v1.module_om.asset.model import Asset
    a = db.query(Asset).first() if False else None  # async 取
    # 用 db 查到 status=="draft"
```

**Step 2: Run → FAIL**

**Step 3: 写 DataWorks importer** —— 用阿里云 DataWorks OpenAPI（`alibabacloud_dataworks20240518` 或对应 SDK）拉表/节点。`list_assets` 调 OpenAPI，`sync` upsert。
```python
# dataworks.py（骨架）
from .base import SourceImporter
from app.config.sources import sources

class DataWorksImporter(SourceImporter):
    async def list_assets(self) -> list[dict]:
        # 调 DataWorks OpenAPI list tables/nodes（用 sources.dataworks_ak/sk/region/project）
        # 真实 SDK 调用在 consult 官方 SDK 文档后补全
        ...

    async def fetch_quality(self, source_refs: list[str]) -> dict:
        # 拉 DataWorks 数据质量状态，刷 asset_quality_cache
        ...
```
> ⚠️ DataWorks OpenAPI Python SDK 调用细节：实现前用 `oh-my-claudecode:document-specialist` 或 context7 查 `alibabacloud-dataworks` SDK 的列表接口签名，不要凭记忆写。

**Step 4: Run → PASS（用 mock）**

**Step 5: 注册成 APScheduler job** —— 按 `{register_job}`，每小时跑一次 `DataWorksImporter().sync(db)` + `fetch_quality`。

**Step 6: 验证** —— 手动触发 job（或调一个 `POST /om/importer/run?source=dataworks` 内部路由），确认 `pyom_asset` 出现 dataworks 资产。

**Step 7: Commit** —— `feat(pyom): DataWorks importer + scheduled sync`。

---

### Task 1.5: Cube importer + Superset importer

**Files:**
- Create: `importer/cube.py`、`importer/superset.py`
- Test: `tests/om/importer/test_cube.py`、`test_superset.py`

**Step 1-5（每个 importer 同 1.4 模式）：**
- Cube：`GET {cube_api_url}/meta` → 指标列表 → upsert `asset(source=cube, type=metric)`
- Superset：`GET {superset_api_url}/dashboard/` → 报表 → upsert `asset(source=superset, type=dashboard)`
- 两个都 mock 测试，注册成 APScheduler job

**Step 6: Commit** —— `feat(pyom): Cube + Superset importers`。

---

### Task 1.6: 审批模型 + FSM

**Files:**
- Create: `module_om/governance/model.py`（ApprovalRequest / ApprovalDecision）
- Create: `module_om/governance/service.py`（FSM 逻辑）
- Test: `tests/om/governance/test_approval_fsm.py`

**Step 1: 写失败测试**
```python
# test_approval_fsm.py
import pytest

@pytest.mark.asyncio
async def test_approve_publish_moves_draft_to_active(db):
    from app.api.v1.module_om.governance.service import GovernanceService
    # 建一个 draft asset + 一个 pending publish 审批
    svc = GovernanceService(db)
    await svc.decide(request_id=1, approver="alice", decision="approved")
    # 断言 asset.status == "active"，审批 status == "approved"

@pytest.mark.asyncio
async def test_invalid_transition_request_rejected(db):
    # active→archived 跳变，create_request 应拒绝
    ...

def test_transition_requires_approval():
    # draft→active 必须审批；archived→active 反向也要审批
    ...
```

**Step 2: Run → FAIL**

**Step 3: 写 model**
```python
# governance/model.py
class ApprovalRequest(Base):
    __tablename__ = "pyom_approval_request"
    id: Mapped[int] = mapped_column(primary_key=True)
    asset_id: Mapped[int] = mapped_column(index=True)
    type: Mapped[str]  # create/publish/deprecate/archive/change_owner/grant_perm
    requester: Mapped[str]
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    approver_role: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime]
    decided_at: Mapped[datetime | None] = None

class ApprovalDecision(Base):
    __tablename__ = "pyom_approval_decision"
    id: Mapped[int] = mapped_column(primary_key=True)
    request_id: Mapped[int] = mapped_column(index=True)
    approver: Mapped[str]
    decision: Mapped[str]  # approved/rejected
    comment: Mapped[str | None]
    decided_at: Mapped[datetime]
```

**Step 4: 写 service FSM**
```python
# governance/service.py
from app.api.v1.module_om.asset.model import Asset, is_valid_transition

REQ_BY_TYPE = {"publish":"draft→active","deprecate":"active→deprecated","archive":"deprecated→archived"}

class GovernanceService:
    def __init__(self, db): self.db = db

    async def create_request(self, asset_id, type_, requester, payload) -> ApprovalRequest:
        asset = await self._get(asset_id)
        # 校验 type 对应当前 status 合法
        ...  # 否则 raise InvalidTransition

    async def decide(self, request_id, approver, decision, comment=None):
        req = await self._get_req(request_id)
        if req.status != "pending": raise AlreadyDecided
        if decision == "approved":
            await self._apply(req)  # 迁移 asset.status + 写操作日志
        req.status = decision; req.decided_at = now
        # 写 ApprovalDecision；发钉钉通知申请人（Task 1.7 接入后）
```

**Step 5: 迁移 + 跑测 → PASS**

**Step 6: Commit** —— `feat(pyom): approval FSM + lifecycle enforcement`。

---

### Task 1.7: 钉钉通知服务（加签）

**Files:**
- Create: `module_om/notification/dingtalk.py`
- Test: `tests/om/notification/test_dingtalk.py`

**Step 1: 失败测试（mock httpx）**
```python
async def test_dingtalk_sign_and_send(monkeypatch):
    from app.api.v1.module_om.notification.dingtalk import DingTalkNotifier
    sent = {}
    async def fake_post(self, url, json): sent["url"]=url; sent["json"]=json; return {"errcode":0}
    monkeypatch.setattr("app.api.v1.module_om.notification.dingtalk._post", fake_post)
    n = DingTalkNotifier()
    await n.send_approval(request_id=1, asset_name="订单事实表", action="publish")
    assert "timestamp" in sent["json"] and "sign" in sent["json"]
    assert sent["json"]["msgtype"] == "markdown"

def test_sign_deterministic():
    from app.api.v1.module_om.notification.dingtalk import sign
    s = sign(timestamp=123, secret="SECxxx")
    assert isinstance(s, str) and len(s) > 0
```

**Step 2: Run → FAIL**

**Step 3: 实现（钉钉加签算法 = HMAC-SHA256(timestamp+"\n"+timestamp, secret) → base64 → urlquote）**
```python
# dingtalk.py
import hmac, hashlib, base64, time, urllib.parse, httpx
from app.config.sources import sources

def sign(timestamp: int, secret: str) -> str:
    string_to_sign = f"{timestamp}\n{timestamp}"
    hmac_code = hmac.new(secret.encode(), string_to_sign.encode(), digestmod=hashlib.sha256).digest()
    return urllib.parse.quote_plus(base64.b64encode(hmac_code))

async def _post(url, json): ...  # httpx 封装

class DingTalkNotifier:
    async def send_approval(self, request_id, asset_name, action, requester=None):
        ts = int(time.time() * 1000)
        url = f"{sources.dingtalk_webhook}&timestamp={ts}&sign={sign(ts, sources.dingtalk_secret)}"
        msg = {"msgtype":"markdown","markdown":{
            "title":f"资产审批：{action}",
            "text":f"### 资产审批待处理\n- 资产：**{asset_name}**\n- 动作：{action}\n- [去审批](http://pyom.local/approval/{request_id})"}}
        return await _post(url, msg)
```
> 钉钉加签算法实现前对照官方文档核一遍（base64 + urlquote 顺序），不要凭记忆。

**Step 4: Run → PASS**

**Step 5: 接入 GovernanceService** —— `create_request` 后调 `DingTalkNotifier().send_approval(...)`；`decide` 后可选通知申请人。

**Step 6: 真机验证**（手动）—— 配真实钉钉机器人 webhook，跑一次审批，确认钉钉群收到消息 + 签名通过。

**Step 7: Commit** —— `feat(pyom): DingTalk signed webhook notifier`。

---

### Task 1.8: 审批中心 UI（Vue）+ 控制器

**Files:**
- Create: `module_om/governance/controller.py`（`GET /om/approvals`、`POST /om/approvals/{id}/decide`，用 `{rbac_dep}`）
- Create: `frontend/web/src/views/om/approval/`（列表 + 通过/拒绝按钮 + 详情）
- seed「治理 > 审批中心」菜单 + `om:approval:decide` 按钮权限

**Step 1: controller 测试**
```python
async def test_list_my_approvals(client, auth_as_steward):
    r = await client.get("/om/approvals")
    assert r.status_code == 200
async def test_decide_requires_perm(client, auth_as_viewer):
    r = await client.post("/om/approvals/1/decide", json={"decision":"approved"})
    assert r.status_code == 403
```

**Step 2: 写 controller** —— 调 `GovernanceService.decide`，`{rbac_dep}` 守 `om:approval:decide`。

**Step 3: Vue 审批中心页** —— 列表（资产/类型/申请人/状态）+ 行内「通过/拒绝」按钮 → 调 controller。按 Element-Plus 表格写，参照 Task 0.4 的自定义页范例。

**Step 4: seed 菜单 + 权限**。

**Step 5: 验证** —— 审批人能看到待办，能通过/拒绝；普通用户被拦。

**Step 6: Commit** —— `feat(pyom): approval center UI + RBAC`。

---

### Task 1.9: MCP Server（旁路，给 cube123 Agent）

**Files:**
- Create: `pyom/mcp_server/server.py`（独立进程，stdio）
- Create: `pyom/mcp_server/tools.py`
- Test: `pyom/backend/tests/om/test_mcp_tools.py`

> 用官方 MCP Python SDK（`pip install mcp`）。MCP server 复用 backend 的 service 层（直接 import GovernanceService 等，或通过内部 HTTP 调 backend API）。MVP 用**内部 HTTP 调 backend API**（解耦进程、复用 RBAC 用一个 service-account token）。

**Step 1: 工具定义 + 失败测试**
```python
# test_mcp_tools.py — 测工具逻辑（不测 stdio 传输）
async def test_search_asset_tool(client):
    from mcp_server.tools import search_asset
    res = await search_asset(q="订单")
    assert any("订单" in a["name"] for a in res)

async def test_request_approval_does_not_mutate(client):
    # request_approval 只建审批单，不动 asset.status
    ...
```

**Step 2: 实现 5 个 tools**：`search_asset`、`get_asset`、`list_my_approvals`、`request_approval`、`check_permission`。每个调 backend 的对应内部 API（带 service-account header）。`request_approval` 守 CLAUDE.md「建议→人工确认→执行」——只建 pending 审批。

**Step 3: 起 stdio server**
```python
# mcp_server/server.py
from mcp.server.fastmcp import FastMCP
mcp = FastMCP("py-om")
# register tools...
if __name__ == "__main__":
    mcp.run(transport="stdio")
```

**Step 4: cube123 端挂 MCP** —— 更新 `cube123/config/mcp-servers.jsonc`，加 py-om MCP server 条目（command 指向 `pyom/mcp_server`）。

**Step 5: 验证** —— cube123 Agent 能调 `search_asset`/`request_approval`；`request_approval` 后 py-om 出现 pending 审批 + 钉钉通知。

**Step 6: Commit** —— `feat(pyom): MCP server (search/get/approval/permission)`。

---

### Task 1.10: 工作台看板（资产健康 / 僵尸资产）

**Files:**
- Create: `module_om/dashboard/controller.py`（`GET /om/dashboard/summary`）
- Create: `frontend/web/src/views/om/dashboard/`（ECharts 图）

**Step 1: controller 返回** `{by_status, by_domain, by_source, zombie_count}`，zombie = 30 天无访问 + status=active（访问数据 MVP 没有，先用「30 天无更新 + 无 owner」近似，标 TODO）。

**Step 2: Vue 看板** —— 工作台首页加 py-om 卡片：状态分布饼图、僵尸资产 Top10 表。

**Step 3: Commit** —— `feat(pyom): workbench dashboard (status/zombie)`。

---

### Task 1.11: 迁移脚本（OpenMetadata 1.13.1 → py-om）

**Files:**
- Create: `pyom/backend/app/scripts/migrate_from_om.py`

**Step 1: 脚本** —— 读 OM REST `/api/v1/tables`（用 OM token），映射到 `asset(source=dataworks/oracle/..., type=model)`，只取 name/description/owner/columns 计数，丢 glossary/tag/profile。幂等：按 source_ref 去重。

**Step 2: 干跑** —— `uv run python app/scripts/migrate_from_om.py --dry-run`，打印将迁移的资产数 + 样本。

**Step 3: 真跑 + 校验** —— 迁移后 `SELECT count(*) FROM pyom_asset` 与 OM 表数对齐。

**Step 4: Commit** —— `feat(pyom): one-shot migration from OpenMetadata`。

---

### Task 1.12: 端到端 demo 自检（ponytail check）

**Files:**
- Create: `pyom/backend/scripts/demo_e2e.py`

**Step 1: 写 demo**
```python
# scripts/demo_e2e.py
"""
闭环验收：DataWorks 自动发现表 → 补中文名/owner → 申请发布 → 钉钉通知 → 后台审批 → 发布 + 操作日志。
用法：uv run python scripts/demo_e2e.py
断言：最终 asset.status == 'active'，审批 approved，操作日志存在。
"""
```
**Step 2: 跑通**（mock DataWorks + 真实 py-om + mock 钉钉）→ 全绿。

**Step 3: Commit** —— `test(pyom): e2e demo self-check`。

---

## 验收：Phase 1 MVP 完成

- [ ] 代码覆盖率 ≥ 80%（`uv run pytest --cov=app --cov-report=term-missing`）
- [ ] 所有 Task 1.x 测试绿
- [ ] 端到端 demo（Task 1.12）跑通
- [ ] DataWorks/Cube/Superset importer 定时同步生效（看 `pyom_asset` 有数据）
- [ ] 审批闭环：申请 → 钉钉通知 → 后台审批 → 状态迁移 + 操作日志
- [ ] MCP server 挂到 cube123，Agent 能查资产/发起审批
- [ ] 从 OM 迁移脚本干跑 + 真跑成功
- [ ] cube123 `config/mcp-servers.jsonc` 双挂 OM + py-om（过渡期）

---

## Phase 2 预告（不在本计划）

钉钉交互卡片回调 · api/subscription 资产 · **血缘（`lineage_edge` + 递归 CTE + 影响分析）** · 使用度分析（doc-21）· 术语表/标签 · 独立门户（若 A 体验不足）

---

## 关键风险与对策（执行时盯）

| 风险 | 触发任务 | 对策 |
|---|---|---|
| FastapiAdmin 的 RBAC/菜单机制反人类 | Task 0.4 | No-Go → 回退 fastapi-amis-admin/自建 |
| APScheduler 不能代码注册 job | Task 0.3 | No-Go；或退化为后台 UI 手配（importer 不优雅但能跑） |
| DataWorks OpenAPI SDK 细节不准 | Task 1.4 | 实现前用 document-specialist / context7 查 `alibabacloud-dataworks` SDK |
| 钉钉加签算法写错 | Task 1.7 | 对照钉钉官方文档核 base64+urlquote 顺序；真机验证 |
| 测试 DB 与 jsonb/async 不兼容 | Task 1.0 | 用真实 Postgres（asyncpg）+ 每测事务回滚，别用 sqlite |
| `.env.dev` 泄密 | Task 1.1 | `.gitignore` 忽略；只 commit `.example` |

---

> 关联：`cube123/docs/plans/2026-08-11-openmetadata-python-rewrite-design.md`（设计）· `cube123/docs/plans/fastapiadmin-cheatsheet.md`（Task 0.3 产出，Phase 1 地基）
