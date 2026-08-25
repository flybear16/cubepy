"""M2 ask-layer endpoint tests: shadow paths, security red line, audit.

Uses FakeLLM (deterministic, no network) + a fake executor + fakeredis, the
same seams test_api_rest.py uses. The PG integration test rides ``pg_reseed``
with the Hologres-emulated schema.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import fakeredis
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.sql.elements import TextClause

from cubepy.api.app import create_app
from cubepy.cache.redis_cache import RedisCache
from cubepy.config import settings
from cubepy.llm import FakeLLM
from cubepy.orchestrator.orchestrator import QueryOrchestrator
from cubepy.schema.loader import cube, dimension, measure
from cubepy.schema.meta import MeasureType
from cubepy.schema.registry import registry
from cubepy.security.context import create_token


class _FakeExecutor:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows

    async def execute(self, stmt: TextClause) -> list[dict]:
        return [dict(r) for r in self.rows]


_GOOD_QUERY = '{"measures": ["Orders.revenue"], "dimensions": ["Orders.status"]}'
_GOOD_ROWS = [{"Orders.status": "shipped", "Orders.revenue": 40.0}]


@pytest.fixture(autouse=True)
def _schema() -> Iterator[None]:
    registry.clear()

    @cube(
        "Orders",
        "orders",
        security_context={"check_permission": lambda ctx: [f"orders.tenant_id = {ctx.tenant_id}"]},
    )
    class _O:
        revenue = measure("amount", MeasureType.SUM)
        count = measure(None, MeasureType.COUNT)
        status = dimension("status", "string")

    @cube("Secret", "secret", shown=lambda ctx: ctx.role == "admin")
    class _S:
        salary = measure("salary", MeasureType.SUM)

    yield
    registry.clear()


def _client(
    rows: list[dict],
    llm: FakeLLM,
) -> AsyncClient:
    orch = QueryOrchestrator(
        RedisCache(fakeredis.FakeAsyncRedis()), _FakeExecutor(rows), settings=settings
    )
    app = create_app(orchestrator=orch, llm=llm)
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _auth(role: str = "admin") -> dict[str, str]:
    token = create_token({"sub": "u1", "role": role, "tid": "42"}, secret=settings.jwt_secret)
    return {"Authorization": f"Bearer {token}"}


async def _ask(ac: AsyncClient, question: str, role: str = "admin"):
    return await ac.post("/cubepy/v1/ask", headers=_auth(role), json={"question": question})


# --- happy path ---------------------------------------------------------------


async def test_ask_happy_path_includes_data_query_and_insight() -> None:
    llm = FakeLLM(responses=[_GOOD_QUERY, "已发货收入共 40。"])
    async with _client(_GOOD_ROWS, llm) as ac:
        r = await _ask(ac, "已发货收入多少？")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["data"] == _GOOD_ROWS
    assert body["answer"] == "已发货收入共 40。"
    assert body["query"]["measures"] == ["Orders.revenue"]
    assert body["auditId"]


async def test_ask_prompt_contains_catalog_and_glossary() -> None:
    llm = FakeLLM(responses=[_GOOD_QUERY, "ok"])
    async with _client(_GOOD_ROWS, llm) as ac:
        await _ask(ac, "收入")
    system = llm.calls[0][0]["content"]
    assert "## Catalog" in system
    assert "Orders.revenue" in system
    assert "## Glossary" in system  # hardcoded term table injected (F-E1.3)
    # Date anchor (M2 real-LLM acceptance caught the training-cutoff year bug)
    from datetime import UTC, datetime

    assert f"Today's date is {datetime.now(UTC).date().isoformat()}" in system


# --- security red line (F-E1.2): LLM sees only what ctx may query -------------


async def test_ask_prompt_excludes_cubes_invisible_to_role() -> None:
    llm = FakeLLM(responses=[_GOOD_QUERY, "ok"])
    async with _client(_GOOD_ROWS, llm) as ac:
        await _ask(ac, "收入", role="viewer")
    system = llm.calls[0][0]["content"]
    assert "Orders.revenue" in system  # visible cube present
    assert "Secret" not in system  # admin-only cube invisible to viewer


async def test_ask_rejects_query_for_invisible_cube_member() -> None:
    # LLM hallucinates the admin-only cube twice; whitelist must reject it
    # fail-closed (400), never execute it.
    llm = FakeLLM(responses=['{"measures": ["Secret.salary"]}'] * 2)
    async with _client(_GOOD_ROWS, llm) as ac:
        r = await _ask(ac, "工资总额", role="viewer")
    assert r.status_code == 400
    assert "Secret.salary" in r.json()["detail"]
    assert len(llm.calls) == 2  # initial + one repair round-trip


# --- shadow paths -------------------------------------------------------------


async def test_ask_not_answerable_returns_400_with_reason() -> None:
    llm = FakeLLM(responses=['{"notAnswerable": true, "reason": "没有工资类指标"}'])
    async with _client(_GOOD_ROWS, llm) as ac:
        r = await _ask(ac, "公司食堂菜单？")
    assert r.status_code == 400
    assert "没有工资类指标" in r.json()["detail"]


async def test_ask_empty_result_returns_200_with_no_data_answer() -> None:
    llm = FakeLLM(responses=[_GOOD_QUERY])
    async with _client([], llm) as ac:
        r = await _ask(ac, "去年已发货收入？")
    assert r.status_code == 200
    assert "没有数据" in r.json()["answer"]
    assert r.json()["data"] == []


async def test_ask_invalid_member_repairs_once_then_succeeds() -> None:
    llm = FakeLLM(responses=['{"measures": ["Orders.nope"]}', _GOOD_QUERY, "修好了：40。"])
    async with _client(_GOOD_ROWS, llm) as ac:
        r = await _ask(ac, "收入")
    assert r.status_code == 200, r.text
    assert r.json()["data"] == _GOOD_ROWS
    # repair message told the LLM exactly what was wrong
    assert "Orders.nope" in llm.calls[1][-1]["content"]


async def test_ask_invalid_member_twice_returns_400() -> None:
    llm = FakeLLM(responses=['{"measures": ["Orders.nope"]}'] * 2)
    async with _client(_GOOD_ROWS, llm) as ac:
        r = await _ask(ac, "收入")
    assert r.status_code == 400
    assert "无法生成有效查询" in r.json()["detail"]


async def test_ask_llm_unavailable_returns_503() -> None:
    llm = FakeLLM()  # exhausted -> LLMError on first call
    async with _client(_GOOD_ROWS, llm) as ac:
        r = await _ask(ac, "收入")
    assert r.status_code == 503


async def test_ask_interpret_failure_degrades_to_generic_answer() -> None:
    # LLM#2 gets no scripted response -> LLMError -> degrade, not 500.
    llm = FakeLLM(responses=[_GOOD_QUERY])
    async with _client(_GOOD_ROWS, llm) as ac:
        r = await _ask(ac, "收入")
    assert r.status_code == 200
    assert r.json()["answer"] == "查询完成，见 data。"


async def test_ask_empty_question_returns_400() -> None:
    llm = FakeLLM(responses=[_GOOD_QUERY])
    async with _client(_GOOD_ROWS, llm) as ac:
        r = await _ask(ac, "   ")
    assert r.status_code == 400


# --- audit (day-1 observability) ----------------------------------------------


async def test_ask_writes_audit_jsonl(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    audit = tmp_path / "ask.jsonl"
    monkeypatch.setattr(settings, "ask_audit_log", str(audit))
    llm = FakeLLM(responses=[_GOOD_QUERY, "40。"])
    async with _client(_GOOD_ROWS, llm) as ac:
        r = await _ask(ac, "收入")
    assert r.status_code == 200
    lines = audit.read_text(encoding="utf-8").strip().splitlines()
    entry = json.loads(lines[-1])
    assert entry["outcome"] == "ok"
    assert entry["question"] == "收入"
    assert entry["rows"] == 1
    assert entry["user_id"] == "u1"
    assert entry["tenant_id"] == "42"
    assert entry["query"]["measures"] == ["Orders.revenue"]
    assert entry["audit_id"] == r.json()["auditId"]


# --- glossary config (M3 lift) ------------------------------------------------


async def test_ask_glossary_swappable_via_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        settings,
        "ask_glossary",
        "cubepy.samples.glossary_trade.TRADE_GLOSSARY",
    )
    llm = FakeLLM(responses=[_GOOD_QUERY, "ok"])
    async with _client(_GOOD_ROWS, llm) as ac:
        r = await _ask(ac, "收入")
    assert r.status_code == 200
    system = llm.calls[0][0]["content"]
    assert "DwdOrders.gmv" in system  # trade glossary won over the default


async def test_ask_bad_glossary_config_fails_loud(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "ask_glossary", "cubepy.nope.MISSING")
    llm = FakeLLM(responses=[_GOOD_QUERY, "ok"])
    async with _client(_GOOD_ROWS, llm) as ac:
        r = await _ask(ac, "收入")
    assert r.status_code == 500
    assert "ask_glossary 配置无效" in r.json()["detail"]


# --- RLS end-to-end on the Hologres-emulated PG --------------------------------


@pytest_asyncio.fixture
async def pg_client(pg_reseed: str) -> AsyncIterator[AsyncClient]:
    from cubepy.orchestrator.executor import make_engine_and_executor

    engine, exe, _ = make_engine_and_executor(pg_reseed)
    orch = QueryOrchestrator(RedisCache(fakeredis.FakeAsyncRedis()), exe, settings=settings)
    app = create_app(orchestrator=orch, llm=FakeLLM(responses=[_GOOD_QUERY, "45。"]))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    await engine.dispose()  # type: ignore[func-returns-value]


@pytest.mark.integration
async def test_ask_rls_isolates_tenant_on_real_pg(pg_client: AsyncClient) -> None:
    r = await pg_client.post(
        "/cubepy/v1/ask",
        headers=_auth(),
        json={"question": "按状态看收入"},
    )
    assert r.status_code == 200, r.text
    by_status = {row["Orders.status"]: row["Orders.revenue"] for row in r.json()["data"]}
    # tenant 42 only: shipped 40 + pending 5; tenant 99's 100 must NOT appear.
    assert by_status == {"shipped": 40.0, "pending": 5.0}
