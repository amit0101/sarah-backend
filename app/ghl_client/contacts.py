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
    try:
        return await client.request(
            "POST", "/contacts/", location_id=location_id, json_body=body
        )
    except GHLAPIError as e:
        # MHFH location (and any GHL location with "allow duplicate contacts"
        # disabled) rejects a create that collides on email/phone with
        # `400 "This location does not allow duplicated contacts"` and a
        # `meta.contactId` pointing at the existing record. Previously this
        # bubbled up through `find_or_create` and the outer `except` in
        # `sarah_tools._create_contact` swallowed it, leaving `ghl_id = None`
        # — so tags, custom fields, and the pipeline opportunity were all
        # silently skipped.
        #
        # With the duplicate detected here, bring the existing contact up to
        # date (custom fields, assignee, source, name) via `update_contact`
        # and return the shape the caller expects so `body.get("contact",
        # {}).get("id")` resolves to the real id. Tags are intentionally
        # excluded from the update payload to avoid clobbering pre-existing
        # tags — the caller re-applies Sarah's entry tags additively via
        # `ghl_tags.add_tags` immediately after.
        if e.status_code == 400 and isinstance(e.body, dict):
            meta = e.body.get("meta") or {}
            existing_id = meta.get("contactId")
            if existing_id:
                logger.info(
                    "GHL create_contact hit duplicate location=%s existing_id=%s "
                    "matching_field=%s — updating existing contact in place",
                    location_id,
                    existing_id,
                    meta.get("matchingField"),
                )
                update_body = {
                    k: v for k, v in body.items() if k not in ("locationId", "tags")
                }
                try:
                    await update_contact(
                        client,
                        existing_id,
                        location_id=location_id,
                        **update_body,
                    )
                except GHLAPIError as upd_err:
                    logger.error(
                        "GHL update_contact after duplicate-detect failed "
                        "existing_id=%s: %s",
                        existing_id,
                        upd_err,
                        exc_info=True,
                    )
                return {"contact": {"id": existing_id}}
        raise


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
    # GHL V2: lookup-by-attributes uses `/contacts/search/duplicate` with
    # `number` (not `phone`) and `email`. The earlier `/contacts/lookup` path
    # is interpreted by GHL as a contact-id route and returns 400. Verified
    # against the live API on 2026-05-04.
    params: Dict[str, Any] = {"locationId": location_id}
    if phone:
        params["number"] = phone
    if email:
        params["email"] = email
    try:
        data = await client.request(
            "GET",
            "/contacts/search/duplicate",
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
    # `/contacts/search/duplicate` always wraps the hit under `contact`
    # and returns `{"contact": null, "traceId": "..."}` on no match. The
    # whole response dict is truthy (it has `traceId`), so an earlier
    # `return contact if contact else data` fallback caused callers to
    # treat "no match" as "found an anonymous record", short-circuiting
    # the create path in `ContactService.find_or_create` for every brand-
    # new email (silent `RuntimeError("GHL did not return contact id")`,
    # caught by the outer except in `_create_contact` → `ok=true,
    # ghl_contact_id=null` → downstream tag/opportunity/booking calls
    # all no-op'd with "no ghl contact"). Always return `None` when the
    # wrapped contact is absent.
    if not isinstance(data, dict):
        return None
    contact = data.get("contact")
    return contact if contact else None


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
