"""Tests for the GHL → Sarah appointment webhook (Priority B, session 15).

Covers:
  * Route existence + HTTP method
  * HMAC signature validation (pass-through, accept, reject)
  * Pydantic payload validation
  * `upsert_from_ghl` idempotency + state transitions
  * Helper functions (`_map_status`, `_is_sarah_origin`, `_infer_*`)

Heavy DB integration is intentionally out of scope (Appointment uses
UUID(as_uuid=True) + gen_random_uuid() which the sqlite test infra doesn't
support). The upsert tests mock the AsyncSession instead — same approach
as `test_internal_calendars.py` for the same reason.
"""

from __future__ import annotations

import hashlib
import hmac
import uuid
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
from typing import Any, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError

from app.api.routes.webhooks import _validate_ghl_signature, router
from app.services.ghl_appointment_sync import (
    GhlAppointmentEvent,
    SyncOutcome,
    _infer_intent,
    _infer_service_type,
    _is_sarah_origin,
    _map_status,
    upsert_from_ghl,
)


# ─── Route registration ──────────────────────────────────────────────────────


def test_appointment_webhook_route_registered():
    paths = {r.path for r in router.routes if hasattr(r, "path")}
    assert "/webhooks/ghl/{org_slug}/appointment" in paths


def test_appointment_webhook_route_is_post():
    for r in router.routes:
        if getattr(r, "path", "") == "/webhooks/ghl/{org_slug}/appointment":
            assert "POST" in r.methods
            return
    pytest.fail("appointment webhook route not found")


# ─── HMAC validation ─────────────────────────────────────────────────────────


class TestSignatureValidation:
    def _sig(self, body: bytes, secret: str) -> str:
        return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

    def test_passthrough_when_secret_unset(self):
        with patch(
            "app.api.routes.webhooks.get_settings",
            return_value=SimpleNamespace(ghl_webhook_secret=""),
        ):
            # Should NOT raise even with no signature header
            _validate_ghl_signature(b'{"x":1}', None)

    def test_rejects_missing_signature_when_secret_set(self):
        from fastapi import HTTPException

        with patch(
            "app.api.routes.webhooks.get_settings",
            return_value=SimpleNamespace(ghl_webhook_secret="topsecret"),
        ):
            with pytest.raises(HTTPException) as exc:
                _validate_ghl_signature(b'{"x":1}', None)
            assert exc.value.status_code == 401
            assert "missing" in exc.value.detail.lower()

    def test_rejects_invalid_signature(self):
        from fastapi import HTTPException

        body = b'{"x":1}'
        with patch(
            "app.api.routes.webhooks.get_settings",
            return_value=SimpleNamespace(ghl_webhook_secret="topsecret"),
        ):
            with pytest.raises(HTTPException) as exc:
                _validate_ghl_signature(body, "sha256=deadbeef")
            assert exc.value.status_code == 401
            assert "invalid" in exc.value.detail.lower()

    def test_accepts_valid_signature(self):
        body = b'{"ghl_appointment_id":"abc","status":"new"}'
        secret = "topsecret"
        sig = self._sig(body, secret)
        with patch(
            "app.api.routes.webhooks.get_settings",
            return_value=SimpleNamespace(ghl_webhook_secret=secret),
        ):
            # Should not raise
            _validate_ghl_signature(body, sig)

    def test_signature_constant_time_compare(self):
        """A signature of the wrong length should still return 401, not a
        Python exception. (Defense against timing oracle.)"""
        from fastapi import HTTPException

        with patch(
            "app.api.routes.webhooks.get_settings",
            return_value=SimpleNamespace(ghl_webhook_secret="topsecret"),
        ):
            with pytest.raises(HTTPException):
                _validate_ghl_signature(b"x", "sha256=short")


# ─── Pydantic schema ─────────────────────────────────────────────────────────


