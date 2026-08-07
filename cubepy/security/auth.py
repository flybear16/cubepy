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
    if settings.jwt_algorithm.upper() == "RS256":
        secret = settings.jwt_public_key
        if not secret:
            raise HTTPException(
                status_code=500, detail="CUBEPY_JWT_PUBLIC_KEY required for RS256"
            )
    else:
        secret = settings.jwt_secret
    try:
        return SecurityContext.from_jwt(
            token,
            secret=secret,
            algorithm=settings.jwt_algorithm,
        )
    except jwt.PyJWTError as exc:
        raise HTTPException(status_code=401, detail="Invalid or expired token") from exc
