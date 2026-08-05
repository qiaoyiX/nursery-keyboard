import json
import logging
import os
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta

try:
    import psycopg2
    from psycopg2.extras import RealDictCursor
    PSYCOPG2_AVAILABLE = True
except ImportError:
    PSYCOPG2_AVAILABLE = False

DATABASE_URL = os.environ.get("DATABASE_URL")
USE_DB = bool(DATABASE_URL and PSYCOPG2_AVAILABLE)

DATA_FILE        = os.path.join(os.path.dirname(__file__), "log.json")
SETTINGS_FILE    = os.path.join(os.path.dirname(__file__), "settings.json")
SLEEP_FILE       = os.path.join(os.path.dirname(__file__), "sleep_sessions.json")
SLEEP_STATE_FILE = os.path.join(os.path.dirname(__file__), "sleep_state.json")
CALIBRATE_FLAG   = os.path.join(os.path.dirname(__file__), "calibrate.flag")

DEFAULT_SETTINGS = {
    "feed_interval_minutes":    180,
    "camera_rtsp_url":          "",
    "sleep_motion_fraction":    0.01,  # fraction of pixels changed vs previous frame = "moving"
    "sleep_min_minutes":        10,
    "sleep_wake_seconds":       20,   # short window for "life evidence" (probation/away override), NOT for waking a nap
    "sleep_wake_minutes":       3,    # sustained-motion minutes to END a sleep session; brief in-sleep arousals (startles, active-sleep squirms) below this are kept as sleep
    "sleep_max_session_hours":  14,    # sanity cap: force-end a sleep session open longer than this
    "debounce_minutes":         {"Feed": 5, "Wet": 1, "Dirty": 1, "Play": 5, "Probiotic": 720},  # discard repeat presses of a type within N min (0 = off)
    "huckleberry_email":        "",
    "huckleberry_password":     "",
    "huckleberry_child_index":  0,
    "huckleberry_timezone":     "America/New_York",
    "sleep_presence_threshold": 0.02,  # fraction of ROI that must differ from empty-crib reference
    "sleep_crib_roi":            [0.0, 0.0, 1.0, 1.0],  # crib region as [x0, y0, x1, y1] fractions of the frame
    "sleep_disturbance_fraction": 0.30,  # motion fraction = parent-scale disturbance; presence re-evaluated after it settles.
                                         # Measured: awake baby squirming reaches 0.10-0.17, pickups/put-downs 0.57-1.0 — 0.30 splits the gap.
    "sleep_settle_seconds":      10,    # quiet seconds after a disturbance before re-evaluating presence
    "sleep_micromotion_fraction": 0.002, # motion fraction counting as living-thing micro-motion (must sit above camera noise floor)
    "sleep_probation_minutes":   10,    # life evidence must appear within this window after an ambiguous settle, else crib ruled empty.
                                        # Measured: real-baby confirms in 13s-4.2min across 3 clips; empty-crib windows never produced >=2 episodes.
    "sleep_liveness_minutes":    20,    # reference-free empty-crib backstop: ASLEEP with zero micro-motion this long -> session closed
                                        # backdated to the last life sign. Longest measured fully-still sleeping stretch is 7.5 min (2.7x margin);
                                        # an empty crib produced 0 micro-frames in 49 min. Catches bedding-ghost phantoms no presence check can
                                        # (2026-07-15: session 394 ran 9h over an empty crib whose ghost presence defeated every reference test).
}

log_lock      = threading.Lock()
sleep_lock    = threading.Lock()
settings_lock = threading.Lock()


# ── Postgres connection ───────────────────────────────────────────────────────

@contextmanager
def db():
    conn = psycopg2.connect(DATABASE_URL)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ── Events — Postgres path ────────────────────────────────────────────────────

def _pg_get_entries():
    with db() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT id, type, time FROM events ORDER BY time")
            return [{"id": r["id"], "type": r["type"], "time": r["time"].isoformat()} for r in cur.fetchall()]


def _pg_add_entry(event_type):
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO events (type, time) VALUES (%s, %s)", (event_type, datetime.now()))


def _pg_clear_today():
    today = datetime.now().date()
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM events WHERE time::date = %s", (today,))


def _pg_delete_entry(entry_id):
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM events WHERE id = %s", (entry_id,))
            return cur.rowcount


def _pg_update_entry(entry_id, event_type, time):
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE events SET type = %s, time = %s WHERE id = %s",
                        (event_type, time, entry_id))
            return cur.rowcount


# ── Events — JSON fallback path ───────────────────────────────────────────────

def _json_load():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE) as f:
            return json.load(f)
    return []


def _json_save(entries):
    # Atomic replace + fsync: as the primary store, a power cut mid-write must
    # never be able to destroy the whole event history.
    tmp = DATA_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(entries, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, DATA_FILE)


def _json_get_entries():
    with log_lock:
        entries = _json_load()
    return [{"id": i, "type": e["type"], "time": e["time"]} for i, e in enumerate(entries)]


def _json_add_entry(event_type):
    with log_lock:
        entries = _json_load()
        entries.append({"type": event_type, "time": datetime.now().isoformat()})
        _json_save(entries)


