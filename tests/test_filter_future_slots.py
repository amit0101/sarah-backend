"""Past-slot filter for `propose_slots` — Session 28 regression.

Bug: `check_calendar` returned same-day slots whose start time had already
passed (e.g. proposing 09:00 at 13:00 local). Sarah told the customer
"you're booked" because Google Calendar happily accepts past events, but
GHL rightly refused the push (`400 "The slot you have selected is no longer
available"`) and the broad-except in `_book_appointment` swallowed the
failure — silent customer-confirmation regression.

Fix: `calendar_service._filter_future_slots` drops candidates earlier than
`now + _MIN_SLOT_LEAD` (90 min). These tests pin that behaviour so a future
refactor of the candidate generators can't reintroduce the bug.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.services import calendar_service as cal_svc


TZ = ZoneInfo("America/Edmonton")


def test_filter_drops_past_same_day_candidates(monkeypatch) -> None:
    """At 13:00 local, 09:00 / 12:15 candidates are filtered; 15:00 stays."""
    fixed_now = datetime(2026, 5, 4, 13, 0, tzinfo=TZ)

    class _FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[override]
            return fixed_now if tz is None else fixed_now.astimezone(tz)

    monkeypatch.setattr(cal_svc, "datetime", _FrozenDatetime)

    window_start, _ = cal_svc._day_window(fixed_now.date(), TZ)
    out = cal_svc._candidate_starts_for("park_memorial", window_start, TZ)

    # Legacy grid is 09:00 / 12:15 / 15:00. At 13:00 + 90min lead, only 15:00
    # is far enough in the future to survive.
    assert all(c >= fixed_now + cal_svc._MIN_SLOT_LEAD for c in out)
    assert any(c.hour == 15 and c.minute == 0 for c in out)
    assert not any(c.hour == 9 for c in out)
    assert not any(c.hour == 12 and c.minute == 15 for c in out)


def test_filter_keeps_all_candidates_on_future_day(monkeypatch) -> None:
    """A target one day ahead is unaffected — every 9–16 candidate survives."""
    fixed_now = datetime(2026, 5, 4, 13, 0, tzinfo=TZ)
    target_day = (fixed_now + timedelta(days=1)).date()

    class _FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[override]
            return fixed_now if tz is None else fixed_now.astimezone(tz)

    monkeypatch.setattr(cal_svc, "datetime", _FrozenDatetime)

    window_start, _ = cal_svc._day_window(target_day, TZ)
    out = cal_svc._generic_candidate_starts(window_start, TZ)

    # Hourly 09:00–16:00 = 8 candidates. None should be filtered.
    assert len(out) == 8
    assert out[0].hour == 9 and out[-1].hour == 16


def test_filter_uses_90_minute_lead_buffer() -> None:
    """The lead buffer is exactly 90 min — tighter than that risks
    proposing slots the customer can't realistically attend."""
    assert cal_svc._MIN_SLOT_LEAD == timedelta(minutes=90)
