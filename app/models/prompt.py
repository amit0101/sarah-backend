"""Config-driven prompts per path; optional location override within org."""

from datetime import datetime
from typing import Any, Optional
import uuid

from sqlalchemy import DateTime, ForeignKey, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class Prompt(Base):
    """Org-scoped prompts; location_id NULL = org-wide default for path."""

    __tablename__ = "prompts"

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
    )  # location_id validated in app against sarah.locations when set
    location_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True, index=True)
    path: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    global_instructions: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    path_instructions: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    extra_config: Mapped[Optional[Any]] = mapped_column(JSONB, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )
