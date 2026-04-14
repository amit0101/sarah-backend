"""Initial sarah schema: contacts, conversations, messages, locations, prompts."""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "001_initial"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS sarah")

    op.create_table(
        "locations",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("ghl_location_id", sa.Text(), nullable=True),
        sa.Column("vector_store_id", sa.Text(), nullable=True),
        sa.Column("calendar_id", sa.Text(), nullable=True),
        sa.Column("ghl_calendar_id", sa.Text(), nullable=True),
        sa.Column("availability_calendar_id", sa.Text(), nullable=True),
        sa.Column("escalation_contacts", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("config", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        schema="sarah",
    )

    op.create_table(
        "contacts",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("ghl_contact_id", sa.Text(), nullable=True),
        sa.Column("name", sa.Text(), nullable=True),
        sa.Column("phone", sa.Text(), nullable=True),
        sa.Column("email", sa.Text(), nullable=True),
        sa.Column("location_id", sa.Text(), nullable=True),
        sa.Column(
            "last_seen",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=True,
        ),
        sa.Column("conversation_mode", sa.String(length=32), server_default="ai", nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("ghl_contact_id"),
        schema="sarah",
    )
    op.create_index("ix_sarah_contacts_phone", "contacts", ["phone"], unique=False, schema="sarah")
    op.create_index("ix_sarah_contacts_email", "contacts", ["email"], unique=False, schema="sarah")
    op.create_index(
        "ix_sarah_contacts_location_id", "contacts", ["location_id"], unique=False, schema="sarah"
    )

    op.create_table(
        "conversations",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("contact_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("location_id", sa.Text(), nullable=False),
        sa.Column("channel", sa.String(length=32), nullable=False),
        sa.Column("mode", sa.String(length=16), server_default="ai", nullable=False),
        sa.Column("assigned_staff_id", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=16), server_default="active", nullable=False),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("last_message_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("openai_response_id", sa.Text(), nullable=True),
        sa.Column("active_path", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["contact_id"],
            ["sarah.contacts.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        schema="sarah",
    )
    op.create_index(
        "ix_sarah_conversations_contact_id",
        "conversations",
        ["contact_id"],
        unique=False,
        schema="sarah",
    )
    op.create_index(
        "ix_sarah_conversations_location_id",
        "conversations",
        ["location_id"],
        unique=False,
        schema="sarah",
    )

    op.create_table(
        "messages",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("channel", sa.String(length=32), nullable=False),
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
        "ix_sarah_messages_conversation_id",
        "messages",
        ["conversation_id"],
        unique=False,
        schema="sarah",
    )

    op.create_table(
        "prompts",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("location_id", sa.Text(), nullable=True),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("global_instructions", sa.Text(), nullable=True),
        sa.Column("path_instructions", sa.Text(), nullable=True),
        sa.Column("extra_config", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["location_id"],
            ["sarah.locations.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("location_id", "path", name="uq_prompt_location_path"),
        schema="sarah",
    )
    op.create_index("ix_sarah_prompts_location_id", "prompts", ["location_id"], unique=False, schema="sarah")
    op.create_index("ix_sarah_prompts_path", "prompts", ["path"], unique=False, schema="sarah")


def downgrade() -> None:
    op.drop_table("prompts", schema="sarah")
    op.drop_table("messages", schema="sarah")
    op.drop_table("conversations", schema="sarah")
    op.drop_table("contacts", schema="sarah")
    op.drop_table("locations", schema="sarah")
    op.execute("DROP SCHEMA IF EXISTS sarah CASCADE")
