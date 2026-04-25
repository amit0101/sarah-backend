"""Pure-function tests for app.services.scheduling.

Covers the brief-aligned helpers introduced for the venue routing rules
(see `mh_venues_brief.html` 2026-04-25):

  - retired REDUCED_SLOTS — every site uses the standard 3-slot grid
  - is_strict_territory_slot — 9am only
  - apply_priority_order — Aaron-Beck-first style ordering
"""

from __future__ import annotations

from app.services.scheduling import (
    PRIORITY_DIRECTORS,
    STANDARD_SLOTS,
    apply_priority_order,
    available_slots,
    is_strict_territory_slot,
    location_region,
)


def test_available_slots_is_uniform_across_all_sites():
    """Brief: Airdrie + Cochrane use the same 3 slots as everyone else.

    Regression — earlier code path returned a 2-slot REDUCED_SLOTS list
    for these sites, based on a now-superseded guess.
    """
    for slug in (
        "park_memorial",
        "airdrie",
        "cochrane",
        "chapel_of_the_bells",
        "calgary_crematorium",
        "unknown_slug_does_not_break",
    ):
        assert available_slots(slug) == STANDARD_SLOTS


def test_strict_territory_slot_only_for_9am():
    assert is_strict_territory_slot("09:00") is True
    assert is_strict_territory_slot("12:15") is False
    assert is_strict_territory_slot("15:00") is False
    assert is_strict_territory_slot("nonsense") is False


def test_apply_priority_order_moves_priority_names_to_front():
    names = ["Terra S.", "Aaron B.", "Ashley R."]
    out = apply_priority_order(names, ["Aaron B."])
    assert out == ["Aaron B.", "Terra S.", "Ashley R."]


def test_apply_priority_order_preserves_order_when_no_priority_present():
    names = ["Terra S.", "Ashley R."]
    out = apply_priority_order(names, ["Aaron B."])
    assert out == ["Terra S.", "Ashley R."]


def test_apply_priority_order_handles_multiple_priority_names():
    names = ["Sharon K.", "Aaron B.", "Terra S.", "Ashley R."]
    out = apply_priority_order(names, ["Aaron B.", "Ashley R."])
    assert out == ["Aaron B.", "Ashley R.", "Sharon K.", "Terra S."]


def test_priority_directors_has_aaron_beck_for_north_at_need():
    """Brief rule 8: Aaron Beck is the priority director for North at-need."""
    assert PRIORITY_DIRECTORS[("north", "at_need")] == ["Aaron B."]


def test_location_region_unchanged():
    assert location_region("park_memorial") == "south"
    assert location_region("airdrie") == "north"
    assert location_region("calgary_crematorium") == "north"
    assert location_region("eastside") == "south"
    assert location_region("nonsense") == "unknown"
