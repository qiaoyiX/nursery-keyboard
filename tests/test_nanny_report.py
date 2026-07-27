"""
Unit tests for the nanny-report pipeline's Gemini-free logic: env parsing,
segment bookkeeping, offset conversion, chunk validation, interval math, nap
splitting, and report/date targeting. Everything that talks to a camera or to
Gemini is exercised on the Pi / with a real key instead (see plan).

Run:  venv/bin/python tests/test_nanny_report.py
"""

import json
import os
import shutil
import sys
import tempfile
from datetime import date, datetime, time as dtime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import nanny_common
from nanny_common import (
    load_cameras, load_days, load_window, offset_to_wallclock, segment_start,
)

FAILURES = []


def check(name, cond, detail=""):
    status = "ok" if cond else "FAIL"
    print(f"  [{status}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


# ── Env parsing ───────────────────────────────────────────────────────────────

def test_env_parsing():
    print("env parsing")
    cams = load_cameras({"NANNY_CAM_2": "kitchen=rtsp://u:p@2/s",
                         "NANNY_CAM_1": "living-room=rtsp://u:p=x@1/s",
                         "OTHER": "zzz"})
    check("two cameras parsed", set(cams) == {"living-room", "kitchen"})
    check("url may contain '='", cams["living-room"] == "rtsp://u:p=x@1/s")

    try:
        load_cameras({"NANNY_CAM_1": "bad url no equals"})
        check("missing '=' rejected", False)
    except ValueError:
        check("missing '=' rejected", True)
    try:
        load_cameras({"NANNY_CAM_1": "../evil=rtsp://x"})
        check("path-unsafe name rejected", False)
    except ValueError:
        check("path-unsafe name rejected", True)

    check("window parsed", load_window({"NANNY_WINDOW": "9:30-17:00"})
          == (dtime(9, 30), dtime(17, 0)))
    check("window default", load_window({}) == (dtime(10, 0), dtime(18, 0)))
    try:
        load_window({"NANNY_WINDOW": "18:00-10:00"})
        check("inverted window rejected", False)
    except ValueError:
        check("inverted window rejected", True)

    check("days parsed", load_days({"NANNY_DAYS": "mon,Wed"}) == {0, 2})
    check("days default is weekdays", load_days({}) == {0, 1, 2, 3, 4})


# ── Segment names and offsets ─────────────────────────────────────────────────

def test_segments_and_offsets():
    print("segment names / offsets")
    check("segment name parsed",
          segment_start("20260727_101500.mp4") == datetime(2026, 7, 27, 10, 15))
    check("non-segment ignored", segment_start("junk.mp4") is None)
    check("impossible date ignored", segment_start("20261399_101500.mp4") is None)

    base = datetime(2026, 7, 27, 10, 15)   # mid-hour restart segment
    check("MM:SS offset", offset_to_wallclock(base, "05:30")
          == datetime(2026, 7, 27, 10, 20, 30))
    check("HH:MM:SS offset", offset_to_wallclock(base, "1:02:03")
          == datetime(2026, 7, 27, 11, 17, 3))
    check("int seconds tolerated", offset_to_wallclock(base, 90)
          == datetime(2026, 7, 27, 10, 16, 30))
    check("garbage offset → None", offset_to_wallclock(base, "abc") is None)
    check("negative offset → None", offset_to_wallclock(base, "-1:00") is None)


# ── pending_segments ──────────────────────────────────────────────────────────

def test_pending_segments():
    print("pending segment selection")
    tmp = tempfile.mkdtemp()
    orig_raw, orig_chunks = nanny_common.RAW_DIR, nanny_common.CHUNKS_DIR
    nanny_common.RAW_DIR = os.path.join(tmp, "raw")
    nanny_common.CHUNKS_DIR = os.path.join(tmp, "chunks")
    try:
        cam = os.path.join(nanny_common.RAW_DIR, "living")
        os.makedirs(cam)
        old = os.path.join(cam, "20260727_100000.mp4")
        hot = os.path.join(cam, "20260727_110000.mp4")
        for p in (old, hot):
            open(p, "w").close()
        now = datetime.now()
        os.utime(old, (now.timestamp() - 3600,) * 2)
        os.utime(hot, (now.timestamp() - 10,) * 2)   # freshly written, no newer sibling

        pend = nanny_common.pending_segments(now=now)
        check("closed segment pending", [(c, os.path.basename(p)) for c, p, _ in pend]
              == [("living", "20260727_100000.mp4")])

        # A chunk JSON marks it done.
        cp = nanny_common.chunk_path("living", datetime(2026, 7, 27, 10, 0))
        os.makedirs(os.path.dirname(cp), exist_ok=True)
        open(cp, "w").close()
        check("chunk marker excludes it", nanny_common.pending_segments(now=now) == [])

        # The hot segment becomes pending once a newer sibling appears.
        open(os.path.join(cam, "20260727_120000.mp4"), "w").close()
        pend = nanny_common.pending_segments(now=now)
        check("newer sibling closes previous",
              any(os.path.basename(p) == "20260727_110000.mp4" for _, p, _ in pend))
    finally:
        nanny_common.RAW_DIR, nanny_common.CHUNKS_DIR = orig_raw, orig_chunks
        shutil.rmtree(tmp)


# ── Chunk validation (malformed Gemini output) ────────────────────────────────

def test_chunk_tolerance():
    print("chunk build tolerance")
    from nanny_analyze import build_chunk
    seg_start = datetime(2026, 7, 27, 10, 0)
    parsed = {
        "activities": [
            {"start": "00:00", "end": "10:00", "category": "play", "description": "ok"},
            {"start": "junk", "end": "10:00", "category": "play"},          # bad start
            {"start": "20:00", "end": "10:00", "category": "play"},         # end < start
            {"start": "00:00", "end": "59:00", "category": "not-a-category"},
        ],
        "phone_use": [
            {"start": "05:00", "end": "07:00", "context": "made_up", "confidence": "high"},
            {"start": "99:99:99:99", "end": "07:00", "context": "unclear", "confidence": "low"},
        ],
        "notable_events": [{"time": "03:00", "type": "other", "description": "x"},
                           {"time": None, "type": "other", "description": "y"}],
        "summary": "hour summary",
    }
    chunk = build_chunk(parsed, "living", seg_start, 60, "m", {})
    check("good + unknown-category activities kept", len(chunk["activities"]) == 2)
    check("unknown category preserved as-is",
          chunk["activities"][1]["category"] == "not-a-category")
    check("bad events dropped, counted", chunk["dropped_events"] == 4)
    check("unknown phone context → unclear",
          chunk["phone_use"][0]["context"] == "unclear")
    check("wallclock conversion",
          chunk["phone_use"][0]["start_iso"] == "2026-07-27T10:05:00")
    check("notable kept", len(chunk["notable_events"]) == 1)


# ── Interval math & nap splitting ─────────────────────────────────────────────

def test_interval_math():
    print("interval union / nap split")
    from nanny_report import intersect_minutes, total_minutes, union_intervals
    t = lambda h, m=0: datetime(2026, 7, 27, h, m)

    u = union_intervals([(t(10), t(10, 30)), (t(10, 20), t(11)), (t(12), t(12, 10)),
                         (t(11), t(11), )])   # zero-length dropped
    check("overlaps merged", u == [(t(10), t(11)), (t(12), t(12, 10))])
    check("union total", total_minutes(u) == 70)

    # Two cameras both seeing the same 10:00-10:30 phone session must count once.
    both = union_intervals([(t(10), t(10, 30)), (t(10), t(10, 30))])
    check("double coverage counted once", total_minutes(both) == 30)

    naps = [(t(10, 15), t(10, 45)), (t(13), t(14))]
    check("nap overlap minutes", intersect_minutes(u, naps) == 30)
    check("no overlap", intersect_minutes([(t(15), t(16))], naps) == 0)


def test_nap_windows_clipping():
    print("nap windows from storage (clipped to day)")
    import nanny_report
    day = date(2026, 7, 27)
    sessions = [
        # Overnight: started yesterday, ended 01:00 today → clipped to midnight–01:00
        {"start_time": "2026-07-26T20:00:00", "end_time": "2026-07-27T01:00:00"},
        {"start_time": "2026-07-27T13:00:00", "end_time": "2026-07-27T14:30:00"},
        {"start_time": "2026-07-27T17:00:00", "end_time": None},              # open: ignored
        {"start_time": "2026-07-25T10:00:00", "end_time": "2026-07-25T11:00:00"},  # other day
    ]
    import storage
    orig = storage.get_sleep_sessions_range
    storage.get_sleep_sessions_range = lambda days=7: sessions
    try:
        w = nanny_report.nap_windows_for(day)
    finally:
        storage.get_sleep_sessions_range = orig
    check("clipped + filtered", w == [
        (datetime(2026, 7, 27, 0, 0), datetime(2026, 7, 27, 1, 0)),
        (datetime(2026, 7, 27, 13, 0), datetime(2026, 7, 27, 14, 30))])


# ── Report date targeting ─────────────────────────────────────────────────────

def test_unreported_dates():
    print("unreported-date targeting")
    import nanny_report
    tmp = tempfile.mkdtemp()
    orig_chunks, orig_reports = nanny_report.CHUNKS_DIR, nanny_report.REPORTS_DIR
    nanny_report.CHUNKS_DIR = os.path.join(tmp, "chunks")
    nanny_report.REPORTS_DIR = os.path.join(tmp, "reports")
    try:
        for d in ("2026-07-24", "2026-07-25", "not-a-date"):
            os.makedirs(os.path.join(nanny_report.CHUNKS_DIR, d))
        os.makedirs(nanny_report.REPORTS_DIR)
        with open(os.path.join(nanny_report.REPORTS_DIR, "2026-07-24.json"), "w") as f:
            json.dump({}, f)
        days = nanny_report.unreported_dates(today=date(2026, 7, 27))
        check("only unreported valid dates", days == [date(2026, 7, 25)])
    finally:
        nanny_report.CHUNKS_DIR, nanny_report.REPORTS_DIR = orig_chunks, orig_reports
        shutil.rmtree(tmp)


# ── Coverage gaps ─────────────────────────────────────────────────────────────

def test_coverage():
    print("coverage / gaps")
    from nanny_report import coverage_for
    window = (dtime(10, 0), dtime(18, 0))
    chunks = [
        {"camera": "living", "segment_start_iso": "2026-07-27T10:00:00", "segment_minutes": 60},
        {"camera": "living", "segment_start_iso": "2026-07-27T12:00:00", "segment_minutes": 60},
    ]
    cov = coverage_for(chunks, ["living", "kitchen"], window)
    check("analyzed minutes", cov["living"]["analyzed_minutes"] == 120)
    check("gap 11-12 found", {"start_iso": "2026-07-27T11:00:00",
                              "end_iso": "2026-07-27T12:00:00"} in cov["living"]["gaps"])
    check("tail gap 13-18 found", {"start_iso": "2026-07-27T13:00:00",
                                   "end_iso": "2026-07-27T18:00:00"} in cov["living"]["gaps"])
    check("silent camera flagged", cov["kitchen"]["gaps"] == [{"whole_day": True}])


def main():
    for fn in (test_env_parsing, test_segments_and_offsets, test_pending_segments,
               test_chunk_tolerance, test_interval_math, test_nap_windows_clipping,
               test_unreported_dates, test_coverage):
        fn()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S): {', '.join(FAILURES)}")
        sys.exit(1)
    print("All nanny-report tests passed.")


if __name__ == "__main__":
    main()
