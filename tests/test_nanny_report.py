"""
Unit tests for the nanny-report pipeline's Gemini-free logic: env parsing,
segment bookkeeping, offset conversion, chunk validation, interval math, nap
splitting, and report/date targeting. Everything that talks to a camera or to
Gemini is exercised on the Pi / with a real key instead (see plan).

Run:  venv/bin/python tests/test_nanny_report.py
"""

import inspect
import io
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

    # Only NANNY_CAM_<number> is a camera. Treating every NANNY_CAM_* var as one
    # made a NANNY_CAM_ROOMS line poison the whole config for all three services.
    cams = load_cameras({"NANNY_CAM_1": "crib=rtsp://1/s",
                         "NANNY_CAM_2": "bed=rtsp://2/s",
                         "NANNY_CAM_ROOMS": "crib:nursery,bed:bedroom",
                         "NANNY_CAM_EXTRA_NOTE": "ignore me"})
    check("NANNY_CAM_ROOMS is not a camera", set(cams) == {"crib", "bed"}, str(cams))
    check("cameras ordered numerically, not lexically",
          list(load_cameras({"NANNY_CAM_10": "j=rtsp://10/s",
                             "NANNY_CAM_2": "b=rtsp://2/s"})) == ["b", "j"])

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
    # Relative to now, never a fixed date: give_up_on_failed_raw() also writes
    # footage off once it is older than RAW_MAX_AGE_HOURS and has been tried,
    # so a hard-coded segment name silently starts failing this test two days
    # after it was written.
    seg_start = (datetime.now() - timedelta(hours=2)).replace(
        minute=0, second=0, microsecond=0)
    budget = nanny_analyze.max_segment_attempts()

    with RawFixture() as fx:
        raw = fx.raw("kitchen", seg_start.strftime("%Y%m%d_%H%M%S") + ".mp4")
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
        # Every event carries a clip, as the analyzer cuts one for each
        # medium/high detection — allowed-absent events must shed theirs.
        return {"camera": cam, "start_iso": iso(s), "end_iso": iso(e),
                "context": ctx, "confidence": conf, "description": "",
                "clip": f"2026-07-27/{cam}_{s.strftime('%H%M')}_phone_1.mp4"}

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
    check("baby-elsewhere minutes accounted for",
          stats["while_baby_absent_minutes"] == 20, str(stats["while_baby_absent_minutes"]))
    # Was 40 before allowed_baby_absent existed: B's 20 minutes are explained
    # now, and must not still be reported as unexplained.
    check("unclear is the remainder", stats["unclear_minutes"] == 20,
          str(stats["unclear_minutes"]))
    check("flagged event count", stats["unauthorized_event_count"] == 3)

    verdicts = [e["authorization"] for e in events]
    check("per-event verdicts", verdicts == [
        "unauthorized", "allowed_baby_absent", "allowed_baby_asleep", "unconfirmed",
        "allowed_baby_asleep", "unauthorized", "unauthorized"], str(verdicts))
    # THE regression that matters: A is also baby_not_in_frame, but the room's
    # other camera saw an awake baby. If allowing "not in frame" ever moves above
    # the flagged branch, stepping just outside one camera's view hides phone use.
    check("cross-room evidence still beats not-in-frame",
          events[0]["authorization"] == "unauthorized")
    check("a flagged not-in-frame event keeps its clip", "clip" in events[0])
    check("an allowed-absent event drops its clip", "clip" not in events[1])
    check("clips elsewhere untouched", "clip" in events[2] and "clip" in events[5])
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


def test_config_errors_never_delete_the_report():
    print("bad config degrades, never aborts")
    import nanny_report, storage

    tmp = tempfile.mkdtemp()
    orig = (nanny_report.CHUNKS_DIR, nanny_report.REPORTS_DIR,
            nanny_common.CHUNKS_DIR, nanny_common.REPORTS_DIR,
            nanny_report.analyze_pending, storage.get_sleep_sessions_range)
    nanny_report.CHUNKS_DIR = nanny_common.CHUNKS_DIR = os.path.join(tmp, "chunks")
    nanny_report.REPORTS_DIR = nanny_common.REPORTS_DIR = os.path.join(tmp, "reports")
    nanny_report.analyze_pending = lambda limit=-1: (0, 0)
    storage.get_sleep_sessions_range = lambda days=7: []
    saved_env = {k: os.environ.get(k) for k in
                 ("NANNY_CAM_1", "NANNY_CAM_ROOMS", "NANNY_WINDOW", "NANNY_DAYS",
                  "GEMINI_API_KEY")}
    try:
        os.makedirs(nanny_report.CHUNKS_DIR)
        os.makedirs(nanny_report.REPORTS_DIR)
        day = date(2026, 7, 27)
        os.makedirs(os.path.join(nanny_report.CHUNKS_DIR, day.isoformat()))
        with open(os.path.join(nanny_report.CHUNKS_DIR, day.isoformat(),
                               "kitchen_100000.json"), "w") as f:
            json.dump({"camera": "kitchen", "segment_start_iso": "2026-07-27T10:00:00",
                       "segment_minutes": 60, "activities": [], "phone_use": [],
                       "notable_events": [], "summary": "ok"}, f)

        os.environ.pop("GEMINI_API_KEY", None)
        os.environ["NANNY_CAM_1"] = "kitchen=rtsp://x/y"
        os.environ["NANNY_CAM_ROOMS"] = "nurserycam:nursery"   # camera does not exist
        os.environ["NANNY_WINDOW"] = "10:00-18:00"
        os.environ["NANNY_DAYS"] = "Mon,Tue,Wed,Thu,Fri"

        cams, rooms, window, days, errors = nanny_report.load_config()
        check("NANNY_CAM_ROOMS is not mistaken for a camera", set(cams) == {"kitchen"},
              str(cams))
        check("bad rooms recorded, not raised",
              len(errors) == 1 and errors[0].startswith("NANNY_CAM_ROOMS"), str(errors))
        check("rooms degrade to empty", rooms == {})

        raised = None
        try:
            nanny_report.main()
        except SystemExit as e:
            raised = e
        check("main() does not abort", raised is None, f"SystemExit({raised})")
        written = os.path.join(nanny_report.REPORTS_DIR, f"{day.isoformat()}.json")
        check("the day's report is still written", os.path.exists(written))
        if os.path.exists(written):
            with open(written) as f:
                rep = json.load(f)
            check("report carries the config error",
                  rep["config_errors"] and "NANNY_CAM_ROOMS" in rep["config_errors"][0])
            check("chunk's own camera still used", rep["cameras"] == ["kitchen"])
    finally:
        (nanny_report.CHUNKS_DIR, nanny_report.REPORTS_DIR,
         nanny_common.CHUNKS_DIR, nanny_common.REPORTS_DIR,
         nanny_report.analyze_pending, storage.get_sleep_sessions_range) = orig
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(tmp)


