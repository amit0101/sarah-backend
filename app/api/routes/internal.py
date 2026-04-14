"""Internal API for Comms Platform — Section 9.4."""

from __future__ import annotations

import uuid
from typing import Any, Optional

from fastapi import APIRouter, Query
from sqlalchemy import select

from app.database.session import DbSession
from app.models.contact import Contact
from app.models.conversation import Conversation
from app.models.message import Message

router = APIRouter(prefix="/api", tags=["internal"])


@router.get("/conversations/{conversation_id}")
async def get_conversation(conversation_id: uuid.UUID, db: DbSession) -> dict[str, Any]:
    conv = await db.get(Conversation, conversation_id)
    if not conv:
        return {"error": "not_found"}
    return {
        "id": str(conv.id),
        "organization_id": str(conv.organization_id),
        "contact_id": str(conv.contact_id),
        "location_id": conv.location_id,
        "channel": conv.channel,
        "mode": conv.mode,
        "status": conv.status,
        "openai_response_id": conv.openai_response_id,
        "active_path": conv.active_path,
    }


@router.get("/conversations/{conversation_id}/messages")
async def get_conversation_messages(conversation_id: uuid.UUID, db: DbSession) -> dict[str, Any]:
    r = await db.execute(
        select(Message)
        .where(Message.conversation_id == conversation_id)
        .order_by(Message.created_at)
    )
    rows = r.scalars().all()
    return {
        "conversation_id": str(conversation_id),
        "messages": [
            {
                "id": str(m.id),
                "role": m.role,
                "content": m.content,
                "channel": m.channel,
                "created_at": m.created_at.isoformat(),
            }
            for m in rows
        ],
    }


@router.get("/contacts/{ghl_contact_id}/conversations")
async def list_contact_conversations(
    ghl_contact_id: str,
    db: DbSession,
    organization_id: Optional[uuid.UUID] = Query(
        None,
        description="Required when the same GHL id could exist in multiple orgs",
    ),
) -> dict[str, Any]:
    q = select(Contact).where(Contact.ghl_contact_id == ghl_contact_id)
    if organization_id:
        q = q.where(Contact.organization_id == organization_id)
    r = await db.execute(q)
    contacts = r.scalars().all()
    if not contacts:
        return {"conversations": []}
    if len(contacts) > 1 and not organization_id:
        return {
            "error": "ambiguous_contact",
            "message": "Pass organization_id to disambiguate",
        }
    c = contacts[0]
    r2 = await db.execute(select(Conversation).where(Conversation.contact_id == c.id))
    convs = r2.scalars().all()
    return {
        "organization_id": str(c.organization_id),
        "conversations": [{"id": str(x.id), "status": x.status} for x in convs],
    }
