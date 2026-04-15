"""Sarah contact — minimal routing record linked to GHL (scoped per organization)."""

from datetime import datetime
from typing import TYPE_CHECKING, List, Optional
import uuid

from sqlalchemy import DateTime, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base

if TYPE_CHECKING:
    from app.models.conversation import Conversation


class Contact(Base):
    """Minimal contact row for Sarah routing; GHL is master within the org sub-account."""

    __tablename__ = "contacts"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("sarah.organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    ghl_contact_id: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        index=True,
    )
    name: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    phone: Mapped[Optional[str]] = mapped_column(Text, nullable=True, index=True)
    email: Mapped[Optional[str]] = mapped_column(Text, nullable=True, index=True)
    location_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True, index=True)
    last_seen: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        server_default=func.now(),
        onupdate=func.now(),
    )
    conversation_mode: Mapped[str] = mapped_column(
        String(32),
        default="ai",
        server_default="ai",
    )

    conversations: Mapped[List["Conversation"]] = relationship(
        "Conversation",
        back_populates="contact",
        lazy="noload",
    )
