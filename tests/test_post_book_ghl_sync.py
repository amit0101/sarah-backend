"""Regression tests for SarahToolRunner._post_book_ghl_sync (session 30, Option A).

After every successful booking, the helper must:
  1. Build a customFields payload from `location.config.appointment_custom_fields`
     mapped to logical keys (starts_at, ends_at, title, location, host, intent,
     notes, conversation_id) and PUT it onto the GHL contact.
  2. Apply the `sarah_appointment_scheduled` tag.
  3. Never raise — failures are best-effort and must not roll back the
     already-materialised Google event + sarah.appointments row.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services import sarah_tools


_FIELD_IDS = {
    "starts_at":       "fid_starts",
    "ends_at":         "fid_ends",
    "title":           "fid_title",
    "location":        "fid_location",
    "host":            "fid_host",
    "intent":          "fid_intent",
    "notes":           "fid_notes",
    "reschedule_link": "fid_resched",
    "cancel_link":     "fid_cancel",
    "conversation_id": "fid_conv",
}


def _make_ctx(*, ghl_contact_id, location_config):
    return SimpleNamespace(
        ghl=object(),
        contact=SimpleNamespace(ghl_contact_id=ghl_contact_id),
        location=SimpleNamespace(
            id="park_memorial",
            ghl_location_id="LOC123",
            config=location_config,
            name="Park Memorial Chapel",
        ),
        organization=SimpleNamespace(id=uuid.uuid4(), ghl_location_id="LOC123"),
        conversation=SimpleNamespace(id=uuid.uuid4()),
    )


@pytest.mark.asyncio
async def test_writes_custom_fields_and_applies_tag(monkeypatch):
    update_mock = AsyncMock(return_value={"ok": True})
    tags_mock = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(sarah_tools.ghl_contacts, "update_contact", update_mock)
    monkeypatch.setattr(sarah_tools.ghl_tags, "add_tags", tags_mock)

    ctx = _make_ctx(
        ghl_contact_id="C123",
        location_config={"appointment_custom_fields": _FIELD_IDS},
    )

    await sarah_tools.SarahToolRunner()._post_book_ghl_sync(
        ctx,
        intent="at_need",
        starts_at=datetime(2026, 5, 7, 9, 0, tzinfo=timezone.utc),
        ends_at=datetime(2026, 5, 7, 10, 30, tzinfo=timezone.utc),
        title="At-Need Arrangement \u2014 Smith Family with Sharon K.",
        location_name="Park Memorial Chapel",
        counselor_name="Sharon K.",
        notes="Family prefers afternoon visits.",
    )

    update_mock.assert_awaited_once()
    kwargs = update_mock.await_args.kwargs
    assert kwargs["location_id"] == "LOC123"
    payload = kwargs["customFields"]
    by_id = {entry["id"]: entry["field_value"] for entry in payload}
    assert by_id["fid_starts"].startswith("2026-05-07T09:00:00")
    assert by_id["fid_ends"].startswith("2026-05-07T10:30:00")
    assert by_id["fid_title"] == "At-Need Arrangement \u2014 Smith Family with Sharon K."
    assert by_id["fid_location"] == "Park Memorial Chapel"
    assert by_id["fid_host"] == "Sharon K."
    assert by_id["fid_intent"] == "at_need"
    assert by_id["fid_notes"] == "Family prefers afternoon visits."
    assert by_id["fid_conv"]  # any non-empty string
    # reschedule_link / cancel_link are reserved \u2014 not yet populated.
    assert "fid_resched" not in by_id
    assert "fid_cancel" not in by_id

    tags_mock.assert_awaited_once()
    assert tags_mock.await_args.kwargs["tags"] == ["sarah_appointment_scheduled"]


@pytest.mark.asyncio
async def test_skips_when_no_ghl_contact_id(monkeypatch):
    update_mock = AsyncMock()
    tags_mock = AsyncMock()
    monkeypatch.setattr(sarah_tools.ghl_contacts, "update_contact", update_mock)
    monkeypatch.setattr(sarah_tools.ghl_tags, "add_tags", tags_mock)

    ctx = _make_ctx(
        ghl_contact_id=None,
        location_config={"appointment_custom_fields": _FIELD_IDS},
    )
    await sarah_tools.SarahToolRunner()._post_book_ghl_sync(
        ctx,
        intent="pre_need",
        starts_at=datetime(2026, 5, 7, 14, 0, tzinfo=timezone.utc),
        ends_at=datetime(2026, 5, 7, 15, 0, tzinfo=timezone.utc),
        title="Pre-Need Planning \u2014 Doe Family",
        location_name="Fish Creek Chapel",
        counselor_name=None,
        notes=None,
    )
    update_mock.assert_not_awaited()
    tags_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_still_applies_tag_when_field_ids_missing(monkeypatch, caplog):
    update_mock = AsyncMock()
    tags_mock = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(sarah_tools.ghl_contacts, "update_contact", update_mock)
    monkeypatch.setattr(sarah_tools.ghl_tags, "add_tags", tags_mock)

    ctx = _make_ctx(ghl_contact_id="C123", location_config={})

    with caplog.at_level("WARNING"):
        await sarah_tools.SarahToolRunner()._post_book_ghl_sync(
            ctx,
            intent="at_need",
            starts_at=datetime(2026, 5, 7, 9, 0, tzinfo=timezone.utc),
            ends_at=datetime(2026, 5, 7, 10, 30, tzinfo=timezone.utc),
            title="X",
            location_name="Park Memorial Chapel",
            counselor_name="Sharon K.",
            notes=None,
        )

    update_mock.assert_not_awaited()
    tags_mock.assert_awaited_once()
    assert any("appointment_custom_fields_not_configured" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_swallow_failures(monkeypatch):
    """Both the customFields PUT and tag-apply must be best-effort."""
    update_mock = AsyncMock(side_effect=RuntimeError("ghl 5xx"))
    tags_mock = AsyncMock(side_effect=RuntimeError("ghl 5xx"))
    monkeypatch.setattr(sarah_tools.ghl_contacts, "update_contact", update_mock)
    monkeypatch.setattr(sarah_tools.ghl_tags, "add_tags", tags_mock)

    ctx = _make_ctx(
        ghl_contact_id="C123",
        location_config={"appointment_custom_fields": _FIELD_IDS},
    )
    # Must not raise.
    await sarah_tools.SarahToolRunner()._post_book_ghl_sync(
        ctx,
        intent="at_need",
        starts_at=datetime(2026, 5, 7, 9, 0, tzinfo=timezone.utc),
        ends_at=datetime(2026, 5, 7, 10, 30, tzinfo=timezone.utc),
        title="X",
        location_name="Park Memorial Chapel",
        counselor_name="Sharon K.",
        notes=None,
    )
    update_mock.assert_awaited_once()
    tags_mock.assert_awaited_once()
