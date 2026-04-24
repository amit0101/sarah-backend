"""Contact CRUD and lookup for GHL API V2."""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from app.ghl_client.client import GHLAPIError, GHLClient

logger = logging.getLogger(__name__)


async def create_contact(
    client: GHLClient,
    *,
    location_id: str,
    name: Optional[str] = None,
    first_name: Optional[str] = None,
    last_name: Optional[str] = None,
    phone: Optional[str] = None,
    email: Optional[str] = None,
    tags: Optional[list[str]] = None,
    custom_fields: Optional[list[dict[str, Any]]] = None,
    source: Optional[str] = None,
    assigned_to: Optional[str] = None,
) -> Dict[str, Any]:
    """Create a GHL contact. `assigned_to` is a GHL user ID (e.g. from
    location.config['default_assigned_user_id']). Setting this ensures
    the Opportunity Stage-Based Notifications workflow can send 'assigned
    user' internal notifications — without it those steps are silently
    skipped for every Sarah-originated contact."""
    body: Dict[str, Any] = {"locationId": location_id}
    if name:
        body["name"] = name
    if first_name:
        body["firstName"] = first_name
    if last_name:
        body["lastName"] = last_name
    if phone:
        body["phone"] = phone
    if email:
        body["email"] = email
    if tags:
        body["tags"] = tags
    if custom_fields:
        body["customFields"] = custom_fields
    if source:
        body["source"] = source
    if assigned_to:
        body["assignedTo"] = assigned_to
    return await client.request("POST", "/contacts/", location_id=location_id, json_body=body)


async def update_contact(
    client: GHLClient,
    contact_id: str,
    *,
    location_id: str,
    **fields: Any,
) -> Dict[str, Any]:
    body = {k: v for k, v in fields.items() if v is not None}
    return await client.request(
        "PUT",
        f"/contacts/{contact_id}",
        location_id=location_id,
        json_body=body,
    )


async def get_contact(
    client: GHLClient,
    contact_id: str,
    *,
    location_id: str,
) -> Dict[str, Any]:
    return await client.request(
        "GET",
        f"/contacts/{contact_id}",
        location_id=location_id,
    )


async def lookup_contact(
    client: GHLClient,
    *,
    location_id: str,
    phone: Optional[str] = None,
    email: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    params: Dict[str, Any] = {"locationId": location_id}
    if phone:
        params["phone"] = phone
    if email:
        params["email"] = email
    try:
        data = await client.request(
            "GET",
            "/contacts/lookup",
            location_id=location_id,
            params=params,
        )
    except GHLAPIError as e:
        if e.status_code in (400, 404, 422):
            logger.debug("GHL lookup returned %s — treating as not found", e.status_code)
            return None
        raise
    if not data:
        return None
    contact = data.get("contact") if isinstance(data, dict) else None
    return contact if contact else data


async def add_contact_note(
    client: GHLClient,
    contact_id: str,
    *,
    location_id: str,
    body: str,
) -> Dict[str, Any]:
    payload = {"body": body}
    return await client.request(
        "POST",
        f"/contacts/{contact_id}/notes",
        location_id=location_id,
        json_body=payload,
    )
