"""
Hourly nanny-footage analyzer: for every closed raw segment that has no chunk
JSON yet, downsample → upload to Gemini → structured-JSON analysis → extract
evidence clips → delete the raw footage.

Runs as a oneshot systemd timer (nursery-nanny-analyze.timer, :05 past each
hour 11:00-18:00, Persistent=true) and is also invoked by nanny_report.py as a
straggler sweep; an flock (nanny_common.AnalyzeLock) keeps the two from racing.

Idempotency: the chunk JSON's existence is the "done" marker, and only closed
segments are picked up (see nanny_common.pending_segments) — a missed timer
run or a crash mid-batch is repaired by the next run.

Failure policy: per-segment Gemini failures leave the raw file in place for
the next hourly retry and the run still exits 0 (a partially-failed hour must
not mark the unit failed and mask real config errors — it is logged loudly
instead). Raw older than RAW_MAX_AGE_HOURS is deleted regardless (SD-card
safety) and shows up as a coverage gap in the daily report. A Gemini response
that isn't valid JSON writes a chunk with "parse_error": true so the segment
is never retried forever.

Cost levers (see plan): 1 fps downsample before upload (~50-100x smaller) and
media_resolution=LOW (~66 tokens/frame) → roughly 240k tokens per camera-hour,
~$0.60/day for 3 cameras on flash-lite-class pricing.
"""

import json
import logging
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta

from nanny_common import (
    CLIPS_DIR, LOWRES_DIR, AnalyzeLock, atomic_write_json, chunk_path,
    ensure_dirs, offset_to_wallclock, pending_segments, update_status,
)

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s [nanny_analyze] %(message)s")

DEFAULT_MODEL     = "gemini-2.5-flash-lite"
UPLOAD_TIMEOUT_S  = 300
GEMINI_RETRIES    = 3
RAW_MAX_AGE_HOURS = 24
CLIP_LEAD_S       = 15      # clip starts this long before the phone-use event
CLIP_TAIL_S       = 15      # and runs this long past its end
CLIP_MAX_S        = 300     # cap a single evidence clip at 5 minutes

PHONE_CONTEXTS = ["while_holding_baby", "baby_nearby_awake", "baby_unattended",
                  "baby_napping", "unclear"]
ACTIVITY_CATEGORIES = ["feeding", "diaper", "play", "holding_baby", "sleep_prep",
                       "housework", "eating", "resting", "out_of_frame", "other"]

CHUNK_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "activities": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "start":        {"type": "STRING"},
                    "end":          {"type": "STRING"},
                    "category":     {"type": "STRING", "enum": ACTIVITY_CATEGORIES},
                    "description":  {"type": "STRING"},
                    "baby_visible": {"type": "BOOLEAN"},
                },
                "required": ["start", "end", "category", "description"],
            },
        },
        "phone_use": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "start":       {"type": "STRING"},
                    "end":         {"type": "STRING"},
                    "context":     {"type": "STRING", "enum": PHONE_CONTEXTS},
                    "description": {"type": "STRING"},
                    "confidence":  {"type": "STRING", "enum": ["low", "medium", "high"]},
                },
                "required": ["start", "end", "context", "confidence"],
            },
        },
        "notable_events": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "time":        {"type": "STRING"},
                    "type":        {"type": "STRING",
                                    "enum": ["safety_concern", "milestone", "visitor", "other"]},
                    "description": {"type": "STRING"},
                },
                "required": ["time", "type", "description"],
            },
        },
        "summary": {"type": "STRING"},
    },
    "required": ["activities", "phone_use", "notable_events", "summary"],
}

PROMPT = """You are reviewing security-camera footage (sampled at 1 frame per second) \
from the '{camera}' camera in a private home where a nanny cares for an infant. The \
segment starts at {start_local} local time and is about {minutes} minutes long.

Produce:
1. activities — a factual timeline of what the caregiver does. Merge contiguous spans \
of the same activity; minimum granularity about one minute. Use the category enum; put \
specifics in description. Set baby_visible per span.
2. phone_use — EVERY interval where the caregiver is holding, looking at, or \
interacting with a mobile phone. Classify the baby's situation during it (context \
enum). Be conservative: if you are unsure it is a phone, still report it with \
confidence "low" rather than omitting it. Do not count baby monitors or TV remotes as \
phones if distinguishable.
3. notable_events — safety-relevant moments (baby unattended on raised surface, falls, \
distress ignored), visitors, or milestones.
4. summary — 2-3 plain sentences describing this hour.

All times are offsets from the start of the video as MM:SS. If nobody is in frame for \
the whole segment, return empty lists and say so in the summary. Output only JSON \
matching the response schema."""


# ── ffmpeg steps ──────────────────────────────────────────────────────────────