class TestEventSchema:
    def test_minimum_required_fields(self):
        e = GhlAppointmentEvent(ghl_appointment_id="abc", status="new")
        assert e.ghl_appointment_id == "abc"
        assert e.status == "new"
        assert e.starts_at is None  # times are optional

    def test_full_payload(self):
        e = GhlAppointmentEvent(
            ghl_appointment_id="abc",
            ghl_contact_id="ghl-contact",
            status="cancelled",
            starts_at="2026-05-01T14:00:00-06:00",
            ends_at="2026-05-01T15:00:00-06:00",
            title="Arrangement — Smith with Aaron B.",
            notes="Family running late",
            google_event_id="g-evt-id",
            ghl_calendar_id="ghl-cal-id",
            source_channel="webchat",
        )
        assert e.starts_at.isoformat().startswith("2026-05-01T14:00:00")

    def test_invalid_status_rejected(self):
        with pytest.raises(ValidationError):
            GhlAppointmentEvent(
                ghl_appointment_id="abc",
                status="totally_made_up",  # type: ignore[arg-type]
            )

    def test_blank_appointment_id_rejected(self):
        with pytest.raises(ValidationError):
            GhlAppointmentEvent(ghl_appointment_id="   ", status="new")

    def test_strips_whitespace_on_id(self):
        e = GhlAppointmentEvent(ghl_appointment_id="  abc  ", status="new")
        assert e.ghl_appointment_id == "abc"


# ─── Helper unit tests ───────────────────────────────────────────────────────


class TestMapStatus:
    @pytest.mark.parametrize(
        "inbound,expected",
        [
            ("new", "scheduled"),
            ("confirmed", "scheduled"),
            ("scheduled", "scheduled"),
            ("rescheduled", "rescheduled"),
            ("cancelled", "cancelled"),
            ("canceled", "cancelled"),  # American spelling tolerated
            ("no_show", "no_show"),
            ("noshow", "no_show"),
            ("completed", "completed"),
            ("showed", "completed"),
            ("CANCELLED", "cancelled"),  # case-insensitive
            ("  scheduled  ", "scheduled"),  # whitespace tolerant
        ],
    )
    def test_status_mapping(self, inbound, expected):
        assert _map_status(inbound) == expected

    def test_is_update_flag_does_not_change_mapping(self):
        """Backwards-compat shim — the kwarg is accepted but inert."""
        assert _map_status("new", is_update=False) == "scheduled"
        assert _map_status("new", is_update=True) == "scheduled"


class TestIsSarahOrigin:
    @pytest.mark.parametrize(
        "channel,expected",
        [
            ("webchat", True),
            ("WEBCHAT", True),
            ("  sms  ", True),
            ("sarah", True),
            ("sarah_handover", True),
            ("ghl_ui", False),
            ("comms_ui", False),
            ("", False),
            (None, False),
            ("staff", False),
        ],
    )
    def test_origin_detection(self, channel, expected):
        assert _is_sarah_origin(channel) is expected


class TestInferServiceType:
    @pytest.mark.parametrize(
        "title,expected",
        [
            (None, "arrangement_conf"),
            ("", "arrangement_conf"),
            ("Arrangement — Smith Family with Aaron B.", "arrangement_conf"),
            ("Pre-need consultation", "pre_need_consult"),
            ("Preplanning meeting", "pre_need_consult"),
            ("Visitation - Smith family", "visitation"),
            ("Memorial Service", "service"),
            ("Transport call - removal", "transport"),
            ("Reception room booking", "reception"),
        ],
    )
    def test_service_type(self, title, expected):
        assert _infer_service_type(title) == expected


class TestInferIntent:
    @pytest.mark.parametrize(
        "title,expected",
        [
            (None, "at_need"),
            ("Arrangement — Smith Family", "at_need"),
            ("Pre-need consultation", "pre_need"),
            ("Preplanning meeting", "pre_need"),
            ("Pre-plan call", "pre_need"),
            ("Visitation", "at_need"),
        ],
    )
    def test_intent(self, title, expected):
        assert _infer_intent(title) == expected


# ─── upsert_from_ghl integration (mocked DB) ─────────────────────────────────


def _mock_org() -> Any:
    return SimpleNamespace(
        id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
        slug="mhc",
    )


def _mock_db_with_existing(existing: Any) -> Any:
    """AsyncMock session whose first execute() returns a result whose
    scalar_one_or_none yields `existing` (an Appointment-like or None).
    Subsequent execute() calls return None (used for contact lookup)."""
    db = MagicMock()
    db.execute = AsyncMock()

    def _make_result(value: Any) -> MagicMock:
        r = MagicMock()
        r.scalar_one_or_none = MagicMock(return_value=value)
        return r

    # First: existing appointment lookup. Second: contact lookup (None).
    db.execute.side_effect = [_make_result(existing), _make_result(None)]
    db.add = MagicMock()
    db.flush = AsyncMock()
    return db


