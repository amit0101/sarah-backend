"""Conversation state for webchat, SMS, and campaign replies."""

from datetime import datetime
from typing import TYPE_CHECKING, List, Optional
import uuid

from sqlalchemy import DateTime, ForeignKey, ForeignKeyConstraint, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base

if TYPE_CHECKING:
    from app.models.contact import Contact
    from app.models.message import Message


class Conversation(Base):
    """Active or recent conversation; mode drives AI vs staff."""

    __tablename__ = "conversations"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "location_id"],
            ["sarah.locations.organization_id", "sarah.locations.id"],
            ondelete="RESTRICT",
        ),
        {"schema": "sarah"},
    )

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
    contact_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("sarah.contacts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    location_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    channel: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
    )
    mode: Mapped[str] = mapped_column(
        String(16),
        default="ai",
        server_default="ai",
    )
    assigned_staff_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(
        String(16),
        default="active",
        server_default="active",
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
    last_message_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    openai_response_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    active_path: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
    )

    contact: Mapped["Contact"] = relationship("Contact", back_populates="conversations", lazy="noload")
    messages: Mapped[List["Message"]] = relationship(
        "Message",
        back_populates="conversation",
        primaryjoin="Conversation.id == Message.conversation_id",
        foreign_keys="Message.conversation_id",
        order_by="Message.created_at",
        lazy="noload",
    )
