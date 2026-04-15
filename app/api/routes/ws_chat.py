"""WebSocket — `/ws/chat/{org_slug}/...` (multi-org revision).

Supports connection recovery: client can send a `resume` message with
a previous conversation_id to continue an existing conversation.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from openai import APIError, PermissionDeniedError
from sqlalchemy import select

from app.database.session import async_session_factory
from app.models.contact import Contact
from app.models.conversation import Conversation
from app.services.conversation_service import ConversationService
from app.services.location_resolve import resolve_org_and_location_for_public
from app.webhooks.dispatcher import WebhookDispatcher

logger = logging.getLogger(__name__)

router = APIRouter()


async def _run_ws_session(
    websocket: WebSocket,
    org_slug: str,
    location_slug: str | None,
) -> None:
    await websocket.accept()
    conv_id: uuid.UUID
    try:
        async with async_session_factory() as db:
            org, loc, err = await resolve_org_and_location_for_public(
                db, org_slug=org_slug, location_slug=location_slug
            )
            if err or not org or not loc:
                await websocket.send_json({"type": "error", "error": err or "invalid session"})
                await websocket.close()
                return

            first_raw = await websocket.receive_text()
            try:
                first_data = json.loads(first_raw)
            except json.JSONDecodeError:
                first_data = {"text": first_raw}

            resume_id = first_data.get("resume_conversation_id")
            first_text = first_data.get("message") or first_data.get("text") or ""
            resumed = False

            if resume_id:
                try:
                    existing = await db.get(Conversation, uuid.UUID(str(resume_id)))
                    if (
                        existing
                        and existing.organization_id == org.id
                        and existing.status == "active"
                    ):
                        conv_id = existing.id
                        resumed = True
                        logger.info("WebSocket resumed conversation %s", conv_id)
                except Exception:
                    pass

            if not resumed:
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
                await db.commit()
                conv_id = conv.id

                dispatcher = WebhookDispatcher()
                await dispatcher.emit(
                    "conversation.started",
                    {
                        "conversation_id": str(conv.id),
                        "organization_id": str(org.id),
                        "location_id": loc.id,
                        "channel": "webchat",
                    },
                )

            await websocket.send_json(
                {
                    "type": "ready",
                    "conversation_id": str(conv_id),
                    "organization_id": str(org.id),
                    "organization_slug": org.slug,
                    "location_id": loc.id,
                    "resumed": resumed,
                }
            )
    except WebSocketDisconnect:
        logger.info("WebSocket disconnected during handshake (org=%s)", org_slug)
        return
    except Exception:
        logger.exception("WebSocket handshake failed (org=%s)", org_slug)
        try:
            await websocket.send_json({"type": "error", "error": "Failed to start session. Please try again."})
            await websocket.close()
        except Exception:
            pass
        return

    # Process real messages (including first_text if it wasn't a resume-only message)
    pending_text = first_text if first_text else None

    try:
        while True:
            if pending_text:
                text = pending_text
                pending_text = None
            else:
                raw = await websocket.receive_text()
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    data = {"text": raw}
                text = data.get("message") or data.get("text") or ""
                if not text:
                    continue

            await websocket.send_json({"type": "typing", "value": True})
            try:
                async with async_session_factory() as db:
                    svc = ConversationService(db)
                    reply, responded = await svc.process_user_message(
                        conversation_id=conv_id,
                        user_text=text,
                        channel="webchat",
                    )
                    await db.commit()
            except PermissionDeniedError as e:
                logger.warning("OpenAI permission denied: %s", e)
                reply = (
                    "The AI model configured for Sarah is not available on this OpenAI API key/project. "
                    "In backend `.env`, set `OPENAI_MODEL` to a model your account can use "
                    "(for example `gpt-4o-mini` or `gpt-4-turbo`)."
                )
                responded = False
            except APIError as e:
                logger.warning("OpenAI API error: %s", e)
                reply = "Sorry, the AI service returned an error. Please try again in a moment."
                responded = False
            except Exception:
                logger.exception("Webchat turn failed conv=%s", conv_id)
                reply = "Sorry, something went wrong processing your message."
                responded = False
            await websocket.send_json({"type": "typing", "value": False})
            await websocket.send_json(
                {
                    "type": "message",
                    "role": "assistant",
                    "content": reply,
                    "responded": responded,
                }
            )
    except WebSocketDisconnect:
        logger.info("WebSocket disconnected %s", conv_id)
        # Section 4.12 — webchat-to-SMS continuity
        try:
            async with async_session_factory() as db:
                conv = await db.get(Conversation, conv_id)
                if conv and conv.mode == "ai" and conv.status == "active":
                    contact = await db.get(Contact, conv.contact_id)
                    if contact and contact.phone:
                        conv.channel = "sms"
                        await db.commit()
                        logger.info(
                            "Webchat conv %s marked for SMS fallback (contact phone: %s)",
                            conv_id,
                            contact.phone[:6] + "...",
                        )
        except Exception:
            logger.debug("SMS fallback check failed for conv %s", conv_id, exc_info=True)


@router.websocket("/ws/chat/{org_slug}/{location_slug}")
async def ws_chat_with_location(
    websocket: WebSocket,
    org_slug: str,
    location_slug: str,
) -> None:
    await _run_ws_session(websocket, org_slug, location_slug)


@router.websocket("/ws/chat/{org_slug}")
async def ws_chat_org_only(websocket: WebSocket, org_slug: str) -> None:
    """Single-location organizations: omit location segment; 403 if multi-location."""
    await _run_ws_session(websocket, org_slug, None)
