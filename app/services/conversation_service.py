"""Orchestrate turns: guardrails, path, engine, persistence, webhooks."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Optional, Tuple

from sqlalchemy.ext.asyncio import AsyncSession

from app.conversation_engine.engine import ConversationEngine
from app.conversation_engine.guardrails import evaluate_guardrails
from app.conversation_engine.path_router import classify_path
from app.ghl_client.factory import get_ghl_client_for_org
from app.models.contact import Contact
from app.models.conversation import Conversation
from app.models.location import Location
from app.models.message import Message
from app.models.organization import Organization
from app.notifications.service import NotificationService
from app.obituary_client.client import TributeCenterClient
from app.calendar_client.google_adapter import GoogleCalendarAdapter
from app.services.sarah_tools import ToolContext
from app.webhooks.dispatcher import WebhookDispatcher

logger = logging.getLogger(__name__)


class ConversationService:
    def __init__(self, db: AsyncSession) -> None:
        self._db = db
        self._dispatcher = WebhookDispatcher()
        self._calendar = GoogleCalendarAdapter()
        self._obits = TributeCenterClient()
        self._notify = NotificationService()

    async def process_user_message(
        self,
        *,
        conversation_id: uuid.UUID,
        user_text: str,
        channel: str,
    ) -> Tuple[str, bool]:
        conv = await self._db.get(Conversation, conversation_id)
        if not conv:
            raise ValueError("conversation not found")
        if conv.mode == "staff":
            await self._log_message(conv.id, "user", user_text, channel)
            return "", False

        loc = await self._db.get(Location, (conv.organization_id, conv.location_id))
        if not loc:
            raise ValueError("location not found")

        org = await self._db.get(Organization, conv.organization_id)
        if not org:
            raise ValueError("organization not found")
        if org.status != "active":
            raise ValueError("organization suspended")

        contact = await self._db.get(Contact, conv.contact_id)
        if not contact:
            contact = Contact(
                id=uuid.uuid4(),
                organization_id=org.id,
                location_id=loc.id,
                conversation_mode="ai",
            )
            self._db.add(contact)
            await self._db.flush()
            conv.contact_id = contact.id

        # Section 4.7 — minimal pre-filter for extreme cases only
        gr = evaluate_guardrails(user_text)
        if gr.blocked and gr.reply:
            await self._log_message(conv.id, "user", user_text, channel)
            await self._log_message(conv.id, "assistant", gr.reply, channel)
            await self._dispatcher.emit(
                "message.sent",
                {
                    "conversation_id": str(conv.id),
                    "organization_id": str(org.id),
                    "message": {"content": gr.reply, "role": "assistant", "channel": channel},
                    "location_id": loc.id,
                    "contact": {"ghl_contact_id": contact.ghl_contact_id, "name": contact.name, "phone": contact.phone, "email": contact.email},
                },
            )
            return gr.reply, True

        # Section 4.2 — AI-powered path classification on first message only;
        # subsequent path changes handled by switch_conversation_path tool
        path = await classify_path(user_text, conv.active_path)
        conv.active_path = path

        await self._log_message(conv.id, "user", user_text, channel)
        await self._dispatcher.emit(
            "message.received",
            {
                "conversation_id": str(conv.id),
                "organization_id": str(org.id),
                "message": {"content": user_text, "role": "user", "channel": channel},
                "location_id": loc.id,
                "contact": {"ghl_contact_id": contact.ghl_contact_id, "name": contact.name, "phone": contact.phone, "email": contact.email},
            },
        )

        turn_id = uuid.uuid4()
        # Session 21 — we already loaded `org` above; skip the redundant
        # Organization fetch inside get_ghl_client() and use the cached sync path.
        ghl = get_ghl_client_for_org(org)
        ctx = ToolContext(
            db=self._db,
            ghl=ghl,
            organization=org,
            location=loc,
            conversation=conv,
            contact=contact,
            dispatcher=self._dispatcher,
            calendar=self._calendar,
            obituaries=self._obits,
            notifications=self._notify,
            turn_id=turn_id,
        )
        engine = ConversationEngine(self._db, ctx)
        reply, new_rid = await engine.run_turn(
            user_text=user_text,
            previous_response_id=conv.openai_response_id,
            path=path,
        )
        conv.openai_response_id = new_rid
        conv.last_message_at = datetime.now(timezone.utc)

        await self._log_message(conv.id, "assistant", reply, channel)
        await self._dispatcher.emit(
            "message.sent",
            {
                "conversation_id": str(conv.id),
                "organization_id": str(org.id),
                "message": {"content": reply, "role": "assistant", "channel": channel},
                "location_id": loc.id,
                "contact": {"ghl_contact_id": contact.ghl_contact_id, "name": contact.name, "phone": contact.phone, "email": contact.email},
            },
        )
        return reply, True

    async def _log_message(
        self,
        conversation_id: uuid.UUID,
        role: str,
        content: str,
        channel: str,
    ) -> None:
        self._db.add(
            Message(
                id=uuid.uuid4(),
                conversation_id=conversation_id,
                role=role,
                content=content,
                channel=channel,
            )
        )

    async def close_conversation(self, conversation_id: uuid.UUID) -> bool:
        """Mark a conversation as closed and emit webhook event."""
        conv = await self._db.get(Conversation, conversation_id)
        if not conv or conv.status == "closed":
            return False
        conv.status = "closed"
        await self._dispatcher.emit(
            "conversation.closed",
            {
                "conversation_id": str(conv.id),
                "organization_id": str(conv.organization_id),
                "location_id": conv.location_id,
                "channel": conv.channel,
                "mode": conv.mode,
            },
        )
        return True
