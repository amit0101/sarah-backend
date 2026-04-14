"""Tag application and removal — GHL API V2."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from app.ghl_client.client import GHLClient


async def add_tags(
    client: GHLClient,
    contact_id: str,
    *,
    location_id: str,
    tags: List[str],
) -> Dict[str, Any]:
    """POST /contacts/{id}/tags — body lists tag names."""
    return await client.request(
        "POST",
        f"/contacts/{contact_id}/tags",
        location_id=location_id,
        json_body={"tags": tags},
    )


async def remove_tags(
    client: GHLClient,
    contact_id: str,
    *,
    location_id: str,
    tag_ids: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """DELETE /contacts/{id}/tags — GHL may expect tag ids in body or query."""
    return await client.request(
        "DELETE",
        f"/contacts/{contact_id}/tags",
        location_id=location_id,
        json_body={"tags": tag_ids} if tag_ids else None,
    )
