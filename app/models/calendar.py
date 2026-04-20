"""sarah.calendars — typed calendar inventory.

See `sarah-podium-plan/APPOINTMENTS_ARCHITECTURE.md` §4.1 for the full design,
and §3.0 for the read-convention gotcha (events-as-availability vs events-as-busy).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any, Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

if TYPE_CHECKING:
    pass


# Allowed enum values — kept in sync with migration 007 CHECK constraints.
CALENDAR_KINDS = ("primaries_roster", "primary", "pre_arranger", "venue")
READ_CONVENTIONS = ("busy", "availability")


class Calendar(Base):
    __tablename__ = "calendars"
    __table_args__ = (
        UniqueConstraint(
            "organization_id", "google_id", name="uq_sarah_calendars_org_google_id"
        ),
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
    name: Mapped[str] = mapped_column(Text, nullable=False)
    google_id: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    read_convention: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        server_default="busy",
    )
    active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default="true",
    )
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata",
        JSONB,
        nullable=False,
        server_default="{}",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
