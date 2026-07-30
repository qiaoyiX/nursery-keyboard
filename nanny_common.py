"""
Shared plumbing for the nanny-report pipeline (nanny_record / nanny_analyze /
nanny_report). See CLAUDE.md "Nanny report".

Configuration comes from the environment (systemd EnvironmentFile
/etc/nursery-tracker/nanny.env — RTSP URLs embed camera passwords and the
Gemini key lives there too, so none of it belongs in settings.json):

    GEMINI_API_KEY               required for analysis
    GEMINI_MODEL                 default gemini-2.5-flash-lite
    NANNY_CAM_1..N               name=rtsp-url  (one var per camera: RTSP
                                 passwords can contain any list delimiter)
    NANNY_CAM_ROOMS              cam:room,cam:room  — which physical room each
                                 camera watches; two cameras sharing a room is
                                 the point (they see the same scene from two
                                 angles). Unlisted cameras get their own room.
    NANNY_WINDOW                 default 10:00-18:00
    NANNY_DAYS                   default Mon,Tue,Wed,Thu,Fri
    NANNY_CLIP_RETENTION_DAYS    default 14

    Quota shaping (read by nanny_analyze; a camera-hour is ~240k input tokens
    and the API limits input tokens per MINUTE, so these matter more than the
    request count does):
    NANNY_PIECE_MINUTES          footage per Gemini call, default 30 (0 = hour)
    NANNY_TPM_BUDGET             input tokens/min to stay under, default 200000
    NANNY_MAX_SEGMENTS_PER_RUN   segments per analyzer run, default 4
    GEMINI_THINKING_LEVEL        optional; caps thinking so the JSON still fits
    GEMINI_MAX_OUTPUT_TOKENS     default 32768

Disk layout (all gitignored, all under the repo dir):

    nanny/raw/<cam>/<YYYYmmdd_HHMMSS>.mp4    ffmpeg segments; deleted after analysis
    nanny/lowres/<cam>/...                   1fps transcodes; transient
    nanny/chunks/<YYYY-MM-DD>/<cam>_<HHMMSS>.json   per-segment Gemini output;
                                             its existence marks the segment done
    nanny/clips/<YYYY-MM-DD>/*.mp4           kept phone-use evidence clips
    nanny/reports/<YYYY-MM-DD>.json          merged daily report
"""

import fcntl
import json
import os
import re
import shutil
from datetime import datetime, time as dtime

BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
NANNY_DIR   = os.path.join(BASE_DIR, "nanny")
RAW_DIR     = os.path.join(NANNY_DIR, "raw")
LOWRES_DIR  = os.path.join(NANNY_DIR, "lowres")
CHUNKS_DIR  = os.path.join(NANNY_DIR, "chunks")
CLIPS_DIR   = os.path.join(NANNY_DIR, "clips")
REPORTS_DIR = os.path.join(NANNY_DIR, "reports")
STATUS_FILE = os.path.join(BASE_DIR, "nanny_status.json")
LOCK_FILE   = os.path.join(NANNY_DIR, ".analyze.lock")

SEGMENT_NAME_RE = re.compile(r"^(\d{8}_\d{6})\.mp4$")
# Only NANNY_CAM_<number> is a camera. A prefix match would also swallow
# NANNY_CAM_ROOMS, whose value is not a name=url pair — which made every
# service reject the whole config the moment a rooms line was added
# (2026-07-28: recorder idled, analyzer lost its rooms, report exited 1).
CAMERA_VAR_RE = re.compile(r"^NANNY_CAM_(\d+)$")
DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

# A raw segment counts as closed (safe to analyze) once a newer segment exists
# for the same camera, or nothing has been appended for this long.
SEGMENT_CLOSED_SECONDS = 120

# One floor shared by both services: the recorder refuses to start a camera
# below it, and the analyzer starts dropping its oldest unanalyzed footage
# below it. Two different floors would let one service quietly undo the other's
# safety margin.
MIN_FREE_BYTES = 2 * 1024**3


def ensure_dirs():
    for d in (RAW_DIR, LOWRES_DIR, CHUNKS_DIR, CLIPS_DIR, REPORTS_DIR):
        os.makedirs(d, exist_ok=True)


