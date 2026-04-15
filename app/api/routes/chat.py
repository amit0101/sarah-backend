"""Webchat REST — Section 9.1 (org + location scoped)."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException
from sqlalchemy import select

from app.api.schemas import ChatMessageIn, ChatMessageOut, MessageRow
from app.database.session import DbSession
from app.models.contact import Contact
from app.models.conversation import Conversation
from app.models.message import Message
from app.models.organization import Organization
from app.services.conversation_service import ConversationService
from app.services.location_resolve import get_location_by_org_slug

router = APIRouter(prefix="/api/chat", tags=["chat"])


@router.post("/message", response_model=ChatMessageOut)
async def post_message(body: ChatMessageIn, db: DbSession) -> ChatMessageOut:
    svc = ConversationService(db)
    if body.conversation_id:
        conv = await db.get(Conversation, body.conversation_id)
        if not conv:
            raise HTTPException(status_code=404, detail="conversation not found")
        org = await db.get(Organization, conv.organization_id)
        if not org or org.slug != body.organization_slug:
            raise HTTPException(status_code=403, detail="organization mismatch")
        if conv.location_id != body.location_id:
            raise HTTPException(status_code=403, detail="location mismatch for conversation")
        conv_id = body.conversation_id
    else:
        org, loc = await get_location_by_org_slug(db, body.organization_slug, body.location_id)
        if not org or not loc:
            raise HTTPException(status_code=404, detail="unknown organization or location")
        contact = Contact(
            id=uuid.uuid4(),
            organization_id=org.id,
            location_id=loc.id,
            conversation_mode="ai",
        )
        db.add(contact)
        await db.flush()
        conv = Conversation(
            id=uuid.uuid4(),
            organization_id=org.id,
            contact_id=contact.id,
            location_id=loc.id,
            channel="webchat",
            mode="ai",
            status="active",
            started_at=datetime.now(timezone.utc),
        )
        db.add(conv)
        await db.flush()
        conv_id = conv.id
    try:
        reply, responded = await svc.process_user_message(
            conversation_id=conv_id,
            user_text=body.message,
            channel="webchat",
        )
        await db.commit()
    except Exception:
        logging.getLogger(__name__).exception("REST chat turn failed conv=%s", conv_id)
        raise HTTPException(
            status_code=500,
            detail="Sorry, something went wrong processing your message.",
        )
    return ChatMessageOut(conversation_id=conv_id, reply=reply, responded=responded)


@router.get("/history/{conversation_id}")
async def get_history(conversation_id: uuid.UUID, db: DbSession) -> dict[str, Any]:
    r = await db.execute(
        select(Message)
        .where(Message.conversation_id == conversation_id)
        .order_by(Message.created_at)
    )
    rows = r.scalars().all()
    return {
        "conversation_id": str(conversation_id),
        "messages": [MessageRow.model_validate(m).model_dump() for m in rows],
    }
