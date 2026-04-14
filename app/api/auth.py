"""Admin API authentication — Section 4.1 (Security Hardening).

Static API key authentication for all /admin/* routes.
The key is set via ADMIN_API_KEY environment variable.
"""

from __future__ import annotations

from fastapi import Depends, Header, HTTPException

from app.config import get_settings


async def require_admin_key(
    x_admin_key: str = Header(
        ...,
        alias="X-Admin-Key",
        description="Static API key for admin access",
    ),
) -> str:
    """FastAPI dependency — validates the admin API key header.

    Usage: Add as a dependency to the admin router:
        router = APIRouter(prefix="/admin", dependencies=[Depends(require_admin_key)])
    """
    settings = get_settings()
    expected = settings.admin_api_key
    if not expected:
        # No key configured — block all admin access for safety
        raise HTTPException(
            status_code=503,
            detail="Admin API key not configured. Set ADMIN_API_KEY in environment.",
        )
    if x_admin_key != expected:
        raise HTTPException(status_code=401, detail="Invalid admin API key")
    return x_admin_key