def _json_clear_today():
    today = datetime.now().date().isoformat()
    with log_lock:
        entries = _json_load()
        _json_save([e for e in entries if not e["time"].startswith(today)])


def _json_delete_entry(entry_id):
    with log_lock:
        entries = _json_load()
        if not (0 <= entry_id < len(entries)):
            return 0
        entries.pop(entry_id)
        _json_save(entries)
    return 1


def _json_update_entry(entry_id, event_type, time):
    with log_lock:
        entries = _json_load()
        if not (0 <= entry_id < len(entries)):
            return 0
        entries[entry_id] = {"type": event_type, "time": time}
        _json_save(entries)
    return 1


# ── Public events API ─────────────────────────────────────────────────────────

def get_entries():
    return _pg_get_entries() if USE_DB else _json_get_entries()


def add_entry(event_type):
    if USE_DB:
        _pg_add_entry(event_type)
    else:
        _json_add_entry(event_type)


def clear_today():
    if USE_DB:
        _pg_clear_today()
    else:
        _json_clear_today()


def delete_entry(entry_id):
    return _pg_delete_entry(entry_id) if USE_DB else _json_delete_entry(entry_id)


def update_entry(entry_id, event_type, time):
    return (_pg_update_entry(entry_id, event_type, time) if USE_DB
            else _json_update_entry(entry_id, event_type, time))


# ── Settings ──────────────────────────────────────────────────────────────────

def load_settings():
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE) as f:
                saved = json.load(f)
            merged = {**DEFAULT_SETTINGS, **saved}
            # debounce_minutes is nested — merge per-type so a newly added type's
            # default isn't shadowed by an older saved dict that predates it.
            merged["debounce_minutes"] = {**DEFAULT_SETTINGS["debounce_minutes"], **saved.get("debounce_minutes", {})}
            return merged
        except json.JSONDecodeError as e:
            logging.warning("settings.json is corrupt (%s) — using defaults and resetting file", e)
            save_settings({})
    return dict(DEFAULT_SETTINGS)


def save_settings(s):
    with open(SETTINGS_FILE, "w") as f:
        json.dump(s, f)


def update_setting(key, value):
    """
    Persist ONE explicit override, leaving every other key to follow the code defaults.

    Never write the merged load_settings() dict back to disk: that bakes every default
    of that moment into settings.json, where it shadows all future tuning of
    DEFAULT_SETTINGS. That is how the Pi kept sleep_disturbance_fraction=0.10 (and its
    era's companions) long after the measured default moved to 0.30 — the drift behind
    the 2026-07-14 missed put-down.
    """
    saved = {}
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE) as f:
                saved = json.load(f)
        except json.JSONDecodeError:
            pass   # corrupt file: rebuild it with just this override
    saved[key] = value
    save_settings(saved)


# ── Sleep sessions — Postgres path ────────────────────────────────────────────

def _pg_start_sleep(start_time, detected_at=None):
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO sleep_sessions (start_time, start_detected_at)
                   VALUES (%s, %s) RETURNING id""",
                (start_time, detected_at)
            )
            return cur.fetchone()[0]


def _pg_end_sleep(session_id, end_time, detected_at=None, reason=None):
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE sleep_sessions
                   SET end_time = %s,
                       duration_minutes = EXTRACT(EPOCH FROM (%s - start_time)) / 60,
                       end_detected_at = %s,
                       end_reason = %s
                   WHERE id = %s""",
                (end_time, end_time, detected_at, reason, session_id)
            )


def _pg_get_sleep_sessions_today():
    today_start = datetime.combine(datetime.now().date(), datetime.min.time())
    cutoff = datetime.now() - timedelta(days=1)
    with db() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """SELECT id, start_time, end_time, duration_minutes
                   FROM sleep_sessions
                   WHERE (end_time IS NOT NULL AND end_time >= %s)
                      OR (end_time IS NULL AND start_time > %s)
                   ORDER BY start_time""",
                (today_start, cutoff)
            )
            return [dict(r) for r in cur.fetchall()]


def _pg_get_sleep_sessions_range(days=7):
    """Sessions overlapping the last `days` local calendar days (today inclusive):
    any closed session ending on/after the window's first midnight, plus open
    sessions started within the last 24h (same cutoff as the today variant)."""
    window_start = datetime.combine(
        datetime.now().date() - timedelta(days=days - 1), datetime.min.time())
    cutoff = datetime.now() - timedelta(days=1)
    with db() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """SELECT id, start_time, end_time, duration_minutes
                   FROM sleep_sessions
                   WHERE (end_time IS NOT NULL AND end_time >= %s)
                      OR (end_time IS NULL AND start_time > %s)
                   ORDER BY start_time""",
                (window_start, cutoff)
            )
            return [dict(r) for r in cur.fetchall()]


def _pg_get_open_sleep_session():
    with db() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT id, start_time FROM sleep_sessions WHERE end_time IS NULL ORDER BY start_time DESC LIMIT 1"
            )
            row = cur.fetchone()
            return dict(row) if row else None


# ── Sleep sessions — JSON fallback path ───────────────────────────────────────

