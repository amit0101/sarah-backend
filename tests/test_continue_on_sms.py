"""Phase B — continue_on_sms tool unit tests.

Spec: PROMPT_AND_TOOL_CHANGES_2026-04-18.md §"Phase B — Proactive continuation tool"

Pure mocked-object tests — no DB roundtrip — because the conftest test_org
fixture uses Postgres JSONB which SQLite cannot compile.
"""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.sarah_tools import SarahToolRunner, ToolContext


def _ctx(*, contact_phone: str = "+14035559999", channel: str = "webchat"):
    """Build a ToolContext with stub attribute objects (no SQLAlchemy)."""
    contact = SimpleNamespace(
        id=uuid.uuid4(),
        name="John Doe",
        phone=contact_phone,
    )
    conversation = SimpleNamespace(id=uuid.uuid4(), channel=channel)
    db = MagicMock()
    db.add = MagicMock()
    db.flush = AsyncMock()
    return ToolContext(
        db=db,
        ghl=MagicMock(),
        organization=SimpleNamespace(id=uuid.uuid4()),
        location=SimpleNamespace(id=uuid.uuid4()),
        conversation=conversation,
        contact=contact,
        dispatcher=AsyncMock(),
        calendar=MagicMock(),
        obituaries=MagicMock(),
        notifications=AsyncMock(),
    )


def _patch_sms(send_return_value):
    """Patch SmsService so .send returns the given value (sid str, None, or raises)."""
    patcher = patch("app.services.sarah_tools.SmsService")
    mock_cls = patcher.start()
    mock = MagicMock()
    if isinstance(send_return_value, Exception):
        mock.send = AsyncMock(side_effect=send_return_value)
    else:
        mock.send = AsyncMock(return_value=send_return_value)
    mock_cls.return_value = mock
    return patcher, mock


@pytest.mark.asyncio
async def test_happy_path_flips_channel_and_logs_message():
    runner = SarahToolRunner()
    ctx = _ctx()
    patcher, mock_sms = _patch_sms("SM_test_sid_123")
    try:
        result = await runner.run(
            "continue_on_sms",
            json.dumps({
                "phone": "+14035559999",
                "consent_text": "By sharing your number you consent... Reply STOP to opt out.",
            }),
            ctx,
        )
    finally:
        patcher.stop()

    payload = json.loads(result)
    assert payload["ok"] is True
    assert payload["sms_sid"] == "SM_test_sid_123"
    assert payload["channel"] == "sms"

    # Channel flipped on the conversation row.
    assert ctx.conversation.channel == "sms"

    # Handover message row added (db.add called once with a Message).
    assert ctx.db.add.call_count == 1
    msg = ctx.db.add.call_args[0][0]
    assert msg.channel == "sms"
    assert msg.role == "assistant"
    assert "John" in msg.content
    assert "STOP" in msg.content

    # Twilio called with the captured phone.
    mock_sms.send.assert_awaited_once()
    args, _ = mock_sms.send.call_args
    assert args[0] == "+14035559999"


@pytest.mark.asyncio
async def test_raw_phone_matches_e164_contact():
    """Regression: the LLM often passes the raw form the user typed
    ("403-555-9999") while `create_contact` stores E.164 ("+14035559999").
    Both should normalize and compare equal. See session 13 SMS-handoff probe.
    """
    runner = SarahToolRunner()
    ctx = _ctx(contact_phone="+14035559999")
    patcher, mock_sms = _patch_sms("SM_test_raw_match")
    try:
        result = await runner.run(
            "continue_on_sms",
            json.dumps({
                "phone": "403-555-9999",  # raw, not E.164
                "consent_text": "consent line",
            }),
            ctx,
        )
    finally:
        patcher.stop()

    payload = json.loads(result)
    assert payload["ok"] is True, payload
    assert ctx.conversation.channel == "sms"
    # Twilio must be called with the normalized E.164 form.
    args, _ = mock_sms.send.call_args
    assert args[0] == "+14035559999"


@pytest.mark.asyncio
async def test_invalid_phone_returns_invalid_phone_error():
    runner = SarahToolRunner()
    ctx = _ctx(contact_phone="+14035559999")
    patcher, mock_sms = _patch_sms("SM_should_not_send")
    try:
        result = await runner.run(
            "continue_on_sms",
            json.dumps({
                "phone": "not-a-number",
                "consent_text": "consent line",
            }),
            ctx,
        )
    finally:
        patcher.stop()

    payload = json.loads(result)
    assert payload["ok"] is False
    assert payload["error"] == "invalid_phone"
    mock_sms.send.assert_not_called()


