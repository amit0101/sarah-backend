"""Create/update contacts in GHL and sarah.contacts (per organization)."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.contact_manager.validation import normalize_phone_ca_us, validate_email_addr
from app.ghl_client import GHLClient
from app.ghl_client import contacts as ghl_contacts
from app.models.contact import Contact
from app.models.location import Location
from app.models.organization import Organization

logger = logging.getLogger(__name__)


class ContactService:
    def __init__(self, db: AsyncSession, ghl: GHLClient) -> None:
        self._db = db
        self._ghl = ghl

    async def resolve_ghl_scope_location_id(self, location: Location) -> str:
        """GHL Location-Id header: prefer per-location override, else org default."""
        if location.ghl_location_id:
            return location.ghl_location_id
        org = await self._db.get(Organization, location.organization_id)
        if org and org.ghl_location_id:
            return org.ghl_location_id
        from app.config import get_settings

        return get_settings().ghl_location_id

    async def find_or_create(
        self,
        *,
        location: Location,
        phone: Optional[str] = None,
        email: Optional[str] = None,
        name: Optional[str] = None,
        first_name: Optional[str] = None,
        last_name: Optional[str] = None,
        source_channel: str = "webchat",
        conversation_id: Optional[str] = None,
    ) -> tuple[Contact, str]:
        """Returns (Contact, ghl_contact_id)."""
        ghl_loc = await self.resolve_ghl_scope_location_id(location)
        phone_ok, phone_e164 = normalize_phone_ca_us(phone) if phone else (False, None)
        email_ok, email_norm = validate_email_addr(email) if email else (False, None)

        # GHL custom fields required for workflow isolation gates.
        # source_channel → Appointment Confirmation/Reminder isolation
        # conversation_mode → Inbound Router isolation
        # sarah_location_id → location-based routing
        # sarah_last_conversation_id → conversation traceability
        custom_fields = [
            {"id": "rOnwZB1ZYly10bWBo8XJ", "field_value": source_channel},   # source_channel
            {"id": "MCbDtnYunljo58wxLxPt", "field_value": "ai"},              # conversation_mode
            {"id": "GIKbV4hAdLucXHdpqIAt", "field_value": location.id},       # sarah_location_id
        ]
        if conversation_id:
            custom_fields.append(
                {"id": "cffxjpazXikidAt7qh9E", "field_value": conversation_id}  # sarah_last_conversation_id
            )

        existing_ghl: Optional[Dict[str, Any]] = None
        if phone_e164:
            existing_ghl = await ghl_contacts.lookup_contact(
                self._ghl, location_id=ghl_loc, phone=phone_e164
            )
        if not existing_ghl and email_norm:
            existing_ghl = await ghl_contacts.lookup_contact(
                self._ghl, location_id=ghl_loc, email=email_norm
            )

        ghl_id: Optional[str] = None
        if existing_ghl:
            ghl_id = str(existing_ghl.get("id") or existing_ghl.get("contactId") or "")
            if ghl_id:
                await ghl_contacts.update_contact(
                    self._ghl,
                    ghl_id,
                    location_id=ghl_loc,
                    name=name or existing_ghl.get("name"),
                    phone=phone_e164 or existing_ghl.get("phone"),
                    email=email_norm or existing_ghl.get("email"),
                    customFields=custom_fields,
                )
        else:
            tags: list[str] = []
            body = await ghl_contacts.create_contact(
                self._ghl,
                location_id=ghl_loc,
                name=name,
                first_name=first_name,
                last_name=last_name,
                phone=phone_e164,
                email=email_norm,
                tags=tags,
                custom_fields=custom_fields,
                source=source_channel,
            )
            ghl_id = str(body.get("contact", {}).get("id") or body.get("id") or "")

        if not ghl_id:
            raise RuntimeError("GHL did not return contact id")

        r = await self._db.execute(
            select(Contact).where(
                Contact.organization_id == location.organization_id,
                Contact.ghl_contact_id == ghl_id,
            )
        )
        row = r.scalar_one_or_none()
        if not row:
            row = Contact(
                id=uuid.uuid4(),
                organization_id=location.organization_id,
                ghl_contact_id=ghl_id,
                name=name,
                phone=phone_e164,
                email=email_norm,
                location_id=location.id,
                last_seen=datetime.now(timezone.utc),
            )
            self._db.add(row)
        else:
            row.name = name or row.name
            row.phone = phone_e164 or row.phone
            row.email = email_norm or row.email
            row.location_id = location.id
            row.last_seen = datetime.now(timezone.utc)
        await self._db.flush()
        return row, ghl_id