def _json_load_sleep():
    if os.path.exists(SLEEP_FILE):
        with open(SLEEP_FILE) as f:
            return json.load(f)
    return []


def _json_save_sleep_atomic(sessions):
    tmp = SLEEP_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(sessions, f)
    os.replace(tmp, SLEEP_FILE)


def _json_start_sleep(start_time, detected_at=None):
    with sleep_lock:
        sessions = _json_load_sleep()
        new_id = max((s["id"] for s in sessions), default=-1) + 1
        sessions.append({
            "id": new_id,
            "start_time": start_time.isoformat(),
            "end_time": None,
            "duration_minutes": None,
            "start_detected_at": detected_at.isoformat() if detected_at else None,
            "end_detected_at": None,
            "end_reason": None,
        })
        _json_save_sleep_atomic(sessions)
    return new_id


def _json_end_sleep(session_id, end_time, detected_at=None, reason=None):
    with sleep_lock:
        sessions = _json_load_sleep()
        for s in sessions:
            if s["id"] == session_id:
                s["end_time"] = end_time.isoformat()
                start = datetime.fromisoformat(s["start_time"])
                s["duration_minutes"] = round((end_time - start).total_seconds() / 60, 1)
                s["end_detected_at"] = detected_at.isoformat() if detected_at else None
                s["end_reason"] = reason
                break
        _json_save_sleep_atomic(sessions)


def _json_get_sleep_sessions_today():
    """Sessions overlapping today: any closed session ending on/after today's
    midnight (overnight sleep that started yesterday must not vanish from the
    day view), plus open sessions started within the last 24h."""
    today_start = datetime.combine(
        datetime.now().date(), datetime.min.time()).isoformat()
    cutoff = datetime.now() - timedelta(days=1)
    with sleep_lock:
        sessions = _json_load_sleep()
    result = []
    for s in sessions:
        if s["end_time"] is not None:
            if s["end_time"] >= today_start:
                result.append(s)
        elif datetime.fromisoformat(s["start_time"]) > cutoff:
            result.append(s)
    return sorted(result, key=lambda x: x["start_time"])


def _json_get_sleep_sessions_range(days=7):
    """JSON mirror of _pg_get_sleep_sessions_range — ISO timestamps compare
    lexicographically, so no parsing needed for the closed-session filter."""
    window_start = datetime.combine(
        datetime.now().date() - timedelta(days=days - 1), datetime.min.time()).isoformat()
    cutoff = datetime.now() - timedelta(days=1)
    with sleep_lock:
        sessions = _json_load_sleep()
    result = []
    for s in sessions:
        if s["end_time"] is not None:
            if s["end_time"] >= window_start:
                result.append(s)
        elif datetime.fromisoformat(s["start_time"]) > cutoff:
            result.append(s)
    return sorted(result, key=lambda x: x["start_time"])


def _json_get_open_sleep_session():
    with sleep_lock:
        sessions = _json_load_sleep()
    open_sessions = [s for s in sessions if s["end_time"] is None]
    return open_sessions[-1] if open_sessions else None


# ── Public sleep API ──────────────────────────────────────────────────────────

def start_sleep_session(start_time, detected_at=None):
    """start_time is backdated to when stillness began; detected_at is when the
    monitor confirmed it. The gap between them is the detector's onset lag."""
    return (_pg_start_sleep(start_time, detected_at) if USE_DB
            else _json_start_sleep(start_time, detected_at))


def end_sleep_session(session_id, end_time, detected_at=None, reason=None):
    """reason is one of sleep_monitor's END_* codes — see there for why each matters.
    Only 'sustained_wake' means she woke up; the rest are the detector self-correcting."""
    if USE_DB:
        _pg_end_sleep(session_id, end_time, detected_at, reason)
    else:
        _json_end_sleep(session_id, end_time, detected_at, reason)


def get_sleep_sessions_today():
    return _pg_get_sleep_sessions_today() if USE_DB else _json_get_sleep_sessions_today()


def get_sleep_sessions_range(days=7):
    return _pg_get_sleep_sessions_range(days) if USE_DB else _json_get_sleep_sessions_range(days)


def get_open_sleep_session():
    return _pg_get_open_sleep_session() if USE_DB else _json_get_open_sleep_session()


# ── Heartbeat (daemon liveness) ───────────────────────────────────────────────

def write_sleep_heartbeat(daemon_state="awake"):
    """daemon_state: 'away' | 'awake' | 'asleep'"""
    tmp = SLEEP_STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"heartbeat": datetime.now().isoformat(), "state": daemon_state}, f)
    os.replace(tmp, SLEEP_STATE_FILE)


def read_sleep_status():
    """Returns 'away' | 'awake' | 'asleep' | 'unknown'. Called by Flask."""
    if not os.path.exists(SLEEP_STATE_FILE):
        return "unknown"
    try:
        with open(SLEEP_STATE_FILE) as f:
            data = json.load(f)
        if (datetime.now() - datetime.fromisoformat(data["heartbeat"])).total_seconds() > 60:
            return "unknown"
        return data.get("state", "unknown")
    except Exception:
        return "unknown"
