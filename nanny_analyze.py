"""
Hourly nanny-footage analyzer: for every closed raw segment that has no chunk
JSON yet, downsample → upload to Gemini → structured-JSON analysis → extract
evidence clips → delete the raw footage.

Each segment is analyzed alone, but the prompt describes the whole camera
topology (scene_description): which room each camera watches, which cameras
share a room, and that they all record the same hours. Without that the model
reads one frame as the entire world and calls a baby who is simply in the other
room "unattended"; with it, "not in frame" stays "not in frame" and the daily
report does the cross-room fusion (see nanny_report.classify_phone_use).

Runs as a oneshot systemd timer (nursery-nanny-analyze.timer, :05 and :35 past
each hour of the care window, Persistent=true) and is also invoked by
nanny_report.py as a straggler sweep; an flock (nanny_common.AnalyzeLock)
keeps the two from racing.

Idempotency: the chunk JSON's existence is the "done" marker, and only closed
segments are picked up (see nanny_common.pending_segments) — a missed timer
run or a crash mid-batch is repaired by the next run. Within a segment, each
analyzed piece is checkpointed to <chunk>.partial, so a retry never pays for a
piece that already came back.

Quota shape (this is why the code looks the way it does): a camera-hour at
1 fps / MEDIA_RESOLUTION_LOW is ~240k input tokens, and the Gemini API limits
input *tokens per minute* as well as requests per minute. Three cameras
uploaded back to back therefore blow a 250k-1M TPM quota in a single minute
even though that is only three requests, and an hour-long video is also the
shape most likely to come back 500 or truncated. Three levers keep it inside
the quota:
  - PIECE_MINUTES splits the hour into smaller videos (fewer tokens and less
    output JSON per request — a truncated piece costs 30 min of coverage, not
    the whole hour);
  - Pacer spaces the calls by what they actually cost (60s x tokens/TPM
    budget), and a 429 pushes every following call out by the server's own
    retryDelay;
  - MAX_SEGMENTS_PER_RUN caps one run so a backlog (Persistent catch-up after
    downtime) drains over several runs instead of one burst. The report's
    straggler sweep passes limit=None — the day's report must not go out
    incomplete.

Failure policy: per-segment Gemini failures leave the raw file in place for
the next retry and the run still exits 0 (a partially-failed hour must not
mark the unit failed and mask real config errors — it is logged loudly
instead). Raw older than RAW_MAX_AGE_HOURS is deleted regardless (SD-card
safety) and shows up as a coverage gap in the daily report. A response that
isn't valid JSON, or that is still truncated after every retry, writes the
piece off with "parse_error": true so the segment is never retried forever —
deterministic failures must not be paid for once an hour, transient ones
(429/5xx/network) must.

Cost levers (see plan): 1 fps downsample before upload (~50-100x smaller) and
media_resolution=LOW (~66 tokens/frame) → roughly 240k tokens per camera-hour,
~$0.60/day for 3 cameras on flash-lite-class pricing.
"""

import json
import logging
import os
import random
import re
import shutil
import subprocess
import time
from datetime import datetime, timedelta

from nanny_common import (
    CLIPS_DIR, LOWRES_DIR, AnalyzeLock, atomic_write_json, chunk_path,
    ensure_dirs, load_camera_rooms, load_cameras, offset_to_wallclock,
    pending_segments, update_status,
)

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s [nanny_analyze] %(message)s")

DEFAULT_MODEL     = "gemini-2.5-flash-lite"
UPLOAD_TIMEOUT_S  = 600
RAW_MAX_AGE_HOURS = 24
CLIP_LEAD_S       = 15      # clip starts this long before the phone-use event
CLIP_TAIL_S       = 15      # and runs this long past its end
CLIP_MAX_S        = 300     # cap a single evidence clip at 5 minutes

# Files API polling: the upload/processing poll is by far the chattiest thing
# in the pipeline (one segment = 1 generate call but a poll every few seconds).
# Backing off turns ~15 requests per segment into ~5 and keeps RPM headroom for
# the calls that matter.
POLL_INITIAL_S = 3
POLL_MAX_S     = 20
POLL_GROWTH    = 1.5

