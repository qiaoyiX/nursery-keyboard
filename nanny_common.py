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
DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

# A raw segment counts as closed (safe to analyze) once a newer segment exists
# for the same camera, or nothing has been appended for this long.
SEGMENT_CLOSED_SECONDS = 120


def ensure_dirs():
    for d in (RAW_DIR, LOWRES_DIR, CHUNKS_DIR, CLIPS_DIR, REPORTS_DIR):
        os.makedirs(d, exist_ok=True)


def load_cameras(env=os.environ):
    """NANNY_CAM_* vars ('name=rtsp://...') → {name: url}, sorted by var name."""
    cams = {}
    for var in sorted(k for k in env if k.startswith("NANNY_CAM_")):
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
