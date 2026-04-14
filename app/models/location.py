"""Per-location configuration within an organization."""

from typing import TYPE_CHECKING, Any, List, Optional
import uuid

from sqlalchemy import ForeignKeyConstraint, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base

if TYPE_CHECKING:
    from app.models.organization import Organization


class Location(Base):
    """
    Physical or branded site within an organization.
    Composite PK (organization_id, id) where `id` is the location slug (e.g. bowness).
    """

    __tablename__ = "locations"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id"],
            ["sarah.organizations.id"],
            ondelete="CASCADE",
        ),
        UniqueConstraint("organization_id", "id", name="uq_locations_org_slug"),
        {"schema": "sarah"},
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
    )
    id: Mapped[str] = mapped_column(Text, primary_key=True)  # slug within org
    name: Mapped[str] = mapped_column(Text, nullable=False)
    ghl_location_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    vector_store_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    calendar_id: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
    )
    ghl_calendar_id: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
    )
    availability_calendar_id: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
    )
    escalation_contacts: Mapped[Optional[Any]] = mapped_column(JSONB, nullable=True)
    config: Mapped[Optional[Any]] = mapped_column(JSONB, nullable=True)

    organization: Mapped["Organization"] = relationship(
        "Organization",
        back_populates="locations",
    )
