"""Per-organization GHL client factory — SARAH_MULTI_ORG_GHL_REVISION §3."""

from __future__ import annotations

import logging
import uuid
from typing import Dict

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.ghl_client.client import GHLClient
from app.models.organization import Organization

logger = logging.getLogger(__name__)

_cache: Dict[uuid.UUID, GHLClient] = {}


def effective_ghl_credentials(org: Organization) -> tuple[str, str]:
    """Bootstrap: env vars may override placeholder rows from migration."""
    s = get_settings()
    key = org.ghl_api_key
    loc = org.ghl_location_id
    if (not key or key.startswith("placeholder")) and s.ghl_api_key:
        key = s.ghl_api_key
    if (not loc or loc.startswith("placeholder")) and s.ghl_location_id:
        loc = s.ghl_location_id
    return key, loc


def get_ghl_client_for_org(org: Organization, *, use_cache: bool = True) -> GHLClient:
    """Return a GHL client for this org's sub-account (cached by organization id)."""
    key, loc = effective_ghl_credentials(org)
    if not key or not loc:
        raise ValueError(f"Organization {org.id} missing GHL credentials")
    if use_cache and org.id in _cache:
        return _cache[org.id]
    client = GHLClient(api_key=key, default_location_id=loc)
    if use_cache:
        _cache[org.id] = client
    return client


def clear_ghl_client_cache(organization_id: uuid.UUID | None = None) -> None:
    """Call after updating org GHL credentials in admin."""
    global _cache
    if organization_id is None:
        _cache = {}
    else:
        _cache.pop(organization_id, None)


async def get_ghl_client(
    db: AsyncSession,
    organization_id: uuid.UUID,
) -> GHLClient:
    org = await db.get(Organization, organization_id)
    if not org:
        raise ValueError("organization not found")
    if org.status != "active":
        raise ValueError("organization suspended")
    return get_ghl_client_for_org(org)


async def get_organization_by_slug(db: AsyncSession, slug: str) -> Organization | None:
    r = await db.execute(select(Organization).where(Organization.slug == slug))
    return r.scalar_one_or_none()