def test_empty_care_day_still_reported():
    print("a care day with nothing analyzed still appears")
    import nanny_report
    today = date.today()
    weekday_only = {today.weekday()}
    other_day = {(today.weekday() + 1) % 7}
    past = (dtime(0, 1), dtime(0, 2))       # window closed hours ago
    future = (dtime(0, 1), dtime(23, 59))   # still inside the window

    tmp = tempfile.mkdtemp()
    orig = (nanny_report.REPORTS_DIR, nanny_common.REPORTS_DIR)
    nanny_report.REPORTS_DIR = nanny_common.REPORTS_DIR = os.path.join(tmp, "reports")
    try:
        os.makedirs(nanny_report.REPORTS_DIR)
        check("care day, window closed → report it",
              nanny_report.care_day_awaiting_report(today, weekday_only, past))
        check("not a care day → leave it alone",
              not nanny_report.care_day_awaiting_report(today, other_day, past))
        check("still inside the window → wait for tonight",
              not nanny_report.care_day_awaiting_report(today, weekday_only, future))
        with open(os.path.join(nanny_report.REPORTS_DIR, f"{today.isoformat()}.json"),
                  "w") as f:
            json.dump({}, f)
        check("already reported → not again",
              not nanny_report.care_day_awaiting_report(today, weekday_only, past))
    finally:
        nanny_report.REPORTS_DIR, nanny_common.REPORTS_DIR = orig
        shutil.rmtree(tmp)

    # An empty chunk list must still produce a renderable report.
    import storage
    orig_sessions = storage.get_sleep_sessions_range
    key = os.environ.pop("GEMINI_API_KEY", None)
    storage.get_sleep_sessions_range = lambda days=7: []
    try:
        rep = nanny_report.build_report(date(2026, 7, 28), [], ["kitchen", "bedcam"],
                                        (dtime(10, 0), dtime(18, 0)))
    finally:
        storage.get_sleep_sessions_range = orig_sessions
        if key is not None:
            os.environ["GEMINI_API_KEY"] = key
    check("empty day is flagged", rep["no_analysis"] is True)
    check("every camera shows a whole-day gap",
          all(c["gaps"] == [{"whole_day": True}] for c in rep["coverage"].values()))
    check("zeroed phone stats", rep["phone_use"]["total_minutes"] == 0
          and rep["phone_use"]["unauthorized_minutes"] == 0)


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


# ── Day metrics ───────────────────────────────────────────────────────────────

WINDOW = (dtime(10, 0), dtime(18, 0))
DAY = date(2026, 7, 27)


def _iso(h, m=0):
    return datetime.combine(DAY, dtime(h, m)).isoformat()


def _chunk(camera, hour, activities=(), phone=(), minutes=60):
    return {"camera": camera, "segment_start_iso": _iso(hour),
            "segment_minutes": minutes, "activities": list(activities),
            "phone_use": list(phone), "notable_events": [], "summary": ""}


def _act(cat, h0, h1, state="awake", m0=0, m1=0):
    return {"category": cat, "start_iso": _iso(h0, m0), "end_iso": _iso(h1, m1),
            "description": "", "baby_visible": True, "baby_state": state}


def test_sleep_metrics():
    print("sleep metrics (window clipping, crib ∪ camera, cross-check)")
    import nanny_report as R

    # Crib monitor: 09:00-11:00 (overnight tail crosses into the window) and
    # 14:00-15:00. Bedroom camera additionally sees 16:00-16:30, which the crib
    # monitor cannot: there is no crib monitor in the bedroom.
    naps = [(datetime.combine(DAY, dtime(9, 0)), datetime.combine(DAY, dtime(11, 0))),
            (datetime.combine(DAY, dtime(14, 0)), datetime.combine(DAY, dtime(15, 0)))]
    chunks = [_chunk("bedcam", 16, [_act("resting", 16, 16, "asleep", m1=30)])]
    rooms = {"bedcam": "bedroom"}

    m = R.sleep_metrics(DAY, chunks, rooms, naps, WINDOW)
    # 10-11 (clipped from 09:00) + 14-15 + 16:00-16:30 = 60 + 60 + 30
    check("sleep clipped to the care window", m["total_sleep_minutes"] == 150.0,
          str(m["total_sleep_minutes"]))
    check("nap count on the merged union", m["nap_count"] == 3, str(m["nap_count"]))
    check("longest nap", m["longest_nap_minutes"] == 60.0, str(m["longest_nap_minutes"]))
    check("first sleep is the clipped start", m["first_sleep_start_iso"] == _iso(10))
    check("last wake", m["last_wake_iso"] == _iso(16, 30))
    # Awake gaps inside 10-18: 11-14 (180), 15-16 (60), 16:30-18 (90)
    check("longest awake stretch", m["longest_awake_stretch_minutes"] == 180.0,
          str(m["longest_awake_stretch_minutes"]))
    check("crib monitor minutes clipped", m["crib_monitor_minutes"] == 120.0,
          str(m["crib_monitor_minutes"]))
    check("camera-observed minutes", m["camera_observed_minutes"] == 30.0,
          str(m["camera_observed_minutes"]))
    check("the bedroom nap is camera-only", m["camera_only_minutes"] == 30.0,
          str(m["camera_only_minutes"]))
    check("crib-only is what the cameras missed", m["crib_only_minutes"] == 120.0,
          str(m["crib_only_minutes"]))
    check("no agreement in this fixture", m["agreement_minutes"] == 0.0)

    empty = R.sleep_metrics(DAY, [], {}, [], WINDOW)
    check("no sleep → zeros, not a crash",
          empty["total_sleep_minutes"] == 0.0 and empty["nap_count"] == 0
          and empty["first_sleep_start_iso"] is None)
    check("no sleep → the whole window is one awake stretch",
          empty["longest_awake_stretch_minutes"] == 480.0,
          str(empty["longest_awake_stretch_minutes"]))


