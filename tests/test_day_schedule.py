"""Anchor and re-anchor behaviour for the dashboard's Today's Plan card.

Two things here are easy to get wrong and expensive when wrong, so they are pinned:

  1. The morning anchor. Feeds logged just after midnight are night feeds, and anchoring
     the 4-hour chain on one shifts every projected time that day by ~5 hours. Real
     examples in the data: 2026-08-03 00:30 and 2026-08-08 00:42.
  2. Re-anchoring. The nap projection must follow the most recent REAL wake, not a plan
     frozen at breakfast — a schedule you've caught being wrong is one you stop reading.

Run:  venv/bin/python tests/test_day_schedule.py
"""
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import WAKE_WINDOW_MINUTES, day_schedule  # noqa: E402

D = "2026-08-10"


def feed(hhmm):
    return {"type": "Feed", "time": f"{D}T{hhmm}:00"}


def nap(start, end=None):
    return {"start_time": f"{D}T{start}:00",
            "end_time": f"{D}T{end}:00" if end else None}


def at(hhmm):
    h, m = hhmm.split(":")
    return datetime(2026, 8, 10, int(h), int(m))


def feeds_in(sched):
    return [i["iso"][11:16] for i in sched["items"] if i["kind"] == "feed"]


def naps_in(sched):
    return [(i["iso"][11:16], i["end_iso"][11:16])
            for i in sched["items"] if i["kind"] == "nap"]


def test_overnight_feed_is_not_the_anchor():
    # The trap: a 00:42 night feed followed by the real 05:19 morning feed. Anchoring on
    # 00:42 would put the chain at 04:42/08:42/12:42 — every time wrong by ~4.5h.
    s = day_schedule([feed("00:42"), feed("05:19")], [], 240, now=at("06:00"))
    assert s["morning_feed_iso"] == f"{D}T05:19:00", s["morning_feed_iso"]
    assert feeds_in(s)[:3] == ["05:19", "09:19", "13:19"], feeds_in(s)
    print("  overnight 00:42 feed rejected as anchor  ✓")


def test_no_feed_yet_gives_empty_anchor_not_a_guess():
    s = day_schedule([], [], 240, now=at("06:00"))
    assert s["morning_feed_iso"] is None
    assert feeds_in(s) == [], "fabricated a feed chain with no anchor"
    print("  no feed logged yet → empty anchor, no invented chain  ✓")


def test_drift_is_reported_against_the_plan():
    # Feed logged 25 min late is that feed late, not a new one; and the chain must stay
    # pinned to the anchor rather than sliding forward with every late feed.
    s = day_schedule([feed("05:13"), feed("09:38")], [], 240, now=at("10:00"))
    second = [i for i in s["items"] if i["kind"] == "feed"][1]
    assert second["iso"][11:16] == "09:13"
    assert second["actual_iso"][11:16] == "09:38"
    assert second["drift_minutes"] == 25, second
    assert feeds_in(s)[2] == "13:13", "chain slid with the late feed instead of staying pinned"
    print("  late feed shown as +25m drift, chain stays pinned  ✓")


def test_naps_reanchor_on_the_actual_wake():
    # She woke at 11:28. The next window opens 150 min later, regardless of what a
    # morning projection would have said.
    s = day_schedule([feed("05:13")], [nap("10:25", "11:28")], 240, now=at("12:00"))
    first = naps_in(s)[0]
    assert first[0] == "13:58", f"next nap {first[0]}, want 11:28 + {WAKE_WINDOW_MINUTES}m"
    print("  nap window re-anchored to the 11:28 wake  ✓")


def test_early_wake_pulls_the_day_earlier():
    # Same setup, woke 40 min earlier — the whole projection must move with her.
    late = day_schedule([feed("05:13")], [nap("10:25", "11:28")], 240, now=at("12:00"))
    early = day_schedule([feed("05:13")], [nap("09:45", "10:48")], 240, now=at("12:00"))
    assert naps_in(early)[0][0] == "13:18", naps_in(early)
    assert naps_in(early)[0][0] < naps_in(late)[0][0]
    print("  a 40-min-earlier wake moves the next window earlier  ✓")


def test_currently_asleep_projects_the_end_not_a_new_start():
    s = day_schedule([feed("05:13")], [nap("14:30", None)], 240, now=at("15:00"))
    live = [i for i in s["items"] if i["kind"] == "nap" and i.get("in_progress")]
    assert len(live) == 1, "open session not surfaced as the in-progress nap"
    assert live[0]["iso"][11:16] == "14:30"
    assert live[0]["end_iso"][11:16] == "15:42", live[0]   # 14:30 + 72m (14-16 band)
    print("  open session projected to its end, no phantom start  ✓")


def test_overdue_nap_opens_now_not_in_the_past():
    # Woke 11:28, now 15:00 — the 13:58 window closed while she stayed up. The card must
    # say "now", never a time that has already passed.
    s = day_schedule([feed("05:13")], [nap("10:25", "11:28")], 240, now=at("15:00"))
    assert naps_in(s)[0][0] == "15:00", naps_in(s)
    print("  overdue window opens now rather than in the past  ✓")


def test_shift_bounds_and_bad_shift_string():
    s = day_schedule([feed("05:13")], [], 240, shift="10:00-18:00", now=at("12:00"))
    assert s["shift_start_iso"] == f"{D}T10:00:00"
    assert s["shift_end_iso"] == f"{D}T18:00:00"
    junk = day_schedule([feed("05:13")], [], 240, shift="not-a-shift", now=at("12:00"))
    assert junk["shift_start_iso"] == f"{D}T10:00:00", "bad shift string should fall back"
    print("  shift bounds parsed, junk falls back to 10:00-18:00  ✓")


def test_no_nap_projected_into_the_night():
    s = day_schedule([feed("05:13")], [nap("17:00", "18:00")], 240, now=at("18:30"))
    assert all(int(a[:2]) < 20 for a, _ in naps_in(s)), naps_in(s)
    print("  nothing projected past the 20:00 cutoff  ✓")


def main():
    print("Today's Plan schedule:")
    for fn in (test_overnight_feed_is_not_the_anchor,
               test_no_feed_yet_gives_empty_anchor_not_a_guess,
               test_drift_is_reported_against_the_plan,
               test_naps_reanchor_on_the_actual_wake,
               test_early_wake_pulls_the_day_earlier,
               test_currently_asleep_projects_the_end_not_a_new_start,
               test_overdue_nap_opens_now_not_in_the_past,
               test_shift_bounds_and_bad_shift_string,
               test_no_nap_projected_into_the_night):
        fn()
    print("All schedule tests pass.")


if __name__ == "__main__":
    main()
