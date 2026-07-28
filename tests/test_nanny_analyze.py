"""
Unit tests for the analyzer's quota plumbing — the part that decides HOW OFTEN
and HOW HARD we hit Gemini: error classification, server-suggested backoff, the
token pacer, per-piece checkpointing and the piece merge. No key, no network,
no ffmpeg (analyze_video's imports are function-local by design).

Run:  venv/bin/python tests/test_nanny_analyze.py
"""

import os
import shutil
import sys
import tempfile
from datetime import datetime

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


def main():
    for fn in (test_error_classification, test_retry_delay, test_pacer,
               test_merge_pieces, test_partial_checkpoint, test_part_note,
               test_env_knobs):
        fn()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S): {', '.join(FAILURES)}")
        sys.exit(1)
    print("All nanny-analyze tests passed.")


if __name__ == "__main__":
    main()