GEMINI_RETRIES    = 5       # attempts per piece, transient failures only
RETRY_BASE_S      = 30      # doubled per attempt when the server suggests nothing
MAX_RETRY_SLEEP_S = 300
MAX_PACE_SLEEP_S  = 300
MIN_REQUEST_GAP_S = 10      # never fire two video calls closer than this
MIN_PIECE_SECONDS = 30      # shorter tail pieces aren't worth a request

# 408/429 + 5xx are what the SDK itself considers retryable; 409 is a transient
# Files API conflict. Everything else (400 bad request, 403 bad key, 404 model
# not found) is a config error that retrying only turns into wasted quota.
RETRYABLE_CODES = {408, 409, 429, 500, 502, 503, 504}


def _env_int(name, default, minimum=0):
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return max(int(raw), minimum)
    except ValueError:
        logging.warning("%s=%r is not an integer — using %s", name, raw, default)
        return default


def piece_minutes():
    """Minutes of footage per Gemini call (0 = one call per whole segment)."""
    return _env_int("NANNY_PIECE_MINUTES", 30)


def tpm_budget():
    """Input tokens per minute this pipeline allows itself. Deliberately below
    the real quota: the crib monitor and the daily narrative share the key."""
    return _env_int("NANNY_TPM_BUDGET", 200_000, minimum=10_000)


def max_segments_per_run():
    return _env_int("NANNY_MAX_SEGMENTS_PER_RUN", 4)

PHONE_CONTEXTS = ["while_holding_baby", "baby_nearby_awake", "baby_unattended",
                  "baby_napping", "baby_not_in_frame", "unclear"]
ACTIVITY_CATEGORIES = ["feeding", "diaper", "play", "holding_baby", "sleep_prep",
                       "housework", "eating", "resting", "out_of_frame", "other"]
# Judged from THIS camera only; the report fuses the rooms afterwards.
BABY_STATES = ["awake", "asleep", "not_visible", "unclear"]

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
                    "baby_state":   {"type": "STRING", "enum": BABY_STATES},
                },
                "required": ["start", "end", "category", "description",
                             "baby_visible", "baby_state"],
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
from a private home where a nanny cares for an infant.

THE CAMERA SYSTEM
{scene}
All of these cameras record the SAME hours simultaneously; you are being shown one \
camera at a time. There is exactly one baby and normally one caregiver in the home, so \
when they are not in this camera's frame they are usually somewhere else in the house — \
possibly in a room another camera covers. Never infer from this video alone that the \
baby is alone or unsupervised: report only what THIS camera shows, and let \
"not in frame" mean exactly that. Cameras that share a room are two angles on the same \
scene, not two different places.

THIS VIDEO: camera '{camera}', which watches the {room}. The segment starts at \
{start_local} local time and is about {minutes} minutes long.{part_note}

Produce:
1. activities — a factual timeline of what the caregiver does. Merge contiguous spans \
of the same activity; minimum granularity about one minute. Use the category enum; put \
specifics in description. For every span set baby_visible (is the baby in THIS frame at \
all) and baby_state: "asleep" only when the baby is visibly settled and still (lying \
down, eyes closed, no active movement) for the span, "awake" when visibly moving, \
being held, fed, played with or attended to, "not_visible" when the baby is not in \
frame, "unclear" when in frame but you cannot tell.
2. phone_use — EVERY interval where the caregiver is holding, looking at, or \
interacting with a mobile phone. Classify the baby's situation during it with the \
context enum: while_holding_baby, baby_nearby_awake (baby awake in the same room), \
baby_unattended (baby awake and needing attention while the caregiver is on the phone), \
baby_napping (baby visibly asleep in this room), baby_not_in_frame (the baby is simply \
not in this camera's view), unclear. Be conservative: if you are unsure it is a phone, \
still report it with confidence "low" rather than omitting it. Do not count baby \
monitors or TV remotes as phones if distinguishable.
3. notable_events — safety-relevant moments (baby unattended on a raised surface, \
falls, distress ignored), visitors, or milestones.
4. summary — 2-3 plain sentences describing this hour. Mention which room this is.