@pytest.mark.asyncio
async def test_phone_mismatch_refuses_send():
    runner = SarahToolRunner()
    ctx = _ctx(contact_phone="+14035559999")
    patcher, mock_sms = _patch_sms("SM_should_not_send")
    try:
        result = await runner.run(
            "continue_on_sms",
            json.dumps({
                "phone": "+14039998888",  # different from contact.phone
                "consent_text": "consent line",
            }),
            ctx,
        )
    finally:
        patcher.stop()

    payload = json.loads(result)
    assert payload["ok"] is False
    assert payload["error"] == "phone_mismatch"
    assert ctx.conversation.channel == "webchat"
    mock_sms.send.assert_not_called()
    ctx.db.add.assert_not_called()


@pytest.mark.asyncio
async def test_idempotent_when_already_on_sms():
    runner = SarahToolRunner()
    ctx = _ctx(channel="sms")
    patcher, mock_sms = _patch_sms("SM_should_not_send")
    try:
        result = await runner.run(
            "continue_on_sms",
            json.dumps({
                "phone": "+14035559999",
                "consent_text": "consent line",
            }),
            ctx,
        )
    finally:
        patcher.stop()

    payload = json.loads(result)
    assert payload["ok"] is True
    assert payload["already_on_sms"] is True
    mock_sms.send.assert_not_called()
    ctx.db.add.assert_not_called()


@pytest.mark.asyncio
async def test_missing_consent_rejected_for_casl_audit():
    runner = SarahToolRunner()
    ctx = _ctx()
    patcher, mock_sms = _patch_sms("SM_should_not_send")
    try:
        result = await runner.run(
            "continue_on_sms",
            json.dumps({
                "phone": "+14035559999",
                "consent_text": "   ",
            }),
            ctx,
        )
    finally:
        patcher.stop()

    payload = json.loads(result)
    assert payload["ok"] is False
    assert payload["error"] == "missing_consent"
    assert ctx.conversation.channel == "webchat"
    mock_sms.send.assert_not_called()


@pytest.mark.asyncio
async def test_twilio_not_configured_does_not_flip_channel():
    """If SmsService.send returns None (Twilio not configured), do NOT flip channel —
    flipping silently would break replies because there's no inbound webhook target."""
    runner = SarahToolRunner()
    ctx = _ctx()
    patcher, _ = _patch_sms(None)
    try:
        result = await runner.run(
            "continue_on_sms",
            json.dumps({
                "phone": "+14035559999",
                "consent_text": "consent line",
            }),
            ctx,
        )
    finally:
        patcher.stop()

    payload = json.loads(result)
    assert payload["ok"] is False
    assert payload["error"] == "sms_provider_not_configured"
    assert ctx.conversation.channel == "webchat"
    ctx.db.add.assert_not_called()


@pytest.mark.asyncio
async def test_no_phone_on_contact_rejected():
    runner = SarahToolRunner()
    ctx = _ctx(contact_phone=None)
    patcher, mock_sms = _patch_sms("SM_should_not_send")
    try:
        result = await runner.run(
            "continue_on_sms",
            json.dumps({
                "phone": "+14035559999",
                "consent_text": "consent line",
            }),
            ctx,
        )
    finally:
        patcher.stop()

    payload = json.loads(result)
    assert payload["ok"] is False
    assert payload["error"] == "no_contact_phone"
    mock_sms.send.assert_not_called()


@pytest.mark.asyncio
async def test_twilio_send_raises_returns_error_no_channel_flip():
    runner = SarahToolRunner()
    ctx = _ctx()
    patcher, _ = _patch_sms(RuntimeError("twilio 401"))
    try:
        result = await runner.run(
            "continue_on_sms",
            json.dumps({
                "phone": "+14035559999",
                "consent_text": "consent line",
            }),
            ctx,
        )
    finally:
        patcher.stop()

    payload = json.loads(result)
    assert payload["ok"] is False
    assert payload["error"] == "sms_send_failed"
    assert ctx.conversation.channel == "webchat"
    ctx.db.add.assert_not_called()