def downsample(raw_path, camera):
    out_dir = os.path.join(LOWRES_DIR, camera)
    os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, os.path.basename(raw_path))
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
           "-i", raw_path, "-vf", "fps=1",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "30", "-an", out]
    subprocess.run(cmd, check=True, timeout=1800)
    return out


def extract_clip(raw_path, seg_start, event_start, event_end, clip_out):
    """Cut an evidence clip around [event_start, event_end] from the raw segment."""
    begin = max((event_start - seg_start).total_seconds() - CLIP_LEAD_S, 0)
    dur = (event_end - event_start).total_seconds() + CLIP_LEAD_S + CLIP_TAIL_S
    dur = min(max(dur, 20), CLIP_MAX_S)
    os.makedirs(os.path.dirname(clip_out), exist_ok=True)
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
           "-ss", f"{begin:.0f}", "-i", raw_path, "-t", f"{dur:.0f}",
           "-c", "copy", "-an", clip_out]
    subprocess.run(cmd, check=True, timeout=300)


def segment_minutes(raw_path):
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", raw_path],
            capture_output=True, text=True, check=True, timeout=60).stdout.strip()
        return max(round(float(out) / 60), 1)
    except Exception:
        return 60


# ── Gemini ────────────────────────────────────────────────────────────────────

def make_client():
    from google import genai
    return genai.Client(api_key=os.environ["GEMINI_API_KEY"])


def analyze_video(client, model, lowres_path, camera, seg_start, minutes):
    """Upload + generate. Returns (parsed_dict_or_None, usage_dict). Raises on
    transport-level failure (caller retries); returns None on unparseable JSON."""
    from google.genai import types

    video = client.files.upload(file=lowres_path)
    try:
        deadline = time.time() + UPLOAD_TIMEOUT_S
        while video.state and video.state.name == "PROCESSING":
            if time.time() > deadline:
                raise TimeoutError(f"Gemini file processing timed out for {lowres_path}")
            time.sleep(5)
            video = client.files.get(name=video.name)
        if video.state and video.state.name == "FAILED":
            raise RuntimeError(f"Gemini file processing FAILED for {lowres_path}")

        prompt = PROMPT.format(camera=camera, minutes=minutes,
                               start_local=seg_start.strftime("%H:%M on %A"))
        resp = client.models.generate_content(
            model=model,
            contents=[video, prompt],
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=CHUNK_SCHEMA,
                media_resolution="MEDIA_RESOLUTION_LOW",
                temperature=0.0,
            ),
        )
    finally:
        try:
            client.files.delete(name=video.name)
        except Exception:
            logging.warning("Could not delete uploaded file %s", video.name)

    usage = {}
    if getattr(resp, "usage_metadata", None):
        usage = {"input_tokens": resp.usage_metadata.prompt_token_count,
                 "output_tokens": resp.usage_metadata.candidates_token_count}
    try:
        return json.loads(resp.text), usage
    except (json.JSONDecodeError, TypeError):
        logging.error("Unparseable Gemini JSON for %s (first 200 chars: %r)",
                      lowres_path, (resp.text or "")[:200])
        return None, usage


def is_retryable(exc):
    msg = str(exc)
    return bool(re.search(r"\b(429|500|502|503|504)\b", msg)) or \
        isinstance(exc, (TimeoutError, ConnectionError))


# ── Chunk assembly ────────────────────────────────────────────────────────────

def build_chunk(parsed, camera, seg_start, minutes, model, usage):
    """Validate/convert the model output into the stored chunk record.
    Tolerant: malformed events are dropped and counted, never fatal."""
    seg_end = seg_start + timedelta(minutes=minutes)
    dropped = 0

    def wall(o):
        dt = offset_to_wallclock(seg_start, o)
        if dt is None or not (seg_start <= dt <= seg_end + timedelta(minutes=5)):
            return None
        return dt

    activities, phone_use, notable = [], [], []
    for a in parsed.get("activities") or []:
        s, e = wall(a.get("start")), wall(a.get("end"))
        if s is None or e is None or e < s:
            dropped += 1
            continue
        activities.append({"start_iso": s.isoformat(), "end_iso": e.isoformat(),
                           "category": a.get("category", "other"),
                           "description": a.get("description", ""),
                           "baby_visible": bool(a.get("baby_visible", False))})
    for p in parsed.get("phone_use") or []:
        s, e = wall(p.get("start")), wall(p.get("end"))
        if s is None or e is None or e < s:
            dropped += 1
            continue
        ctx = p.get("context")
        phone_use.append({"start_iso": s.isoformat(), "end_iso": e.isoformat(),
                          "context": ctx if ctx in PHONE_CONTEXTS else "unclear",
                          "description": p.get("description", ""),
                          "confidence": p.get("confidence", "low")})
    for n in parsed.get("notable_events") or []:
        t = wall(n.get("time"))
        if t is None:
            dropped += 1
            continue
        notable.append({"time_iso": t.isoformat(), "type": n.get("type", "other"),
                        "description": n.get("description", "")})

    return {
        "camera": camera,
        "segment_start_iso": seg_start.isoformat(),
        "segment_minutes": minutes,
        "model": model,
        "usage": usage,
        "dropped_events": dropped,
        "activities": activities,
        "phone_use": phone_use,
        "notable_events": notable,
        "summary": parsed.get("summary", ""),
    }


