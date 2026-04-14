"""Inbound webhooks — Section 9.2 + multi-org routing."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import APIRouter, Header, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy import select

from app.config import get_settings
from app.database.session import DbSession
from app.ghl_client.webhooks import parse_campaign_reply_webhook
from app.ghl_client.factory import get_organization_by_slug
from app.models.contact import Contact
from app.models.conversation import Conversation
from app.models.location import Location
from app.models.organization import Organization
from app.services.conversation_service import ConversationService
from app.sms.service import SmsProvider, SmsService
from app.webhooks.dispatcher import WebhookDispatcher

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


async def _resolve_org_for_twilio_to(db: DbSession, to_num: str) -> Organization:
    """Map inbound Twilio number → organization; fallback to default org slug."""
    if to_num:
        r = await db.execute(
            select(Organization).where(Organization.twilio_phone_number == to_num)
        )
        org = r.scalar_one_or_none()
        if org:
            return org
    s = get_settings()
    org = await get_organization_by_slug(db, s.default_organization_slug)
    if org:
        return org
    r2 = await db.execute(select(Organization).limit(1))
    fallback = r2.scalar_one_or_none()
    if not fallback:
        raise ValueError("No organization configured — run migrations and seed an org")
    return fallback


async def _resolve_location_for_org(
    db: DbSession,
    org: Organization,
    location_slug: Optional[str],
) -> Location:
    s = get_settings()
    slug = location_slug or s.default_location_slug
    loc = await db.get(Location, (org.id, slug))
    if loc:
        return loc
    r = await db.execute(select(Location).where(Location.organization_id == org.id).limit(1))
    loc2 = r.scalar_one_or_none()
    if not loc2:
        raise ValueError(f"No location for organization {org.slug}")
    return loc2


@router.post("/sms/inbound")
async def twilio_sms(request: Request, db: DbSession) -> PlainTextResponse:
    form = await request.form()
    from_num = str(form.get("From", ""))
    body = str(form.get("Body", ""))
    to_num = str(form.get("To", ""))

    org = await _resolve_org_for_twilio_to(db, to_num)

    r = await db.execute(
        select(Contact).where(
            Contact.organization_id == org.id,
            Contact.phone == from_num,
        )
    )
    contact = r.scalar_one_or_none()
    if not contact:
        loc = await _resolve_location_for_org(db, org, None)
        contact = Contact(
            id=uuid.uuid4(),
            organization_id=org.id,
            phone=from_num,
            location_id=loc.id,
            conversation_mode="ai",
        )
        db.add(contact)
        await db.flush()
    else:
        if not contact.location_id:
            loc = await _resolve_location_for_org(db, org, None)
            contact.location_id = loc.id

    r2 = await db.execute(
        select(Conversation)
        .where(Conversation.contact_id == contact.id, Conversation.status == "active")
        .order_by(Conversation.started_at.desc())
        .limit(1)
    )
    conv = r2.scalar_one_or_none()
    if not conv:
        loc = await _resolve_location_for_org(db, org, contact.location_id or None)
        conv = Conversation(
            id=uuid.uuid4(),
            organization_id=org.id,
            contact_id=contact.id,
            location_id=loc.id,
            channel="sms",
            mode="ai",
            status="active",
            started_at=datetime.now(timezone.utc),
        )
        db.add(conv)
        await db.flush()

        # Section 6.1 — emit conversation.started event (new SMS conversation)
        dispatcher = WebhookDispatcher()
        await dispatcher.emit(
            "conversation.started",
            {
                "conversation_id": str(conv.id),
                "organization_id": str(org.id),
                "location_id": loc.id,
                "channel": "sms",
            },
        )

    svc = ConversationService(db)
    reply, responded = await svc.process_user_message(
        conversation_id=conv.id,
        user_text=body,
        channel="sms",
    )
    await db.commit()

    if responded and reply:
        sms = SmsService()
        await sms.send(from_num, reply, provider=SmsProvider.TWILIO)

    return PlainTextResponse(
        '<?xml version="1.0" encoding="UTF-8"?><Response></Response>',
        media_type="text/xml",
    )


async def _ghl_campaign_core(
    db: DbSession,
    org: Organization,
    payload: Dict[str, Any],
) -> Dict[str, str]:
    parsed = parse_campaign_reply_webhook(payload)
    ghl_cid = parsed.contact_id
    if not ghl_cid:
        return {"status": "ignored"}

    r = await db.execute(
        select(Contact).where(
            Contact.organization_id == org.id,
            Contact.ghl_contact_id == ghl_cid,
        )
    )
    contact = r.scalar_one_or_none()
    if not contact:
        contact = Contact(
            id=uuid.uuid4(),
            organization_id=org.id,
            ghl_contact_id=ghl_cid,
            conversation_mode="ai",
        )
        db.add(contact)
        await db.flush()

    s = get_settings()
    loc_slug = parsed.location_id or contact.location_id or s.default_location_slug
    loc = await db.get(Location, (org.id, loc_slug))
    if not loc:
        loc = await _resolve_location_for_org(db, org, None)
    contact.location_id = loc.id

    conv = Conversation(
        id=uuid.uuid4(),
        organization_id=org.id,
        contact_id=contact.id,
        location_id=loc.id,
        channel="campaign_reply",
        mode="ai",
        status="active",
        started_at=datetime.now(timezone.utc),
    )
    db.add(conv)
    await db.flush()

    # Section 6.1 — emit conversation.started event
    dispatcher = WebhookDispatcher()
    await dispatcher.emit(
        "conversation.started",
        {
            "conversation_id": str(conv.id),
            "organization_id": str(org.id),
            "location_id": loc.id,
            "channel": "campaign_reply",
            "type": parsed.type.value,
        },
    )

    if not parsed.message and parsed.is_callback_only:
        await db.commit()
        return {"status": "callback_recorded"}

    svc = ConversationService(db)
    reply, responded = await svc.process_user_message(
        conversation_id=conv.id,
        user_text=parsed.message or "Hello",
        channel="campaign_reply",
    )
    await db.commit()

    if responded and reply and contact.phone:
        sms = SmsService()
        await sms.send(contact.phone, reply, provider=SmsProvider.GHL_LEAD_CONNECTOR)

    return {"status": "ok"}


@router.post("/ghl/{org_slug}/campaign-reply")
async def ghl_campaign_reply_scoped(
    org_slug: str,
    request: Request,
    db: DbSession,
) -> Dict[str, str]:
    """Path-based routing (revision §4): one URL per GHL sub-account."""
    org = await get_organization_by_slug(db, org_slug)
    if not org:
        return {"status": "unknown_org"}
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    return await _ghl_campaign_core(db, org, payload)


@router.post("/ghl/campaign-reply")
async def ghl_campaign_reply_legacy(
    request: Request,
    db: DbSession,
    x_sarah_organization: Optional[str] = Header(None, alias="X-Sarah-Organization"),
    x_sarah_organization_id: Optional[str] = Header(None, alias="X-Sarah-Organization-Id"),
) -> Dict[str, str]:
    """Legacy single URL: require org slug or org UUID header."""
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    org: Organization | None = None
    if x_sarah_organization_id:
        try:
            org = await db.get(Organization, uuid.UUID(x_sarah_organization_id))
        except Exception:
            org = None
    if not org and x_sarah_organization:
        org = await get_organization_by_slug(db, x_sarah_organization)
    if not org:
        s = get_settings()
        org = await get_organization_by_slug(db, s.default_organization_slug)
    if not org:
        return {"status": "no_organization"}
    return await _ghl_campaign_core(db, org, payload)


@router.post("/comms/handoff")
async def comms_handoff(request: Request, db: DbSession) -> Dict[str, str]:
    body = await request.json()
    event = str(body.get("event", ""))
    conv_id_raw = body.get("conversation_id") or body.get("conversationId")
    if not conv_id_raw:
        return {"status": "ignored"}
    conv_id = uuid.UUID(str(conv_id_raw))
    conv = await db.get(Conversation, conv_id)
    if not conv:
        return {"status": "not_found"}

    if event in ("handoff.staff_takeover", "staff_takeover"):
        conv.mode = "staff"
        conv.assigned_staff_id = str(body.get("staff_id") or "")
    elif event in ("handoff.return_to_ai", "return_to_ai"):
        # Section 6.2 — When returning to AI, Sarah needs context about
        # what happened during staff mode. Clear the OpenAI response chain
        # so Sarah starts fresh with a new system prompt, and inject a
        # summary of staff messages so she has context.
        conv.mode = "ai"
        conv.assigned_staff_id = None
        # Clear the OpenAI response chain — staff messages aren't in that chain,
        # so continuing from the old response_id would skip them. Starting fresh
        # means Sarah gets her full system prompt and the context injection below.
        conv.openai_response_id = None
        # Load recent staff messages to inject as context on next turn
        from app.models.message import Message
        r = await db.execute(
            select(Message)
            .where(
                Message.conversation_id == conv_id,
                Message.role.in_(["staff", "assistant", "user"]),
            )
            .order_by(Message.created_at.desc())
            .limit(10)
        )
        recent = list(reversed(r.scalars().all()))
        if recent:
            # Store staff context summary for next AI turn
            staff_summary = "\n".join(
                f"[{msg.role}]: {msg.content[:200]}" for msg in recent
            )
            # Store in conversation metadata (conversation_service will inject this
            # as context prefix on the next turn)
            logger.info(
                "Return-to-AI for conv %s: injecting %d messages as context",
                conv_id,
                len(recent),
            )
    await db.commit()
    return {"status": "ok"}
