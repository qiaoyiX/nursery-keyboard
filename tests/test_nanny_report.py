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
from collections import namedtuple
from datetime import date, datetime, time as dtime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import nanny_common
from nanny_common import (
    load_camera_rooms, load_cameras, load_days, load_window, offset_to_wallclock,
    segment_start,
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

    cams3 = {"crib": "u", "play": "u", "bed": "u"}
    rooms = load_camera_rooms(cams3, {"NANNY_CAM_ROOMS": "crib:nursery,play:nursery,bed:bedroom"})
    check("two cameras share a room",
          rooms == {"crib": "nursery", "play": "nursery", "bed": "bedroom"})
    check("unlisted camera becomes its own room",
          load_camera_rooms(cams3, {"NANNY_CAM_ROOMS": "crib:nursery"})["bed"] == "bed")
    check("no room config → each camera its own room",
          load_camera_rooms(cams3, {}) == {"crib": "crib", "play": "play", "bed": "bed"})
    for bad in ("crib:nursery,typo:nursery", "crib", "crib:"):
        try:
            load_camera_rooms(cams3, {"NANNY_CAM_ROOMS": bad})
            check(f"bad room spec rejected ({bad})", False)
        except ValueError:
            check(f"bad room spec rejected ({bad})", True)


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


# ── Unanalyzable raw / give-up policy ─────────────────────────────────────────

class RawFixture:
    """Temp raw+chunks dirs wired into both modules (nanny_analyze imports the
    paths by value, so patching nanny_common alone is not enough)."""

    def __enter__(self):
        import nanny_analyze
        self.mod = nanny_analyze
        self.tmp = tempfile.mkdtemp()
        self.orig = (nanny_common.RAW_DIR, nanny_common.CHUNKS_DIR, nanny_analyze.RAW_DIR)
        nanny_common.RAW_DIR = nanny_analyze.RAW_DIR = os.path.join(self.tmp, "raw")
        nanny_common.CHUNKS_DIR = os.path.join(self.tmp, "chunks")
        return self

    def __exit__(self, *exc):
        (nanny_common.RAW_DIR, nanny_common.CHUNKS_DIR,
         self.mod.RAW_DIR) = self.orig
        shutil.rmtree(self.tmp)

    def raw(self, camera, name, age_seconds=3600, size=0):
        cam_dir = os.path.join(nanny_common.RAW_DIR, camera)
        os.makedirs(cam_dir, exist_ok=True)
        path = os.path.join(cam_dir, name)
        with open(path, "wb") as f:
            f.write(b"\0" * size)
        stamp = datetime.now().timestamp() - age_seconds
        os.utime(path, (stamp, stamp))
        return path

    def chunk(self, camera, seg_start):
        path = nanny_common.chunk_path(camera, seg_start)
        if not os.path.exists(path):
            return None
        with open(path) as f:
            return json.load(f)


def test_preflight():
    print("unanalyzable raw is written off, not retried")
    import nanny_analyze
    seg_start = datetime(2026, 7, 27, 18, 0)   # the window-end sliver

    for probe, expect in ((None, "unreadable_raw"), (1.5, "too_short")):
        with RawFixture() as fx:
            raw = fx.raw("kitchen", "20260727_180000.mp4")
            orig_probe = nanny_analyze.probe_seconds
            nanny_analyze.probe_seconds = lambda p: probe
            try:
                # client=None and pacer=None: preflight must return before either
                # is touched, so a Gemini call would raise here.
                nanny_analyze.process_segment(None, "m", "kitchen", raw, seg_start,
                                              {"kitchen": "kitchen"}, None)
            finally:
                nanny_analyze.probe_seconds = orig_probe
            chunk = fx.chunk("kitchen", seg_start)
            check(f"{expect}: raw deleted", not os.path.exists(raw))
            check(f"{expect}: chunk records the reason",
                  chunk and chunk["error"] == expect, str(chunk))
            check(f"{expect}: counts as a parse error", chunk["parse_error"] is True)
            check(f"{expect}: zero analyzed minutes", chunk["segment_minutes"] == 0)


def test_missing_toolchain_deletes_nothing():
    print("missing ffmpeg never costs footage")
    import nanny_analyze
    orig_which, orig_env = nanny_analyze.shutil.which, os.environ.get("GEMINI_API_KEY")
    nanny_analyze.shutil.which = lambda tool: None
    os.environ["GEMINI_API_KEY"] = "test-key"
    with RawFixture() as fx:
        raw = fx.raw("kitchen", "20260727_120000.mp4")
        try:
            done, failed = nanny_analyze.analyze_pending()
        finally:
            nanny_analyze.shutil.which = orig_which
            if orig_env is None:
                os.environ.pop("GEMINI_API_KEY", None)
            else:
                os.environ["GEMINI_API_KEY"] = orig_env
        check("run bails out", (done, failed) == (0, 0))
        check("raw untouched — a missing apt package is not bad footage",
              os.path.exists(raw))


def test_give_up_policy():
    print("give up on attempts, not on age")
    import nanny_analyze
    seg_start = datetime(2026, 7, 27, 12, 0)
    budget = nanny_analyze.max_segment_attempts()

    with RawFixture() as fx:
        raw = fx.raw("kitchen", "20260727_120000.mp4")
        for i in range(budget - 1):
            nanny_analyze.record_failure(raw, f"boom {i}")
        nanny_analyze.give_up_on_failed_raw()
        check("survives below the attempt budget", os.path.exists(raw))

        rec = nanny_analyze.record_failure(raw, "CalledProcessError(1)")
        check("ledger counts attempts", rec["attempts"] == budget)
        nanny_analyze.give_up_on_failed_raw({"kitchen": "kitchen"})
        chunk = fx.chunk("kitchen", seg_start)
        check("deleted once the budget is spent", not os.path.exists(raw))
        check("ledger deleted with it",
              not os.path.exists(raw + ".fail.json"))
        check("give-up records the last error",
              chunk and chunk["error"] == "gave_up"
              and "CalledProcessError" in chunk["error_detail"], str(chunk))

    # The Persistent-catch-up case: days-old footage nobody has tried yet.
    with RawFixture() as fx:
        old = datetime.now() - timedelta(hours=nanny_analyze.RAW_MAX_AGE_HOURS + 24)
        name = old.strftime("%Y%m%d_%H%M%S") + ".mp4"
        raw = fx.raw("kitchen", name)
        nanny_analyze.give_up_on_failed_raw()
        check("untried backlog survives its age", os.path.exists(raw))
        nanny_analyze.record_failure(raw, "genuinely failed once")
        nanny_analyze.give_up_on_failed_raw()
        check("old AND tried is dropped", not os.path.exists(raw))


def test_disk_pressure():
    print("disk-pressure backstop")
    import nanny_analyze
    with RawFixture() as fx:
        older = fx.raw("kitchen", "20260727_100000.mp4", age_seconds=7200, size=2000)
        newer = fx.raw("kitchen", "20260727_110000.mp4", age_seconds=3600, size=2000)
        fake_usage = namedtuple("usage", "total used free")(
            0, 0, nanny_common.MIN_FREE_BYTES - 1000)
        orig = nanny_analyze.shutil.disk_usage
        nanny_analyze.shutil.disk_usage = lambda p: fake_usage
        try:
            nanny_analyze.purge_raw_under_disk_pressure()
        finally:
            nanny_analyze.shutil.disk_usage = orig
        check("oldest untried raw dropped to keep recording", not os.path.exists(older))
        check("stops as soon as it is back above the floor", os.path.exists(newer))
        check("and says why", (fx.chunk("kitchen", datetime(2026, 7, 27, 10, 0)) or {})
              .get("error") == "disk_pressure")


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
    check("missing baby_state falls back to not_visible",
          chunk["activities"][0]["baby_state"] == "not_visible")
    check("room recorded on the chunk", chunk["room"] == "living")
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

    from nanny_report import intersect_intervals, subtract_intervals
    check("intersect", intersect_intervals(u, naps) == [(t(10, 15), t(10, 45))])
    check("subtract punches a hole",
          subtract_intervals([(t(10), t(11))], [(t(10, 15), t(10, 45))])
          == [(t(10), t(10, 15)), (t(10, 45), t(11))])
    check("subtract whole", subtract_intervals([(t(10), t(11))], [(t(9), t(12))]) == [])
    check("subtract nothing", subtract_intervals(u, []) == u)


# ── Phone-use policy (rooms + sleep) ──────────────────────────────────────────

def test_phone_policy():
    print("phone-use policy classification")
    from nanny_report import classify_phone_use
    t = lambda h, m=0: datetime(2026, 7, 27, h, m)
    iso = lambda d: d.isoformat()
    rooms = {"cribcam": "nursery", "playcam": "nursery", "bedcam": "bedroom"}

    def act(s, e, state):
        return {"start_iso": iso(s), "end_iso": iso(e), "category": "play",
                "description": "", "baby_visible": state != "not_visible",
                "baby_state": state}

    def phone(cam, s, e, ctx, conf="high"):
        return {"camera": cam, "start_iso": iso(s), "end_iso": iso(e),
                "context": ctx, "confidence": conf, "description": ""}

    chunks = [
        {"camera": "cribcam", "activities": [act(t(10), t(11), "awake")], "phone_use": []},
        {"camera": "playcam", "activities": [], "phone_use": []},
        {"camera": "bedcam", "activities": [act(t(15), t(15, 30), "asleep")],
         "phone_use": []},
    ]
    events = [
        # A: baby not in this camera's frame, but the OTHER nursery camera has an
        #    awake baby in the same room → same room, awake baby → flagged.
        phone("playcam", t(10), t(10, 10), "baby_not_in_frame"),
        # B: same shape but in the bedroom while the baby is awake in the nursery
        #    → different room, not together → never flagged.
        phone("bedcam", t(10, 30), t(10, 50), "baby_not_in_frame"),
        # C: inside a crib-monitor nap window → allowed even with no camera verdict.
        phone("cribcam", t(11), t(11, 30), "unclear"),
        # D: with the baby, but low confidence → unconfirmed, not flagged.
        phone("cribcam", t(13), t(13, 20), "while_holding_baby", conf="low"),
        # E: with the baby, but this room's camera says the baby is asleep (no crib
        #    monitor in the bedroom) → asleep evidence outranks with-baby.
        phone("bedcam", t(15), t(15, 10), "while_holding_baby"),
        # F+G: both nursery cameras see the same session → 20 min, not 40.
        phone("cribcam", t(16), t(16, 20), "while_holding_baby"),
        phone("playcam", t(16), t(16, 20), "baby_nearby_awake"),
    ]
    naps = [(t(11), t(12))]
    stats, unauth = classify_phone_use(events, chunks, rooms, naps)

    check("total minutes de-duplicated", stats["total_minutes"] == 110)
    check("flagged minutes", stats["unauthorized_minutes"] == 30)
    check("double coverage flagged once", unauth == [(t(10), t(10, 10)),
                                                     (t(16), t(16, 20))])
    check("asleep minutes (crib nap + camera-seen sleep)",
          stats["while_baby_asleep_minutes"] == 40)
    check("crib-monitor naps still reported", stats["during_naps_minutes"] == 30)
    check("low confidence held back as unconfirmed",
          stats["unauthorized_unconfirmed_minutes"] == 20)
    check("unclear is the remainder", stats["unclear_minutes"] == 40)
    check("flagged event count", stats["unauthorized_event_count"] == 3)

    verdicts = [e["authorization"] for e in events]
    check("per-event verdicts", verdicts == [
        "unauthorized", "unclear", "allowed_baby_asleep", "unconfirmed",
        "allowed_baby_asleep", "unauthorized", "unauthorized"], str(verdicts))
    check("room attached to events", events[0]["room"] == "nursery")
    check("flagged event carries its minutes", events[0]["unauthorized_minutes"] == 10)

    # No room config at all: each camera is its own room, so cross-camera fusion
    # stops and only the model's own with-baby verdicts can flag.
    for e in events:
        e.pop("room", None)
    solo = {c: c for c in rooms}
    stats2, _ = classify_phone_use(events, chunks, solo, naps)
    check("without shared rooms, A is no longer flagged",
          stats2["unauthorized_minutes"] == 20)


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

def test_report_failures():
    print("report names the hours it lost")
    import nanny_report, storage
    chunks = [
        {"camera": "kitchen", "room": "kitchen",
         "segment_start_iso": "2026-07-27T18:00:00", "segment_minutes": 0,
         "parse_error": True, "error": "unreadable_raw",
         "error_detail": "ffprobe could not read a duration",
         "activities": [], "phone_use": [], "notable_events": [], "summary": ""},
        {"camera": "kitchen", "room": "kitchen",
         "segment_start_iso": "2026-07-27T10:00:00", "segment_minutes": 60,
         "activities": [], "phone_use": [], "notable_events": [], "summary": "ok"},
    ]
    orig_sessions = storage.get_sleep_sessions_range
    key = os.environ.pop("GEMINI_API_KEY", None)
    storage.get_sleep_sessions_range = lambda days=7: []
    try:
        rep = nanny_report.build_report(date(2026, 7, 27), chunks, ["kitchen"],
                                        (dtime(10, 0), dtime(18, 0)))
    finally:
        storage.get_sleep_sessions_range = orig_sessions
        if key is not None:
            os.environ["GEMINI_API_KEY"] = key
    check("failure surfaced with its reason",
          rep["failures"] == [{"camera": "kitchen", "room": "kitchen",
                               "segment_start_iso": "2026-07-27T18:00:00",
                               "error": "unreadable_raw",
                               "detail": "ffprobe could not read a duration"}],
          str(rep["failures"]))
    check("still counted as a parse error", rep["parse_errors"] == 1)
    check("zero-length segment does not inflate coverage",
          rep["coverage"]["kitchen"]["analyzed_minutes"] == 60)


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
               test_preflight, test_missing_toolchain_deletes_nothing,
               test_give_up_policy, test_disk_pressure,
               test_chunk_tolerance, test_interval_math, test_phone_policy,
               test_nap_windows_clipping, test_unreported_dates,
               test_report_failures, test_coverage):
        fn()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S): {', '.join(FAILURES)}")
        sys.exit(1)
    print("All nanny-report tests passed.")


if __name__ == "__main__":
    main()