def _existing_appt(
    *,
    ghl_id: str = "ghl-1",
    status: str = "scheduled",
    starts: Optional[datetime] = None,
    ends: Optional[datetime] = None,
) -> Any:
    starts = starts or datetime(2026, 5, 1, 14, 0, tzinfo=timezone.utc)
    ends = ends or datetime(2026, 5, 1, 15, 0, tzinfo=timezone.utc)
    return SimpleNamespace(
        id=uuid.UUID("00000000-0000-0000-0000-000000000abc"),
        organization_id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
        contact_id=None,
        ghl_appointment_id=ghl_id,
        status=status,
        starts_at=starts,
        ends_at=ends,
        notes=None,
        google_event_id=None,
        primary_cal_id=None,
        venue_cal_id=None,
    )


@pytest.mark.asyncio
async def test_upsert_creates_new_appointment_when_none_exists():
    db = _mock_db_with_existing(None)
    org = _mock_org()
    event = GhlAppointmentEvent(
        ghl_appointment_id="ghl-new-1",
        status="new",
        starts_at="2026-05-10T14:00:00-06:00",
        ends_at="2026-05-10T15:00:00-06:00",
        title="Arrangement — Smith Family",
    )

    out = await upsert_from_ghl(db, org, event)

    assert out.action == "created"
    assert out.matched_existing is False
    assert out.status == "scheduled"
    assert db.add.called
    appt = db.add.call_args[0][0]
    assert appt.ghl_appointment_id == "ghl-new-1"
    assert appt.organization_id == org.id
    assert appt.created_by == "ghl"
    assert appt.intent == "at_need"
    assert appt.service_type == "arrangement_conf"


@pytest.mark.asyncio
async def test_upsert_skips_insert_when_no_times_supplied():
    """A cancellation event with no times can't be inserted (CHECK
    constraint requires ends_at > starts_at). Log + ignore."""
    db = _mock_db_with_existing(None)
    org = _mock_org()
    event = GhlAppointmentEvent(
        ghl_appointment_id="ghl-cancel-orphan",
        status="cancelled",
    )

    out = await upsert_from_ghl(db, org, event)

    assert out.action == "ignored_no_change"
    assert not db.add.called


