"""Bucket algebra for the crib-monitor-vs-camera scorer.

The point of sleep_score is that "the crib monitor said asleep and no camera agreed"
splits into two very different facts — a camera actively saw an AWAKE baby, or no
camera committed either way — and only the first is evidence of invented sleep. These
tests pin that split, the room restriction that keeps structurally-invisible bedroom
naps out of the error count, and the attribution back to end_reason.

Run:  venv/bin/python tests/test_sleep_score.py
"""
import os
import sys
from datetime import date, datetime, time as dtime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sleep_score  # noqa: E402

DAY = date(2026, 8, 4)
WINDOW = (dtime(10, 0), dtime(18, 0))


def t(h, m=0):
    return datetime(2026, 8, 4, h, m).isoformat()


def chunk(camera, start_h, end_h, activities=()):
    return {"camera": camera, "start_iso": t(start_h), "end_iso": t(end_h),
            "activities": [{"baby_state": s, "start_iso": t(*a), "end_iso": t(*b)}
                           for s, a, b in activities],
            "phone_use": []}


def with_sessions(sessions):
    """Stub the storage read so these tests never touch sleep_sessions.json."""
    sleep_score.crib_sessions_for = lambda day: [
        {"start": datetime(2026, 8, 4, *s), "end": datetime(2026, 8, 4, *e),
         "reason": r, "id": i}
        for i, (s, e, r) in enumerate(sessions)]


def test_buckets_split_contradicted_from_unconfirmed():
    # Crib claims 10:00-12:00. Camera: awake 10:00-11:00, asleep 11:00-11:30,
    # silent 11:30-12:00 (but the room is being watched throughout).
    with_sessions([((10, 0), (12, 0), "liveness_timeout")])
    chunks = [chunk("nurserycam", 10, 18,
                    [("awake", (10, 0), (11, 0)), ("asleep", (11, 0), (11, 30))])]
    r = sleep_score.score_day(DAY, chunks, {"nurserycam": "nursery"}, WINDOW)

    assert r["crib_minutes"] == 120.0, r
    assert r["contradicted_minutes"] == 60.0, r
    assert r["agreement_minutes"] == 30.0, r
    assert r["unconfirmed_minutes"] == 30.0, r
    # Half of crib-scored sleep is actively disputed — the headline error rate.
    assert r["false_sleep_rate"] == 0.5, r
    # …and the blame lands on the session that produced it.
    assert r["contradicted_by_reason"] == {"liveness_timeout": 60.0}, r
    print("  contradicted vs unconfirmed split, attributed to end_reason  ✓")


def test_unwatched_time_is_not_an_error():
    # Same crib claim, but the cameras only covered the first hour. The uncovered
    # hour must not count as disagreement — nobody was looking.
    with_sessions([((10, 0), (12, 0), "sustained_wake")])
    chunks = [chunk("nurserycam", 10, 11, [("awake", (10, 0), (10, 30))])]
    r = sleep_score.score_day(DAY, chunks, {"nurserycam": "nursery"}, WINDOW)

    assert r["contradicted_minutes"] == 30.0, r
    assert r["unconfirmed_minutes"] == 30.0, r   # 10:30-11:00 watched, no verdict
    assert r["observed_minutes"] == 60.0, r      # 11:00-12:00 simply unobserved
    print("  unobserved time excluded from both error buckets  ✓")


def test_bedroom_sleep_is_out_of_scope():
    # A bedroom nap the crib monitor structurally cannot see is a blind spot, not a
    # miss. Counting it would tune the thresholds toward garbage.
    with_sessions([])
    chunks = [chunk("bedroomcam", 10, 18, [("asleep", (13, 0), (15, 0))]),
              chunk("nurserycam", 10, 18)]
    r = sleep_score.score_day(DAY, chunks, {"nurserycam": "nursery",
                                            "bedroomcam": "bedroom"}, WINDOW)

    assert r["missed_minutes"] == 0.0, r
    assert r["camera_asleep_minutes"] == 0.0, r
    print("  bedroom sleep excluded from the crib monitor's error  ✓")


def test_missed_sleep_in_room_counts():
    # The 07-31 shape: cameras watched a long nursery nap the crib monitor barely saw.
    with_sessions([((10, 0), (10, 10), "sustained_wake")])
    chunks = [chunk("nurserycam", 10, 18, [("asleep", (10, 0), (13, 0))])]
    r = sleep_score.score_day(DAY, chunks, {"nurserycam": "nursery"}, WINDOW)

    assert r["agreement_minutes"] == 10.0, r
    assert r["missed_minutes"] == 170.0, r
    assert r["contradicted_minutes"] == 0.0, r
    print("  missed in-room sleep counted, without inflating false-sleep  ✓")


def test_window_clips_and_legacy_reason():
    # Sessions run past the care window; only the in-window part is scorable. And a
    # session written before end_reason existed attributes to "unknown".
    with_sessions([((8, 0), (11, 0), "unknown")])
    chunks = [chunk("nurserycam", 10, 18, [("awake", (10, 0), (11, 0))])]
    r = sleep_score.score_day(DAY, chunks, {"nurserycam": "nursery"}, WINDOW)

    assert r["crib_minutes"] == 60.0, r          # 08:00-10:00 is outside the window
    assert r["contradicted_by_reason"] == {"unknown": 60.0}, r
    assert r["sessions_missing_reason"] == 1, r
    print("  window clipping and legacy 'unknown' attribution  ✓")


def main():
    print("Crib-monitor scorer:")
    test_buckets_split_contradicted_from_unconfirmed()
    test_unwatched_time_is_not_an_error()
    test_bedroom_sleep_is_out_of_scope()
    test_missed_sleep_in_room_counts()
    test_window_clips_and_legacy_reason()
    print("All scorer tests pass.")


if __name__ == "__main__":
    main()
