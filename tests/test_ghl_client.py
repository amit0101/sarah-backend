"""Tests for GHL client — API calls, retries, tag/pipeline helpers."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.ghl_client import contacts as ghl_contacts
from app.ghl_client import tags as ghl_tags
from app.ghl_client import pipelines as ghl_pipes
from app.ghl_client import calendars as ghl_cals


@pytest.fixture
def ghl():
    """Create a mock GHLClient without calling real constructor."""
    client = MagicMock()
    client.request = AsyncMock(return_value={"ok": True})
    client._default_location = "loc-123"
    return client


class TestGHLContacts:
    @pytest.mark.asyncio
    async def test_create_contact(self, ghl):
        ghl.request = AsyncMock(return_value={"contact": {"id": "c-new-123"}})
        result = await ghl_contacts.create_contact(
            ghl, location_id="loc-123",
            name="Jane Doe", phone="+14035551234",
        )
        assert result["contact"]["id"] == "c-new-123"
        ghl.request.assert_called_once()

    @pytest.mark.asyncio
    async def test_create_contact_duplicate_returns_existing(self, ghl):
        """Regression: MHFH-style 400 'location does not allow duplicated contacts'
        must not bubble up. The client should detect `meta.contactId`, update the
        existing contact with the supplied fields, and return a contact-shaped
        dict so `find_or_create` can populate `ghl_id` as usual."""
        from app.ghl_client.client import GHLAPIError
        dup_body = {
            "statusCode": 400,
            "message": "This location does not allow duplicated contacts.",
            "meta": {
                "contactId": "c-existing-999",
                "matchingField": "email",
                "contactName": "Jane Doe",
            },
        }
        # 1st call (POST /contacts/) raises duplicate 400; 2nd call (PUT update) succeeds.
        ghl.request = AsyncMock(
            side_effect=[
                GHLAPIError("GHL API error 400", status_code=400, body=dup_body),
                {"contact": {"id": "c-existing-999"}},
            ]
        )
        result = await ghl_contacts.create_contact(
            ghl, location_id="loc-123",
            first_name="Jane", last_name="Doe",
            email="jane@example.com",
            custom_fields=[{"id": "cf-1", "field_value": "webchat"}],
        )
        assert result == {"contact": {"id": "c-existing-999"}}
        assert ghl.request.await_count == 2
        # Second call must be the PUT update on the existing id, without `tags`
        # (avoid clobbering pre-existing tags on the duplicate).
        update_call = ghl.request.await_args_list[1]
        assert update_call.args[0] == "PUT"
        assert update_call.args[1] == "/contacts/c-existing-999"
        assert "tags" not in (update_call.kwargs.get("json_body") or {})

    @pytest.mark.asyncio
    async def test_create_contact_non_duplicate_400_still_raises(self, ghl):
        """A 400 without `meta.contactId` (e.g. validation error) must still raise."""
        from app.ghl_client.client import GHLAPIError
        ghl.request = AsyncMock(
            side_effect=GHLAPIError(
                "GHL API error 400",
                status_code=400,
                body={"statusCode": 400, "message": "name must be a string"},
            )
        )
        with pytest.raises(GHLAPIError):
            await ghl_contacts.create_contact(
                ghl, location_id="loc-123", first_name="Jane",
            )

    @pytest.mark.asyncio
    async def test_lookup_contact_by_phone(self, ghl):
        ghl.request = AsyncMock(return_value={"contact": {"id": "c-found", "phone": "+1234"}})
        result = await ghl_contacts.lookup_contact(ghl, location_id="loc-123", phone="+1234")
        assert result is not None
        assert result["id"] == "c-found"

    @pytest.mark.asyncio
    async def test_lookup_contact_not_found(self, ghl):
        from app.ghl_client.client import GHLAPIError
        ghl.request = AsyncMock(side_effect=GHLAPIError("Not found", status_code=404))
        result = await ghl_contacts.lookup_contact(ghl, location_id="loc-123", phone="+9999")
        assert result is None

    @pytest.mark.asyncio
    async def test_lookup_contact_null_wrapper_returns_none(self, ghl):
        """Regression (session 29, bug 11): `/contacts/search/duplicate`
        returns 200 with `{"contact": null, "traceId": "..."}` when there
        is no match. Earlier code fell through to `return data`, which is
        truthy, causing `ContactService.find_or_create` to skip both the
        update and create branches and raise `RuntimeError("GHL did not
        return contact id")`. Must return `None` so the create path runs.
        """
        ghl.request = AsyncMock(
            return_value={"contact": None, "traceId": "abc-123"}
        )
        result = await ghl_contacts.lookup_contact(
            ghl, location_id="loc-123", email="nobody@example.com",
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_update_contact(self, ghl):
        ghl.request = AsyncMock(return_value={"contact": {"id": "c-123"}})
        await ghl_contacts.update_contact(
            ghl, "c-123", location_id="loc-123", name="Updated Name"
        )
        ghl.request.assert_called_once()


class TestGHLTags:
    @pytest.mark.asyncio
    async def test_add_tags(self, ghl):
        ghl.request = AsyncMock(return_value={"tags": ["webchat_lead"]})
        await ghl_tags.add_tags(ghl, "c-123", location_id="loc-123", tags=["webchat_lead"])
        ghl.request.assert_called_once()

    @pytest.mark.asyncio
    async def test_remove_tags(self, ghl):
        ghl.request = AsyncMock(return_value={"tags": []})
        await ghl_tags.remove_tags(ghl, "c-123", location_id="loc-123", tag_ids=["old_tag"])
        ghl.request.assert_called_once()


class TestGHLPipelines:
    @pytest.mark.asyncio
    async def test_create_opportunity(self, ghl):
        ghl.request = AsyncMock(return_value={"opportunity": {"id": "opp-123"}})
        result = await ghl_pipes.create_opportunity(
            ghl, location_id="loc-123",
            contact_id="c-123", pipeline_id="pipe-1", pipeline_stage_id="stage-1",
        )
        assert result["opportunity"]["id"] == "opp-123"


class TestGHLCalendars:
    @pytest.mark.asyncio
    async def test_create_appointment(self, ghl):
        ghl.request = AsyncMock(return_value={"id": "apt-123"})
        result = await ghl_cals.create_appointment(
            ghl, calendar_id="cal-1", location_id="loc-123",
            contact_id="c-123", start_time="2026-04-10T10:00:00",
            end_time="2026-04-10T11:00:00", title="Pre-Need Consultation",
        )
        ghl.request.assert_called_once()
        # Default ignore_date_range=True so GHL accepts slots that Sarah
        # validated against Google but which would otherwise be refused by
        # the GHL calendar's `allowBookingAfter` lead-time rule.
        body = ghl.request.call_args.kwargs["json_body"]
        assert body["ignoreDateRange"] is True

    @pytest.mark.asyncio
    async def test_create_appointment_respects_explicit_ignore_flag(self, ghl):
        ghl.request = AsyncMock(return_value={"id": "apt-123"})
        await ghl_cals.create_appointment(
            ghl, calendar_id="cal-1", location_id="loc-123",
            contact_id="c-123", start_time="2026-04-10T10:00:00",
            ignore_date_range=False,
        )
        body = ghl.request.call_args.kwargs["json_body"]
        assert "ignoreDateRange" not in body

    @pytest.mark.asyncio
    async def test_cancel_appointment(self, ghl):
        ghl.request = AsyncMock(return_value={})
        await ghl_cals.cancel_appointment(ghl, "apt-123", location_id="loc-123")
        ghl.request.assert_called_once()
