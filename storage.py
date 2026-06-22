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
    "sleep_wake_seconds":       20,
    "sleep_max_session_hours":  14,    # sanity cap: force-end a sleep session open longer than this
    "debounce_minutes":         {"Feed": 5, "Wet": 1, "Dirty": 1, "Play": 5},  # discard repeat presses of a type within N min (0 = off)
    "huckleberry_email":        "",
    "huckleberry_password":     "",
    "huckleberry_child_index":  0,
    "huckleberry_timezone":     "America/New_York",
    "sleep_presence_threshold": 0.02,  # fraction of 320×240 that must differ from empty-crib reference
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


# ── Events — JSON fallback path ───────────────────────────────────────────────

def _json_load():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE) as f:
            return json.load(f)
    return []


def _json_save(entries):
    with open(DATA_FILE, "w") as f:
        json.dump(entries, f)


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


# ── Settings ──────────────────────────────────────────────────────────────────

def load_settings():
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE) as f:
                return {**DEFAULT_SETTINGS, **json.load(f)}
        except json.JSONDecodeError as e:
            logging.warning("settings.json is corrupt (%s) — using defaults and resetting file", e)
            save_settings(dict(DEFAULT_SETTINGS))
    return dict(DEFAULT_SETTINGS)


def save_settings(s):
    with open(SETTINGS_FILE, "w") as f:
        json.dump(s, f)


# ── Sleep sessions — Postgres path ────────────────────────────────────────────

def _pg_start_sleep(start_time):
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO sleep_sessions (start_time) VALUES (%s) RETURNING id",
                (start_time,)
            )
            return cur.fetchone()[0]


def _pg_end_sleep(session_id, end_time):
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE sleep_sessions
                   SET end_time = %s,
                       duration_minutes = EXTRACT(EPOCH FROM (%s - start_time)) / 60
                   WHERE id = %s""",
                (end_time, end_time, session_id)
            )


def _pg_get_sleep_sessions_today():
    today = datetime.now().date()
    cutoff = datetime.now() - timedelta(days=1)
    with db() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """SELECT id, start_time, end_time, duration_minutes
                   FROM sleep_sessions
                   WHERE start_time::date = %s
                      OR (end_time IS NULL AND start_time > %s)
                   ORDER BY start_time""",
                (today, cutoff)
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


def _json_start_sleep(start_time):
    with sleep_lock:
        sessions = _json_load_sleep()
        new_id = max((s["id"] for s in sessions), default=-1) + 1
        sessions.append({
            "id": new_id,
            "start_time": start_time.isoformat(),
            "end_time": None,
            "duration_minutes": None,
        })
        _json_save_sleep_atomic(sessions)
    return new_id


def _json_end_sleep(session_id, end_time):
    with sleep_lock:
        sessions = _json_load_sleep()
        for s in sessions:
            if s["id"] == session_id:
                s["end_time"] = end_time.isoformat()
                start = datetime.fromisoformat(s["start_time"])
                s["duration_minutes"] = round((end_time - start).total_seconds() / 60, 1)
                break
        _json_save_sleep_atomic(sessions)


def _json_get_sleep_sessions_today():
    today = datetime.now().date().isoformat()
    cutoff = datetime.now() - timedelta(days=1)
    with sleep_lock:
        sessions = _json_load_sleep()
    result = []
    for s in sessions:
        start_str = s["start_time"]
        if start_str.startswith(today):
            result.append(s)
        elif s["end_time"] is None and datetime.fromisoformat(start_str) > cutoff:
            result.append(s)
    return sorted(result, key=lambda x: x["start_time"])


def _json_get_open_sleep_session():
    with sleep_lock:
        sessions = _json_load_sleep()
    open_sessions = [s for s in sessions if s["end_time"] is None]
    return open_sessions[-1] if open_sessions else None


# ── Public sleep API ──────────────────────────────────────────────────────────

def start_sleep_session(start_time):
    return _pg_start_sleep(start_time) if USE_DB else _json_start_sleep(start_time)


def end_sleep_session(session_id, end_time):
    if USE_DB:
        _pg_end_sleep(session_id, end_time)
    else:
        _json_end_sleep(session_id, end_time)


def get_sleep_sessions_today():
    return _pg_get_sleep_sessions_today() if USE_DB else _json_get_sleep_sessions_today()


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
