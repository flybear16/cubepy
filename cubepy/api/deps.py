"""FastAPI dependencies shared across routes."""

from __future__ import annotations

from fastapi import HTTPException, Request

from cubepy.config import Settings, settings
from cubepy.orchestrator.orchestrator import QueryOrchestrator


def get_settings() -> Settings:
    return settings


def get_orchestrator(request: Request) -> QueryOrchestrator:
    orch: QueryOrchestrator | None = getattr(request.app.state, "orchestrator", None)
    if orch is None:
        raise HTTPException(status_code=503, detail="orchestrator not initialized")
    return orch
