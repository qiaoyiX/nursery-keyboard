"""Today's Plan card: a fixed daily template with logged events paired against it.

Pinned here:
  1. The template renders in order, dated today, and never moves with real events.
  2. A logged feed/solid pairs with its row and reports drift, instead of shifting the plan.
  3. Night feeds (before 04:00) are not claimed by the 06:00 wake row. Real examples in
     the data: 2026-08-03 00:30 and 2026-08-08 00:42.

Run:  venv/bin/python tests/test_day_schedule.py
"""
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import DAY_PLAN, day_schedule  # noqa: E402

D = "2026-08-10"


def ev(kind, hhmm):
    return {"type": kind, "time": f"{D}T{hhmm}:00"}


def at(hhmm):
    h, m = hhmm.split(":")
    return datetime(2026, 8, 10, int(h), int(m))


def row(sched, hhmm):
    return next(i for i in sched["items"] if i["iso"][11:16] == hhmm)


def test_template_renders_in_order_for_today():
    s = day_schedule([], now=at("12:00"))
    assert [i["iso"][11:16] for i in s["items"]] == [p[0] for p in DAY_PLAN]
    assert all(i["iso"].startswith(D) for i in s["items"])
    assert row(s, "08:00")["end_iso"] == f"{D}T09:30:00"
    assert row(s, "10:00")["end_iso"] is None
    print("  template in order, dated today, nap ends set  ✓")


def test_logged_feed_pairs_with_its_row():
    s = day_schedule([ev("Feed", "06:10"), ev("Feed", "10:22")], now=at("11:00"))
    assert row(s, "06:00")["drift_minutes"] == 10
    assert row(s, "10:00")["actual_iso"] == f"{D}T10:22:00"
    assert row(s, "10:00")["drift_minutes"] == 22
    assert row(s, "14:00")["actual_iso"] is None
    print("  logged feeds pair with rows, drift reported  ✓")


def test_logged_solid_pairs_with_solid_row_only():
    s = day_schedule([ev("Solid", "18:05")], now=at("19:00"))
    assert row(s, "18:00")["drift_minutes"] == 5
    assert row(s, "17:30")["actual_iso"] is None, "a solid must not fill a milk row"
    print("  solids pair with solid rows, not milk  ✓")


def test_night_feed_is_not_the_wake_feed():
    s = day_schedule([ev("Feed", "00:42"), ev("Feed", "03:55")], now=at("06:30"))
    assert row(s, "06:00")["actual_iso"] is None
    print("  pre-04:00 feeds don't claim the wake row  ✓")


def test_rows_without_a_log_type_carry_no_match_fields():
    s = day_schedule([ev("Feed", "08:10")], now=at("09:00"))
    assert "actual_iso" not in row(s, "08:00"), "a nap row must never claim a feed"
    print("  nap/routine rows never pair with events  ✓")


def test_shift_bounds_and_bad_shift_string():
    s = day_schedule([], shift="10:00-18:00", now=at("12:00"))
    assert s["shift_start_iso"] == f"{D}T10:00:00"
    assert s["shift_end_iso"] == f"{D}T18:00:00"
    junk = day_schedule([], shift="not-a-shift", now=at("12:00"))
    assert junk["shift_start_iso"] == f"{D}T10:00:00"
    print("  shift bounds + junk fallback  ✓")


def main():
    print("Today's Plan schedule:")
    for fn in (test_template_renders_in_order_for_today,
               test_logged_feed_pairs_with_its_row,
               test_logged_solid_pairs_with_solid_row_only,
               test_night_feed_is_not_the_wake_feed,
               test_rows_without_a_log_type_carry_no_match_fields,
               test_shift_bounds_and_bad_shift_string):
        fn()
    print("All schedule tests pass.")


if __name__ == "__main__":
    main()
