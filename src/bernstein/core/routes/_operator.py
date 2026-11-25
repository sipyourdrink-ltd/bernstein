"""Authenticated authority for installation-wide operator surfaces."""

from __future__ import annotations

from fastapi import HTTPException, Request


def require_operator(request: Request) -> str:
    # Check the authenticated context again so auth-disabled installations and
    # direct router mounts cannot manufacture operator authority.
    claims = getattr(request.state, "auth_claims", {})
    if getattr(request.state, "agent_identity", None) is not None or claims.get("cluster"):
        raise HTTPException(status_code=403, detail="Authenticated operator authority required")
    user = getattr(request.state, "user", None)
    if user is not None and user.has_permission("admin:manage"):
        return f"operator:{user.id}"
    if claims.get("legacy") is True:
        return "operator:installation-token"
    raise HTTPException(status_code=403, detail="Authenticated operator authority required")
