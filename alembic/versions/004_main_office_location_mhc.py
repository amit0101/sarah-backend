"""Add main_office location for mhc (matches webchat-widget first option)."""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "004_main_office_mhc"
down_revision: Union[str, None] = "003_mhc_widget_locations"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    conn.execute(
        sa.text(
            """
            INSERT INTO sarah.locations (
                organization_id,
                id,
                name,
                ghl_location_id,
                vector_store_id,
                calendar_id,
                ghl_calendar_id,
                availability_calendar_id,
                escalation_contacts,
                config
            )
            SELECT
                o.id,
                'main_office',
                'Main office',
                NULL,
                NULL,
                NULL,
                NULL,
                NULL,
                NULL,
                '{}'::jsonb
            FROM sarah.organizations o
            WHERE o.slug = 'mhc'
            ON CONFLICT (organization_id, id) DO NOTHING
            """
        )
    )


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(
        sa.text(
            """
            DELETE FROM sarah.locations l
            USING sarah.organizations o
            WHERE l.organization_id = o.id
              AND o.slug = 'mhc'
              AND l.id = 'main_office'
            """
        )
    )
