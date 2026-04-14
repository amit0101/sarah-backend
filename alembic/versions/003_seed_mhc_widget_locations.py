"""Seed sarah.locations for org slug mhc — matches webchat-widget LOCATIONS list."""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "003_mhc_widget_locations"
down_revision: Union[str, None] = "002_multi_org_ghl"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Must stay in sync with webchat-widget/src/App.tsx LOCATIONS
_MHC_LOCATIONS: list[tuple[str, str]] = [
    ("bowness", "Bowness"),
    ("crowfoot", "Crowfoot"),
    ("fish_creek", "Fish Creek"),
    ("parkland", "Parkland"),
    ("queens_park", "Queen's Park"),
    ("riverview", "Riverview"),
    ("south_calgary", "South Calgary"),
    ("thornhill", "Thornhill"),
    ("acadia", "Acadia"),
    ("cedar", "Cedar"),
]


def upgrade() -> None:
    conn = op.get_bind()
    for slug, name in _MHC_LOCATIONS:
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
                    :loc_id,
                    :loc_name,
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
            ),
            {"loc_id": slug, "loc_name": name},
        )


def downgrade() -> None:
    conn = op.get_bind()
    for slug, _ in _MHC_LOCATIONS:
        conn.execute(
            sa.text(
                """
                DELETE FROM sarah.locations l
                USING sarah.organizations o
                WHERE l.organization_id = o.id
                  AND o.slug = 'mhc'
                  AND l.id = :loc_id
                """
            ),
            {"loc_id": slug},
        )
