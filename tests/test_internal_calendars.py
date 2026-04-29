"""Tests for /api/internal/calendars catalog endpoints.

Mirrors the lightweight style of `tests/test_api_routes.py`: route existence
+ auth-dependency contract, plus a couple of small request-validation checks
on the Pydantic models. Heavy DB integration is intentionally out of scope —
the existing test infra has no JSONB→JSON shim, so anything that touches
`sarah.calendars` rows directly lives in calendar_service routing tests
(which mock the DB layer).
"""

from __future__ import annotations

import pytest
from unittest.mock import MagicMock, patch

from app.api.routes.internal import (
    CalendarCreateRequest,
    CalendarPatchRequest,
    CalendarShareRequest,
    FeatureFlagsPatchRequest,
    _require_webhook_secret,
    router,
)


def _route_paths() -> list[str]:
    return [r.path for r in router.routes if hasattr(r, "path")]


class TestCatalogRoutesExist:
    def test_list_calendars_route(self):
        assert "/api/internal/calendars" in _route_paths()

    def test_patch_calendar_route(self):
        assert "/api/internal/calendars/{calendar_id}" in _route_paths()

    def test_calendar_events_route(self):
        assert "/api/internal/calendars/{calendar_id}/events" in _route_paths()

    def test_calendar_acl_route(self):
        assert "/api/internal/calendars/{calendar_id}/acl" in _route_paths()

    def test_calendar_share_route(self):
        assert "/api/internal/calendars/{calendar_id}/share" in _route_paths()

    def test_calendar_revoke_share_route(self):
        assert (
            "/api/internal/calendars/{calendar_id}/share/{rule_id:path}"
            in _route_paths()
        )

    def test_feature_flags_patch_route(self):
        assert "/api/internal/org/feature-flags" in _route_paths()


class TestWebhookSecretGuard:
    """The new endpoints all gate on X-Webhook-Secret. Verify the helper."""

    def test_rejects_missing_secret_when_configured(self):
        from fastapi import HTTPException

        with patch("app.api.routes.internal.get_settings") as ms:
            ms.return_value = MagicMock(sarah_webhook_secret="real-secret")
            with pytest.raises(HTTPException) as exc:
                _require_webhook_secret(None)
            assert exc.value.status_code == 401

    def test_rejects_wrong_secret(self):
        from fastapi import HTTPException

        with patch("app.api.routes.internal.get_settings") as ms:
            ms.return_value = MagicMock(sarah_webhook_secret="real-secret")
            with pytest.raises(HTTPException) as exc:
                _require_webhook_secret("nope")
            assert exc.value.status_code == 401

    def test_accepts_correct_secret(self):
        with patch("app.api.routes.internal.get_settings") as ms:
            ms.return_value = MagicMock(sarah_webhook_secret="real-secret")
            # Should not raise.
            _require_webhook_secret("real-secret")

    def test_blocks_when_not_configured(self):
        from fastapi import HTTPException

        with patch("app.api.routes.internal.get_settings") as ms:
            ms.return_value = MagicMock(sarah_webhook_secret="")
            with pytest.raises(HTTPException) as exc:
                _require_webhook_secret("anything")
            assert exc.value.status_code == 503


class TestPayloadValidation:
    """The Pydantic models enforce field-level invariants — sanity-check them."""

    def test_create_request_rejects_empty_name(self):
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            CalendarCreateRequest(name="", kind="venue")

    def test_create_request_accepts_minimal_payload(self):
        m = CalendarCreateRequest(name="PM-5", kind="venue")
        assert m.read_convention == "busy"
        assert m.time_zone == "America/Edmonton"
        assert m.metadata == {}

    def test_create_request_passes_through_existing_google_id(self):
        # When operator wants to register an already-existing M&H calendar
        # (e.g. Primaries roster) we skip the Google insert — the API
        # surface allows it via google_id.
        m = CalendarCreateRequest(
            name="Primaries Roster",
            kind="primaries_roster",
            read_convention="availability",
            google_id="abc@group.calendar.google.com",
        )
        assert m.google_id == "abc@group.calendar.google.com"

    def test_patch_request_all_fields_optional(self):
        # Empty patch is legal — caller may PATCH with no-op (returns
        # current row state). All four fields are independently optional.
        m = CalendarPatchRequest()
        assert m.name is None
        assert m.active is None
        assert m.metadata is None
        assert m.read_convention is None

    def test_share_request_defaults_to_writer(self):
        m = CalendarShareRequest(email="staff@mhfh.com")
        assert m.role == "writer"

    def test_share_request_rejects_too_short_email(self):
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            CalendarShareRequest(email="x")

    def test_feature_flags_request_all_fields_optional(self):
        # Operator may toggle one flag at a time — both fields are
        # independently optional and absent keys preserve current value.
        m = FeatureFlagsPatchRequest(organization_slug="mhc")
        assert m.room_calendars_enabled is None
        assert m.pre_arrangers_enabled is None

    def test_feature_flags_request_accepts_partial_toggle(self):
        m = FeatureFlagsPatchRequest(
            organization_slug="mhc", room_calendars_enabled=True
        )
        assert m.room_calendars_enabled is True
        assert m.pre_arrangers_enabled is None
