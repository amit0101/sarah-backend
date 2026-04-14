"""Multi-organization + per-org GHL credentials; composite location key."""

from __future__ import annotations

import os
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "002_multi_org_ghl"
down_revision: Union[str, None] = "001_initial"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "organizations",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("slug", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), server_default="active", nullable=False),
        sa.Column("ghl_api_key", sa.Text(), nullable=False),
        sa.Column("ghl_location_id", sa.Text(), nullable=False),
        sa.Column("twilio_phone_number", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("slug"),
        schema="sarah",
    )
    op.create_index("ix_sarah_organizations_slug", "organizations", ["slug"], unique=False, schema="sarah")

    slug = os.environ.get("DEFAULT_ORGANIZATION_SLUG", "mhc")
    name = os.environ.get("DEFAULT_ORGANIZATION_NAME", "McInnis & Holloway")
    ghl_key = os.environ.get("GHL_API_KEY", "placeholder-update-in-admin")
    ghl_loc = os.environ.get("GHL_LOCATION_ID", "placeholder-update-in-admin")
    conn = op.get_bind()
    conn.execute(
        sa.text(
            """
            INSERT INTO sarah.organizations (id, name, slug, status, ghl_api_key, ghl_location_id)
            VALUES (gen_random_uuid(), :name, :slug, 'active', :ghl_key, :ghl_loc)
            """
        ),
        {"name": name, "slug": slug, "ghl_key": ghl_key, "ghl_loc": ghl_loc},
    )

    op.execute(
        sa.text("ALTER TABLE sarah.prompts DROP CONSTRAINT IF EXISTS prompts_location_id_fkey")
    )

    op.add_column(
        "locations",
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=True),
        schema="sarah",
    )
    op.create_foreign_key(
        "fk_locations_organization",
        "locations",
        "organizations",
        ["organization_id"],
        ["id"],
        source_schema="sarah",
        referent_schema="sarah",
        ondelete="CASCADE",
    )
    conn.execute(
        sa.text(
            """
            UPDATE sarah.locations l
            SET organization_id = (SELECT id FROM sarah.organizations ORDER BY created_at LIMIT 1)
            """
        )
    )
    op.alter_column("locations", "organization_id", nullable=False, schema="sarah")

    op.drop_constraint("locations_pkey", "locations", schema="sarah", type_="primary")
    op.create_primary_key("locations_pkey", "locations", ["organization_id", "id"], schema="sarah")

    op.add_column(
        "conversations",
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=True),
        schema="sarah",
    )
    conn.execute(
        sa.text(
            """
            UPDATE sarah.conversations c
            SET organization_id = l.organization_id
            FROM sarah.locations l
            WHERE c.location_id = l.id AND l.organization_id IS NOT NULL
            """
        )
    )
    op.alter_column("conversations", "organization_id", nullable=False, schema="sarah")
    op.create_foreign_key(
        "fk_conversations_organization",
        "conversations",
        "organizations",
        ["organization_id"],
        ["id"],
        source_schema="sarah",
        referent_schema="sarah",
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_conversations_location",
        "conversations",
        "locations",
        ["organization_id", "location_id"],
        ["organization_id", "id"],
        source_schema="sarah",
        referent_schema="sarah",
        ondelete="RESTRICT",
    )

    op.add_column(
        "contacts",
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=True),
        schema="sarah",
    )
    conn.execute(
        sa.text(
            """
            UPDATE sarah.contacts c
            SET organization_id = l.organization_id
            FROM sarah.locations l
            WHERE c.location_id = l.id AND l.organization_id IS NOT NULL
            """
        )
    )
    conn.execute(
        sa.text(
            """
            UPDATE sarah.contacts
            SET organization_id = (SELECT id FROM sarah.organizations ORDER BY created_at LIMIT 1)
            WHERE organization_id IS NULL
            """
        )
    )
    op.alter_column("contacts", "organization_id", nullable=False, schema="sarah")
    op.create_foreign_key(
        "fk_contacts_organization",
        "contacts",
        "organizations",
        ["organization_id"],
        ["id"],
        source_schema="sarah",
        referent_schema="sarah",
        ondelete="CASCADE",
    )

    op.execute(
        sa.text("ALTER TABLE sarah.contacts DROP CONSTRAINT IF EXISTS contacts_ghl_contact_id_key")
    )
    op.create_index(
        "ix_sarah_contacts_org_ghl_unique",
        "contacts",
        ["organization_id", "ghl_contact_id"],
        unique=True,
        schema="sarah",
        postgresql_where=sa.text("ghl_contact_id IS NOT NULL"),
    )

    op.add_column(
        "prompts",
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=True),
        schema="sarah",
    )
    conn.execute(
        sa.text(
            """
            UPDATE sarah.prompts p
            SET organization_id = l.organization_id
            FROM sarah.locations l
            WHERE p.location_id = l.id AND l.organization_id IS NOT NULL
            """
        )
    )
    conn.execute(
        sa.text(
            """
            UPDATE sarah.prompts
            SET organization_id = (SELECT id FROM sarah.organizations ORDER BY created_at LIMIT 1)
            WHERE organization_id IS NULL
            """
        )
    )
    op.alter_column("prompts", "organization_id", nullable=False, schema="sarah")
    op.create_foreign_key(
        "fk_prompts_organization",
        "prompts",
        "organizations",
        ["organization_id"],
        ["id"],
        source_schema="sarah",
        referent_schema="sarah",
        ondelete="CASCADE",
    )
    op.drop_constraint("uq_prompt_location_path", "prompts", schema="sarah", type_="unique")
    op.create_index(
        "uq_prompts_org_path_global",
        "prompts",
        ["organization_id", "path"],
        unique=True,
        schema="sarah",
        postgresql_where=sa.text("location_id IS NULL"),
    )
    op.create_index(
        "uq_prompts_org_loc_path",
        "prompts",
        ["organization_id", "location_id", "path"],
        unique=True,
        schema="sarah",
        postgresql_where=sa.text("location_id IS NOT NULL"),
    )

    op.create_index(
        "ix_sarah_conversations_org_loc",
        "conversations",
        ["organization_id", "location_id"],
        unique=False,
        schema="sarah",
    )


def downgrade() -> None:
    raise NotImplementedError("downgrade not supported for multi-org migration")
