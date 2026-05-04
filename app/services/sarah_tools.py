"""Execute Sarah function tools (GHL, calendar, obituary, escalation)."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Dict, Optional
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.contact_manager.service import ContactService
from app.contact_manager.validation import normalize_phone_ca_us
import asyncio as _asyncio
from app.escalation.router import EscalationRouter
from app.ghl_client import GHLClient
from app.ghl_client import calendars as ghl_cal
from app.ghl_client import contacts as ghl_contacts
from app.ghl_client import pipelines as ghl_pipes
from app.ghl_client import tags as ghl_tags
from app.models.appointment import Appointment
from app.models.calendar import Calendar
from app.models.contact import Contact
from app.models.conversation import Conversation
from app.models.location import Location
from app.models.message import Message
from app.models.organization import Organization
from app.sms.service import SmsProvider, SmsService
from app.notifications.service import NotificationService
from app.obituary_client.client import TributeCenterClient
from app.calendar_client.google_adapter import GoogleCalendarAdapter
from app.services import calendar_service as cal_svc
from app.services import ghl_push
from app.services.location_config import get_pipeline_map, get_tag_map, resolve_tag_key
from app.services.postal_code import resolve_area as _resolve_area
from app.services.postal_code import resolve_postal_code as _resolve_postal_code
from app.services.scheduling import build_availability_response, parse_counselor_from_event
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
    # One UUID per user message turn; used to correlate openai_response_logs rows.
    turn_id: Optional[uuid.UUID] = None


class SarahToolRunner:
    async def run(self, name: str, arguments_json: str, ctx: ToolContext) -> str:
        try:
            args = json.loads(arguments_json) if arguments_json else {}
        except json.JSONDecodeError:
            args = {}
        # Temporary: confirm which tools actually run (remove or downgrade when stable).
        _args_preview = (arguments_json or "")[:800]
        logger.info(
            "sarah_tool_invoked name=%s conversation_id=%s turn_id=%s args_preview=%r",
            name,
            ctx.conversation.id,
            getattr(ctx, "turn_id", None),
            _args_preview,
        )
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
        if name == "reschedule_appointment":
            return await self._reschedule_appointment(ctx, args)
        if name == "cancel_appointment":
            return await self._cancel_appointment(ctx, args)
        if name == "search_obituary":
            return await self._search_obituary(ctx, args)
        if name == "escalate_to_staff":
            return await self._escalate(ctx, args)
        # resolve_postal_code and resolve_area removed — location resolution
        # is now model-driven via location_slug enum on check_calendar.
        if name == "switch_conversation_path":
            return await self._switch_path(ctx, args)
        if name == "continue_on_sms":
            return await self._continue_on_sms(ctx, args)
        return json.dumps({"ok": False, "error": f"unknown tool {name}"})

    def _ghl_scope(self, ctx: ToolContext) -> str:
        return ctx.location.ghl_location_id or ctx.organization.ghl_location_id

    def _opt_str(self, v: Any) -> Any:
        if v is None:
            return None
        s = str(v).strip()
        # Models occasionally serialize a missing field as the literal string
        # "undefined" / "null" / "None" — treat those the same as empty so we
        # don't leak ghost contacts named "undefined undefined" into GHL.
        if s.lower() in {"", "undefined", "null", "none"}:
            return None
        return s

    async def _create_contact(self, ctx: ToolContext, args: Dict[str, Any]) -> str:
        svc = ContactService(ctx.db, ctx.ghl)
        fn = self._opt_str(args.get("first_name"))
        ln = self._opt_str(args.get("last_name"))
        phone = self._opt_str(args.get("phone"))
        email = self._opt_str(args.get("email"))

        # Guard: with the eager-capture prompt, the model sometimes fires
        # create_contact before any name is in the conversation. Without this
        # guard those calls produce ghost "undefined undefined" rows in GHL.
        # Soft-error back to the model so it asks the user for a name first.
        if not fn and not ln:
            logger.info(
                "create_contact rejected: empty first_name + last_name conversation_id=%s args_preview=%r",
                ctx.conversation.id,
                {
                    "first_name": args.get("first_name"),
                    "last_name": args.get("last_name"),
                },
            )
            return json.dumps({
                "ok": False,
                "error": "missing_name",
                "message": (
                    "Need at least a first or last name before creating a contact. "
                    "Ask the user for their name first, then call create_contact again."
                ),
            })

        name = " ".join(x for x in (fn, ln) if x).strip() or None
        try:
            contact, ghl_id = await svc.find_or_create(
                location=ctx.location,
                phone=phone,
                email=email,
                name=name,
                first_name=fn,
                last_name=ln,
                source_channel=ctx.conversation.channel,
                conversation_id=str(ctx.conversation.id),
            )
        except Exception as e:
            logger.error("GHL contact creation failed, saving locally: %s", e)
            contact = ctx.contact
            if name:
                contact.name = name
            if phone:
                contact.phone = phone
            if email:
                contact.email = email
            contact.location_id = ctx.location.id
            await ctx.db.flush()
            ghl_id = None
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
                "contact": {"ghl_contact_id": ghl_id, "name": contact.name, "phone": contact.phone, "email": contact.email},
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

    # ─── Helpers shared by check / book / reschedule / cancel ────────────────

    def _resolve_ghl_calendar_id(self, ctx: ToolContext, intent: str) -> Optional[str]:
        """Pick the GHL calendar id Sarah-originated appointments are pushed to.

        Today MHFH uses two location-wide event calendars (Preplanning vs
        Immediate Need); per-chapel GHL calendars don't exist yet but the
        plumbing is preserved for a future split. Resolution order:

        1. At-need (intent != 'pre_need') → settings.ghl_calendar_id_atneed.
           No per-location override today; add one when needed.
        2. Pre-need → ctx.location.ghl_calendar_id if populated (future
           per-chapel split), else settings.ghl_calendar_id_preneed.
        """
        s = get_settings()
        if intent != "pre_need":
            return s.ghl_calendar_id_atneed or None
        return ctx.location.ghl_calendar_id or (s.ghl_calendar_id_preneed or None)

    def _intent_from_path(self, ctx: ToolContext) -> str:
        """Map Sarah's conversation path to the appointments-architecture intent."""
        path = (ctx.conversation.active_path or "").strip()
        if path == "pre_need":
            return "pre_need"
        # immediate_need, general, obituary, pet_cremation → at_need is the
        # only flow that does anything useful here. Pre-need is the only path
        # with a different booking algorithm.
        return "at_need"

    def _service_type_from_appt_type(
        self, appointment_type: str, intent: str
    ) -> str:
        """Map Sarah's user-facing appointment_type enum to the DB CHECK list."""
        norm = (appointment_type or "").strip().lower()
        if norm in {"pre-arrangement", "pre arrangement", "preplanning", "pre-planning"}:
            return "pre_need_consult"
        if norm in {"after care", "aftercare"}:
            # No dedicated 'after_care' DB enum value; fold into arrangement_conf.
            return "arrangement_conf"
        # Default: at-need arrangement conference.
        return "pre_need_consult" if intent == "pre_need" else "arrangement_conf"

    async def _has_seeded_primaries(
        self, ctx: ToolContext, *, kind: str = "primary"
    ) -> bool:
        """Legacy gate retained for tests: true iff at least one active
        sarah.calendars row of `kind` exists. Production now uses
        `_typed_pool_active`, which is feature-flag + roster driven.
        """
        from sqlalchemy import select as _select

        stmt = _select(Calendar.id).where(
            Calendar.organization_id == ctx.organization.id,
            Calendar.kind == kind,
            Calendar.active.is_(True),
        ).limit(1)
        return (await ctx.db.execute(stmt)).first() is not None

    async def _typed_pool_active(self, ctx: ToolContext) -> bool:
        """True iff the new typed-pool at-need flow is enabled for this org.

        Gates:
          1. `feature_flags.room_calendars_enabled` is true on the organization.
          2. A `kind='primaries_roster'` Calendar row exists (the shared roster
             calendar that drives on-shift detection).

        Director busy state is read from the location's shared booking calendar
        (`location.calendar_id`); per-director Calendar rows are not required.
        """
        from sqlalchemy import select as _select

        cfg = (ctx.organization.config or {}).get("feature_flags") or {}
        if not bool(cfg.get("room_calendars_enabled", False)):
            return False
        stmt = _select(Calendar.id).where(
            Calendar.organization_id == ctx.organization.id,
            Calendar.kind == "primaries_roster",
            Calendar.active.is_(True),
        ).limit(1)
        return (await ctx.db.execute(stmt)).first() is not None

    async def _check_calendar(self, ctx: ToolContext, args: Dict[str, Any]) -> str:
        from datetime import datetime, timedelta
        from zoneinfo import ZoneInfo

        date_str = str(args.get("date", ""))
        tz_name = str(args.get("timezone", "America/Edmonton"))

        try:
            tz = ZoneInfo(tz_name)
        except Exception:
            tz = ZoneInfo("America/Edmonton")

        try:
            dt = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            dt = datetime.now(tz).date() + timedelta(days=1)
            date_str = dt.isoformat()

        if dt < datetime.now(tz).date():
            dt = datetime.now(tz).date() + timedelta(days=1)
            date_str = dt.isoformat()

        # ── Resolve location from model-provided slug ────────────────────────
        location_slug = str(args.get("location_slug", "any")).strip().lower()
        if location_slug and location_slug != "any":
            from sqlalchemy import select as _sel
            resolved = (
                await ctx.db.execute(
                    _sel(Location).where(
                        Location.id == location_slug,
                        Location.organization_id == ctx.organization.id,
                    )
                )
            ).scalar_one_or_none()
            if resolved is not None:
                ctx.location = resolved
                # Persist so book_appointment (next turn) picks up the same chapel.
                ctx.conversation.location_id = resolved.id
                logger.info(
                    "check_calendar resolved location_slug=%s → %s conv=%s",
                    location_slug, resolved.name, ctx.conversation.id,
                )

        intent = self._intent_from_path(ctx)
        use_new_path = await self._typed_pool_active(ctx)
        booking_cal_id = ctx.location.calendar_id

        if use_new_path:
            for _attempt in range(3):
                try:
                    slots = await cal_svc.propose_slots(
                        db=ctx.db,
                        calendar=ctx.calendar,
                        organization=ctx.organization,
                        intent=intent,
                        location_slug=ctx.location.id,
                        target_date=dt,
                        timezone=tz_name,
                        booking_calendar_google_id=booking_cal_id,
                    )
                    break  # success
                except Exception:
                    if _attempt < 2:
                        logger.warning(
                            "propose_slots attempt %d failed, retrying conv=%s",
                            _attempt + 1, ctx.conversation.id,
                        )
                        await _asyncio.sleep(1 * (_attempt + 1))
                    else:
                        logger.exception(
                            "propose_slots_failed after 3 attempts conv=%s loc=%s intent=%s",
                            ctx.conversation.id, ctx.location.id, intent,
                        )
                        slots = []

            if slots:
                return json.dumps({
                    "ok": True,
                    "date": date_str,
                    "location": ctx.location.name,
                    "intent": intent,
                    "available": True,
                    "slots": [
                        {
                            "starts_at": s.starts_at.isoformat(),
                            "ends_at": s.ends_at.isoformat(),
                            "primary": s.primary_label,
                            "venue": s.venue_label,
                        }
                        for s in slots
                    ],
                })

        # Legacy fallback: roster-based availability against the shared
        # availability_calendar_id (preserves Sarah's current behaviour while
        # Primary calendars are unseeded).
        cal_id = ctx.location.availability_calendar_id or ctx.location.calendar_id
        if not cal_id:
            return json.dumps({"ok": False, "error": "no calendar configured"})

        start = datetime(dt.year, dt.month, dt.day, 0, 0, 0, tzinfo=tz)
        end = datetime(dt.year, dt.month, dt.day, 23, 59, 59, tzinfo=tz)
        for _attempt in range(3):
            try:
                events = await ctx.calendar.list_events(
                    cal_id,
                    time_min_iso=start.isoformat(),
                    time_max_iso=end.isoformat(),
                )
                break  # success
            except Exception:
                if _attempt < 2:
                    logger.warning(
                        "list_events attempt %d failed, retrying conv=%s",
                        _attempt + 1, ctx.conversation.id,
                    )
                    await _asyncio.sleep(1 * (_attempt + 1))
                else:
                    logger.exception(
                        "list_events_failed after 3 attempts conv=%s cal=%s",
                        ctx.conversation.id, cal_id,
                    )
                    return json.dumps({"ok": False, "error": "calendar unavailable"})
        result = build_availability_response(
            date_str=date_str,
            location_slug=ctx.location.id,
            location_name=ctx.location.name,
            events=events,
        )
        return json.dumps(result)

    async def _book_appointment(self, ctx: ToolContext, args: Dict[str, Any]) -> str:
        from datetime import datetime
        from sqlalchemy import select as _select

        start = str(args.get("start_iso", ""))
        end = str(args.get("end_iso", ""))
        family_name = str(self._opt_str(args.get("family_name")) or "Family")
        counselor_name = self._opt_str(args.get("counselor_name")) or ""
        appointment_type = str(self._opt_str(args.get("appointment_type")) or "Arrangement")
        notes = self._opt_str(args.get("notes"))

        intent = self._intent_from_path(ctx)
        service_type = self._service_type_from_appt_type(appointment_type, intent)

        # Try the new typed-pool path: find a Primary calendar matching the
        # counselor name the model picked from check_calendar.
        primary_cal: Optional[Calendar] = None
        if counselor_name:
            stmt = _select(Calendar).where(
                Calendar.organization_id == ctx.organization.id,
                Calendar.kind == "primary",
                Calendar.active.is_(True),
                Calendar.name == counselor_name,
            ).limit(1)
            primary_cal = (await ctx.db.execute(stmt)).scalar_one_or_none()

        if primary_cal is not None:
            try:
                start_dt = datetime.fromisoformat(start)
                end_dt = datetime.fromisoformat(end)
            except ValueError:
                return json.dumps({
                    "ok": False,
                    "error": "invalid start_iso/end_iso (must be ISO 8601 with timezone)",
                })

            slot = cal_svc.SlotProposal(
                starts_at=start_dt,
                ends_at=end_dt,
                primary_calendar_id=primary_cal.google_id,
                primary_label=primary_cal.name,
            )

            ghl_cal_id = self._resolve_ghl_calendar_id(ctx, intent)
            ghl_loc = self._ghl_scope(ctx)
            ghl_contact_id = ctx.contact.ghl_contact_id

            push_to_ghl = self._make_ghl_create_push(
                ctx, ghl_cal_id=ghl_cal_id, ghl_loc=ghl_loc, ghl_contact_id=ghl_contact_id,
            )

            for _attempt in range(3):
                try:
                    appt = await cal_svc.confirm_booking(
                        db=ctx.db,
                        calendar=ctx.calendar,
                        organization=ctx.organization,
                        slot=slot,
                        contact=ctx.contact,
                        intent=intent,
                        service_type=service_type,
                        created_by="sarah",
                        conversation_id=ctx.conversation.id,
                        notes=notes,
                        push_to_ghl=push_to_ghl,
                    )
                    break  # success
                except Exception:
                    if _attempt < 2:
                        logger.warning(
                            "confirm_booking attempt %d failed, retrying conv=%s",
                            _attempt + 1, ctx.conversation.id,
                        )
                        await _asyncio.sleep(1 * (_attempt + 1))
                    else:
                        logger.exception(
                            "confirm_booking_failed after 3 attempts conv=%s",
                            ctx.conversation.id,
                        )
                        return json.dumps({"ok": False, "error": "booking failed"})

            # Apply `appointment_booked_sarah` tag so the GHL confirmation
            # workflow (which triggers on this tag, not on `customer_appointment`
            # — that trigger does not fire for API-created appointments where
            # source="third_party") sends the confirmation email + SMS.
            if ghl_contact_id:
                try:
                    await ghl_tags.add_tags(
                        ctx.ghl, ghl_contact_id, location_id=ghl_loc,
                        tags=["appointment_booked_sarah"],
                    )
                except Exception as e:
                    logger.warning("Failed to apply appointment_booked_sarah tag: %s", e)

            await ctx.dispatcher.emit(
                "appointment.booked",
                {
                    "conversation_id": str(ctx.conversation.id),
                    "organization_id": str(ctx.organization.id),
                    "contact": {"ghl_contact_id": ghl_contact_id},
                    "appointment_id": str(appt.id),
                    "calendar": primary_cal.google_id,
                    "start": start,
                    "location_id": ctx.location.id,
                    "counselor": counselor_name,
                    "family_name": family_name,
                    "intent": intent,
                    "service_type": service_type,
                },
            )
            return json.dumps({
                "ok": True,
                "appointment_id": str(appt.id),
                "intent": intent,
                "service_type": service_type,
                "starts_at": appt.starts_at.isoformat(),
                "ends_at": appt.ends_at.isoformat(),
                "primary": primary_cal.name,
            })

        # ── Legacy fallback (no Primary calendars seeded yet) ────────────────
        cal_id = ctx.location.calendar_id
        if not cal_id:
            return json.dumps({"ok": False, "error": "no calendar_id"})

        location_name = ctx.location.name
        summary_parts = [appointment_type, "—", f"{family_name} Family"]
        if counselor_name:
            summary_parts.extend(["with", counselor_name])
        summary_parts.extend(["at", location_name])
        summary = " ".join(summary_parts)

        desc_lines = [
            f"Appointment Type: {appointment_type}",
            f"Family: {family_name}",
            f"Counselor: {counselor_name or 'TBD'}",
            f"Location: {location_name}",
            f"Booked by: Sarah AI",
        ]
        if notes:
            desc_lines.append(f"Notes: {notes}")
        description = "\n".join(desc_lines)

        ev = await ctx.calendar.create_event(
            cal_id,
            start_iso=start,
            end_iso=end,
            summary=summary,
            description=description,
        )

        ghl_cal_id = self._resolve_ghl_calendar_id(ctx, intent)
        ghl_loc = self._ghl_scope(ctx)
        cid = ctx.contact.ghl_contact_id
        ghl_appt_ok = False
        if ghl_cal_id and cid:
            try:
                await ghl_cal.create_appointment(
                    ctx.ghl,
                    ghl_cal_id,
                    location_id=ghl_loc,
                    contact_id=cid,
                    start_time=start,
                    end_time=end,
                    title=summary,
                    notes=description,
                )
                ghl_appt_ok = True
            except Exception as e:
                logger.warning("GHL appointment sync failed: %s", e)

        # Apply `appointment_booked_sarah` tag to trigger the GHL confirmation
        # workflow (tag-based trigger avoids the `customer_appointment` trigger
        # not firing for API-created appointments — see typed-pool branch above).
        if ghl_appt_ok and cid:
            try:
                await ghl_tags.add_tags(
                    ctx.ghl, cid, location_id=ghl_loc,
                    tags=["appointment_booked_sarah"],
                )
            except Exception as e:
                logger.warning("Failed to apply appointment_booked_sarah tag: %s", e)

        # Write canonical sarah.appointments row so the booking surfaces
        # in the comms platform calendar page (and any future reports).
        try:
            start_dt = datetime.fromisoformat(start)
            end_dt = datetime.fromisoformat(end)
        except ValueError:
            start_dt = None
            end_dt = None

        appt_id: Optional[uuid.UUID] = None
        if start_dt and end_dt:
            google_event_id = None
            if isinstance(ev, dict):
                google_event_id = ev.get("id")
            appt = Appointment(
                organization_id=ctx.organization.id,
                contact_id=ctx.contact.id,
                conversation_id=ctx.conversation.id,
                service_type=service_type,
                intent=intent,
                starts_at=start_dt,
                ends_at=end_dt,
                google_event_id=google_event_id,
                status="scheduled",
                created_by="sarah",
                notes=notes,
            )
            ctx.db.add(appt)
            await ctx.db.flush()
            appt_id = appt.id

        await ctx.dispatcher.emit(
            "appointment.booked",
            {
                "conversation_id": str(ctx.conversation.id),
                "organization_id": str(ctx.organization.id),
                "contact": {"ghl_contact_id": cid},
                "appointment_id": str(appt_id) if appt_id else None,
                "calendar": cal_id,
                "start": start,
                "location_id": ctx.location.id,
                "counselor": counselor_name,
                "family_name": family_name,
                "legacy_path": True,
            },
        )
        return json.dumps({"ok": True, "event": ev, "appointment_id": str(appt_id) if appt_id else None, "legacy_path": True})

    # ─── GHL push wrappers (thin delegations to app.services.ghl_push) ───────

    def _make_ghl_create_push(
        self,
        ctx: ToolContext,
        *,
        ghl_cal_id: Optional[str],
        ghl_loc: str,
        ghl_contact_id: Optional[str],
    ):
        return ghl_push.make_create_push(
            ctx.ghl,
            ghl_calendar_id=ghl_cal_id,
            ghl_location_id=ghl_loc,
            ghl_contact_id=ghl_contact_id,
        )

    def _make_ghl_update_push(self, ctx: ToolContext, *, ghl_loc: str):
        return ghl_push.make_update_push(ctx.ghl, ghl_location_id=ghl_loc)

    def _make_ghl_cancel_push(self, ctx: ToolContext, *, ghl_loc: str):
        return ghl_push.make_cancel_push(ctx.ghl, ghl_location_id=ghl_loc)

    # ─── Reschedule / cancel handlers ────────────────────────────────────────

    async def _load_appointment_for_org(
        self, ctx: ToolContext, appointment_id_str: str
    ) -> Optional[Appointment]:
        try:
            appt_uuid = uuid.UUID(appointment_id_str)
        except (ValueError, TypeError):
            return None
        appt = await ctx.db.get(Appointment, appt_uuid)
        if appt is None or appt.organization_id != ctx.organization.id:
            return None
        return appt

    async def _reschedule_appointment(
        self, ctx: ToolContext, args: Dict[str, Any]
    ) -> str:
        from datetime import datetime

        appointment_id = self._opt_str(args.get("appointment_id"))
        new_start = self._opt_str(args.get("start_iso"))
        new_end = self._opt_str(args.get("end_iso"))
        notes = self._opt_str(args.get("notes"))

        if not appointment_id or not new_start or not new_end:
            return json.dumps({
                "ok": False,
                "error": "appointment_id, start_iso, end_iso are required",
            })

        appt = await self._load_appointment_for_org(ctx, appointment_id)
        if appt is None:
            return json.dumps({
                "ok": False,
                "error": "appointment not found for this organization",
            })

        try:
            start_dt = datetime.fromisoformat(new_start)
            end_dt = datetime.fromisoformat(new_end)
        except ValueError:
            return json.dumps({
                "ok": False,
                "error": "invalid start_iso/end_iso (must be ISO 8601 with timezone)",
            })

        push = self._make_ghl_update_push(ctx, ghl_loc=self._ghl_scope(ctx))
        try:
            updated = await cal_svc.reschedule_booking(
                db=ctx.db,
                calendar=ctx.calendar,
                organization=ctx.organization,
                appointment=appt,
                new_starts_at=start_dt,
                new_ends_at=end_dt,
                notes=notes,
                push_to_ghl=push,
            )
        except ValueError as e:
            return json.dumps({"ok": False, "error": str(e)})
        except Exception:
            logger.exception("reschedule_booking_failed appointment_id=%s", appt.id)
            return json.dumps({"ok": False, "error": "reschedule failed"})

        await ctx.dispatcher.emit(
            "appointment.rescheduled",
            {
                "conversation_id": str(ctx.conversation.id),
                "organization_id": str(ctx.organization.id),
                "contact": {"ghl_contact_id": ctx.contact.ghl_contact_id},
                "appointment_id": str(updated.id),
                "starts_at": updated.starts_at.isoformat(),
                "ends_at": updated.ends_at.isoformat(),
                "location_id": ctx.location.id,
            },
        )
        return json.dumps({
            "ok": True,
            "appointment_id": str(updated.id),
            "status": updated.status,
            "starts_at": updated.starts_at.isoformat(),
            "ends_at": updated.ends_at.isoformat(),
        })

    async def _cancel_appointment(
        self, ctx: ToolContext, args: Dict[str, Any]
    ) -> str:
        appointment_id = self._opt_str(args.get("appointment_id"))
        if not appointment_id:
            return json.dumps({"ok": False, "error": "appointment_id is required"})

        appt = await self._load_appointment_for_org(ctx, appointment_id)
        if appt is None:
            return json.dumps({
                "ok": False,
                "error": "appointment not found for this organization",
            })

        push = self._make_ghl_cancel_push(ctx, ghl_loc=self._ghl_scope(ctx))
        try:
            cancelled = await cal_svc.cancel_booking(
                db=ctx.db,
                calendar=ctx.calendar,
                organization=ctx.organization,
                appointment=appt,
                push_to_ghl=push,
            )
        except Exception:
            logger.exception("cancel_booking_failed appointment_id=%s", appt.id)
            return json.dumps({"ok": False, "error": "cancel failed"})

        await ctx.dispatcher.emit(
            "appointment.cancelled",
            {
                "conversation_id": str(ctx.conversation.id),
                "organization_id": str(ctx.organization.id),
                "contact": {"ghl_contact_id": ctx.contact.ghl_contact_id},
                "appointment_id": str(cancelled.id),
                "location_id": ctx.location.id,
            },
        )
        return json.dumps({
            "ok": True,
            "appointment_id": str(cancelled.id),
            "status": cancelled.status,
        })

    async def _search_obituary(self, ctx: ToolContext, args: Dict[str, Any]) -> str:
        nm = self._opt_str(args.get("name"))
        dt = self._opt_str(args.get("date"))
        res = await ctx.obituaries.search(name=nm, date=dt)
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

        # Apply sarah_escalated tag to GHL contact → triggers Sarah Escalation
        # Alert (Enhanced) workflow in GHL.
        ghl_cid = ctx.contact.ghl_contact_id
        if ghl_cid:
            ghl_loc = self._ghl_scope(ctx)
            try:
                await ghl_tags.add_tags(
                    ctx.ghl, ghl_cid, location_id=ghl_loc,
                    tags=["sarah_escalated"],
                )
                logger.info("Applied sarah_escalated tag to contact %s", ghl_cid)
            except Exception as e:
                logger.warning("Failed to apply sarah_escalated tag: %s", e)

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
        valid_paths = {"immediate_need", "pre_need", "obituary", "general"}
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

    async def _continue_on_sms(self, ctx: ToolContext, args: Dict[str, Any]) -> str:
        """Phase B — proactive SMS continuation.

        Sends an opening text from the M&H Twilio number, flips the conversation
        channel to SMS so any later replies land in the same thread, and writes
        an assistant `sms` message row so the comms-platform inbox renders the
        channel-switch divider.

        Idempotent: if conv.channel is already 'sms', returns ok without re-sending.
        Spec: PROMPT_AND_TOOL_CHANGES_2026-04-18.md §"Phase B — Proactive continuation tool".
        """
        phone = self._opt_str(args.get("phone"))
        consent_text = self._opt_str(args.get("consent_text")) or ""

        # Guard 1: phone must be present and match the contact's captured phone.
        if not phone:
            return json.dumps({
                "ok": False,
                "error": "missing_phone",
                "message": "Need the visitor's phone number. Ask for it and call create_contact first.",
            })
        contact_phone = (ctx.contact.phone or "").strip()
        if not contact_phone:
            return json.dumps({
                "ok": False,
                "error": "no_contact_phone",
                "message": "Phone is not on file. Call create_contact with the phone number first, then try again.",
            })

        # Normalize both sides to E.164 before comparing. `create_contact` stores
        # E.164 (via contact_manager.validation.normalize_phone_ca_us) but the
        # model often passes the raw form the visitor typed (e.g. "403-555-0989").
        # Normalize defensively on both sides — protects against legacy contact
        # rows that pre-date the normalization gate too.
        ok_arg, arg_e164 = normalize_phone_ca_us(phone)
        ok_contact, contact_e164 = normalize_phone_ca_us(contact_phone)
        if not ok_arg or not arg_e164:
            return json.dumps({
                "ok": False,
                "error": "invalid_phone",
                "message": "The phone you passed isn't a valid Canadian/US number.",
            })
        # Fall back to raw contact_phone string if it can't be parsed (legacy data).
        compare_contact = contact_e164 or contact_phone
        if compare_contact != arg_e164:
            logger.warning(
                "continue_on_sms phone mismatch conv=%s contact=%s arg=%s",
                ctx.conversation.id, compare_contact[:6] + "...", arg_e164[:6] + "...",
            )
            return json.dumps({
                "ok": False,
                "error": "phone_mismatch",
                "message": (
                    "The phone you passed doesn't match the one on the contact record. "
                    "Use the phone exactly as captured by create_contact."
                ),
            })
        # Use the normalized form everywhere downstream.
        phone = arg_e164

        # Guard 2: idempotent — already on SMS.
        if ctx.conversation.channel == "sms":
            return json.dumps({
                "ok": True,
                "already_on_sms": True,
                "message": "Conversation is already on SMS; no action taken.",
            })

        # Guard 3: CASL — consent_text must be non-empty (audit trail).
        if not consent_text.strip():
            return json.dumps({
                "ok": False,
                "error": "missing_consent",
                "message": "consent_text is required for CASL audit. Pass the exact line the visitor agreed to.",
            })

        # Build opening text. Keep under 160 chars where possible to stay 1 segment.
        full_name = (ctx.contact.name or "").strip()
        first_name = full_name.split()[0] if full_name else "there"
        body = (
            f"Hi {first_name}, this is Sarah from McInnis & Holloway — continuing our chat here. "
            f"Reply anytime, or STOP to opt out."
        )

        # Send via Twilio. Provider hint reserved for future GHL Lead Connector path.
        sms = SmsService()
        try:
            sms_sid = await sms.send(phone, body, provider=SmsProvider.TWILIO)
        except Exception as e:
            logger.error("continue_on_sms Twilio send failed conv=%s err=%s", ctx.conversation.id, e)
            return json.dumps({
                "ok": False,
                "error": "sms_send_failed",
                "message": "Could not send the opening SMS. Conversation channel was NOT flipped.",
            })

        if not sms_sid:
            # Twilio not configured — log but don't flip channel (would silently break replies).
            logger.warning(
                "continue_on_sms: SmsService returned no sid (Twilio not configured?) conv=%s",
                ctx.conversation.id,
            )
            return json.dumps({
                "ok": False,
                "error": "sms_provider_not_configured",
                "message": "Twilio is not configured on this environment; cannot continue on SMS.",
            })

        # Flip channel + log handover message row (visible in comms-platform inbox).
        ctx.conversation.channel = "sms"
        ctx.db.add(Message(
            id=uuid.uuid4(),
            conversation_id=ctx.conversation.id,
            role="assistant",
            content=body,
            channel="sms",
        ))
        await ctx.db.flush()

        logger.info(
            "continue_on_sms ok conv=%s contact=%s sid=%s consent_len=%d",
            ctx.conversation.id, ctx.contact.id, sms_sid, len(consent_text),
        )
        return json.dumps({
            "ok": True,
            "sms_sid": sms_sid,
            "channel": "sms",
            "message": (
                "Opening SMS sent and conversation flipped to SMS. The visitor can close this "
                "window; any replies will land in the same thread."
            ),
        })