All times are offsets from the start of the video as MM:SS. If nobody is in frame for \
the whole segment, return empty lists and say so in the summary. Output only JSON \
matching the response schema."""


def scene_description(rooms, this_camera):
    """The camera-topology block of the prompt: every camera and its room, so the
    model reads one video as one vantage point on a multi-room house instead of
    as the whole world (a caregiver alone in frame is not a baby left alone)."""
    if not rooms:
        return f"- '{this_camera}' (this video) — the only camera configured."
    by_room = {}
    for cam, room in sorted(rooms.items()):
        by_room.setdefault(room, []).append(cam)
    lines = []
    for room, cams in sorted(by_room.items()):
        shared = " (two angles on the same room)" if len(cams) > 1 else ""
        for cam in cams:
            mark = "  <- THIS VIDEO" if cam == this_camera else ""
            lines.append(f"- '{cam}' watches the {room}{shared}{mark}")
    return "\n".join(lines)


def part_note(index, total):
    """Tell the model it is looking at part of a longer recording, so it does
    not report a nap as "the whole afternoon" or an activity as unfinished."""
    if total <= 1:
        return ""
    return (f" This is part {index + 1} of {total} of one continuous recording; "
            "describe only what happens in THIS part, and treat activities that "
            "are already under way at 00:00 or still under way at the end as "
            "ongoing rather than as starting or stopping here.")


# ── ffmpeg steps ──────────────────────────────────────────────────────────────

def probe_seconds(path):
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, text=True, check=True, timeout=60).stdout.strip()
        return float(out)
    except (subprocess.SubprocessError, OSError, ValueError):
        return None


def downsample(raw_path, camera, piece_seconds=0):
    """1 fps transcode of one raw segment, optionally cut into pieces.

    Returns (pieces, work_dir) where pieces is [(path, offset_seconds, minutes)].
    Offsets come from ffprobe on the produced files rather than from the
    requested piece length, so the wall-clock start of every piece is right even
    if ffmpeg lands a cut a second or two off.
    """
    work_dir = os.path.join(LOWRES_DIR, camera,
                            os.path.splitext(os.path.basename(raw_path))[0])
    shutil.rmtree(work_dir, ignore_errors=True)
    os.makedirs(work_dir, exist_ok=True)
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
           "-i", raw_path, "-vf", "fps=1",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "30", "-an"]
    if piece_seconds:
        # A keyframe exactly on each cut point: the segment muxer cuts at the
        # first keyframe at or after -segment_time, so without this the pieces
        # drift by up to a GOP and the offsets stop matching wall clock.
        cmd += ["-force_key_frames", f"expr:gte(t,n_forced*{piece_seconds})",
                "-f", "segment", "-segment_time", str(piece_seconds),
                "-reset_timestamps", "1"]
    cmd.append(os.path.join(work_dir, "p%03d.mp4" if piece_seconds else "p000.mp4"))
    subprocess.run(cmd, check=True, timeout=1800)

    pieces, offset = [], 0.0
    for name in sorted(os.listdir(work_dir)):
        path = os.path.join(work_dir, name)
        dur = probe_seconds(path) or float(piece_seconds or 0)
        # A cut that lands a second past the end leaves a sliver; uploading it
        # would cost a whole request for nothing.
        if dur >= MIN_PIECE_SECONDS or not pieces:
            pieces.append((path, offset, max(round(dur / 60), 1)))
        else:
            logging.info("Skipping %.0fs tail piece %s", dur, name)
        offset += dur
    if not pieces:
        raise RuntimeError(f"ffmpeg produced no output for {raw_path}")
    return pieces, work_dir


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
    dur = probe_seconds(raw_path)
    return max(round(dur / 60), 1) if dur else 60


# ── Gemini ────────────────────────────────────────────────────────────────────

def make_client():
    from google import genai
    from google.genai import types

    key = os.environ["GEMINI_API_KEY"]
    try:
        # SDK-level retries cover the chatter this file does not wrap itself —
        # the resumable upload and the Files API polls. The per-piece loop below
        # still owns the long, quota-aware waits for generate_content.
        return genai.Client(api_key=key, http_options=types.HttpOptions(
            retry_options=types.HttpRetryOptions(attempts=3, initial_delay=2,
                                                 max_delay=30, exp_base=2)))
    except (AttributeError, TypeError) as e:
        logging.info("SDK retry options unavailable (%s) — using a plain client", e)
        return genai.Client(api_key=key)


class TruncatedResponse(Exception):
    """The call succeeded but returned no usable text — the model spent its
    output budget (usually on thinking) before emitting the JSON."""


def status_code(exc):
    """HTTP status of a google-genai APIError, or None. `.code` is the SDK's own
    field; the regex is a fallback for wrapped/stringified errors."""
    code = getattr(exc, "code", None)
    if isinstance(code, int) and 100 <= code < 600:
        return code
    m = re.search(r"\b([45]\d\d)\b", str(exc))
    return int(m.group(1)) if m else None


def retry_delay_seconds(exc):
    """Seconds the server asked us to wait (RetryInfo 'retryDelay': '31s').

    Honouring it is the difference between backing off once and hammering a
    quota that resets on the server's schedule, not ours.
    """
    def walk(node):
        if isinstance(node, dict):
            for k, v in node.items():
                if k in ("retryDelay", "retry_delay") and isinstance(v, str):
                    m = re.fullmatch(r"(\d+(?:\.\d+)?)s?", v.strip())
                    if m:
                        return float(m.group(1))
                found = walk(v)
                if found is not None:
                    return found
        elif isinstance(node, list):
            for item in node:
                found = walk(item)
                if found is not None:
                    return found
        return None

    found = walk(getattr(exc, "details", None))
    if found is not None:
        return found
    m = re.search(r"retry[_ ]?[Dd]elay['\"]?\s*[:=]\s*['\"]?(\d+(?:\.\d+)?)s", str(exc))
    return float(m.group(1)) if m else None


def is_retryable(exc):
    if isinstance(exc, (TimeoutError, ConnectionError, TruncatedResponse)):
        return True
    code = status_code(exc)
    if code is not None:
        return code in RETRYABLE_CODES
    return False


class Pacer:
    """Spaces Gemini video calls by what they cost.

    A camera-hour is ~240k input tokens, so two of them in the same minute
    exceed a 250k input-TPM quota even though that is only two requests. After
    every call the pacer charges 60s x tokens/budget of cooldown; a 429 charges
    the server's own retryDelay. Everything sleeps in one place (wait()), so a
    backlog drains at quota speed instead of all at once.
    """

    def __init__(self, budget=None, min_gap=MIN_REQUEST_GAP_S):
        self.budget = budget or tpm_budget()
        self.min_gap = min_gap
        self.next_allowed = None    # monotonic deadline; None = go now
        self.slept = 0.0

    def wait(self):
        if self.next_allowed is None:
            return 0.0
        delay = self.next_allowed - time.monotonic()
        if delay <= 0:
            return 0.0
        logging.info("Pacing: waiting %.0fs before the next Gemini call", delay)
        time.sleep(delay)
        self.slept += delay
        return delay

    def charge(self, input_tokens):
        """Cooldown proportional to the tokens just spent."""
        cost = 60.0 * (input_tokens or 0) / self.budget
        self.defer(min(max(cost, self.min_gap), MAX_PACE_SLEEP_S))

    def defer(self, seconds):
        # Jitter so a restart loop or a second Pi never re-synchronises onto the
        # same second of the quota window.
        seconds *= random.uniform(0.9, 1.1)
        deadline = time.monotonic() + seconds
        self.next_allowed = max(self.next_allowed or 0.0, deadline)


def generation_config(types, extras=True):
    """Config for one video call. `extras` are the knobs a given model may not
    accept (thinking, explicit output cap) — dropped and retried on a 400."""
    kwargs = dict(response_mime_type="application/json",
                  response_schema=CHUNK_SCHEMA,
                  media_resolution="MEDIA_RESOLUTION_LOW",
                  temperature=0.0)
    if not extras:
        return types.GenerateContentConfig(**kwargs)

    max_output = _env_int("GEMINI_MAX_OUTPUT_TOKENS", 32768, minimum=1024)
    kwargs["max_output_tokens"] = max_output
    level = os.environ.get("GEMINI_THINKING_LEVEL", "").strip().lower()
    budget = os.environ.get("GEMINI_THINKING_BUDGET", "").strip()
    # Thinking is charged against the same output budget as the JSON, so an
    # over-thinking model returns a truncated (unusable) answer. Capping it is
    # opt-in because the knob differs by model generation: 3.x takes
    # thinking_level (minimal|low|medium|high), 2.5 takes thinking_budget, and
    # sending both is an error — hence the elif.
    if level:
        kwargs["thinking_config"] = types.ThinkingConfig(thinking_level=level)
    elif budget:
        kwargs["thinking_config"] = types.ThinkingConfig(thinking_budget=int(budget))
    return types.GenerateContentConfig(**kwargs)


def analyze_video(client, model, lowres_path, camera, piece_start, minutes, rooms,
                  note="", extras=True):
    """Upload + generate for ONE video. Returns (parsed_dict_or_None, usage).

    Raises on transport failure or a truncated answer (the caller retries);
    returns None only for output that is genuinely unparseable, which retrying
    would just buy again.
    """
    from google.genai import types

    video = client.files.upload(file=lowres_path)
    try:
        deadline = time.time() + UPLOAD_TIMEOUT_S
        poll = POLL_INITIAL_S
        while video.state and video.state.name == "PROCESSING":
            if time.time() > deadline:
                raise TimeoutError(f"Gemini file processing timed out for {lowres_path}")
            time.sleep(poll)
            poll = min(poll * POLL_GROWTH, POLL_MAX_S)
            video = client.files.get(name=video.name)
        if video.state and video.state.name == "FAILED":
            raise RuntimeError(f"Gemini file processing FAILED for {lowres_path}")

        prompt = PROMPT.format(camera=camera, minutes=minutes,
                               room=rooms.get(camera, camera),
                               scene=scene_description(rooms, camera),
                               start_local=piece_start.strftime("%H:%M on %A"),
                               part_note=note)
        resp = client.models.generate_content(
            model=model,
            contents=[video, prompt],
            config=generation_config(types, extras=extras),
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
    text = getattr(resp, "text", None)
    if not text:
        cand = (getattr(resp, "candidates", None) or [None])[0]
        raise TruncatedResponse(
            f"empty response for {os.path.basename(lowres_path)} "
            f"(finish_reason={getattr(cand, 'finish_reason', None)}, "
            f"block_reason={getattr(getattr(resp, 'prompt_feedback', None), 'block_reason', None)})")
    try:
        return json.loads(text), usage
    except json.JSONDecodeError:
        logging.error("Unparseable Gemini JSON for %s (first 200 chars: %r)",
                      lowres_path, text[:200])
        return None, usage


def analyze_with_retries(client, model, pacer, *args, **kwargs):
    """analyze_video + quota-aware retries. Returns (parsed, usage, error):
    error is None on success, else a short reason recorded in the chunk.

    Only transient failures are retried. A 400 that names one of the optional
    config knobs retries once without them rather than failing the whole day.
    """
    extras = True
    for attempt in range(1, GEMINI_RETRIES + 1):
        pacer.wait()
        try:
            parsed, usage = analyze_video(client, model, *args, extras=extras, **kwargs)
            pacer.charge(usage.get("input_tokens"))
            return parsed, usage, None if parsed is not None else "unparseable_json"
        except Exception as e:
            code = status_code(e)
            if extras and code == 400 and re.search(r"thinking|max_output_tokens|maxOutputTokens",
                                                    str(e), re.I):
                logging.error("Model %s rejected the optional generation config (%s) — "
                              "retrying without it; unset GEMINI_THINKING_LEVEL/"
                              "GEMINI_MAX_OUTPUT_TOKENS to silence this", model, e)
                extras = False
                continue
            if not is_retryable(e):
                raise
            if attempt >= GEMINI_RETRIES:
                if isinstance(e, TruncatedResponse):
                    # Deterministic: the same video will truncate again next
                    # hour. Write it off instead of paying for it every run.
                    logging.error("Giving up on a truncated response after %d attempts (%s)",
                                  attempt, e)
                    return None, {}, "truncated"
                raise
            wait = retry_delay_seconds(e) or RETRY_BASE_S * 2 ** (attempt - 1)
            wait = min(max(wait, 5), MAX_RETRY_SLEEP_S)
            if code == 429:
                logging.warning("Rate limited (429) on attempt %d — backing off %.0fs "
                                "(and pushing the rest of this run out with it)",
                                attempt, wait)
            else:
                logging.warning("Gemini attempt %d failed (%s) — retrying in %.0fs",
                                attempt, e, wait)
            pacer.defer(wait)
    # Only reachable if the last attempt was spent dropping the optional config.
    raise RuntimeError(f"exhausted {GEMINI_RETRIES} attempts without a verdict")


# ── Chunk assembly ────────────────────────────────────────────────────────────

def build_chunk(parsed, camera, seg_start, minutes, model, usage, room=None):
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
        state = a.get("baby_state")
        if state not in BABY_STATES:
            # Older/looser output: fall back to the boolean we do have.
            state = "unclear" if a.get("baby_visible") else "not_visible"
        activities.append({"start_iso": s.isoformat(), "end_iso": e.isoformat(),
                           "category": a.get("category", "other"),
                           "description": a.get("description", ""),
                           "baby_visible": bool(a.get("baby_visible", False)),
                           "baby_state": state})
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
        "room": room or camera,
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


def merge_pieces(piece_chunks, camera, room, seg_start, minutes, model):
    """Fold the per-piece results back into ONE chunk per raw segment.

    The chunk file stays the unit of idempotency and of the daily report's
    coverage accounting, whether the hour was one Gemini call or four. Every
    piece already carries wall-clock times (build_chunk was given the piece's
    own start), so merging is a sort, not an offset fix-up.
    """
    merged = {
        "camera": camera,
        "room": room,
        "segment_start_iso": seg_start.isoformat(),
        "segment_minutes": minutes,
        "model": model,
        "usage": {"input_tokens": 0, "output_tokens": 0},
        "dropped_events": 0,
        "activities": [],
        "phone_use": [],
        "notable_events": [],
        "summary": "",
    }
    summaries, failed = [], 0
    for piece in piece_chunks:
        usage = piece.get("usage") or {}
        merged["usage"]["input_tokens"] += usage.get("input_tokens") or 0
        merged["usage"]["output_tokens"] += usage.get("output_tokens") or 0
        merged["dropped_events"] += piece.get("dropped_events", 0)
        for key in ("activities", "phone_use", "notable_events"):
            merged[key].extend(piece.get(key) or [])
        if piece.get("parse_error"):
            failed += 1
        if piece.get("summary"):
            summaries.append(piece["summary"].strip())
    merged["activities"].sort(key=lambda a: a["start_iso"])
    merged["phone_use"].sort(key=lambda p: p["start_iso"])
    merged["notable_events"].sort(key=lambda n: n["time_iso"])
    merged["summary"] = " ".join(summaries)
    if failed:
        # The report counts this as a parse error; the pieces that DID come back
        # still contribute their events, so a bad 30 minutes no longer voids the
        # hour around it.
        merged["parse_error"] = True
        merged["failed_pieces"] = failed
        merged["pieces"] = len(piece_chunks)
    return merged


def _partial_path(camera, seg_start):
    return chunk_path(camera, seg_start) + ".partial"


def load_partial(camera, seg_start, piece_count):
    """Pieces already analyzed for this segment on an earlier run, if the split
    still matches. Retrying a segment must not re-buy work already paid for."""
    path = _partial_path(camera, seg_start)
    if not os.path.exists(path):
        return {}
    try:
        with open(path) as f:
            saved = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}
    if saved.get("piece_count") != piece_count:
        logging.info("Discarding checkpoint for %s %s: split changed (%s → %s pieces)",
                     camera, seg_start, saved.get("piece_count"), piece_count)
        return {}
    return {int(k): v for k, v in (saved.get("pieces") or {}).items()}


def save_partial(camera, seg_start, piece_count, done):
    atomic_write_json(_partial_path(camera, seg_start),
                      {"piece_count": piece_count,
                       "pieces": {str(k): v for k, v in done.items()}})


def drop_partial(camera, seg_start):
    try:
        os.remove(_partial_path(camera, seg_start))
    except OSError:
        pass


def process_segment(client, model, camera, raw_path, seg_start, rooms, pacer):
    minutes = segment_minutes(raw_path)
    room = rooms.get(camera, camera)
    pieces, work_dir = downsample(raw_path, camera, piece_minutes() * 60)
    try:
        done = load_partial(camera, seg_start, len(pieces))
        if done:
            logging.info("[%s %s] resuming: %d of %d pieces already analyzed",
                         camera, seg_start.strftime("%H:%M"), len(done), len(pieces))
        for i, (path, offset_s, piece_mins) in enumerate(pieces):
            if i in done:
                continue
            piece_start = seg_start + timedelta(seconds=offset_s)
            parsed, usage, err = analyze_with_retries(
                client, model, pacer, path, camera, piece_start, piece_mins, rooms,
                note=part_note(i, len(pieces)))
            if parsed is None:
                logging.error("[%s %s] piece %d/%d unusable (%s) — the rest of the "
                              "segment still counts", camera,
                              piece_start.strftime("%H:%M"), i + 1, len(pieces), err)
                done[i] = {"parse_error": True, "error": err, "usage": usage,
                           "activities": [], "phone_use": [], "notable_events": [],
                           "summary": ""}
            else:
                done[i] = build_chunk(parsed, camera, piece_start, piece_mins,
                                      model, usage, room)
            # Checkpoint after every piece: a 429 four pieces in must not throw
            # away the three that already came back.
            save_partial(camera, seg_start, len(pieces), done)

        chunk = merge_pieces([done[i] for i in sorted(done)], camera, room,
                             seg_start, minutes, model)
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
        drop_partial(camera, seg_start)
        os.remove(raw_path)
        logging.info("[%s %s] analyzed in %d piece(s): %d activities, %d phone events, "
                     "%s input tokens", camera, seg_start.strftime("%H:%M"), len(pieces),
                     len(chunk["activities"]), len(chunk["phone_use"]),
                     (chunk.get("usage") or {}).get("input_tokens", "?"))
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def purge_stale_raw():
    """Delete raw segments that kept failing for >RAW_MAX_AGE_HOURS (disk safety).
    They surface as coverage gaps in the daily report."""
    for camera, path, seg_start in pending_segments(now=datetime.now()):
        if (datetime.now() - seg_start).total_seconds() > RAW_MAX_AGE_HOURS * 3600:
            logging.error("[%s] giving up on %s after %dh of failed analysis — deleting "
                          "(will appear as a coverage gap)", camera,
                          os.path.basename(path), RAW_MAX_AGE_HOURS)
            os.remove(path)
            drop_partial(camera, seg_start)


def analyze_pending(limit=-1):
    """Process pending segments. Returns (done, failed). Import-safe for
    nanny_report's straggler sweep.

    limit: max segments this run (-1 = NANNY_MAX_SEGMENTS_PER_RUN, None = all).
    The timer runs half-hourly and takes the cap so a backlog spreads across
    runs; the report's sweep passes None because the day's report must not be
    published with hours missing.
    """
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

        if limit == -1:
            limit = max_segments_per_run()
        if limit and len(pending) > limit:
            logging.info("%d segments pending — taking the %d oldest this run "
                         "(the rest go out on the next run, to stay inside the "
                         "per-minute token quota)", len(pending), limit)
            pending = sorted(pending, key=lambda p: p[2])[:limit]

        model = os.environ.get("GEMINI_MODEL", DEFAULT_MODEL)
        try:
            rooms = load_camera_rooms(load_cameras())
        except ValueError as e:
            # Bad room config must not stall analysis; the segments are still
            # analyzable one-camera-at-a-time, just without the scene context.
            logging.error("Camera/room configuration invalid (%s) — analyzing "
                          "without room context", e)
            rooms = {}
        client = make_client()
        pacer = Pacer()
        done = failed = 0
        for camera, raw_path, seg_start in pending:
            try:
                process_segment(client, model, camera, raw_path, seg_start, rooms, pacer)
                done += 1
            except Exception as e:
                logging.exception("[%s %s] segment analysis failed — raw kept for "
                                  "the next retry", camera, seg_start)
                failed += 1
                if status_code(e) == 429:
                    # The quota is the constraint, not this segment: stop the run
                    # rather than march the same 429 through every camera.
                    logging.error("Rate limited — ending this run early; the next "
                                  "run picks up where it stopped.")
                    break
        logging.info("Run finished: %d done, %d failed, %.0fs spent pacing "
                     "(budget %d input tokens/min, %d min per request)",
                     done, failed, pacer.slept, pacer.budget, piece_minutes())
        update_status("analyze", done=done, failed=failed, model=model,
                      paced_seconds=round(pacer.slept),
                      tpm_budget=pacer.budget, piece_minutes=piece_minutes())
        return done, failed
    finally:
        lock.release()


def main():
    done, failed = analyze_pending()
    if failed:
        logging.error("%d segment(s) failed and will be retried on the next run.", failed)
    # Exit 0 even on per-segment failures: the raw files persist and the next
    # timer run retries; a red unit here would mask genuine config errors.


if __name__ == "__main__":
    main()