def load_cameras(env=os.environ):
    """NANNY_CAM_<n> vars ('name=rtsp://...') → {name: url}, in numeric order."""
    cams = {}
    numbered = sorted(((int(m.group(1)), m.group(0)) for m in
                       (CAMERA_VAR_RE.match(k) for k in env) if m))
    for _, var in numbered:
        value = env[var].strip()
        if not value:
            continue
        name, sep, url = value.partition("=")
        name = name.strip()
        if not sep or not name or not url.strip():
            raise ValueError(f"{var} must look like name=rtsp://... (got {value[:30]!r})")
        if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
            raise ValueError(f"{var}: camera name {name!r} must be [A-Za-z0-9_-]+ "
                             "(it becomes a directory and filename part)")
        cams[name] = url.strip()
    return cams


def load_camera_rooms(cameras, env=os.environ):
    """NANNY_CAM_ROOMS 'cam:room,cam:room' → {camera: room} for every camera.

    Rooms are what make the cameras a *scene* rather than three unrelated
    videos: cameras sharing a room watch the same caregiver/baby from two
    angles, and the phone-use policy is judged per room, not per camera. A
    camera nobody assigned a room becomes its own single-camera room, which is
    exactly the pre-rooms behaviour.
    """
    mapping = {}
    raw = env.get("NANNY_CAM_ROOMS", "").strip()
    for part in filter(None, (p.strip() for p in raw.split(","))):
        cam, sep, room = part.partition(":")
        cam, room = cam.strip(), room.strip()
        if not sep or not cam or not room:
            raise ValueError(f"NANNY_CAM_ROOMS entry {part!r} must look like camera:room")
        if not re.fullmatch(r"[A-Za-z0-9_ -]+", room):
            raise ValueError(f"NANNY_CAM_ROOMS: room {room!r} must be [A-Za-z0-9_ -]+")
        if cameras and cam not in cameras:
            raise ValueError(f"NANNY_CAM_ROOMS names unknown camera {cam!r} "
                             f"(known: {', '.join(sorted(cameras)) or 'none'})")
        mapping[cam] = room
    for cam in cameras:
        mapping.setdefault(cam, cam)
    return mapping


CONTEXT_FILE_DEFAULT = "/etc/nursery-tracker/nanny_context.md"
CONTEXT_MAX_CHARS = 4000


def load_context(env=os.environ):
    """Standing household context for the prompt: who the caregiver is, who else
    lives here, the baby's name and age.

    Kept out of the repo (real names of a child and an employee do not belong in
    git history) and out of nanny.env, which is a shell-ish key=value file that
    cannot hold a paragraph. Never raises: a missing or unreadable context file
    costs the model some background, it does not stop the day being analyzed.
    Truncated because it rides on every single call.
    """
    path = env.get("NANNY_CONTEXT_FILE", "").strip() or CONTEXT_FILE_DEFAULT
    try:
        with open(path) as f:
            text = f.read()
    except OSError:
        return ""
    lines = [ln for ln in text.splitlines() if not ln.lstrip().startswith("#")]
    return "\n".join(lines).strip()[:CONTEXT_MAX_CHARS]


def context_age_days(env=os.environ):
    """Days since the household context was last edited, or None if absent.

    A caregiver change against a stale file miscasts people in every prompt,
    indefinitely and silently — the model keeps confidently naming someone who
    no longer works here.
    """
    path = env.get("NANNY_CONTEXT_FILE", "").strip() or CONTEXT_FILE_DEFAULT
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return None
    return (datetime.now() - datetime.fromtimestamp(mtime)).days


def disk_status():
    """Free space and the unanalyzed-raw backlog behind it.

    The silent failure mode of this pipeline is the analyzer falling behind on
    quota until purge_raw_under_disk_pressure() starts deleting hours nobody
    ever looked at. That is visible here *before* it fires; until now the first
    sign was footage already gone.
    """
    out = {"free_gb": None, "free_floor_gb": round(MIN_FREE_BYTES / 1024**3, 1),
           "pending_segments": 0, "oldest_pending_hours": None}
    try:
        out["free_gb"] = round(shutil.disk_usage(BASE_DIR).free / 1024**3, 1)
    except OSError:
        pass
    try:
        pending = pending_segments(now=datetime.now())
    except OSError:
        return out
    out["pending_segments"] = len(pending)
    if pending:
        oldest = min(seg_start for _, _, seg_start in pending)
        out["oldest_pending_hours"] = round(
            (datetime.now() - oldest).total_seconds() / 3600, 1)
    return out