def test_activity_metrics():
    print("care activities (double coverage counts once)")
    import nanny_report as R

    # The same feeding, seen by two cameras in the same room, with slightly
    # different boundaries — the union must not bill it twice.
    chunks = [
        _chunk("cam1", 10, [_act("feeding", 10, 10, m1=30), _act("play", 11, 12)]),
        _chunk("cam2", 10, [_act("feeding", 10, 10, m0=10, m1=40)]),
        _chunk("cam1", 12, [_act("diaper", 12, 12, m1=10),
                            _act("housework", 17, 19)]),   # runs past the window
    ]
    m = R.activity_metrics(DAY, chunks, {"cam1": "nursery", "cam2": "nursery"}, WINDOW)

    check("overlapping feeding counted once", m["minutes_by_category"]["feeding"] == 40.0,
          str(m["minutes_by_category"]["feeding"]))
    check("one merged feeding event", m["feeding_count"] == 1, str(m["feeding_count"]))
    check("diaper counted", m["diaper_count"] == 1)
    check("housework clipped at the window end",
          m["minutes_by_category"]["housework"] == 60.0,
          str(m["minutes_by_category"]["housework"]))
    check("active care = feeding ∪ play", m["active_care_minutes"] == 100.0,
          str(m["active_care_minutes"]))
    check("categories with no minutes are omitted",
          "sleep_prep" not in m["minutes_by_category"])

    unknown = _chunk("cam1", 10, [_act("napping_on_the_job", 10, 11)])
    m2 = R.activity_metrics(DAY, [unknown], {}, WINDOW)
    check("a category outside the schema is ignored", m2["minutes_by_category"] == {})


def test_attendance_metrics():
    print("attendance (presence outranks absence; gaps are unclear, not neglect)")
    import nanny_report as R

    # 10-11: the caregiver is out of frame with an awake baby and no camera
    # anywhere shows them → the one flaggable span. 11-12: out of frame again,
    # but the kitchen camera has them doing housework → presence clears it.
    chunks = [
        _chunk("nursery1", 10, [_act("out_of_frame", 10, 11)]),
        _chunk("nursery2", 11, [_act("out_of_frame", 11, 12)]),
        _chunk("kitchen", 11, [_act("housework", 11, 12)]),
    ]
    rooms = {"nursery1": "nursery", "nursery2": "nursery", "kitchen": "kitchen"}

    m = R.attendance_metrics(DAY, chunks, rooms, [], WINDOW)
    check("awake baby + nobody in frame is flagged",
          m["unattended_minutes"] == 60.0, str(m["unattended_minutes"]))
    check("longest unattended stretch", m["longest_unattended_stretch_minutes"] == 60.0)
    check("presence in another room clears the second span",
          all(iv["start_iso"] != _iso(11) for iv in m["unattended_intervals"]),
          str(m["unattended_intervals"]))

    # An asleep baby alone in a crib is the normal state of affairs.
    naps = [(datetime.combine(DAY, dtime(10, 0)), datetime.combine(DAY, dtime(11, 0)))]
    m2 = R.attendance_metrics(DAY, chunks, rooms, naps, WINDOW)
    check("a sleeping baby alone is not a finding", m2["unattended_minutes"] == 0.0,
          str(m2["unattended_minutes"]))

    # Only two hours were analyzed at all; the rest of the window is a blind
    # spot and must land in unclear, never in the flagged number.
    check("unanalyzed hours are uncovered", m["uncovered_minutes"] == 360.0,
          str(m["uncovered_minutes"]))
    check("blind spots are unclear, not neglect", m["unclear_minutes"] >= 360.0,
          str(m["unclear_minutes"]))
    check("observed minutes", m["observed_minutes"] == 120.0, str(m["observed_minutes"]))

    # A low-confidence baby_unattended detection can never flag.
    low = [_chunk("nursery1", 13,
                  [_act("play", 13, 14)],
                  [{"start_iso": _iso(13), "end_iso": _iso(14),
                    "context": "baby_unattended", "confidence": "low"}])]
    m3 = R.attendance_metrics(DAY, low, {"nursery1": "nursery"}, [], WINDOW)
    check("low-confidence unattended never flags", m3["unattended_minutes"] == 0.0,
          str(m3["unattended_minutes"]))


# ── Dry-run CLI ───────────────────────────────────────────────────────────────

def _cli_fixture(tmp):
    """chunks/reports dirs under tmp with one day of chunks. Returns the day."""
    import nanny_report, storage
    nanny_report.CHUNKS_DIR = nanny_common.CHUNKS_DIR = os.path.join(tmp, "chunks")
    nanny_report.REPORTS_DIR = nanny_common.REPORTS_DIR = os.path.join(tmp, "reports")
    nanny_common.STATUS_FILE = os.path.join(tmp, "status.json")
    storage.get_sleep_sessions_range = lambda days=7: []
    os.makedirs(os.path.join(nanny_report.CHUNKS_DIR, DAY.isoformat()))
    os.makedirs(nanny_report.REPORTS_DIR)
    with open(os.path.join(nanny_report.CHUNKS_DIR, DAY.isoformat(),
                           "cam1_100000.json"), "w") as f:
        json.dump(_chunk("cam1", 10, [_act("feeding", 10, 11)]), f)
    return DAY


