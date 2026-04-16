"""openai_response_logs — raw Responses API payloads per turn."""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "006_openai_response_logs"
down_revision: Union[str, None] = "005_seed_default_prompts"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "openai_response_logs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("turn_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("round_index", sa.Integer(), nullable=False),
        sa.Column("openai_response_id", sa.String(length=128), nullable=True),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["sarah.conversations.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        schema="sarah",
    )
    op.create_index(
        "ix_sarah_openai_response_logs_conversation_id",
        "openai_response_logs",
        ["conversation_id"],
        unique=False,
        schema="sarah",
    )
    op.create_index(
        "ix_sarah_openai_response_logs_turn_id",
        "openai_response_logs",
        ["turn_id"],
        unique=False,
        schema="sarah",
    )


def downgrade() -> None:
    op.drop_index(
        "ix_sarah_openai_response_logs_turn_id",
        table_name="openai_response_logs",
        schema="sarah",
    )
    op.drop_index(
        "ix_sarah_openai_response_logs_conversation_id",
        table_name="openai_response_logs",
        schema="sarah",
    )
    op.drop_table("openai_response_logs", schema="sarah")
