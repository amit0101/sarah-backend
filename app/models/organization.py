"""Tenant / funeral home operator — one GHL sub-account per org (typical)."""

from datetime import datetime
from typing import TYPE_CHECKING, Any, List, Optional
import uuid

from sqlalchemy import DateTime, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base

if TYPE_CHECKING:
    from app.models.location import Location


class Organization(Base):
    """Business customer of Sarah; owns 1..N locations and GHL credentials."""

    __tablename__ = "organizations"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    slug: Mapped[str] = mapped_column(String(128), unique=True, nullable=False, index=True)
    status: Mapped[str] = mapped_column(
        String(32),
        default="active",
        server_default="active",
    )  # active | suspended
    ghl_api_key: Mapped[str] = mapped_column(Text, nullable=False)
    ghl_location_id: Mapped[str] = mapped_column(Text, nullable=False)
    vector_store_id: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
    )  # shared OpenAI Vector Store for org-wide RAG
    twilio_phone_number: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
    )  # E.164 for inbound SMS routing to this org
    # Org-level configuration; feature flags live under config['feature_flags'].
    # See APPOINTMENTS_ARCHITECTURE.md §4.3 (room_calendars_enabled, pre_arrangers_enabled).
    config: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        server_default="{}",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )

    locations: Mapped[List["Location"]] = relationship(
        "Location",
        back_populates="organization",
        cascade="all, delete-orphan",
        lazy="noload",
    )