def test_dry_run_writes_nothing():
    print("dry run: no sweep, no writes, no cleanup")
    import nanny_report as R, storage

    tmp = tempfile.mkdtemp()
    orig = (R.CHUNKS_DIR, R.REPORTS_DIR, nanny_common.CHUNKS_DIR,
            nanny_common.REPORTS_DIR, nanny_common.STATUS_FILE,
            R.analyze_pending, R.cleanup, storage.get_sleep_sessions_range)
    swept, cleaned = [], []
    R.analyze_pending = lambda limit=-1: swept.append(limit)
    R.cleanup = lambda: cleaned.append(True)
    saved = {k: os.environ.get(k) for k in ("NANNY_CAM_1", "NANNY_WINDOW", "GEMINI_API_KEY")}
    out = io.StringIO()
    try:
        day = _cli_fixture(tmp)
        os.environ.pop("GEMINI_API_KEY", None)
        os.environ["NANNY_CAM_1"] = "cam1=rtsp://x/y"
        os.environ["NANNY_WINDOW"] = "10:00-18:00"

        stdout, sys.stdout = sys.stdout, out
        try:
            R.main(["--dry-run", "--date", day.isoformat()])
        finally:
            sys.stdout = stdout

        check("no straggler sweep", swept == [], str(swept))
        check("no cleanup", cleaned == [], str(cleaned))
        check("nothing written to the reports dir",
              os.listdir(R.REPORTS_DIR) == [], str(os.listdir(R.REPORTS_DIR)))
        check("no status file", not os.path.exists(nanny_common.STATUS_FILE))
        printed = json.loads(out.getvalue())
        check("the report went to stdout", printed["date"] == day.isoformat())
        check("narrative suppressed under dry run", printed["narrative"] is None)
        check("metrics present", {"sleep", "care", "attendance"} <= set(printed))
        check("care metrics computed",
              printed["care"]["minutes_by_category"]["feeding"] == 60.0)

        # --no-sweep alone still writes; it is only the API call that is skipped.
        R.main(["--no-sweep", "--date", day.isoformat(), "--no-narrative"])
        check("--no-sweep still writes the report",
              os.path.exists(os.path.join(R.REPORTS_DIR, f"{day.isoformat()}.json")))
        check("--no-sweep skipped the sweep", swept == [], str(swept))
    finally:
        (R.CHUNKS_DIR, R.REPORTS_DIR, nanny_common.CHUNKS_DIR,
         nanny_common.REPORTS_DIR, nanny_common.STATUS_FILE,
         R.analyze_pending, R.cleanup, storage.get_sleep_sessions_range) = orig
        for k, v in saved.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
        shutil.rmtree(tmp)


def test_force_and_out():
    print("--force rebuilds, --out redirects")
    import nanny_report as R, storage

    tmp = tempfile.mkdtemp()
    orig = (R.CHUNKS_DIR, R.REPORTS_DIR, nanny_common.CHUNKS_DIR,
            nanny_common.REPORTS_DIR, nanny_common.STATUS_FILE,
            R.analyze_pending, R.cleanup, storage.get_sleep_sessions_range)
    R.analyze_pending = lambda limit=-1: (0, 0)
    R.cleanup = lambda: None
    saved = {k: os.environ.get(k) for k in ("NANNY_CAM_1", "NANNY_WINDOW", "GEMINI_API_KEY")}
    try:
        day = _cli_fixture(tmp)
        os.environ.pop("GEMINI_API_KEY", None)
        os.environ["NANNY_CAM_1"] = "cam1=rtsp://x/y"
        os.environ["NANNY_WINDOW"] = "10:00-18:00"
        live = os.path.join(R.REPORTS_DIR, f"{day.isoformat()}.json")

        R.main(["--date", day.isoformat(), "--no-sweep", "--no-narrative"])
        first = json.load(open(live))["generated_at"]

        # Without --force the day is already reported, so an auto-selected run
        # must leave it exactly as it was.
        R.main(["--no-sweep", "--no-narrative"])
        check("an already-reported day is not rebuilt",
              json.load(open(live))["generated_at"] == first)

        R.main(["--date", day.isoformat(), "--force", "--no-sweep", "--no-narrative"])
        check("--force rebuilds it",
              json.load(open(live))["generated_at"] != first)

        scratch = os.path.join(tmp, "scratch")
        live_before = json.load(open(live))["generated_at"]
        status_before = open(nanny_common.STATUS_FILE).read()
        R.main(["--date", day.isoformat(), "--out", scratch, "--no-sweep",
                "--no-narrative"])
        check("--out writes there", os.path.exists(
            os.path.join(scratch, f"{day.isoformat()}.json")))
        check("--out leaves the live report alone",
              json.load(open(live))["generated_at"] == live_before)
        check("--out does not claim a production run",
              open(nanny_common.STATUS_FILE).read() == status_before)
    finally:
        (R.CHUNKS_DIR, R.REPORTS_DIR, nanny_common.CHUNKS_DIR,
         nanny_common.REPORTS_DIR, nanny_common.STATUS_FILE,
         R.analyze_pending, R.cleanup, storage.get_sleep_sessions_range) = orig
        for k, v in saved.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
        shutil.rmtree(tmp)


# ── Cross-camera phone merge / person attribution / clip hygiene ──────────────

def _phone(camera, room, h0, m0, h1, m1, conf="high", ctx="unclear",
           person="caregiver", clip=None):
    ev = {"camera": camera, "room": room, "confidence": conf, "context": ctx,
          "person": person, "description": f"{camera} view",
          "start_iso": _iso(h0, m0), "end_iso": _iso(h1, m1)}
    if clip:
        ev["clip"] = clip
    return ev


def test_merge_phone_events():
    print("two angles on one phone pickup are one event")
    import nanny_report as R

    # Same room, overlapping: 10:04-10:06 and 10:05-10:07.
    events = [
        _phone("nursery1", "nursery", 10, 4, 10, 6, conf="medium",
               ctx="baby_not_in_frame", clip="d/nursery1_phone_1.mp4"),
        _phone("nursery2", "nursery", 10, 5, 10, 7, conf="high",
               ctx="while_holding_baby", clip="d/nursery2_phone_1.mp4"),
        # Different room at the same time: a genuinely separate observation.
        _phone("kitchen", "kitchen", 10, 5, 10, 6, clip="d/kitchen_phone_1.mp4"),
    ]
    merged = R.merge_phone_events(events)
    check("nursery pair collapsed, kitchen kept", len(merged) == 2, str(len(merged)))

    nursery = [m for m in merged if m["room"] == "nursery"][0]
    check("span is the union", nursery["start_iso"] == _iso(10, 4)
          and nursery["end_iso"] == _iso(10, 7), str(nursery))
    check("highest confidence wins", nursery["confidence"] == "high")
    check("most specific context wins", nursery["context"] == "while_holding_baby")
    check("one clip, from the most confident camera",
          nursery["clip"] == "d/nursery2_phone_1.mp4", nursery.get("clip"))
    check("the other angle is credited, not shown",
          nursery.get("also_seen_by") == ["nursery1"], str(nursery.get("also_seen_by")))
    check("the other room is untouched",
          [m for m in merged if m["room"] == "kitchen"][0]["clip"] == "d/kitchen_phone_1.mp4")

    # A chain of staggered overlaps is still one event.
    chain = [_phone("a", "r", 10, 0, 10, 2), _phone("b", "r", 10, 1, 10, 5),
             _phone("c", "r", 10, 4, 10, 6)]
    check("staggered chain collapses", len(R.merge_phone_events(chain)) == 1)
    # Far apart in the same room: two real events.
    apart = [_phone("a", "r", 10, 0, 10, 2), _phone("a", "r", 11, 0, 11, 2)]
    check("distant events stay separate", len(R.merge_phone_events(apart)) == 2)
    # Unparseable timestamps must never be silently dropped.
    bad = [{"camera": "a", "room": "r", "start_iso": "nonsense"}]
    check("unusable events pass through", len(R.merge_phone_events(bad)) == 1)


