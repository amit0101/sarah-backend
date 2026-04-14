"""Resolve Location + Organization; validate slug ownership."""

from __future__ import annotations

import uuid
from typing import Optional, Tuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.location import Location
from app.models.organization import Organization


async def get_location_by_org_slug(
    db: AsyncSession,
    org_slug: str,
    location_slug: str,
) -> Tuple[Optional[Organization], Optional[Location]]:
    r = await db.execute(select(Organization).where(Organization.slug == org_slug))
    org = r.scalar_one_or_none()
    if not org:
        return None, None
    r2 = await db.execute(
        select(Location).where(
            Location.organization_id == org.id,
            Location.id == location_slug,
        )
    )
    loc = r2.scalar_one_or_none()
    return org, loc


async def get_default_location_if_single(
    db: AsyncSession,
    org: Organization,
) -> Optional[Location]:
    r = await db.execute(select(Location).where(Location.organization_id == org.id))
    rows = list(r.scalars().all())
    if len(rows) != 1:
        return None
    return rows[0]


async def resolve_org_and_location_for_public(
    db: AsyncSession,
    *,
    org_slug: str,
    location_slug: Optional[str],
) -> Tuple[Optional[Organization], Optional[Location], Optional[str]]:
    """
    Returns (org, location, error_message).
    If location_slug is None and org has exactly one location, uses that location.
    """
    r = await db.execute(select(Organization).where(Organization.slug == org_slug))
    org = r.scalar_one_or_none()
    if not org:
        return None, None, "unknown organization"
    if org.status != "active":
        return None, None, "organization suspended"
    loc_slug = location_slug
    if not loc_slug:
        loc = await get_default_location_if_single(db, org)
        if not loc:
            return org, None, "location required for this organization"
        return org, loc, None
    r2 = await db.execute(
        select(Location).where(
            Location.organization_id == org.id,
            Location.id == loc_slug,
        )
    )
    loc = r2.scalar_one_or_none()
    if not loc:
        return org, None, "invalid location for organization"
    return org, loc, None
