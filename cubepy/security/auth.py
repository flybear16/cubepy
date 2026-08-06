"""FastAPI dependency: ``Authorization: Bearer <jwt>`` -> SecurityContext."""

from __future__ import annotations

import jwt
from fastapi import Header, HTTPException

from cubepy.config import settings
from cubepy.security.context import SecurityContext


def security_context(
    authorization: str | None = Header(default=None),
) -> SecurityContext:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    token = authorization.split(" ", 1)[1].strip()
    try:
        return SecurityContext.from_jwt(
            token,
            secret=settings.jwt_secret,
            algorithm=settings.jwt_algorithm,
        )
    except jwt.PyJWTError as exc:
        raise HTTPException(status_code=401, detail="Invalid or expired token") from exc
