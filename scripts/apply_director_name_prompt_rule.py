"""Inject the DIRECTOR NAME prompt rule into existing sarah.prompts rows.

The rule lives in the GLOBAL_BRAND code-level fallback and the
seed_v31_prompts.sql source of truth. This script splices it into all live
sarah.prompts.global_instructions rows that contain the existing CALENDAR
SLOTS block, so production picks up the change without a full re-seed.

Idempotent: re-running won't duplicate the rule (we check before inserting).

USAGE
  cd backend
  python -m scripts.apply_director_name_prompt_rule --dry-run
  python -m scripts.apply_director_name_prompt_rule
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from sqlalchemy import select, text

from app.database.session import async_session_factory


# Splice anchor: the line that ends the CALENDAR SLOTS block (we insert AFTER it).
ANCHOR = (
    "Even when the slot IS today or tomorrow, write the explicit weekday + date: "
    '"today, Wednesday April 23rd at 10:00 AM" is acceptable; "today at 10 AM" is NOT. '
    "The user may be reading the chat hours later — relative words like \"today\" "
    "become wrong."
)

DIRECTOR_NAME_RULE = (
    "\n\nDIRECTOR NAME — surface the primary on every slot. When check_calendar "
    "returns slots with a non-null \"primary\" field (the assigned director's name), "
    "you MUST name that director in your reply. Required format per slot: "
    '"Wednesday, April 29th at 9:00 AM with Aaron B." When the same director covers '
    "multiple slots in the day (the common case), you may name them once at the top "
    "and list times below — for example: \"Aaron B. is available at the following "
    "times on Wednesday, April 29th: 9:00 AM, 12:15 PM, and 3:00 PM.\" Never present "
    "slots without naming the primary if the slot data includes one. The director's "
    "name is essential context — families want to know who they'll be meeting."
)

UNIQ_MARKER = "DIRECTOR NAME — surface the primary on every slot."


async def main(dry: bool) -> int:
    print(f"DRY RUN: {dry}")
    async with async_session_factory() as db:
        rows = (
            await db.execute(
                text(
                    "SELECT id, organization_id, location_id, path, "
                    "global_instructions FROM sarah.prompts "
                    "WHERE global_instructions IS NOT NULL"
                )
            )
        ).all()
        print(f"\n{len(rows)} sarah.prompts rows with global_instructions found")

        updates = 0
        skipped_already = 0
        skipped_no_anchor = 0
        for r in rows:
            pid, org_id, loc_id, path, gi = r
            if UNIQ_MARKER in (gi or ""):
                skipped_already += 1
                continue
            if ANCHOR not in (gi or ""):
                skipped_no_anchor += 1
                print(f"  ! skip id={pid} path={path} — anchor not found")
                continue
            new_gi = (gi or "").replace(ANCHOR, ANCHOR + DIRECTOR_NAME_RULE, 1)
            if dry:
                print(f"  DRY: would patch id={pid} path={path} loc={loc_id}")
            else:
                await db.execute(
                    text("UPDATE sarah.prompts SET global_instructions = :gi WHERE id = :id"),
                    {"gi": new_gi, "id": pid},
                )
                print(f"  ✓ patched id={pid} path={path} loc={loc_id}")
            updates += 1

        if not dry:
            await db.commit()

        print(
            f"\nSummary: patch={updates}  already-applied={skipped_already}  "
            f"no-anchor={skipped_no_anchor}"
        )
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    sys.exit(asyncio.run(main(args.dry_run)))