def test_care_timeline():
    print("day at a glance merges the model's fragments")
    from nanny_report import care_timeline
    d = datetime(2026, 8, 27)
    def t(h, m): return d.replace(hour=h, minute=m).isoformat()
    def act(cat, s, e, cam="nurserycam", **kw):
        return {"category": cat, "start_iso": s, "end_iso": e, "camera": cam,
                "room": "nursery", "description": kw.pop("desc", ""), **kw}

    # The real 2026-08-27 feeding spans: three actual feeds arriving as twelve
    # fragments, the last one split seven ways.
    feeds = [(10, 2, 10, 19),
             (13, 33, 13, 38), (13, 46, 13, 58), (14, 0, 14, 2), (14, 3, 14, 8),
             (16, 29, 16, 33), (16, 33, 16, 35), (16, 40, 16, 52), (16, 52, 16, 53),
             (16, 53, 16, 57), (16, 58, 17, 0), (17, 3, 17, 4)]
    timeline = [act("feeding", t(a, b), t(c, e)) for a, b, c, e in feeds]
    # Same play session seen by both nursery cameras — activities are the one event
    # family that never got a cross-camera merge before this.
    timeline += [act("play", t(14, 13), t(14, 21), play_types=["tummy_time"],
                     desc="on the mat"),
                 act("play", t(14, 14), t(14, 20), cam="nurserycam2",
                     play_types=["floor_toys"], desc="reaching for a rattle toy")]
    timeline += [act("housework", t(11, 0), t(11, 30)),
                 act("holding_baby", t(12, 0), t(12, 40)),
                 act("out_of_frame", t(15, 0), t(15, 20))]
    naps = [(d.replace(hour=11, minute=28), d.replace(hour=12, minute=26))]

    ct = care_timeline(timeline, naps)
    ev = ct["events"]
    feed_rows = [e for e in ev if e["category"] == "feeding"]
    check("twelve feeding fragments become three feeds",
          len(feed_rows) == 3, f"got {len(feed_rows)}")
    seven = [e for e in feed_rows if e["parts"] == 7]
    check("the seven-way 16:29-17:04 feed merges to one",
          len(seven) == 1 and seven[0]["start_iso"] == t(16, 29)
          and seven[0]["end_iso"] == t(17, 4), str(seven))
    # Real feeds sit ~4h apart at this age, so the 10-minute window must never
    # reach across them — a merged 10:02-17:04 "feed" would be worse than fragments.
    check("feeds hours apart stay separate",
          [e["start_iso"] for e in feed_rows] == [t(10, 2), t(13, 33), t(16, 29)],
          str([e["start_iso"] for e in feed_rows]))

    play = [e for e in ev if e["category"] == "play"]
    check("both cameras' view of one play session merge", len(play) == 1, str(len(play)))
    check("play types union across cameras",
          sorted(play[0]["play_types"]) == ["floor_toys", "tummy_time"],
          str(play[0]["play_types"]))
    check("both cameras credited", sorted(play[0]["cameras"]) ==
          ["nurserycam", "nurserycam2"], str(play[0]["cameras"]))
    check("the fullest description wins",
          play[0]["description"] == "reaching for a rattle toy", play[0]["description"])

    sleep = [e for e in ev if e["category"] == "sleep"]
    check("sleep comes from naps, not the timeline",
          len(sleep) == 1 and sleep[0]["start_iso"] == t(11, 28), str(sleep))
    check("events are in time order",
          [e["start_iso"] for e in ev] == sorted(e["start_iso"] for e in ev))

    # Demoted, never dropped: the parent should still be able to account for the day.
    b = ct["bands"]
    check("holding folds into the with-baby band", len(b["with_baby"]) == 1)
    check("housework and out-of-frame fold into the other band",
          len(b["away"]) == 2, str(b["away"]))
    check("no headline category leaks into the bands",
          not any(x["start_iso"] == t(10, 2) for x in b["with_baby"] + b["away"]))


def test_care_timeline_has_one_truth_per_moment():
    print("one thing at a time, inside the shift")
    from nanny_report import care_timeline
    d = datetime(2026, 8, 28)
    win = (dtime(10, 0), dtime(18, 0))
    def t(h, m, sec=0): return d.replace(hour=h, minute=m, second=sec).isoformat()
    def act(cat, s, e, cam="nurserycam", room="nursery"):
        return {"category": cat, "start_iso": s, "end_iso": e, "camera": cam,
                "room": room, "description": ""}

    # The real 2026-08-28 contradiction: the crib monitor called a nap from 16:28
    # to 17:03 while cameras watched play, a diaper change and more play inside it.
    naps = [(d.replace(hour=16, minute=28), d.replace(hour=17, minute=3)),
            (d.replace(hour=1, minute=0), d.replace(hour=4, minute=0))]   # overnight
    timeline = [
        act("play",   t(16, 37), t(16, 52)),
        act("diaper", t(16, 52), t(17, 17)),
        act("play",   t(16, 57), t(16, 59), cam="nurserycam2"),
        # Two cameras on one moment at different specificity — 71 of these a day.
        act("feeding", t(10, 7), t(10, 8)),
        act("holding_baby", t(10, 7), t(10, 11), cam="nurserycam2"),
        # Same rank, wildly different length: a 32s "diaper" inside a 35m feed.
        act("feeding", t(11, 0), t(11, 35)),
        act("diaper",  t(11, 20), t(11, 20, 32)),
    ]
    ev = care_timeline(timeline, naps, window=win, day=d.date())["events"]

    # The invariant this whole change exists to create.
    pairs = [(a, b) for i, a in enumerate(ev) for b in ev[i + 1:]
             if b["start_iso"] < a["end_iso"]]
    check("no two events overlap", not pairs,
          str([(a["category"], b["category"]) for a, b in pairs][:3]))
    check("everything is inside 10:00-18:00",
          all("10:00" <= e["start_iso"][11:16] < "18:00" for e in ev),
          str([e["start_iso"][11:16] for e in ev]))
    check("the overnight nap is off the card",
          not any(e["category"] == "sleep" and e["start_iso"][11:16] < "10:00" for e in ev))

    sleeps = [e for e in ev if e["category"] == "sleep"]
    check("the contradicted nap is trimmed, not deleted",
          len(sleeps) == 1 and sleeps[0]["end_iso"] == t(16, 37),
          str([(s["start_iso"][11:16], s["end_iso"][11:16]) for s in sleeps]))

    # holding_baby never competes for a row — it is a demoted category and lands in
    # the bands — so the feeding survives the moment intact.
    at_1007 = [e for e in ev if e["start_iso"] == t(10, 7)]
    check("the specific act keeps the moment", len(at_1007) == 1
          and at_1007[0]["category"] == "feeding", str(at_1007))
    check("the generic view is demoted, not shown as a rival row",
          not any(e["category"] == "holding_baby" for e in ev))

    # Where two headline categories really do collide, rank decides. This is the
    # only such collision that reaches the card: 4 diaper-vs-play pairs on 08-28.
    ev2 = care_timeline(
        [act("play", t(15, 20), t(15, 40)), act("diaper", t(15, 18), t(15, 25))],
        [], window=win, day=d.date())["events"]
    check("diaper outranks play where they overlap",
          [(e["category"], e["start_iso"][11:16], e["end_iso"][11:16]) for e in ev2]
          == [("diaper", "15:18", "15:25"), ("play", "15:25", "15:40")],
          str([(e["category"], e["start_iso"][11:16], e["end_iso"][11:16]) for e in ev2]))

    check("a 32-second diaper does not split a 35-minute feed",
          any(e["category"] == "feeding" and e["start_iso"] == t(11, 0)
              and e["end_iso"] == t(11, 35) for e in ev),
          str([(e["category"], e["start_iso"][11:19], e["end_iso"][11:19]) for e in ev]))


