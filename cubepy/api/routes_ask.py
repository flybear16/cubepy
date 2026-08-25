"""AI ask layer (M2): natural language -> Cube Query -> answer.

``POST /cubepy/v1/ask {"question": "..."}``

Security model (F-E1.2 red line): the LLM prompt is built from cubes visible
to the caller's ``SecurityContext`` — the exact same ctx that later drives
RLS in the orchestrator. The LLM can never even *name* a member the caller
may not query. LLM output is untrusted: member whitelist + ``Query.parse``
fail-closed validation before anything reaches the executor (F-E1.1).

Shadow paths (all covered by tests):
- Nil: unrelated/empty question -> LLM returns notAnswerable -> 400, no 500.
- Empty: valid query, zero rows -> 200 with an explicit "no data" answer.
- Upstream: LLM failure after retry -> 503; DB errors ride the orchestrator's
  existing error path.
- Invalid members: whitelist reject -> one repair round-trip -> 400 if still bad.
"""

from __future__ import annotations

import importlib
import json
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from cubepy.ai import members_index, system_prompt
from cubepy.api.deps import get_orchestrator
from cubepy.config import Settings, settings
from cubepy.llm import ChatModel, LLMError, extract_json
from cubepy.orchestrator.orchestrator import QueryOrchestrator
from cubepy.schema.registry import registry
from cubepy.security.auth import security_context
from cubepy.security.context import SecurityContext
from cubepy.security.permissions import PermissionBuilder
from cubepy.sqlgen.query import Query

router = APIRouter(dependencies=[Depends(security_context)])


class AskRequest(BaseModel):
    question: str


def get_llm(request: Request) -> ChatModel:
    llm: ChatModel | None = getattr(request.app.state, "llm", None)
    if llm is None:
        raise HTTPException(status_code=503, detail="ask layer not configured (no LLM)")
    return llm


def _glossary(conf: Settings) -> dict[str, str]:
    """Resolve ``ask_glossary`` ("module.ATTR") per request.

    A bad path fails LOUD (500 with the path) — silently dropping the glossary
    would disarm the term-hallucination defense without anyone noticing.
    """
    module, _, attr = conf.ask_glossary.rpartition(".")
    try:
        resolved = getattr(importlib.import_module(module), attr)
    except (ImportError, AttributeError, ValueError) as exc:
        raise HTTPException(
            status_code=500, detail=f"ask_glossary 配置无效: {conf.ask_glossary}"
        ) from exc
    if not isinstance(resolved, dict):
        raise HTTPException(
            status_code=500, detail=f"ask_glossary 不是 dict: {conf.ask_glossary}"
        )
    return dict(resolved)


def _visible_cubes(ctx: SecurityContext) -> list:
    """Permission-filtered cube list — same visibility rules as /meta."""
    return [c for c in registry.all() if PermissionBuilder.cube_visible(c, ctx)]


def _collect_members(query: dict[str, Any]) -> list[str]:
    """Every member path an LLM query payload references (filters are recursive)."""
    out: list[str] = list(query.get("measures") or [])
    out += list(query.get("dimensions") or [])
    out += list(query.get("segments") or [])
    out += [td.get("dimension", "") for td in query.get("timeDimensions") or []]
    out += list((query.get("order") or {}).keys())

    def walk_filters(filters: Any) -> None:
        if isinstance(filters, dict):
            for key in ("or", "and"):
                if key in filters:
                    walk_filters(filters[key])
            member = filters.get("member")
            if isinstance(member, str):
                out.append(member)
        elif isinstance(filters, list):
            for f in filters:
                walk_filters(f)

    walk_filters(query.get("filters"))
    return [m for m in out if m]


def _audit(conf: Settings, entry: dict[str, Any]) -> str:
    """Append one JSONL audit line (identity + question + query + outcome)."""
    audit_id = uuid.uuid4().hex[:12]
    entry = {"audit_id": audit_id, "ts": datetime.now(UTC).isoformat(), **entry}
    if conf.ask_audit_log:
        try:
            path = Path(conf.ask_audit_log)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError:  # noqa: BLE001 — audit must never break the request
            pass
    return audit_id