def process_segment(client, model, camera, raw_path, seg_start):
    minutes = segment_minutes(raw_path)
    lowres = downsample(raw_path, camera)
    try:
        parsed = usage = None
        for attempt in range(1, GEMINI_RETRIES + 1):
            try:
                parsed, usage = analyze_video(client, model, lowres, camera,
                                              seg_start, minutes)
                break
            except Exception as e:
                if attempt < GEMINI_RETRIES and is_retryable(e):
                    wait = 30 * 2 ** (attempt - 1)
                    logging.warning("[%s %s] Gemini attempt %d failed (%s) — retrying in %ds",
                                    camera, seg_start, attempt, e, wait)
                    time.sleep(wait)
                else:
                    raise

        if parsed is None:
            chunk = {"camera": camera, "segment_start_iso": seg_start.isoformat(),
                     "segment_minutes": minutes, "model": model, "usage": usage,
                     "parse_error": True, "activities": [], "phone_use": [],
                     "notable_events": [], "summary": ""}
        else:
            chunk = build_chunk(parsed, camera, seg_start, minutes, model, usage)
            # Evidence clips come from the RAW segment, before it is deleted.
            day = seg_start.date().isoformat()
            for i, p in enumerate([p for p in chunk["phone_use"]
                                   if p["confidence"] in ("medium", "high")], start=1):
                clip_name = f"{camera}_{seg_start.strftime('%H%M')}_phone_{i}.mp4"
                clip_out = os.path.join(CLIPS_DIR, day, clip_name)
                try:
                    extract_clip(raw_path, seg_start,
                                 datetime.fromisoformat(p["start_iso"]),
                                 datetime.fromisoformat(p["end_iso"]), clip_out)
                    p["clip"] = f"{day}/{clip_name}"
                except (subprocess.SubprocessError, OSError) as e:
                    logging.warning("Clip extraction failed for %s: %s", clip_name, e)

        atomic_write_json(chunk_path(camera, seg_start), chunk)
        os.remove(raw_path)
        logging.info("[%s %s] analyzed: %d activities, %d phone events, %s tokens",
                     camera, seg_start.strftime("%H:%M"), len(chunk["activities"]),
                     len(chunk["phone_use"]), (chunk.get("usage") or {}).get("input_tokens", "?"))
    finally:
        if os.path.exists(lowres):
            os.remove(lowres)


def purge_stale_raw():
    """Delete raw segments that kept failing for >RAW_MAX_AGE_HOURS (disk safety).
    They surface as coverage gaps in the daily report."""
    for camera, path, seg_start in pending_segments(now=datetime.now()):
        if (datetime.now() - seg_start).total_seconds() > RAW_MAX_AGE_HOURS * 3600:
            logging.error("[%s] giving up on %s after %dh of failed analysis — deleting "
                          "(will appear as a coverage gap)", camera,
                          os.path.basename(path), RAW_MAX_AGE_HOURS)
            os.remove(path)


def analyze_pending():
    """Process all pending segments. Returns (done, failed). Import-safe for
    nanny_report's straggler sweep."""
    if not os.environ.get("GEMINI_API_KEY"):
        logging.info("GEMINI_API_KEY not set — analysis disabled, nothing to do.")
        return 0, 0

    lock = AnalyzeLock()
    if not lock.acquire():
        logging.info("Another analyzer run holds the lock — skipping.")
        return 0, 0
    try:
        ensure_dirs()
        purge_stale_raw()
        pending = pending_segments()
        if not pending:
            logging.info("No pending segments.")
            return 0, 0

        model = os.environ.get("GEMINI_MODEL", DEFAULT_MODEL)
        client = make_client()
        done = failed = 0
        for camera, raw_path, seg_start in pending:
            try:
                process_segment(client, model, camera, raw_path, seg_start)
                done += 1
            except Exception:
                logging.exception("[%s %s] segment analysis failed — raw kept for "
                                  "the next hourly retry", camera, seg_start)
                failed += 1
        update_status("analyze", done=done, failed=failed, model=model)
        return done, failed
    finally:
        lock.release()


def main():
    done, failed = analyze_pending()
    if failed:
        logging.error("%d segment(s) failed and will be retried next hour.", failed)
    # Exit 0 even on per-segment failures: the raw files persist and the next
    # timer run retries; a red unit here would mask genuine config errors.


if __name__ == "__main__":
    main()