def load_window(env=os.environ):
    """NANNY_WINDOW 'HH:MM-HH:MM' → (time, time)."""
    raw = env.get("NANNY_WINDOW", "10:00-18:00")
    m = re.fullmatch(r"(\d{1,2}):(\d{2})-(\d{1,2}):(\d{2})", raw.strip())
    if not m:
        raise ValueError(f"NANNY_WINDOW must be HH:MM-HH:MM (got {raw!r})")
    start = dtime(int(m.group(1)), int(m.group(2)))
    end   = dtime(int(m.group(3)), int(m.group(4)))
    if start >= end:
        raise ValueError(f"NANNY_WINDOW start must precede end (got {raw!r})")
    return start, end


def load_days(env=os.environ):
    """NANNY_DAYS 'Mon,Tue,...' → set of weekday ints (Mon=0)."""
    raw = env.get("NANNY_DAYS", "Mon,Tue,Wed,Thu,Fri")
    days = set()
    for part in raw.split(","):
        part = part.strip().capitalize()
        if part not in DAY_NAMES:
            raise ValueError(f"NANNY_DAYS contains unknown day {part!r}")
        days.add(DAY_NAMES.index(part))
    return days


def segment_start(filename):
    """'20260727_101500.mp4' → datetime(2026,7,27,10,15,0); None if not a segment name."""
    m = SEGMENT_NAME_RE.match(os.path.basename(filename))
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y%m%d_%H%M%S")
    except ValueError:
        return None


def chunk_path(camera, start_dt):
    day_dir = os.path.join(CHUNKS_DIR, start_dt.date().isoformat())
    return os.path.join(day_dir, f"{camera}_{start_dt.strftime('%H%M%S')}.json")


def offset_to_wallclock(start_dt, offset_str):
    """'MM:SS' or 'HH:MM:SS' offset from segment start → datetime.

    Tolerant of model output quirks (int seconds, 'M:SS'); returns None if
    unparseable so callers can drop the event rather than crash.
    """
    from datetime import timedelta
    if isinstance(offset_str, (int, float)):
        return start_dt + timedelta(seconds=float(offset_str))
    if not isinstance(offset_str, str):
        return None
    parts = offset_str.strip().split(":")
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return None
    if len(nums) == 2:
        secs = nums[0] * 60 + nums[1]
    elif len(nums) == 3:
        secs = nums[0] * 3600 + nums[1] * 60 + nums[2]
    else:
        return None
    if secs < 0:
        return None
    return start_dt + timedelta(seconds=secs)


def pending_segments(now=None):
    """Yield (camera, path, start_dt) for closed raw segments with no chunk JSON."""
    now = now or datetime.now()
    if not os.path.isdir(RAW_DIR):
        return []
    out = []
    for camera in sorted(os.listdir(RAW_DIR)):
        cam_dir = os.path.join(RAW_DIR, camera)
        if not os.path.isdir(cam_dir):
            continue
        segs = []
        for name in os.listdir(cam_dir):
            start = segment_start(name)
            if start is not None:
                segs.append((start, os.path.join(cam_dir, name)))
        segs.sort()
        for i, (start, path) in enumerate(segs):
            newer_exists = i < len(segs) - 1
            try:
                age = now.timestamp() - os.path.getmtime(path)
            except OSError:
                continue  # deleted between listdir and stat
            if not newer_exists and age < SEGMENT_CLOSED_SECONDS:
                continue  # still being written
            if os.path.exists(chunk_path(camera, start)):
                continue  # already analyzed
            out.append((camera, path, start))
    return out


def atomic_write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=1)
    os.replace(tmp, path)


def update_status(stage, **fields):
    """Merge one stage's status into nanny_status.json (last-run bookkeeping)."""
    status = {}
    if os.path.exists(STATUS_FILE):
        try:
            with open(STATUS_FILE) as f:
                status = json.load(f)
        except (json.JSONDecodeError, OSError):
            status = {}
    status[stage] = {"at": datetime.now().isoformat(), **fields}
    atomic_write_json(STATUS_FILE, status)


class AnalyzeLock:
    """flock guard: the analyzer can be invoked by its own timer AND by the
    daily report's straggler sweep — never let two runs race on the same
    segments. Non-blocking: the loser skips (the winner will finish the work)."""

    def __init__(self):
        self._fh = None

    def acquire(self):
        ensure_dirs()
        self._fh = open(LOCK_FILE, "w")
        try:
            fcntl.flock(self._fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            self._fh.close()
            self._fh = None
            return False

    def release(self):
        if self._fh is not None:
            fcntl.flock(self._fh, fcntl.LOCK_UN)
            self._fh.close()
            self._fh = None
