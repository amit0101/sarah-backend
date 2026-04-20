"""calendars + appointments + org feature_flags.

Implements the schema described in `sarah-podium-plan/APPOINTMENTS_ARCHITECTURE.md`
sections 4.1, 4.2, 4.3:

  - sarah.organizations.config JSONB         (feature flags live under config.feature_flags.*)
  - sarah.calendars                          (typed calendar inventory; primaries_roster | primary | pre_arranger | venue)
  - sarah.appointments                       (canonical record of any booked appointment)

The two read conventions (events-as-availability vs events-as-busy) are encoded
on sarah.calendars.read_convention so the booking code can dispatch declaratively
without per-calendar special-casing. See APPOINTMENTS_ARCHITECTURE.md §3.0 for
the gotcha this prevents.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "007_calendars_appointments"
down_revision: Union[str, None] = "006_openai_response_logs"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


CALENDAR_KINDS = ("primaries_roster", "primary", "pre_arranger", "venue")
READ_CONVENTIONS = ("busy", "availability")
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


def _check_in(column: str, allowed: tuple[str, ...]) -> str:
    """Build a SQL CHECK clause: column IN ('a','b',...)."""
    quoted = ", ".join(f"'{v}'" for v in allowed)
    return f"{column} IN ({quoted})"


def upgrade() -> None:
    # ── 1. organizations.config (JSONB, default '{}') ──────────────────────
    op.add_column(
        "organizations",
        sa.Column(
            "config",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        schema="sarah",
    )

    # ── 2. sarah.calendars ─────────────────────────────────────────────────
    op.create_table(
        "calendars",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("google_id", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column(
            "read_convention",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'busy'"),
        ),
        sa.Column(
            "active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column(
            "metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["sarah.organizations.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "organization_id", "google_id", name="uq_sarah_calendars_org_google_id"
        ),
        sa.CheckConstraint(
            _check_in("kind", CALENDAR_KINDS),
            name="ck_sarah_calendars_kind",
        ),
        sa.CheckConstraint(
            _check_in("read_convention", READ_CONVENTIONS),
            name="ck_sarah_calendars_read_convention",
        ),
        schema="sarah",
    )
    op.create_index(
        "ix_sarah_calendars_org_kind_active",
        "calendars",
        ["organization_id", "kind", "active"],
        unique=False,
        schema="sarah",
    )

    # ── 3. sarah.appointments ──────────────────────────────────────────────
    op.create_table(
        "appointments",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("contact_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("service_type", sa.Text(), nullable=False),
        sa.Column("intent", sa.Text(), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("primary_cal_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("venue_cal_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("google_event_id", sa.Text(), nullable=True),
        sa.Column("google_venue_event_id", sa.Text(), nullable=True),
        sa.Column("ghl_appointment_id", sa.Text(), nullable=True),
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'scheduled'"),
        ),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
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
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["sarah.organizations.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["contact_id"],
            ["sarah.contacts.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["sarah.conversations.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["primary_cal_id"],
            ["sarah.calendars.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["venue_cal_id"],
            ["sarah.calendars.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            _check_in("intent", APPOINTMENT_INTENTS),
            name="ck_sarah_appointments_intent",
        ),
        sa.CheckConstraint(
            _check_in("service_type", APPOINTMENT_SERVICE_TYPES),
            name="ck_sarah_appointments_service_type",
        ),
        sa.CheckConstraint(
            _check_in("status", APPOINTMENT_STATUSES),
            name="ck_sarah_appointments_status",
        ),
        sa.CheckConstraint(
            _check_in("created_by", APPOINTMENT_CREATED_BY),
            name="ck_sarah_appointments_created_by",
        ),
        sa.CheckConstraint("ends_at > starts_at", name="ck_sarah_appointments_window"),
        schema="sarah",
    )
    op.create_index(
        "ix_sarah_appointments_org_starts_at",
        "appointments",
        ["organization_id", "starts_at"],
        unique=False,
        schema="sarah",
    )
    op.create_index(
        "ix_sarah_appointments_contact_id",
        "appointments",
        ["contact_id"],
        unique=False,
        schema="sarah",
    )
    op.create_index(
        "ix_sarah_appointments_conversation_id",
        "appointments",
        ["conversation_id"],
        unique=False,
        schema="sarah",
    )

    # ── 4. Default feature flags on every existing org ─────────────────────
    # Both default OFF so behaviour is unchanged until calendars are seeded.
    op.execute(
        """
        UPDATE sarah.organizations
        SET config = jsonb_set(
            jsonb_set(
                COALESCE(config, '{}'::jsonb),
                '{feature_flags,room_calendars_enabled}',
                'false'::jsonb,
                true
            ),
            '{feature_flags,pre_arrangers_enabled}',
            'false'::jsonb,
            true
        )
        WHERE config IS NULL
           OR NOT (config ? 'feature_flags')
           OR NOT (config -> 'feature_flags' ? 'room_calendars_enabled')
           OR NOT (config -> 'feature_flags' ? 'pre_arrangers_enabled');
        """
    )


def downgrade() -> None:
    op.drop_index(
        "ix_sarah_appointments_conversation_id",
        table_name="appointments",
        schema="sarah",
    )
    op.drop_index(
        "ix_sarah_appointments_contact_id",
        table_name="appointments",
        schema="sarah",
    )
    op.drop_index(
        "ix_sarah_appointments_org_starts_at",
        table_name="appointments",
        schema="sarah",
    )
    op.drop_table("appointments", schema="sarah")

    op.drop_index(
        "ix_sarah_calendars_org_kind_active",
        table_name="calendars",
        schema="sarah",
    )
    op.drop_table("calendars", schema="sarah")

    op.drop_column("organizations", "config", schema="sarah")
