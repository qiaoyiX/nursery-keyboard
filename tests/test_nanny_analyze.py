"""
Unit tests for the analyzer's quota plumbing — the part that decides HOW OFTEN
and HOW HARD we hit Gemini: error classification, server-suggested backoff, the
token pacer, per-piece checkpointing and the piece merge. No key, no network,
no ffmpeg (analyze_video's imports are function-local by design).

Run:  venv/bin/python tests/test_nanny_analyze.py
"""

import inspect
import os
import shutil
import sys
import tempfile
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import nanny_common
import nanny_analyze
from nanny_analyze import (
    MIN_REQUEST_GAP_S, Pacer, TruncatedResponse, drop_partial, is_retryable,
    load_partial, merge_pieces, part_note, retry_delay_seconds, save_partial,
    status_code,
)

FAILURES = []


def check(name, cond, detail=""):
    status = "ok" if cond else "FAIL"
    print(f"  [{status}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


class FakeAPIError(Exception):
    """Shaped like google.genai.errors.APIError (code + details)."""

    def __init__(self, code, details=None, message=""):
        super().__init__(f"{code} {message} {details}")
        self.code = code
        self.details = details or {}


# ── Error classification ──────────────────────────────────────────────────────

def test_error_classification():
    print("error classification")
    check("429 retryable", is_retryable(FakeAPIError(429)))
    check("500 retryable", is_retryable(FakeAPIError(500)))
    check("503 retryable", is_retryable(FakeAPIError(503)))
    check("400 not retryable", not is_retryable(FakeAPIError(400)))
    check("403 not retryable", not is_retryable(FakeAPIError(403)))
    check("404 not retryable", not is_retryable(FakeAPIError(404)))
    check("timeout retryable", is_retryable(TimeoutError("processing timed out")))
    check("truncation retryable", is_retryable(TruncatedResponse("finish_reason=MAX_TOKENS")))
    check("unknown error not retryable", not is_retryable(ValueError("nope")))
    check("code from attribute", status_code(FakeAPIError(429)) == 429)
    check("code from message", status_code(Exception("got 503 Service Unavailable")) == 503)
    # A 24-hour quota window in the text must not read as a status code.
    check("no false code", status_code(Exception("quota exceeded")) is None)


def test_retry_delay():
    print("server-suggested retry delay")
    err = FakeAPIError(429, {"error": {"details": [
        {"@type": "type.googleapis.com/google.rpc.QuotaFailure"},
        {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "31s"}]}})
    check("nested retryDelay found", retry_delay_seconds(err) == 31.0)
    check("fractional seconds", retry_delay_seconds(
        FakeAPIError(429, {"retry_delay": "7.5s"})) == 7.5)
    check("none when absent", retry_delay_seconds(FakeAPIError(500)) is None)
    check("stringified fallback", retry_delay_seconds(
        Exception("RESOURCE_EXHAUSTED ... 'retryDelay': '12s'}")) == 12.0)


# ── Pacing ────────────────────────────────────────────────────────────────────

def test_pacer():
    print("pacer")
    p = Pacer(budget=200_000)
    check("first call is free", p.next_allowed is None)

    # A camera-hour at ~240k tokens costs more than a minute of budget.
    p.charge(240_000)
    gap = p.next_allowed - _now()
    check("240k tokens on a 200k budget → >60s gap", 60 <= gap <= 90, f"{gap:.0f}s")

    # A 30-minute piece is ~120k → about 36s, still above the floor.
    p2 = Pacer(budget=200_000)
    p2.charge(120_000)
    gap2 = p2.next_allowed - _now()
    check("120k tokens → ~36s gap", 30 <= gap2 <= 45, f"{gap2:.0f}s")

    # Tiny calls still get the minimum gap, never a tight loop.
    p3 = Pacer(budget=200_000)
    p3.charge(500)
    gap3 = p3.next_allowed - _now()
    check("tiny call still spaced", gap3 >= MIN_REQUEST_GAP_S * 0.9, f"{gap3:.0f}s")

    # A 429's backoff must not be shortened by a later cheap charge.
    p4 = Pacer(budget=200_000)
    p4.defer(120)
    after_429 = p4.next_allowed
    p4.charge(1000)
    check("429 backoff is never shortened", p4.next_allowed >= after_429)

    # A bigger quota means less waiting: that is the whole point of the knob.
    p5 = Pacer(budget=1_000_000)
    p5.charge(240_000)
    check("larger budget paces less", (p5.next_allowed - _now()) < gap)


def _now():
    import time
    return time.monotonic()


# ── Piece bookkeeping ─────────────────────────────────────────────────────────

def piece(minute, *, summary="s", tokens=1000, phone=(), parse_error=False):
    day = f"2026-07-27T{10 + minute // 60:02d}:{minute % 60:02d}:00"
    if parse_error:
        # What process_segment records for a piece that never came back.
        return {"parse_error": True, "error": "truncated", "usage": {},
                "activities": [], "phone_use": [], "notable_events": [], "summary": ""}
    return {
        "camera": "living", "room": "living room",
        "segment_start_iso": day, "segment_minutes": 30,
        "usage": {"input_tokens": tokens, "output_tokens": 10},
        "dropped_events": 1,
        "activities": [{"start_iso": day, "end_iso": day, "category": "play",
                        "description": "", "baby_visible": True,
                        "baby_state": "awake"}],
        "phone_use": list(phone),
        "notable_events": [{"time_iso": day, "type": "other", "description": ""}],
        "summary": summary,
    }


def test_merge_pieces():
    print("piece merge")
    seg_start = datetime(2026, 7, 27, 10, 0)
    late = {"start_iso": "2026-07-27T10:40:00", "end_iso": "2026-07-27T10:45:00",
            "context": "unclear", "description": "", "confidence": "high"}
    early = {"start_iso": "2026-07-27T10:05:00", "end_iso": "2026-07-27T10:07:00",
             "context": "unclear", "description": "", "confidence": "low"}
    merged = merge_pieces([piece(0, summary="First half.", tokens=120_000, phone=[early]),
                           piece(30, summary="Second half.", tokens=118_000, phone=[late])],
                          "living", "living room", seg_start, 60, "m")

    check("one chunk per segment", merged["segment_start_iso"] == seg_start.isoformat()
          and merged["segment_minutes"] == 60)
    check("tokens summed", merged["usage"]["input_tokens"] == 238_000)
    check("dropped counts summed", merged["dropped_events"] == 2)
    check("events concatenated", len(merged["activities"]) == 2
          and len(merged["notable_events"]) == 2)
    check("phone events sorted by time",
          [p["start_iso"] for p in merged["phone_use"]]
          == ["2026-07-27T10:05:00", "2026-07-27T10:40:00"])
    check("summaries joined", merged["summary"] == "First half. Second half.")
    check("clean merge has no parse_error", "parse_error" not in merged)

    partial = merge_pieces([piece(0), piece(30, parse_error=True)],
                           "living", "living room", seg_start, 60, "m")
    check("failed piece flagged", partial["parse_error"] is True
          and partial["failed_pieces"] == 1 and partial["pieces"] == 2)
    check("good piece survives a bad sibling", len(partial["activities"]) == 1)


def test_partial_checkpoint():
    print("checkpoint resume")
    tmp = tempfile.mkdtemp()
    old_chunks = nanny_common.CHUNKS_DIR
    nanny_common.CHUNKS_DIR = os.path.join(tmp, "chunks")
    try:
        seg_start = datetime(2026, 7, 27, 10, 0)
        check("no checkpoint yet", load_partial("living", seg_start, 2) == {})

        save_partial("living", seg_start, 2, {0: piece(0)})
        resumed = load_partial("living", seg_start, 2)
        check("checkpoint round-trips", set(resumed) == {0}
              and resumed[0]["summary"] == "s")

        # Changing NANNY_PIECE_MINUTES re-splits the video: old pieces no longer
        # line up, so the checkpoint must be thrown away rather than merged.
        check("split change invalidates", load_partial("living", seg_start, 4) == {})

        drop_partial("living", seg_start)
        check("dropped after success", load_partial("living", seg_start, 2) == {})
        drop_partial("living", seg_start)  # idempotent
    finally:
        nanny_common.CHUNKS_DIR = old_chunks
        shutil.rmtree(tmp, ignore_errors=True)


def test_part_note():
    print("part note")
    check("single piece says nothing", part_note(0, 1) == "")
    note = part_note(1, 4)
    check("mentions which part", "part 2 of 4" in note)
    check("warns about edges", "ongoing" in note)


def test_env_knobs():
    print("env knobs")
    saved = {k: os.environ.get(k) for k in
             ("NANNY_PIECE_MINUTES", "NANNY_TPM_BUDGET", "NANNY_MAX_SEGMENTS_PER_RUN")}
    try:
        for k in saved:
            os.environ.pop(k, None)
        check("piece default 30", nanny_analyze.piece_minutes() == 30)
        check("budget default 200k", nanny_analyze.tpm_budget() == 200_000)
        check("cap default 4", nanny_analyze.max_segments_per_run() == 4)

        os.environ["NANNY_PIECE_MINUTES"] = "0"
        check("0 = whole segment", nanny_analyze.piece_minutes() == 0)
        os.environ["NANNY_TPM_BUDGET"] = "50000"
        check("budget overridable", nanny_analyze.tpm_budget() == 50_000)
        os.environ["NANNY_TPM_BUDGET"] = "12"
        check("budget floored", nanny_analyze.tpm_budget() == 10_000)
        os.environ["NANNY_PIECE_MINUTES"] = "abc"
        check("garbage falls back to default", nanny_analyze.piece_minutes() == 30)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_sample_fps():
    print("sample fps (the token lever)")
    saved = os.environ.get("NANNY_SAMPLE_FPS")
    try:
        os.environ.pop("NANNY_SAMPLE_FPS", None)
        check("default is 0.25", nanny_analyze.sample_fps() == 0.25)
        os.environ["NANNY_SAMPLE_FPS"] = "0.5"
        check("overridable", nanny_analyze.sample_fps() == 0.5)
        os.environ["NANNY_SAMPLE_FPS"] = "0"
        check("floored above zero", nanny_analyze.sample_fps() == 0.01)
        os.environ["NANNY_SAMPLE_FPS"] = "banana"
        check("garbage falls back to the default", nanny_analyze.sample_fps() == 0.25)
    finally:
        if saved is None:
            os.environ.pop("NANNY_SAMPLE_FPS", None)
        else:
            os.environ["NANNY_SAMPLE_FPS"] = saved


def test_downsample_uses_configured_fps():
    print("ffmpeg is told the same fps as the API")
    calls = []
    orig_run, orig_probe = nanny_analyze.subprocess.run, nanny_analyze.probe_seconds
    saved = os.environ.get("NANNY_SAMPLE_FPS")
    tmp = tempfile.mkdtemp()
    orig_lowres = nanny_analyze.LOWRES_DIR
    nanny_analyze.LOWRES_DIR = os.path.join(tmp, "lowres")

    def fake_run(cmd, **kw):
        calls.append(cmd)
        # Stand in for the transcode: one output file where ffmpeg would put it.
        out = cmd[-1].replace("%03d", "000")
        os.makedirs(os.path.dirname(out), exist_ok=True)
        open(out, "w").close()
        return None

    try:
        nanny_analyze.subprocess.run = fake_run
        nanny_analyze.probe_seconds = lambda p: 1800.0
        os.environ["NANNY_SAMPLE_FPS"] = "0.25"
        pieces, work = nanny_analyze.downsample("/tmp/raw.mp4", "kitchen")
        vf = calls[0][calls[0].index("-vf") + 1]
        check("ffmpeg gets the configured rate", vf == "fps=0.25", vf)
        check("a piece came back", len(pieces) == 1)
        # Duration is untouched by the fps filter, so offsets stay wall-clock.
        check("30 minutes of footage stays 30 minutes", pieces[0][2] == 30,
              str(pieces[0]))
    finally:
        nanny_analyze.subprocess.run, nanny_analyze.probe_seconds = orig_run, orig_probe
        nanny_analyze.LOWRES_DIR = orig_lowres
        if saved is None:
            os.environ.pop("NANNY_SAMPLE_FPS", None)
        else:
            os.environ["NANNY_SAMPLE_FPS"] = saved
        shutil.rmtree(tmp, ignore_errors=True)


def test_fps_rejection_falls_back():
    print("a model that rejects video_metadata still gets analyzed")
    seen = []

    class Pacer:
        def wait(self): pass
        def charge(self, n): pass
        def defer(self, s): pass

    def fake_analyze(client, model, *args, extras=True, with_fps=True, **kw):
        seen.append(with_fps)
        if with_fps:
            raise FakeAPIError(400, "Invalid value at 'contents.video_metadata.fps'")
        return {"ok": True}, {"input_tokens": 10}

    orig = nanny_analyze.analyze_video
    try:
        nanny_analyze.analyze_video = fake_analyze
        parsed, usage, err = nanny_analyze.analyze_with_retries(
            None, "some-model", Pacer(), "/tmp/x.mp4", "kitchen",
            datetime(2026, 7, 27, 10, 0), 30, {})
        check("tried with fps, then without", seen == [True, False], str(seen))
        check("the segment still produced a result", parsed == {"ok": True} and err is None)
    finally:
        nanny_analyze.analyze_video = orig


def test_household_context():
    print("household context and continuity blocks")
    tmp = tempfile.mkdtemp()
    saved = os.environ.get("NANNY_CONTEXT_FILE")
    try:
        path = os.path.join(tmp, "ctx.md")
        with open(path, "w") as f:
            f.write("# a comment line that should be stripped\n"
                    "Baby: Mia, 7 months.\nCaregiver: Ana.\n")
        os.environ["NANNY_CONTEXT_FILE"] = path
        ctx = nanny_common.load_context()
        check("context loaded", "Baby: Mia" in ctx and "Caregiver: Ana" in ctx)
        check("comments stripped", "a comment line" not in ctx)

        os.environ["NANNY_CONTEXT_FILE"] = os.path.join(tmp, "nope.md")
        check("a missing file is not an error", nanny_common.load_context() == "")

        with open(path, "w") as f:
            f.write("x" * (nanny_common.CONTEXT_MAX_CHARS + 500))
        os.environ["NANNY_CONTEXT_FILE"] = path
        check("context is truncated",
              len(nanny_common.load_context()) == nanny_common.CONTEXT_MAX_CHARS)

        block = nanny_analyze.household_block("Caregiver: Ana.")
        check("block names the caregiver", "Caregiver: Ana." in block)
        check("block warns about other adults",
              "never assume every adult is the caregiver" in block.lower(), block)
        check("no context → no block", nanny_analyze.household_block("") == "")

        check("no summaries → no block", nanny_analyze.earlier_block([]) == "")
        check("blank summaries → no block", nanny_analyze.earlier_block(["", "  "]) == "")
        e = nanny_analyze.earlier_block(["first hour", "second hour", "third hour"])
        check("keeps the two most recent", "second hour" in e and "third hour" in e
              and "first hour" not in e, e)
    finally:
        if saved is None:
            os.environ.pop("NANNY_CONTEXT_FILE", None)
        else:
            os.environ["NANNY_CONTEXT_FILE"] = saved
        shutil.rmtree(tmp, ignore_errors=True)


def test_failed_piece_records_its_range():
    print("a failed piece says which minutes were lost")
    seg = datetime(2026, 7, 27, 10, 0)

    def piece(offset_min, minutes, ok=True):
        start = seg + timedelta(minutes=offset_min)
        base = {"activities": [], "phone_use": [], "notable_events": [],
                "summary": "ok" if ok else "", "usage": {}}
        if ok:
            return base
        base.update({"parse_error": True, "error": "truncated",
                     "piece_start_iso": start.isoformat(), "piece_minutes": minutes})
        return base

    merged = nanny_analyze.merge_pieces(
        [piece(0, 30), piece(30, 30, ok=False)], "kitchen", "kitchen", seg, 60, "m")
    check("the hour is still one chunk", merged["segment_minutes"] == 60)
    check("only half of it was analyzed", merged["analyzed_minutes"] == 30,
          str(merged.get("analyzed_minutes")))
    check("the lost range is explicit",
          merged["unanalyzed_intervals"] == [{"start_iso": "2026-07-27T10:30:00",
                                              "end_iso": "2026-07-27T11:00:00"}],
          str(merged.get("unanalyzed_intervals")))

    clean = nanny_analyze.merge_pieces([piece(0, 30), piece(30, 30)],
                                       "kitchen", "kitchen", seg, 60, "m")
    check("a clean hour carries no lost ranges", "unanalyzed_intervals" not in clean)


def test_notable_events_get_clips():
    print("notable events get evidence, like phone events already do")
    src = inspect.getsource(nanny_analyze.process_segment)
    notable_part = src.split("notable_events")[-1]
    check("extract_clip is called for them", "extract_clip" in notable_part, notable_part[:200])
    # Clips are cut from the raw segment, so this must happen before it is deleted.
    check("before the raw segment is removed",
          src.index("_notable_") < src.index("os.remove(raw_path)"))


class _NullPacer:
    def wait(self): pass
    def charge(self, n): pass
    def defer(self, s): pass


def test_server_errors_drop_fps():
    print("a model that 500s on video_metadata falls back instead of looping")
    seen = []

    def fake_analyze(client, model, *args, extras=True, with_fps=True, **kw):
        seen.append(with_fps)
        if with_fps:
            # The real symptom: not every model answers 400 for an unsupported
            # video_metadata — some just 500, which the 400 path never catches.
            raise FakeAPIError(500, message="An internal error has occurred")
        return {"ok": True}, {"input_tokens": 10}

    orig = nanny_analyze.analyze_video
    try:
        nanny_analyze.analyze_video = fake_analyze
        parsed, usage, err = nanny_analyze.analyze_with_retries(
            None, "m", _NullPacer(), "/tmp/x.mp4", "kitchen",
            datetime(2026, 7, 27, 10, 0), 30, {})
        check("gives up on fps after two server errors, not five",
              seen == [True, True, False], str(seen))
        check("and the segment still succeeds", parsed == {"ok": True} and err is None)
    finally:
        nanny_analyze.analyze_video = orig


def test_daily_quota_ends_the_run():
    print("a per-day 429 stops the run; a per-minute one retries")
    per_day = FakeAPIError(429, {"error": {"details": [
        {"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": [
            {"quotaId": "GenerateRequestsPerDayPerProjectPerModel"}]}]}})
    per_min = FakeAPIError(429, {"error": {"details": [
        {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "20s"}]}})
    check("per-day quota recognised", nanny_analyze.daily_quota_exhausted(per_day))
    check("per-minute quota is not", not nanny_analyze.daily_quota_exhausted(per_min))
    check("a 500 is never a quota verdict",
          not nanny_analyze.daily_quota_exhausted(FakeAPIError(500)))

    calls = []

    def fake_analyze(client, model, *a, **kw):
        calls.append(1)
        raise per_day

    orig = nanny_analyze.analyze_video
    try:
        nanny_analyze.analyze_video = fake_analyze
        raised = None
        try:
            nanny_analyze.analyze_with_retries(
                None, "m", _NullPacer(), "/tmp/x.mp4", "kitchen",
                datetime(2026, 7, 27, 10, 0), 30, {})
        except nanny_analyze.QuotaExhausted as e:
            raised = e
        check("raises QuotaExhausted", raised is not None)
        # The whole point: one logged error instead of five, times every
        # remaining segment.
        check("without burning the retry budget", len(calls) == 1, str(len(calls)))
    finally:
        nanny_analyze.analyze_video = orig


def test_key_moment_clipping():
    print("clips are cut around the key moment, not the whole event")
    seg = datetime(2026, 7, 27, 10, 0)
    cmds = []
    orig_run = nanny_analyze.subprocess.run
    try:
        nanny_analyze.subprocess.run = lambda cmd, **kw: cmds.append(cmd)

        # A 4-minute event whose key moment is 3 minutes in.
        nanny_analyze.extract_clip(
            "/tmp/raw.mp4", seg, seg + timedelta(minutes=2), seg + timedelta(minutes=6),
            "/tmp/out.mp4", focus=seg + timedelta(minutes=5))
        ss, t = cmds[0][cmds[0].index("-ss") + 1], cmds[0][cmds[0].index("-t") + 1]
        check("starts 20s before the key moment", ss == "280", ss)
        check("and runs 40s, not 4 minutes", t == "40", t)

        # No key moment → the old whole-span behaviour, still capped.
        cmds.clear()
        nanny_analyze.extract_clip(
            "/tmp/raw.mp4", seg, seg + timedelta(minutes=2), seg + timedelta(minutes=6),
            "/tmp/out.mp4")
        t2 = cmds[0][cmds[0].index("-t") + 1]
        check("fallback still covers the event", t2 == "270", t2)

        # A key moment at the very start must not seek to a negative offset.
        cmds.clear()
        nanny_analyze.extract_clip("/tmp/raw.mp4", seg, seg, seg, "/tmp/out.mp4",
                                   focus=seg + timedelta(seconds=5))
        check("never seeks before the segment", cmds[0][cmds[0].index("-ss") + 1] == "0")
    finally:
        nanny_analyze.subprocess.run = orig_run


def test_key_moment_validation():
    print("a key moment outside its own event is discarded")
    seg = datetime(2026, 7, 27, 10, 0)

    def parsed(key_moment):
        return {"activities": [], "notable_events": [], "summary": "",
                "phone_use": [{"start": "05:00", "end": "09:00", "context": "unclear",
                               "confidence": "high", "person": "caregiver",
                               "key_moment": key_moment}]}

    good = nanny_analyze.build_chunk(parsed("07:00"), "cam", seg, 60, "m", {})
    check("a sane key moment is kept",
          good["phone_use"][0]["key_moment_iso"] == "2026-07-27T10:07:00",
          good["phone_use"][0]["key_moment_iso"])

    # Outside the event, or unparseable: fall back to the start of the event,
    # since the pickup itself is the informative part.
    for bad in ("59:00", "01:00", "not a time"):
        chunk = nanny_analyze.build_chunk(parsed(bad), "cam", seg, 60, "m", {})
        check(f"{bad!r} falls back to the event start",
              chunk["phone_use"][0]["key_moment_iso"] == "2026-07-27T10:05:00",
              chunk["phone_use"][0]["key_moment_iso"])


def test_phone_prompt_requires_active_use():
    print("phone detection requires visible interaction")
    prompt = nanny_analyze.PROMPT.lower()
    check("looking and glancing are excluded",
          "looking or facing in a phone's direction, glancing at a screen" in prompt)
    check("reaching toward a phone is excluded",
          "reaching toward a \nphone" in prompt or "reaching toward a phone" in prompt)
    check("a phone carried on the back is excluded",
          "worn or carried on the person's back" in prompt)
    check("sustained operation is the test",
          "sustained operation is the whole test" in prompt)
    check("a duration floor is stated to the model",
          "at least 15 seconds" in prompt)
    # The clause this replaces admitted a bare reach as phone use, directly
    # contradicting the "looking is not phone use" rule two lines above it. On
    # 2026-08-11 the model cited exactly that: "glances and reaches for her phone"
    # scored as 24 seconds of unauthorized use. It must not come back.
    check("the reach-for loophole is gone",
          "unless the adult reaches for or operates it" not in prompt)
    check("low confidence cannot revive a stored phone false positive",
          "never use low confidence to report a stationary, stored, or merely glanced-at phone"
          in prompt)


def test_visitors_are_no_longer_reported():
    print("visitors are not an event type")
    prompt = nanny_analyze.PROMPT.lower()
    types = nanny_analyze.CHUNK_SCHEMA["properties"]["notable_events"]["items"] \
        ["properties"]["type"]["enum"]
    # In a family home people come and go constantly: this produced ~9 events a
    # day, 100% of 2026-08-27's notable events, each cutting an unwatched clip.
    check("removed from the enum", "visitor" not in types, str(types))
    check("safety and milestones survive",
          "safety_concern" in types and "milestone" in types, str(types))
    check("the prompt tells it not to report arrivals",
          "do not report someone simply arriving" in prompt)
    check("an empty list is named as correct",
          "empty list is the correct answer" in prompt)


def test_play_types_are_a_closed_list():
    print("play detail is a fixed list, not free text")
    prompt = nanny_analyze.PROMPT.lower()
    check("enum reaches the schema",
          nanny_analyze.CHUNK_SCHEMA["properties"]["activities"]["items"]
          ["properties"]["play_types"]["items"]["enum"] == nanny_analyze.PLAY_TYPES)
    # Structured output cannot say "required only when category is play", so it
    # must stay optional or every non-play activity would be forced to invent one.
    check("not required at the schema level",
          "play_types" not in nanny_analyze.CHUNK_SCHEMA["properties"]["activities"]
          ["items"]["required"])
    # The lesson from the 8-second phone events: permissive wording invents detail.
    check("the prompt offers an explicit way out",
          "return an empty list" in prompt and "better than a guess" in prompt)

    seg = datetime(2026, 8, 27, 14, 0, 0)

    def a(cat, start, end, play_types=None):
        d = {"start": start, "end": end, "category": cat, "description": "x",
             "baby_visible": True, "baby_state": "awake"}
        if play_types is not None:
            d["play_types"] = play_types
        return d

    chunk = nanny_analyze.build_chunk(
        {"phone_use": [], "notable_events": [], "summary": "",
         "activities": [
             a("play", "13:00", "21:00", ["tummy_time", "floor_toys"]),
             a("play", "22:00", "26:00", ["tummy_time", "not_a_real_type"]),
             a("play", "27:00", "29:00", []),
             # The model was told to omit it here; if it does not, drop it —
             # nothing downstream should have to defend against play detail on
             # a diaper change.
             a("diaper", "30:00", "32:00", ["books"]),
             a("housework", "33:00", "35:00"),
         ]},
        "nurserycam", seg, 60, "m", {})

    got = [(x["category"], x["play_types"]) for x in chunk["activities"]]
    check("valid types kept", got[0] == ("play", ["tummy_time", "floor_toys"]), str(got[0]))
    check("unknown values dropped", got[1] == ("play", ["tummy_time"]), str(got[1]))
    check("empty stays empty", got[2] == ("play", []), str(got[2]))
    check("stripped from a non-play activity", got[3] == ("diaper", []), str(got[3]))
    check("absent field defaults to empty", got[4] == ("housework", []), str(got[4]))


def test_phone_events_have_a_duration_floor():
    print("sub-minute phone blips never reach the report")
    seg = datetime(2026, 8, 11, 17, 0, 0)

    def ev(start, end, desc="x"):
        return {"start": start, "end": end, "context": "baby_nearby_awake",
                "confidence": "high", "person": "caregiver", "key_moment": start,
                "description": desc}

    # Shapes taken from the 2026-08-11 report, which scored 8s and 10s blips as
    # unauthorized phone use — nothing in the prompt's own definition (tapping,
    # swiping, typing, taking a call) happens in 8 seconds.
    chunk = nanny_analyze.build_chunk(
        {"activities": [], "notable_events": [], "summary": "",
         "phone_use": [ev("00:20", "00:28"), ev("05:04", "05:14"),
                       ev("07:25", "07:47"), ev("08:25", "08:59"),
                       ev("10:00", "10:00")]},
        "nurserycam2", seg, 60, "m", {})

    kept = [(datetime.fromisoformat(p["end_iso"])
             - datetime.fromisoformat(p["start_iso"])).total_seconds()
            for p in chunk["phone_use"]]
    check("8s and 10s blips dropped", kept == [22, 34], f"kept {kept}")
    check("drops counted separately from malformed events",
          chunk["dropped_short_phone"] == 2, str(chunk.get("dropped_short_phone")))
    # e <= s, not e < s: a zero-length interval observes nothing and used to pass
    # straight through the parser into the report.
    check("zero-length interval rejected as malformed",
          chunk["dropped_events"] == 1, str(chunk.get("dropped_events")))
    check("floor matches the number stated in the prompt",
          nanny_analyze.MIN_PHONE_SECONDS == 15)


def test_poll_schedule_is_cheap():
    print("Files-API polling stays off the request budget")
    # Requests, not tokens, are the binding free-tier limit, and polls are most
    # of them. Walk the schedule for a file that takes ~30s to process.
    polls, waited = 0, 0.0
    p = nanny_analyze.POLL_INITIAL_S
    while waited < 30:
        waited += p
        polls += 1
        p = min(p * nanny_analyze.POLL_GROWTH, nanny_analyze.POLL_MAX_S)
    check("a 30s file costs at most 3 polls", polls <= 3, str(polls))
    check("first poll waits at least 10s", nanny_analyze.POLL_INITIAL_S >= 10)


def main():
    for fn in (test_error_classification, test_retry_delay, test_pacer,
               test_merge_pieces, test_partial_checkpoint, test_part_note,
               test_env_knobs, test_sample_fps, test_downsample_uses_configured_fps,
               test_fps_rejection_falls_back, test_household_context,
               test_failed_piece_records_its_range, test_notable_events_get_clips,
               test_server_errors_drop_fps, test_daily_quota_ends_the_run,
               test_poll_schedule_is_cheap, test_key_moment_clipping,
               test_key_moment_validation, test_phone_prompt_requires_active_use,
               test_phone_events_have_a_duration_floor,
               test_visitors_are_no_longer_reported, test_play_types_are_a_closed_list):
        fn()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S): {', '.join(FAILURES)}")
        sys.exit(1)
    print("All nanny-analyze tests passed.")


if __name__ == "__main__":
    main()
