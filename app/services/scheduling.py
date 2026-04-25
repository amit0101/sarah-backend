"""Scheduling helpers — location regions, counselor routing, time slots."""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

NORTH_LOCATIONS = frozenset({
    "chapel_of_the_bells", "crowfoot", "heritage",
    "airdrie", "cochrane", "calgary_crematorium",
})
SOUTH_LOCATIONS = frozenset({
    "park_memorial", "fish_creek", "deerfoot_south", "eastside",
})

COUNSELOR_REGION: Dict[str, str] = {
    "Aaron B.": "north",
    "Ashley R.": "north",
    "Stephanie Ho.": "north",
    "Terra S.": "north",
    "Jillian G.": "south",
    "McKenzi S.": "south",
    "Sharon K.": "south",
    "Stephanie Hu.": "south",
}

STANDARD_SLOTS = ["09:00", "12:15", "15:00"]

# Slots where the director MUST match the location's territory tag (N or S).
# Per `mh_venues_brief.html` §02 (2026-04-25): only the 9am at-need slot is
# strict; 12:15 and 15:00 prefer same-location continuity but accept any
# available director if continuity isn't possible.
STRICT_TERRITORY_SLOTS = frozenset({"09:00"})

# Priority directors per (region, intent) tuple — the booking algorithm tries
# these names first when the slot allows them. Currently only the brief's
# rule 8: "Aaron Beck takes North at-need first when available." When the
# full roster lands from Jeff, expand keys here (e.g. ("south", "at_need")).
PRIORITY_DIRECTORS: Dict[tuple[str, str], List[str]] = {
    ("north", "at_need"): ["Aaron B."],
}


def location_region(location_slug: str) -> str:
    if location_slug in NORTH_LOCATIONS:
        return "north"
    if location_slug in SOUTH_LOCATIONS:
        return "south"
    return "unknown"


def available_slots(location_slug: str) -> List[str]:
    """Return the fixed at-need slot grid for this location.

    Per the venue brief, every site uses the same 09:00 / 12:15 / 15:00 grid
    (including Airdrie and Cochrane — the previous "REDUCED_SLOTS" model was
    based on a now-superseded guess). The function still accepts a location
    slug for forward-compat (e.g. if ops later cuts back hours at one site).
    """
    del location_slug  # unused — preserved for callers
    return STANDARD_SLOTS


def is_strict_territory_slot(hhmm: str) -> bool:
    """True iff a director's territory tag must match the location's territory."""
    return hhmm in STRICT_TERRITORY_SLOTS


def apply_priority_order(names: List[str], priority: List[str]) -> List[str]:
    """Return `names` with `priority`-listed names moved to the front.

    Order within each group is preserved; names absent from `priority` keep
    their original relative order, names in `priority` keep theirs.
    """
    head = [n for n in priority if n in names]
    tail = [n for n in names if n not in head]
    return head + tail


def parse_counselor_from_event(summary: str) -> Optional[str]:
    """Extract counselor name from a Primaries calendar event summary.

    Expected format: 'Primaries - Ashley R. - 8:45 AM to 5:15 PM'
    Returns: 'Ashley R.' or None.
    """
    m = re.match(r"Primaries\s*-\s*(.+?)\s*-\s*\d", summary or "")
    return m.group(1).strip() if m else None


def filter_counselors_for_region(
    counselor_names: List[str], region: str,
) -> List[str]:
    """Return only counselors belonging to the given region."""
    matched = []
    for name in counselor_names:
        mapped = COUNSELOR_REGION.get(name)
        if mapped == region:
            matched.append(name)
        elif mapped is None:
            matched.append(name)
    return matched


def build_availability_response(
    date_str: str,
    location_slug: str,
    location_name: str,
    events: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Build the structured response for check_calendar."""
    region = location_region(location_slug)
    slots = available_slots(location_slug)

    all_counselors = []
    for ev in events:
        name = parse_counselor_from_event(ev.get("summary", ""))
        if name and name not in all_counselors:
            all_counselors.append(name)

    region_counselors = filter_counselors_for_region(all_counselors, region)

    if not region_counselors:
        return {
            "ok": True,
            "date": date_str,
            "location": location_name,
            "region": region,
            "available": False,
            "reason": f"No {region} counselors on shift for {date_str}",
            "counselors_on_shift": [],
            "available_slots": [],
        }

    return {
        "ok": True,
        "date": date_str,
        "location": location_name,
        "region": region,
        "available": True,
        "counselors_on_shift": region_counselors,
        "available_slots": slots,
    }
