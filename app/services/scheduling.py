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

REDUCED_HOURS_LOCATIONS = frozenset({"airdrie", "cochrane"})
REDUCED_SLOTS = ["10:00", "12:15"]


def location_region(location_slug: str) -> str:
    if location_slug in NORTH_LOCATIONS:
        return "north"
    if location_slug in SOUTH_LOCATIONS:
        return "south"
    return "unknown"


def available_slots(location_slug: str) -> List[str]:
    if location_slug in REDUCED_HOURS_LOCATIONS:
        return REDUCED_SLOTS
    return STANDARD_SLOTS


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
