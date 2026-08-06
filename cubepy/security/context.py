"""Security context: the authenticated identity carried through a request.

Decoded from a JWT (HS256) in the ``Authorization: Bearer`` header. Typed fields
cover the common claims; the full decoded payload is kept in ``claims`` so that
schema ``shown`` / ``check_permission`` callbacks can read custom claims.
"""

from __future__ import annotations

from typing import Any

import jwt
from pydantic import BaseModel, ConfigDict


class SecurityContext(BaseModel):
    model_config = ConfigDict(frozen=True)

    user_id: str = ""
    role: str = "viewer"
    department: str = ""
    tenant_id: str = ""
    # Full decoded JWT payload, for custom claims accessed by callbacks.
    claims: dict[str, Any] = {}

    @classmethod
    def from_jwt(
        cls, token: str, *, secret: str, algorithm: str = "HS256"
    ) -> SecurityContext:
        payload: dict[str, Any] = jwt.decode(token, secret, algorithms=[algorithm])
        data: dict[str, Any] = {
            "user_id": str(payload.get("sub") or payload.get("userId") or ""),
            "role": str(payload.get("role") or "viewer"),
            "department": str(payload.get("dept") or payload.get("department") or ""),
            "tenant_id": str(payload.get("tid") or payload.get("tenantId") or ""),
            "claims": dict(payload),
        }
        return cls.model_validate(data)


def create_token(claims: dict[str, Any], *, secret: str, algorithm: str = "HS256") -> str:
    """Mint a JWT for dev/test. Not wired into the API surface."""
    return jwt.encode(claims, secret, algorithm=algorithm)
