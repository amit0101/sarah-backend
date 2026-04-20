"""sarah.appointments — canonical record of every booked appointment.

See `sarah-podium-plan/APPOINTMENTS_ARCHITECTURE.md` §4.2.

primary_cal_id is the Primary OR Pre-arranger calendar that holds the event of
record. venue_cal_id is set only for at-need appointments when
feature_flags.room_calendars_enabled = true.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


APPOINTMENT_INTENTS = ("at_need", "pre_need")
APPOINTMENT_SERVICE_TYPES = (
    "pre_need_consult",
    "arrangement_conf",
    "visitation",
    "service",
    "reception",
    "transport",
)
APPOINTMENT_STATUSES = (
    "scheduled",
    "rescheduled",
    "cancelled",
    "no_show",
    "completed",
)
APPOINTMENT_CREATED_BY = ("sarah", "staff", "ghl", "self_service")


class Appointment(Base):
    __tablename__ = "appointments"
    __table_args__ = (
        CheckConstraint("ends_at > starts_at", name="ck_sarah_appointments_window"),
        {"schema": "sarah"},
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("sarah.organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    contact_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("sarah.contacts.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    conversation_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("sarah.conversations.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    service_type: Mapped[str] = mapped_column(Text, nullable=False)
    intent: Mapped[str] = mapped_column(Text, nullable=False)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    primary_cal_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("sarah.calendars.id", ondelete="SET NULL"),
        nullable=True,
    )
    venue_cal_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("sarah.calendars.id", ondelete="SET NULL"),
        nullable=True,
    )
    google_event_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    google_venue_event_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    ghl_appointment_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        server_default="scheduled",
    )
    created_by: Mapped[str] = mapped_column(Text, nullable=False)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