def test_merge_notable_events():
    print("two camera descriptions of one notable incident are one finding")
    import nanny_report as R

    events = [
        {"camera": "nursery1", "room": "nursery", "time_iso": _iso(10, 5),
         "type": "other", "description": "Baby slipped near the couch",
         "clip": "d/nursery1_notable_1.mp4"},
        {"camera": "nursery2", "room": "nursery", "time_iso": _iso(10, 5),
         "type": "safety_concern", "description": "Baby fell from the couch",
         "clip": "d/nursery2_notable_1.mp4"},
        {"camera": "kitchen", "room": "kitchen", "time_iso": _iso(10, 5),
         "type": "visitor", "description": "Delivery at the door"},
    ]
    merged = R.merge_notable_events(events)
    check("same-room observations collapse, other room remains",
          len(merged) == 2, str(merged))
    nursery = [e for e in merged if e["room"] == "nursery"][0]
    check("the safety interpretation wins", nursery["type"] == "safety_concern",
          str(nursery))
    check("one strongest clip remains",
          nursery["clip"] == "d/nursery2_notable_1.mp4", str(nursery))
    check("the corroborating camera is credited",
          nursery.get("also_seen_by") == ["nursery1"], str(nursery))

    apart = [dict(events[0]), dict(events[1])]
    apart[1]["time_iso"] = _iso(10, 7)
    check("separate moments stay separate", len(R.merge_notable_events(apart)) == 2)


def test_person_attribution():
    print("only the caregiver's phone minutes are scored")
    import nanny_report as R

    naps = []
    awake = [_chunk("cam1", 10, [_act("play", 10, 12)])]
    rooms = {"cam1": "nursery"}

    def stats_for(person):
        ev = _phone("cam1", "nursery", 10, 0, 10, 30, ctx="baby_nearby_awake",
                    person=person)
        st, _ = R.classify_phone_use([ev], awake, rooms, naps)
        return st, ev

    st, ev = stats_for("caregiver")
    check("caregiver is flagged", st["unauthorized_minutes"] == 30.0
          and ev["authorization"] == "unauthorized", str(st))

    st, ev = stats_for("unclear")
    check("unattributed is still flagged", st["unauthorized_minutes"] == 30.0,
          str(st["unauthorized_minutes"]))

    st, ev = stats_for("other_adult")
    check("another adult counts toward nothing",
          st["unauthorized_minutes"] == 0.0 and st["total_minutes"] == 0.0, str(st))
    check("and is labelled as such", ev["authorization"] == "not_caregiver",
          ev.get("authorization"))
    check("but is still counted separately", st["other_adult_event_count"] == 1
          and st["event_count"] == 0, str(st))


def test_clip_pruning():
    print("superseded clips are pruned, unreported days are not touched")
    import nanny_report as R

    tmp = tempfile.mkdtemp()
    orig = (R.CLIPS_DIR, R.REPORTS_DIR, nanny_common.CLIPS_DIR, nanny_common.REPORTS_DIR)
    R.CLIPS_DIR = nanny_common.CLIPS_DIR = os.path.join(tmp, "clips")
    R.REPORTS_DIR = nanny_common.REPORTS_DIR = os.path.join(tmp, "reports")
    try:
        reported, pending = "2026-07-27", "2026-07-28"
        for day in (reported, pending):
            os.makedirs(os.path.join(R.CLIPS_DIR, day))
            for f in ("keep.mp4", "superseded.mp4"):
                open(os.path.join(R.CLIPS_DIR, day, f), "w").close()
        os.makedirs(R.REPORTS_DIR)
        with open(os.path.join(R.REPORTS_DIR, f"{reported}.json"), "w") as f:
            json.dump({"phone_use": {"events": [{"clip": f"{reported}/keep.mp4"}]}}, f)

        R.prune_superseded_clips()
        left = sorted(os.listdir(os.path.join(R.CLIPS_DIR, reported)))
        check("the referenced clip survives", left == ["keep.mp4"], str(left))
        untouched = sorted(os.listdir(os.path.join(R.CLIPS_DIR, pending)))
        check("a day with no report keeps everything",
              untouched == ["keep.mp4", "superseded.mp4"], str(untouched))
    finally:
        (R.CLIPS_DIR, R.REPORTS_DIR,
         nanny_common.CLIPS_DIR, nanny_common.REPORTS_DIR) = orig
        shutil.rmtree(tmp)


# ── Coverage honesty / verdict / retention ────────────────────────────────────

