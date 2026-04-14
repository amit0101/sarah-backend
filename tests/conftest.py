"""Shared pytest fixtures — test DB, mocks, seed data."""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timezone
from typing import AsyncGenerator, Dict, Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker

from app.models.base import Base
from app.models.organization import Organization
from app.models.location import Location
from app.models.contact import Contact
from app.models.conversation import Conversation
from app.models.message import Message


# ---------------------------------------------------------------------------
# Event loop
# ---------------------------------------------------------------------------
@pytest.fixture(scope="session")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


# ---------------------------------------------------------------------------
# In-memory SQLite for unit tests (no Postgres needed)
# ---------------------------------------------------------------------------
TEST_DB_URL = "sqlite+aiosqlite:///:memory:"


@pytest_asyncio.fixture
async def db_session() -> AsyncGenerator[AsyncSession, None]:
    engine = create_async_engine(TEST_DB_URL, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as session:
        yield session
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


# ---------------------------------------------------------------------------
# Seed data
# ---------------------------------------------------------------------------
TEST_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
TEST_CONTACT_ID = uuid.UUID("00000000-0000-0000-0000-000000000010")
TEST_CONV_ID = uuid.UUID("00000000-0000-0000-0000-000000000100")


@pytest_asyncio.fixture
async def test_org(db_session: AsyncSession) -> Organization:
    org = Organization(
        id=TEST_ORG_ID,
        name="Test Org",
        slug="testorg",
        status="active",
        ghl_api_key="test-ghl-key",
        ghl_location_id="ghl-loc-123",
    )
    db_session.add(org)
    await db_session.flush()
    return org


@pytest_asyncio.fixture
async def test_location(db_session: AsyncSession, test_org: Organization) -> Location:
    loc = Location(
        organization_id=test_org.id,
        id="main_office",
        name="Main Office",
        vector_store_id="vs_test123",
        config={
            "business_hours": {
                "mon": {"open": "08:00", "close": "17:00"},
                "tue": {"open": "08:00", "close": "17:00"},
                "wed": {"open": "08:00", "close": "17:00"},
                "thu": {"open": "08:00", "close": "17:00"},
                "fri": {"open": "08:00", "close": "17:00"},
            },
            "timezone": "America/Edmonton",
            "tag_map": {"qualified": "sarah_qualified", "hot_lead": "sarah_hot_lead"},
            "pipeline_map": {
                "pre_need": {
                    "pipeline_id": "pipe-pre",
                    "stages": {"new_lead": "stage-new"},
                },
                "at_need": {
                    "pipeline_id": "pipe-at",
                    "stages": {"new_lead": "stage-at-new"},
                },
            },
        },
        escalation_contacts=[
            {"name": "Jane Director", "role": "director", "phone": "+14035551234", "email": "jane@test.com"},
            {"name": "Bob Staff", "role": "staff", "phone": "+14035555678"},
        ],
    )
    db_session.add(loc)
    await db_session.flush()
    return loc


@pytest_asyncio.fixture
async def test_contact(db_session: AsyncSession, test_org: Organization, test_location: Location) -> Contact:
    contact = Contact(
        id=TEST_CONTACT_ID,
        organization_id=test_org.id,
        location_id=test_location.id,
        name="John Doe",
        phone="+14035559999",
        email="john@example.com",
        ghl_contact_id="ghl-c-999",
    )
    db_session.add(contact)
    await db_session.flush()
    return contact


@pytest_asyncio.fixture
async def test_conversation(
    db_session: AsyncSession,
    test_org: Organization,
    test_location: Location,
    test_contact: Contact,
) -> Conversation:
    conv = Conversation(
        id=TEST_CONV_ID,
        organization_id=test_org.id,
        contact_id=test_contact.id,
        location_id=test_location.id,
        channel="webchat",
        mode="ai",
        status="active",
        active_path="general",
        started_at=datetime.now(timezone.utc),
    )
    db_session.add(conv)
    await db_session.flush()
    return conv


# ---------------------------------------------------------------------------
# Mocks
# ---------------------------------------------------------------------------
@pytest.fixture
def mock_ghl() -> MagicMock:
    """Mock GHL client — all API calls return success."""
    client = MagicMock()
    client.request = AsyncMock(return_value={"ok": True})
    return client


@pytest.fixture
def mock_openai() -> MagicMock:
    """Mock OpenAI AsyncOpenAI client."""
    client = MagicMock()
    # Mock a simple response with text output
    mock_resp = MagicMock()
    mock_resp.id = "resp_test123"
    mock_resp.output = [
        MagicMock(
            type="message",
            content=[
                MagicMock(type="output_text", text="Hello, I'm Sarah. How can I help you today?")
            ],
        )
    ]
    client.responses.create = AsyncMock(return_value=mock_resp)
    return client


@pytest.fixture
def mock_dispatcher() -> AsyncMock:
    """Mock webhook dispatcher — records emitted events."""
    dispatcher = AsyncMock()
    dispatcher.emitted_events = []

    async def record_emit(event_type, payload):
        dispatcher.emitted_events.append({"type": event_type, "payload": payload})

    dispatcher.emit = AsyncMock(side_effect=record_emit)
    return dispatcher


@pytest.fixture
def mock_notifications() -> AsyncMock:
    """Mock notification service."""
    svc = AsyncMock()
    svc.notify_escalation = AsyncMock()
    svc.notify_hot_lead = AsyncMock()
    return svc
