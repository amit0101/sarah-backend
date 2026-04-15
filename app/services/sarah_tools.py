"""Execute Sarah function tools (GHL, calendar, obituary, escalation)."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Dict

from sqlalchemy.ext.asyncio import AsyncSession

from app.contact_manager.service import ContactService
from app.escalation.router import EscalationRouter
from app.ghl_client import GHLClient
from app.ghl_client import calendars as ghl_cal
from app.ghl_client import contacts as ghl_contacts
from app.ghl_client import pipelines as ghl_pipes
from app.ghl_client import tags as ghl_tags
from app.models.contact import Contact
from app.models.conversation import Conversation
from app.models.location import Location
from app.models.organization import Organization
from app.notifications.service import NotificationService
from app.obituary_client.client import TributeCenterClient
from app.calendar_client.google_adapter import GoogleCalendarAdapter
from app.services.location_config import get_pipeline_map, get_tag_map, resolve_tag_key
from app.services.postal_code import resolve_area as _resolve_area
from app.services.postal_code import resolve_postal_code as _resolve_postal_code
from app.webhooks.dispatcher import WebhookDispatcher

logger = logging.getLogger(__name__)


@dataclass
class ToolContext:
    db: AsyncSession
    ghl: GHLClient
    organization: Organization
    location: Location
    conversation: Conversation
    contact: Contact
    dispatcher: WebhookDispatcher
    calendar: GoogleCalendarAdapter
    obituaries: TributeCenterClient
    notifications: NotificationService


class SarahToolRunner:
    async def run(self, name: str, arguments_json: str, ctx: ToolContext) -> str:
        try:
            args = json.loads(arguments_json) if arguments_json else {}
        except json.JSONDecodeError:
            args = {}
        if name == "create_contact":
            return await self._create_contact(ctx, args)
        if name == "apply_tag":
            return await self._apply_tag(ctx, args)
        if name == "move_pipeline":
            return await self._move_pipeline(ctx, args)
        if name == "check_calendar":
            return await self._check_calendar(ctx, args)
        if name == "book_appointment":
            return await self._book_appointment(ctx, args)
        if name == "search_obituary":
            return await self._search_obituary(ctx, args)
        if name == "escalate_to_staff":
            return await self._escalate(ctx, args)
        if name == "resolve_postal_code":
            return await self._resolve_postal_code(ctx, args)
        if name == "resolve_area":
            return await self._resolve_area(ctx, args)
        if name == "switch_conversation_path":
            return await self._switch_path(ctx, args)
        return json.dumps({"ok": False, "error": f"unknown tool {name}"})

    def _ghl_scope(self, ctx: ToolContext) -> str:
        return ctx.location.ghl_location_id or ctx.organization.ghl_location_id

    def _opt_str(self, v: Any) -> Any:
        if v is None:
            return None
        s = str(v).strip()
        return s or None

    async def _create_contact(self, ctx: ToolContext, args: Dict[str, Any]) -> str:
        svc = ContactService(ctx.db, ctx.ghl)
        fn = self._opt_str(args.get("first_name"))
        ln = self._opt_str(args.get("last_name"))
        phone = self._opt_str(args.get("phone"))
        email = self._opt_str(args.get("email"))
        name = " ".join(x for x in (fn, ln) if x).strip() or None
        contact, ghl_id = await svc.find_or_create(
            location=ctx.location,
            phone=phone,
            email=email,
            name=name,
            first_name=fn,
            last_name=ln,
            source_channel=ctx.conversation.channel,
        )
        ctx.conversation.contact_id = contact.id

        # Section 5.2 — auto-apply entry tags and location tag after contact creation
        if ghl_id:
            ghl_loc = self._ghl_scope(ctx)
            entry_tag = "webchat_lead" if ctx.conversation.channel == "webchat" else "sms_lead"
            location_tag = f"location_{ctx.location.id}"
            tags_to_apply = [entry_tag, location_tag, "sarah_handled"]
            try:
                await ghl_tags.add_tags(
                    ctx.ghl, ghl_id, location_id=ghl_loc, tags=tags_to_apply,
                )
                logger.info(
                    "Auto-applied tags %s to contact %s", tags_to_apply, ghl_id,
                )
            except Exception as e:
                logger.warning("Failed to auto-apply entry tags: %s", e)

            # GHL_INTEGRATION_DOC Section 3 — create pipeline opportunity
            # Map conversation path to pipeline key
            path = ctx.conversation.active_path or "general"
            pipeline_key = "at_need" if path == "immediate_need" else "pre_need"
            pmap = get_pipeline_map(ctx.location.config)
            pipe_cfg = pmap.get(pipeline_key) or {}
            pipeline_id = pipe_cfg.get("pipeline_id")
            stage_id = (pipe_cfg.get("stages") or {}).get("new_lead") or (pipe_cfg.get("stages") or {}).get("new")
            if pipeline_id and stage_id:
                try:
                    await ghl_pipes.create_opportunity(
                        ctx.ghl,
                        location_id=ghl_loc,
                        contact_id=ghl_id,
                        pipeline_id=pipeline_id,
                        pipeline_stage_id=stage_id,
                    )
                    logger.info(
                        "Created %s pipeline opportunity for contact %s",
                        pipeline_key,
                        ghl_id,
                    )
                except Exception as e:
                    logger.warning("Failed to create pipeline opportunity: %s", e)

        await ctx.dispatcher.emit(
            "contact.created",
            {
                "conversation_id": str(ctx.conversation.id),
                "organization_id": str(ctx.organization.id),
                "contact": {"ghl_contact_id": ghl_id, "name": contact.name, "phone": contact.phone},
                "location_id": ctx.location.id,
            },
        )
        return json.dumps({"ok": True, "ghl_contact_id": ghl_id})

    async def _apply_tag(self, ctx: ToolContext, args: Dict[str, Any]) -> str:
        key = str(args.get("tag_key", ""))
        cfg = ctx.location.config or {}
        tag_name = resolve_tag_key(cfg, key) or get_tag_map(cfg).get(key) or key
        ghl_loc = self._ghl_scope(ctx)
        cid = ctx.contact.ghl_contact_id
        if not cid:
            return json.dumps({"ok": False, "error": "no ghl contact"})
        await ghl_tags.add_tags(ctx.ghl, cid, location_id=ghl_loc, tags=[tag_name])
        await ctx.dispatcher.emit(
            "tag.applied",
            {
                "conversation_id": str(ctx.conversation.id),
                "organization_id": str(ctx.organization.id),
                "contact": {"ghl_contact_id": cid},
                "tag": tag_name,
                "location_id": ctx.location.id,
            },
        )
        return json.dumps({"ok": True, "tag": tag_name})

    async def _move_pipeline(self, ctx: ToolContext, args: Dict[str, Any]) -> str:
        # Section 5.3 — pipeline stage management via config-driven mappings
        pmap = get_pipeline_map(ctx.location.config)
        pk = str(args.get("pipeline_key", ""))
        sk = str(args.get("stage_key", ""))
        pipe = pmap.get(pk) or {}
        pipeline_id = pipe.get("pipeline_id")
        stage_id = (pipe.get("stages") or {}).get(sk)
        ghl_loc = self._ghl_scope(ctx)
        cid = ctx.contact.ghl_contact_id
        if not pipeline_id or not stage_id or not cid:
            return json.dumps({"ok": False, "error": "missing pipeline config or contact"})
        raw = await ghl_contacts.get_contact(ctx.ghl, cid, location_id=ghl_loc)
        contact_payload = raw.get("contact", raw)
        opps = contact_payload.get("opportunities") or []
        for o in opps:
            pid = str(o.get("pipelineId") or "")
            if pid == pipeline_id:
                await ghl_pipes.update_opportunity(
                    ctx.ghl,
                    str(o["id"]),
                    location_id=ghl_loc,
                    pipeline_stage_id=stage_id,
                )
                await ctx.dispatcher.emit(
                    "pipeline.updated",
                    {
                        "conversation_id": str(ctx.conversation.id),
                        "organization_id": str(ctx.organization.id),
                        "contact": {"ghl_contact_id": cid},
                        "pipeline_key": pk,
                        "stage_key": sk,
                        "opportunity_id": o["id"],
                        "location_id": ctx.location.id,
                    },
                )
                return json.dumps({"ok": True, "opportunity_id": o["id"]})
        created = await ghl_pipes.create_opportunity(
            ctx.ghl,
            location_id=ghl_loc,
            contact_id=cid,
            pipeline_id=pipeline_id,
            pipeline_stage_id=stage_id,
        )
        await ctx.dispatcher.emit(
            "pipeline.updated",
            {
                "conversation_id": str(ctx.conversation.id),
                "organization_id": str(ctx.organization.id),
                "contact": {"ghl_contact_id": cid},
                "pipeline_key": pk,
                "stage_key": sk,
                "created": True,
                "location_id": ctx.location.id,
            },
        )
        return json.dumps({"ok": True, "created": created})

    async def _check_calendar(self, ctx: ToolContext, args: Dict[str, Any]) -> str:
        start = str(args.get("start_iso", ""))
        end = str(args.get("end_iso", ""))
        tz = str(args.get("timezone", "America/Edmonton"))
        cal_id = ctx.location.availability_calendar_id or ctx.location.calendar_id
        if not cal_id:
            return json.dumps({"ok": False, "error": "no calendar configured"})
        busy = await ctx.calendar.free_busy(
            cal_id,
            time_min_iso=start,
            time_max_iso=end,
            timezone=tz,
        )
        return json.dumps({"ok": True, "busy": busy})

    async def _book_appointment(self, ctx: ToolContext, args: Dict[str, Any]) -> str:
        start = str(args.get("start_iso", ""))
        end = str(args.get("end_iso", ""))
        title = str(self._opt_str(args.get("title")) or "Appointment")
        notes = self._opt_str(args.get("notes"))
        cal_id = ctx.location.calendar_id
        if not cal_id:
            return json.dumps({"ok": False, "error": "no calendar_id"})
        ev = await ctx.calendar.create_event(
            cal_id,
            start_iso=start,
            end_iso=end,
            summary=title,
            description=notes,
        )
        ghl_cal_id = ctx.location.ghl_calendar_id
        ghl_loc = self._ghl_scope(ctx)
        cid = ctx.contact.ghl_contact_id
        if ghl_cal_id and cid:
            try:
                await ghl_cal.create_appointment(
                    ctx.ghl,
                    ghl_cal_id,
                    location_id=ghl_loc,
                    contact_id=cid,
                    start_time=start,
                    end_time=end,
                    title=title,
                    notes=str(notes) if notes else None,
                )
            except Exception as e:
                logger.warning("GHL appointment sync failed: %s", e)
        await ctx.dispatcher.emit(
            "appointment.booked",
            {
                "conversation_id": str(ctx.conversation.id),
                "organization_id": str(ctx.organization.id),
                "contact": {"ghl_contact_id": cid},
                "calendar": cal_id,
                "start": start,
                "location_id": ctx.location.id,
            },
        )
        return json.dumps({"ok": True, "event": ev})

    async def _search_obituary(self, ctx: ToolContext, args: Dict[str, Any]) -> str:
        nm = self._opt_str(args.get("name"))
        dt = self._opt_str(args.get("date"))
        hint = self._opt_str(args.get("location_hint")) or ctx.location.name
        res = await ctx.obituaries.search(
            name=nm,
            date=dt,
            location_hint=hint,
        )
        return json.dumps({"ok": True, "results": res})

    async def _escalate(self, ctx: ToolContext, args: Dict[str, Any]) -> str:
        reason = str(args.get("reason", ""))
        urgency = str(args.get("urgency", "normal"))
        router = EscalationRouter()
        contacts = ctx.location.escalation_contacts
        result = router.route(
            contacts if isinstance(contacts, list) else None,
            urgency=urgency,
            location_config=ctx.location.config,
        )
        body = (
            f"Sarah escalation ({urgency}) at {ctx.location.name}: {reason}. "
            f"Conversation: {ctx.conversation.id}"
        )
        # Use the router's channel recommendation + structured payload
        notify_kwargs = dict(
            to_phone=result.phone if result.channel == "sms" else result.phone,
            to_email=result.email if result.channel == "email" else None,
            body=body,
            prefer_sms_if_business_hours=(result.channel == "sms"),
            contact_name=ctx.contact.name,
            contact_phone=ctx.contact.phone,
            location_name=ctx.location.name,
            conversation_id=str(ctx.conversation.id),
            reason=reason,
            urgency=urgency,
        )
        await ctx.notifications.notify_escalation(**notify_kwargs)
        ctx.conversation.mode = "staff"
        await ctx.dispatcher.emit(
            "escalation.triggered",
            {
                "conversation_id": str(ctx.conversation.id),
                "organization_id": str(ctx.organization.id),
                "reason": reason,
                "urgency": urgency,
                "location_id": ctx.location.id,
                "channel": result.channel,
                "contact": {"ghl_contact_id": ctx.contact.ghl_contact_id},
            },
        )
        return json.dumps({"ok": True, "escalated": True})

    async def _update_conversation_location(self, ctx: ToolContext, slug: str) -> None:
        """Reassign conversation + contact to a new location after postal code resolution."""
        from sqlalchemy import select
        loc = (
            await ctx.db.execute(
                select(Location).where(
                    Location.organization_id == ctx.organization.id,
                    Location.id == slug,
                )
            )
        ).scalar_one_or_none()
        if loc:
            ctx.conversation.location_id = loc.id
            ctx.contact.location_id = loc.id
            ctx.location = loc  # type: ignore[misc]
            logger.info(
                "Conversation %s location reassigned to %s",
                ctx.conversation.id,
                slug,
            )

    async def _resolve_postal_code(self, ctx: ToolContext, args: Dict[str, Any]) -> str:
        raw = str(args.get("postal_code", ""))
        result = _resolve_postal_code(raw)
        if result.get("ok"):
            await self._update_conversation_location(ctx, result["location_slug"])
        return json.dumps(result)

    async def _resolve_area(self, ctx: ToolContext, args: Dict[str, Any]) -> str:
        area = str(args.get("area", ""))
        result = _resolve_area(area)
        if result.get("ok"):
            await self._update_conversation_location(ctx, result["location_slug"])
        return json.dumps(result)

    async def _switch_path(self, ctx: ToolContext, args: Dict[str, Any]) -> str:
        """Section 2.5 / 4.2 — AI-driven topic switching.

        The model calls this tool when it detects the user has changed
        the topic of conversation to a different service area.
        """
        new_path = str(args.get("new_path", ""))
        reason = str(args.get("reason", ""))
        valid_paths = {"immediate_need", "pre_need", "obituary", "general", "pet_cremation"}
        if new_path not in valid_paths:
            return json.dumps({"ok": False, "error": f"invalid path: {new_path}"})
        old_path = ctx.conversation.active_path
        ctx.conversation.active_path = new_path
        logger.info(
            "Conversation %s path switched: %s → %s (reason: %s)",
            ctx.conversation.id,
            old_path,
            new_path,
            reason,
        )
        return json.dumps(
            {"ok": True, "previous_path": old_path, "new_path": new_path, "reason": reason}
        )