def test_coverage_counts_failed_pieces():
    print("a failed piece is a coverage gap, not analyzed time")
    import nanny_report as R

    partial = _chunk("cam1", 10)
    partial["unanalyzed_intervals"] = [{"start_iso": _iso(10, 30), "end_iso": _iso(11)}]
    cov = R.coverage_for([partial, _chunk("cam1", 11)], ["cam1"], WINDOW)["cam1"]
    check("the lost half hour is not counted as analyzed",
          cov["analyzed_minutes"] == 90, str(cov["analyzed_minutes"]))
    check("and shows up as a real gap",
          {"start_iso": _iso(10, 30), "end_iso": _iso(11)} in cov["gaps"], str(cov["gaps"]))

    # A chunk written before this existed must read exactly as it used to.
    old = R.coverage_for([_chunk("cam1", 10)], ["cam1"], WINDOW)["cam1"]
    check("pre-change chunks still count in full", old["analyzed_minutes"] == 60,
          str(old["analyzed_minutes"]))


def test_room_decision_coverage():
    print("redundant camera failure does not manufacture a room blind spot")
    import nanny_report as R

    # nursery1 covers the whole window; nursery2 is silent. The bedroom is also
    # fully covered. Capture diagnostics must still expose nursery2 as failed,
    # while the decision coverage correctly remains complete.
    chunks = []
    for hour in range(10, 18):
        chunks.extend([_chunk("nursery1", hour), _chunk("bedcam", hour)])
    rooms = {"nursery1": "nursery", "nursery2": "nursery", "bedcam": "bedroom"}
    cameras = list(rooms)
    per_camera = R.coverage_for(chunks, cameras, WINDOW, day=DAY)
    per_room = R.room_coverage_for(DAY, chunks, cameras, WINDOW, rooms)

    check("silent redundant camera remains visible in diagnostics",
          per_camera["nursery2"]["analyzed_minutes"] == 0, str(per_camera))
    check("the other angle fully covers the nursery",
          per_room["nursery"]["analyzed_minutes"] == 480, str(per_room))
    check("decision coverage uses rooms, not the weakest camera",
          R.decision_coverage_pct(per_room) == 100, str(per_room))

    report = {"notable_events": [],
              "phone_use": {"unauthorized_minutes": 0, "unauthorized_event_count": 0},
              "coverage": per_camera, "room_coverage": per_room,
              "decision_coverage_pct": R.decision_coverage_pct(per_room),
              "failures": [{"camera": "nursery2"}], "config_errors": [],
              "no_analysis": False}
    check("a redundant camera failure no longer degrades the day",
          R.day_verdict(report)["level"] == "clear", str(R.day_verdict(report)))

    # Losing a whole room is still a real blind spot and must degrade the day.
    bedroom_missing = R.room_coverage_for(
        DAY, [c for c in chunks if c["camera"] != "bedcam"], cameras, WINDOW, rooms)
    report["decision_coverage_pct"] = R.decision_coverage_pct(bedroom_missing)
    check("a missing room still degrades the day",
          R.day_verdict(report)["level"] == "degraded", str(R.day_verdict(report)))


def test_day_verdict():
    print("the verdict a parent reads first")
    import nanny_report as R

    full = {"cam1": {"analyzed_minutes": 480, "window_minutes": 480}}
    def rep(**kw):
        base = {"notable_events": [], "phone_use": {"unauthorized_minutes": 0,
                                                    "unauthorized_event_count": 0},
                "coverage": full, "failures": [], "config_errors": [],
                "no_analysis": False}
        base.update(kw)
        return R.day_verdict(base)

    check("a clean, fully covered day is clear", rep()["level"] == "clear")
    check("flagged minutes need attention",
          rep(phone_use={"unauthorized_minutes": 12, "unauthorized_event_count": 2})["level"]
          == "attention")
    check("a safety concern outranks flagged minutes",
          rep(notable_events=[{"type": "safety_concern", "description": "left on couch"}],
              phone_use={"unauthorized_minutes": 12, "unauthorized_event_count": 1}
              )["level"] == "concern")
    thin = rep(coverage={"cam1": {"analyzed_minutes": 120, "window_minutes": 480}})
    check("a day nobody watched is not a clean day", thin["level"] == "degraded", thin["level"])
    check("and says why", any("25%" in r for r in thin["reasons"]), str(thin["reasons"]))
    check("no analysis at all is degraded", rep(no_analysis=True)["level"] == "degraded")

    # Degradation qualifies a finding rather than replacing it.
    both = rep(phone_use={"unauthorized_minutes": 12, "unauthorized_event_count": 1},
               coverage={"cam1": {"analyzed_minutes": 120, "window_minutes": 480}})
    check("a flag in thin coverage is still a flag", both["level"] == "attention")
    check("but carries the coverage caveat",
          any("25%" in r for r in both["reasons"]), str(both["reasons"]))


def test_retention():
    print("chunks die with their clips; unmerged days survive")
    import nanny_report as R

    tmp = tempfile.mkdtemp()
    orig = (R.CHUNKS_DIR, R.REPORTS_DIR, R.CLIPS_DIR,
            nanny_common.CHUNKS_DIR, nanny_common.REPORTS_DIR, nanny_common.CLIPS_DIR)
    R.CHUNKS_DIR = nanny_common.CHUNKS_DIR = os.path.join(tmp, "chunks")
    R.REPORTS_DIR = nanny_common.REPORTS_DIR = os.path.join(tmp, "reports")
    R.CLIPS_DIR = nanny_common.CLIPS_DIR = os.path.join(tmp, "clips")
    saved = {k: os.environ.get(k) for k in
             ("NANNY_CHUNK_RETENTION_DAYS", "NANNY_REPORT_RETENTION_DAYS")}
    try:
        os.makedirs(R.REPORTS_DIR)
        old = date.today() - timedelta(days=40)
        recent = date.today() - timedelta(days=2)
        unmerged = date.today() - timedelta(days=30)
        for day in (old, recent, unmerged):
            os.makedirs(os.path.join(R.CHUNKS_DIR, day.isoformat()))
        for day in (old, recent):
            with open(os.path.join(R.REPORTS_DIR, f"{day.isoformat()}.json"), "w") as f:
                json.dump({"date": day.isoformat()}, f)

        os.environ["NANNY_CHUNK_RETENTION_DAYS"] = "14"
        R.prune_old_chunks()
        check("old merged chunks pruned",
              not os.path.isdir(os.path.join(R.CHUNKS_DIR, old.isoformat())))
        check("recent chunks kept",
              os.path.isdir(os.path.join(R.CHUNKS_DIR, recent.isoformat())))
        check("an unmerged day is never pruned, however old",
              os.path.isdir(os.path.join(R.CHUNKS_DIR, unmerged.isoformat())))

        os.environ["NANNY_REPORT_RETENTION_DAYS"] = "365"
        R.prune_old_reports()
        check("reports well inside retention survive",
              os.path.exists(os.path.join(R.REPORTS_DIR, f"{old.isoformat()}.json")))
        os.environ["NANNY_REPORT_RETENTION_DAYS"] = "30"
        R.prune_old_reports()
        check("and prune once past it",
              not os.path.exists(os.path.join(R.REPORTS_DIR, f"{old.isoformat()}.json")))
    finally:
        (R.CHUNKS_DIR, R.REPORTS_DIR, R.CLIPS_DIR,
         nanny_common.CHUNKS_DIR, nanny_common.REPORTS_DIR, nanny_common.CLIPS_DIR) = orig
        for k, v in saved.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
        shutil.rmtree(tmp)