async def _interpret(llm: ChatModel, rows: list[dict[str, Any]], question: str) -> str | None:
    """LLM#2: one-line insight. Best-effort — degrades to None on any failure."""
    try:
        sample = json.dumps(rows[:20], ensure_ascii=False, default=str)
        reply = await llm.chat(
            [
                {
                    "role": "system",
                    "content": "你是数据解读助手。用一句中文总结下述查询结果对用户问题的回答，"
                    "直接给数字结论，不要复述数据，不要编造未出现的数字。",
                },
                {"role": "user", "content": f"问题：{question}\n结果（最多20行）：{sample}"},
            ]
        )
        return reply.strip() or None
    except LLMError:
        return None


@router.post("/v1/ask")
async def ask(
    body: AskRequest,
    ctx: SecurityContext = Depends(security_context),
    orch: QueryOrchestrator = Depends(get_orchestrator),
    llm: ChatModel = Depends(get_llm),
    conf: Settings = Depends(lambda: settings),
) -> dict[str, Any]:
    question = body.question.strip()
    if not question:
        raise HTTPException(status_code=400, detail="question 不能为空")

    visible = _visible_cubes(ctx)
    allowed = set(members_index(visible))
    messages: list[dict[str, str]] = [
        {"role": "system", "content": system_prompt(visible, glossary=_glossary(conf))},
        {"role": "user", "content": question},
    ]

    start = time.perf_counter()
    # LLM#1: NL -> query JSON, with one repair round-trip on invalid output.
    payload: dict[str, Any] | None = None
    last_error = "unknown"
    for attempt in (1, 2):
        try:
            raw = await llm.chat(messages)
            payload = extract_json(raw)
        except (LLMError, ValueError) as exc:
            raise HTTPException(
                status_code=503, detail=f"LLM 暂时不可用，请稍后重试（{exc}）"
            ) from exc
        if payload.get("notAnswerable"):
            reason = str(payload.get("reason") or "无法从现有指标回答该问题")
            _audit(
                conf,
                {
                    "outcome": "not_answerable",
                    "user_id": ctx.user_id,
                    "tenant_id": ctx.tenant_id,
                    "question": question,
                    "query": None,
                },
            )
            raise HTTPException(status_code=400, detail=f"{reason}。换个问法试试？")
        invalid = [m for m in _collect_members(payload) if m not in allowed]
        if not invalid:
            try:
                query = Query.parse(payload)
                break
            except (ValueError, KeyError) as exc:  # noqa: PERF203 — last try below
                last_error = str(exc)
        else:
            last_error = f"unknown members: {', '.join(invalid)}"
        payload = None  # reset on every failed attempt; set again by the next round
        if attempt == 1:
            messages += [
                {"role": "assistant", "content": raw},
                {"role": "user", "content": f"无效：{last_error}。只返回修正后的 JSON。"},
            ]
    if payload is None:
        _audit(
            conf,
            {
                "outcome": "invalid_query",
                "user_id": ctx.user_id,
                "tenant_id": ctx.tenant_id,
                "question": question,
                "query": None,
            },
        )
        raise HTTPException(status_code=400, detail=f"无法生成有效查询：{last_error}")

    # Same ctx -> RLS injected (F-E1.2). DB errors ride the orchestrator path.
    env = await orch.load(query, ctx)
    rows = env.get("data") or []
    latency_ms = round((time.perf_counter() - start) * 1000, 1)

    if not rows:  # Empty path: a valid question with no matching rows.
        answer = "该条件下没有数据。试着放宽时间范围或过滤条件。"
    elif conf.ask_interpret:
        answer = await _interpret(llm, rows, question) or "查询完成，见 data。"
    else:
        answer = "查询完成，见 data。"

    audit_id = _audit(
        conf,
        {
            "outcome": "ok",
            "user_id": ctx.user_id,
            "tenant_id": ctx.tenant_id,
            "question": question,
            "query": payload,
            "rows": len(rows),
            "latency_ms": latency_ms,
        },
    )
    return {
        "answer": answer,
        "data": rows,
        "query": payload,
        "usedPreAggregations": env.get("usedPreAggregations") or [],
        "auditId": audit_id,
    }