@pytest.mark.asyncio
async def test_upsert_updates_existing_status_only():
    existing = _existing_appt(ghl_id="ghl-x", status="scheduled")
    db = _mock_db_with_existing(existing)
    org = _mock_org()
    event = GhlAppointmentEvent(
        ghl_appointment_id="ghl-x",
        status="cancelled",
    )

    out = await upsert_from_ghl(db, org, event)

    assert out.action == "updated"
    assert out.matched_existing is True
    assert existing.status == "cancelled"
    # Times unchanged because event didn't supply them
    assert existing.starts_at == datetime(2026, 5, 1, 14, 0, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_upsert_updates_times_and_marks_rescheduled():
    existing = _existing_appt(ghl_id="ghl-r", status="scheduled")
    db = _mock_db_with_existing(existing)
    org = _mock_org()
    new_start = datetime(2026, 5, 1, 16, 0, tzinfo=timezone.utc)
    new_end = datetime(2026, 5, 1, 17, 0, tzinfo=timezone.utc)
    event = GhlAppointmentEvent(
        ghl_appointment_id="ghl-r",
        status="rescheduled",
        starts_at=new_start.isoformat(),
        ends_at=new_end.isoformat(),
    )

    out = await upsert_from_ghl(db, org, event)

    assert out.action == "updated"
    assert existing.starts_at == new_start
    assert existing.ends_at == new_end
    assert existing.status == "rescheduled"


@pytest.mark.asyncio
async def test_upsert_ignores_sarah_origin_event_for_existing_row():
    """Sarah's row is authoritative; GHL workflow firing on Sarah-pushed
    event must not overwrite our local representation."""
    existing = _existing_appt(ghl_id="ghl-sarah", status="scheduled")
    db = _mock_db_with_existing(existing)
    org = _mock_org()
    event = GhlAppointmentEvent(
        ghl_appointment_id="ghl-sarah",
        status="confirmed",
        source_channel="webchat",
        starts_at="2026-06-01T10:00:00-06:00",
        ends_at="2026-06-01T11:00:00-06:00",
    )

    out = await upsert_from_ghl(db, org, event)

    assert out.action == "ignored_sarah_origin"
    # Existing row untouched
    assert existing.starts_at == datetime(2026, 5, 1, 14, 0, tzinfo=timezone.utc)
    assert existing.status == "scheduled"


@pytest.mark.asyncio
async def test_upsert_inserts_when_no_existing_row_even_with_sarah_origin():
    """Sarah-origin gate only protects EXISTING rows. If no row exists
    (e.g. Sarah's GHL push succeeded but local insert failed), the
    webhook is a repair path — insert anyway."""
    db = _mock_db_with_existing(None)
    org = _mock_org()
    event = GhlAppointmentEvent(
        ghl_appointment_id="ghl-orphan",
        status="confirmed",
        source_channel="webchat",
        starts_at="2026-06-01T10:00:00-06:00",
        ends_at="2026-06-01T11:00:00-06:00",
    )

    out = await upsert_from_ghl(db, org, event)

    assert out.action == "created"
    assert db.add.called


@pytest.mark.asyncio
async def test_upsert_ignores_no_change_update():
    """Sending the same status + times as the row already has is a no-op."""
    existing = _existing_appt(ghl_id="ghl-noop", status="scheduled")
    db = _mock_db_with_existing(existing)
    org = _mock_org()
    event = GhlAppointmentEvent(
        ghl_appointment_id="ghl-noop",
        status="scheduled",
        starts_at=existing.starts_at.isoformat(),
        ends_at=existing.ends_at.isoformat(),
    )

    out = await upsert_from_ghl(db, org, event)

    assert out.action == "ignored_no_change"


@pytest.mark.asyncio
async def test_upsert_does_not_null_out_fk_columns():
    """Inbound event without primary_cal_id must not blank the existing
    Sarah-side FK reference."""
    existing = _existing_appt(ghl_id="ghl-fk", status="scheduled")
    existing.primary_cal_id = uuid.UUID("00000000-0000-0000-0000-0000000000aa")
    existing.venue_cal_id = uuid.UUID("00000000-0000-0000-0000-0000000000bb")
    db = _mock_db_with_existing(existing)
    org = _mock_org()
    event = GhlAppointmentEvent(
        ghl_appointment_id="ghl-fk",
        status="cancelled",
    )

    await upsert_from_ghl(db, org, event)

    assert existing.primary_cal_id == uuid.UUID(
        "00000000-0000-0000-0000-0000000000aa"
    )
    assert existing.venue_cal_id == uuid.UUID(
        "00000000-0000-0000-0000-0000000000bb"
    )


@pytest.mark.asyncio
async def test_upsert_outcome_is_serialisable():
    """Route layer dumps the outcome to JSON — must round-trip cleanly."""
    db = _mock_db_with_existing(None)
    org = _mock_org()
    event = GhlAppointmentEvent(
        ghl_appointment_id="ghl-ser",
        status="new",
        starts_at="2026-05-10T14:00:00-06:00",
        ends_at="2026-05-10T15:00:00-06:00",
    )
    out = await upsert_from_ghl(db, org, event)
    payload = out.model_dump()
    assert payload["action"] == "created"
    assert payload["ghl_appointment_id"] == "ghl-ser"
    assert payload["status"] == "scheduled"


# ─── B-soft.8 — comms-platform realtime fanout ────────────────────────────────


class _FakeRequest:
    """Minimal Request stand-in with `body()` and `json()` coroutines."""

    def __init__(self, raw: bytes) -> None:
        self._raw = raw

    async def body(self) -> bytes:
        return self._raw

    async def json(self):
        import json as _json

        return _json.loads(self._raw)


def _outcome(action: str, *, status: str = "scheduled") -> SyncOutcome:
    return SyncOutcome(
        action=action,  # type: ignore[arg-type]
        appointment_id=str(uuid.uuid4()),
        ghl_appointment_id="ghl-fanout",
        status=status,
        matched_existing=action != "created",
    )


@pytest.mark.asyncio
async def test_fanout_emits_appointment_synced_on_create():
    """When upsert returns 'created', a comms fanout event must be dispatched."""
    import json
    from app.api.routes import webhooks as wh

    body = json.dumps({
        "ghl_appointment_id": "ghl-fanout",
        "status": "new",
        "starts_at": "2026-05-10T14:00:00-06:00",
        "ends_at": "2026-05-10T15:00:00-06:00",
        "ghl_contact_id": "ghl-c1",
        "title": "Smoke",
    }).encode()
    req = _FakeRequest(body)
    db = AsyncMock()
    db.commit = AsyncMock()
    org = _mock_org()
    org.slug = "mhc"

    with patch.object(wh, "_validate_ghl_signature"), \
         patch.object(wh, "get_organization_by_slug", AsyncMock(return_value=org)), \
         patch.object(wh, "upsert_from_ghl", AsyncMock(return_value=_outcome("created"))), \
         patch.object(wh, "WebhookDispatcher") as mock_disp_cls:
        mock_disp = MagicMock()
        mock_disp.emit = AsyncMock()
        mock_disp_cls.return_value = mock_disp

        result = await wh.ghl_appointment_webhook("mhc", req, db, None)

    assert result["status"] == "ok"
    assert result["outcome"]["action"] == "created"
    mock_disp.emit.assert_awaited_once()
    event_name, payload_data = mock_disp.emit.await_args.args
    assert event_name == "appointment.synced"
    assert payload_data["action"] == "created"
    assert payload_data["ghl_appointment_id"] == "ghl-fanout"
    assert payload_data["organization_slug"] == "mhc"
    assert payload_data["ghl_contact_id"] == "ghl-c1"
    assert payload_data["status"] == "scheduled"
    assert payload_data["source"] == "ghl_webhook"
    assert payload_data["starts_at"] == "2026-05-10T14:00:00-06:00"


@pytest.mark.asyncio
async def test_fanout_emits_on_update():
    """`updated` outcomes also get a fanout event."""
    import json
    from app.api.routes import webhooks as wh

    body = json.dumps({
        "ghl_appointment_id": "ghl-fanout",
        "status": "cancelled",
    }).encode()
    req = _FakeRequest(body)
    db = AsyncMock()
    db.commit = AsyncMock()
    org = _mock_org()
    org.slug = "mhc"

    with patch.object(wh, "_validate_ghl_signature"), \
         patch.object(wh, "get_organization_by_slug", AsyncMock(return_value=org)), \
         patch.object(wh, "upsert_from_ghl", AsyncMock(
             return_value=_outcome("updated", status="cancelled")
         )), \
         patch.object(wh, "WebhookDispatcher") as mock_disp_cls:
        mock_disp = MagicMock()
        mock_disp.emit = AsyncMock()
        mock_disp_cls.return_value = mock_disp

        await wh.ghl_appointment_webhook("mhc", req, db, None)

    mock_disp.emit.assert_awaited_once()
    _, payload_data = mock_disp.emit.await_args.args
    assert payload_data["action"] == "updated"
    assert payload_data["status"] == "cancelled"


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["ignored_sarah_origin", "ignored_no_change"])
async def test_fanout_skipped_for_no_op_outcomes(action):
    """No-op outcomes carry no new state — comms must NOT be fanned out to."""
    import json
    from app.api.routes import webhooks as wh

    body = json.dumps({
        "ghl_appointment_id": "ghl-fanout",
        "status": "new",
        "starts_at": "2026-05-10T14:00:00-06:00",
        "ends_at": "2026-05-10T15:00:00-06:00",
    }).encode()
    req = _FakeRequest(body)
    db = AsyncMock()
    db.commit = AsyncMock()
    org = _mock_org()

    with patch.object(wh, "_validate_ghl_signature"), \
         patch.object(wh, "get_organization_by_slug", AsyncMock(return_value=org)), \
         patch.object(wh, "upsert_from_ghl", AsyncMock(return_value=_outcome(action))), \
         patch.object(wh, "WebhookDispatcher") as mock_disp_cls:
        mock_disp = MagicMock()
        mock_disp.emit = AsyncMock()
        mock_disp_cls.return_value = mock_disp

        result = await wh.ghl_appointment_webhook("mhc", req, db, None)

    assert result["outcome"]["action"] == action
    mock_disp.emit.assert_not_awaited()


@pytest.mark.asyncio
async def test_fanout_failure_does_not_break_response():
    """A dispatcher exception must not 500 the GHL webhook."""
    import json
    from app.api.routes import webhooks as wh

    body = json.dumps({
        "ghl_appointment_id": "ghl-fanout",
        "status": "new",
        "starts_at": "2026-05-10T14:00:00-06:00",
        "ends_at": "2026-05-10T15:00:00-06:00",
    }).encode()
    req = _FakeRequest(body)
    db = AsyncMock()
    db.commit = AsyncMock()
    org = _mock_org()

    with patch.object(wh, "_validate_ghl_signature"), \
         patch.object(wh, "get_organization_by_slug", AsyncMock(return_value=org)), \
         patch.object(wh, "upsert_from_ghl", AsyncMock(return_value=_outcome("created"))), \
         patch.object(wh, "WebhookDispatcher", side_effect=RuntimeError("boom")):
        # Should not raise
        result = await wh.ghl_appointment_webhook("mhc", req, db, None)

    assert result["status"] == "ok"
    assert result["outcome"]["action"] == "created"