def test_notable_clips_survive_pruning():
    print("notable-event clips are not swept as unreferenced")
    import nanny_report as R

    tmp = tempfile.mkdtemp()
    orig = (R.CLIPS_DIR, R.REPORTS_DIR, nanny_common.CLIPS_DIR, nanny_common.REPORTS_DIR)
    R.CLIPS_DIR = nanny_common.CLIPS_DIR = os.path.join(tmp, "clips")
    R.REPORTS_DIR = nanny_common.REPORTS_DIR = os.path.join(tmp, "reports")
    try:
        day = "2026-07-27"
        os.makedirs(os.path.join(R.CLIPS_DIR, day))
        os.makedirs(R.REPORTS_DIR)
        for f in ("phone.mp4", "notable.mp4", "orphan.mp4"):
            open(os.path.join(R.CLIPS_DIR, day, f), "w").close()
        with open(os.path.join(R.REPORTS_DIR, f"{day}.json"), "w") as f:
            json.dump({"phone_use": {"events": [{"clip": f"{day}/phone.mp4"}]},
                       "notable_events": [{"clip": f"{day}/notable.mp4"}]}, f)
        R.prune_superseded_clips()
        left = sorted(os.listdir(os.path.join(R.CLIPS_DIR, day)))
        check("the safety-concern clip survives", left == ["notable.mp4", "phone.mp4"], str(left))
    finally:
        (R.CLIPS_DIR, R.REPORTS_DIR,
         nanny_common.CLIPS_DIR, nanny_common.REPORTS_DIR) = orig
        shutil.rmtree(tmp)


def test_context_staleness_is_a_warning():
    print("a stale context warns; it never reads as broken config")
    import nanny_report as R

    tmp = tempfile.mkdtemp()
    saved = os.environ.get("NANNY_CONTEXT_FILE")
    try:
        path = os.path.join(tmp, "ctx.md")
        os.environ["NANNY_CONTEXT_FILE"] = path
        check("no context file at all is called out",
              any("cannot tell the caregiver" in w for w in R.pipeline_warnings()))

        with open(path, "w") as f:
            f.write("Caregiver: Ana.\n")
        check("a fresh context is silent", R.pipeline_warnings() == [],
              str(R.pipeline_warnings()))

        old = (datetime.now() - timedelta(days=200)).timestamp()
        os.utime(path, (old, old))
        warns = R.pipeline_warnings()
        check("a stale context warns", len(warns) == 1 and "200 days" in warns[0], str(warns))
        # The distinction that matters: warnings never degrade the report.
        cams, rooms, window, days, errors = R.load_config()
        check("and is not a config error", errors == [], str(errors))
    finally:
        if saved is None:
            os.environ.pop("NANNY_CONTEXT_FILE", None)
        else:
            os.environ["NANNY_CONTEXT_FILE"] = saved
        shutil.rmtree(tmp)


def test_neon_reports_are_an_archive():
    print("nanny reports UPSERT into Neon, never snapshot over it")
    import backup_sync

    class FakeCursor:
        def __init__(self): self.statements = []
        def executemany(self, sql, rows): self.statements.append((sql, list(rows)))

    cur = FakeCursor()
    rows = [("2026-07-27", "2026-07-27T18:45:00", '{"date": "2026-07-27"}'),
            ("2026-07-28", "2026-07-28T18:45:00", '{"date": "2026-07-28"}')]
    n = backup_sync.sync_nanny_reports(cur, rows)
    check("every local report is sent", n == 2)
    sql = cur.statements[0][0]
    # The whole point: local is a window over an archive Neon alone keeps, so a
    # DELETE here would destroy every report older than local retention.
    check("no DELETE anywhere near this table", "DELETE" not in sql.upper(), sql)
    check("conflicts update in place", "ON CONFLICT" in sql and "DO UPDATE" in sql)
    check("keyed by date", "report_date" in sql)
    check("nothing to send is a no-op", backup_sync.sync_nanny_reports(cur, []) == 0)

    # The guard that protects the snapshot tables must not be applied here:
    # fewer local rows than remote is the designed steady state.
    src = inspect.getsource(backup_sync.main)
    guarded = src.split("sync_nanny_reports")[0]
    check("shrinkage guard is not applied to reports",
          guarded.count("guard_shrinkage") == 2, str(guarded.count("guard_shrinkage")))


def main():
    for fn in (test_env_parsing, test_segments_and_offsets, test_pending_segments,
               test_preflight, test_missing_toolchain_deletes_nothing,
               test_give_up_policy, test_disk_pressure,
               test_chunk_tolerance, test_interval_math, test_phone_policy,
               test_nap_windows_clipping, test_unreported_dates,
               test_report_failures, test_config_errors_never_delete_the_report,
               test_empty_care_day_still_reported, test_coverage,
               test_sleep_metrics, test_activity_metrics, test_attendance_metrics,
               test_dry_run_writes_nothing, test_force_and_out,
               test_merge_phone_events, test_merge_notable_events,
               test_person_attribution, test_clip_pruning,
               test_coverage_counts_failed_pieces, test_room_decision_coverage,
               test_day_verdict, test_retention,
               test_notable_clips_survive_pruning, test_context_staleness_is_a_warning,
               test_care_timeline, test_care_timeline_has_one_truth_per_moment,
               test_neon_reports_are_an_archive):
        fn()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S): {', '.join(FAILURES)}")
        sys.exit(1)
    print("All nanny-report tests passed.")


if __name__ == "__main__":
    main()
