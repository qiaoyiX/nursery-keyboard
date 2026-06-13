import threading
import json
import logging
import os
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from flask import Flask, render_template, jsonify, request

try:
    import psycopg2
    from psycopg2.extras import RealDictCursor
    PSYCOPG2_AVAILABLE = True
except ImportError:
    PSYCOPG2_AVAILABLE = False

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)

try:
    import evdev
    EVDEV_AVAILABLE = True
except ImportError:
    EVDEV_AVAILABLE = False
    logging.warning("evdev not available — keypad listener disabled")

app = Flask(__name__)

# Optional: set DATABASE_URL in the systemd unit to use Neon Postgres.
# Without it the app falls back to a local log.json file.
# Schema: CREATE TABLE events (id BIGSERIAL PRIMARY KEY, type VARCHAR(10) NOT NULL,
#           time TIMESTAMP NOT NULL); CREATE INDEX ON events (time);
DATABASE_URL = os.environ.get("DATABASE_URL")
USE_DB = bool(DATABASE_URL and PSYCOPG2_AVAILABLE)

DATA_FILE = os.path.join(os.path.dirname(__file__), "log.json")
SETTINGS_FILE = os.path.join(os.path.dirname(__file__), "settings.json")
DEFAULT_SETTINGS = {"feed_interval_minutes": 180}

KEYPAD_KEYS = {
    "KEY_SPACE":  "Wet",
    "KEY_PAGEUP": "Dirty",
    "KEY_DOWN":   "Play",
    "KEY_UP":     "Feed",
}

log_lock = threading.Lock()
settings_lock = threading.Lock()


# ── Storage — Postgres path ───────────────────────────────────────────────────

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


# ── Storage — JSON fallback path ──────────────────────────────────────────────

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
    # Assign a stable pseudo-id based on list position for delete compatibility
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


# ── Public storage API (dispatches to Postgres or JSON) ──────────────────────

def get_entries():
    return _pg_get_entries() if USE_DB else _json_get_entries()


def add_entry(event_type):
    if USE_DB:
        _pg_add_entry(event_type)
    else:
        _json_add_entry(event_type)


# ── Settings (local file — tiny, no backup needed) ───────────────────────────

def load_settings():
    if os.path.exists(SETTINGS_FILE):
        with open(SETTINGS_FILE) as f:
            return {**DEFAULT_SETTINGS, **json.load(f)}
    return dict(DEFAULT_SETTINGS)


def save_settings(s):
    with open(SETTINGS_FILE, "w") as f:
        json.dump(s, f)


# ── Keypad listener ───────────────────────────────────────────────────────────

def find_all_sayodevices():
    """Return all /dev/input/event* nodes belonging to any SayoDevice interface."""
    devices = []
    for path in evdev.list_devices():
        try:
            dev = evdev.InputDevice(path)
            if "sayodevice" in dev.name.lower():
                devices.append(dev)
        except Exception:
            continue
    return devices


def listen_one_interface(dev):
    """Read key events from a single evdev device, retrying on disconnect."""
    while True:
        try:
            logging.info("Listening on: %s at %s", dev.name, dev.path)
            try:
                dev.grab()
                logging.info("Grabbed %s", dev.path)
            except Exception as e:
                logging.warning("Could not grab %s: %s — listening without grab", dev.path, e)

            try:
                for event in dev.read_loop():
                    if event.type == evdev.ecodes.EV_KEY:
                        key_event = evdev.categorize(event)
                        if key_event.keystate == evdev.KeyEvent.key_down:
                            key_name = key_event.keycode
                            if isinstance(key_name, list):
                                key_name = key_name[0]
                            logging.info("[%s] Key event raw: %s", dev.path, key_name)
                            label = KEYPAD_KEYS.get(key_name)
                            if label:
                                try:
                                    add_entry(label)
                                    logging.info("Logged: %s", label)
                                except Exception as db_err:
                                    logging.error("DB write failed for %s: %s — event dropped", label, db_err)
            finally:
                try:
                    dev.ungrab()
                except Exception:
                    pass

        except Exception as e:
            logging.error("Error on %s: %s — retrying in 2s", dev.path, e)
            time.sleep(2)


def keypad_listener():
    if not EVDEV_AVAILABLE:
        return
    while True:
        devices = find_all_sayodevices()
        if not devices:
            logging.info("No SayoDevice found, retrying in 5s...")
            time.sleep(5)
            continue

        logging.info("Found %d SayoDevice interface(s): %s",
                     len(devices), [d.path for d in devices])

        threads = []
        for dev in devices:
            t = threading.Thread(target=listen_one_interface, args=(dev,), daemon=True)
            t.start()
            threads.append(t)

        for t in threads:
            t.join()
        logging.info("All SayoDevice interfaces died — rescanning in 2s")
        time.sleep(2)


# ── Stats helpers (operate on in-memory entry list, unchanged) ────────────────

def today_stats(entries):
    today = datetime.now().date().isoformat()
    today_entries = [e for e in entries if e["time"].startswith(today)]
    counts = {label: 0 for label in ["Wet", "Dirty", "Play", "Feed"]}
    for e in today_entries:
        if e["type"] in counts:
            counts[e["type"]] += 1
    return counts


def daily_stats(entries, days=7):
    today = datetime.now().date()
    result = []
    for i in range(days - 1, -1, -1):
        d = (today - timedelta(days=i)).isoformat()
        counts = {label: 0 for label in ["Wet", "Dirty", "Play", "Feed"]}
        for e in entries:
            if e["time"].startswith(d) and e["type"] in counts:
                counts[e["type"]] += 1
        result.append({"date": d, **counts})
    return result


def hourly_stats(entries):
    today = datetime.now().date().isoformat()
    result = [{"hour": h, "Wet": 0, "Dirty": 0, "Play": 0, "Feed": 0} for h in range(24)]
    for e in entries:
        if not e["time"].startswith(today):
            continue
        try:
            h = datetime.fromisoformat(e["time"]).hour
            if e["type"] in ("Wet", "Dirty", "Play", "Feed"):
                result[h][e["type"]] += 1
        except Exception:
            pass
    return result


def next_feed_iso(entries, interval_minutes):
    feed_entries = [e for e in entries if e["type"] == "Feed"]
    if not feed_entries:
        return None
    last_dt = datetime.fromisoformat(max(feed_entries, key=lambda e: e["time"])["time"])
    return (last_dt + timedelta(minutes=interval_minutes)).isoformat()


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    entries = get_entries()
    counts = today_stats(entries)
    recent = list(reversed(entries[-50:]))
    return render_template("index.html", counts=counts, recent=recent)


@app.route("/settings", methods=["GET"])
def get_settings():
    with settings_lock:
        return jsonify(load_settings())


@app.route("/settings", methods=["POST"])
def update_settings():
    data = request.get_json(silent=True) or {}
    interval = data.get("feed_interval_minutes")
    if not isinstance(interval, int) or interval % 15 != 0 or not (15 <= interval <= 720):
        return jsonify({"error": "feed_interval_minutes must be a multiple of 15 between 15 and 720"}), 400
    with settings_lock:
        s = load_settings()
        s["feed_interval_minutes"] = interval
        save_settings(s)
    return jsonify({"ok": True})


@app.route("/log/today", methods=["DELETE"])
def clear_today():
    if USE_DB:
        _pg_clear_today()
    else:
        _json_clear_today()
    return jsonify({"ok": True})


@app.route("/log/entry", methods=["DELETE"])
def delete_entry():
    data = request.get_json(silent=True) or {}
    entry_id = data.get("id")
    if not isinstance(entry_id, int):
        return jsonify({"error": "missing id"}), 400
    deleted = _pg_delete_entry(entry_id) if USE_DB else _json_delete_entry(entry_id)
    if deleted == 0:
        return jsonify({"error": "not found"}), 404
    return jsonify({"ok": True})


@app.route("/log", methods=["POST"])
def log_event():
    data = request.get_json(silent=True) or {}
    event_type = data.get("type")
    if event_type not in ["Wet", "Dirty", "Play", "Feed"]:
        return jsonify({"error": "invalid type"}), 400
    add_entry(event_type)
    return jsonify({"ok": True})


@app.route("/data")
def get_data():
    entries = get_entries()
    with settings_lock:
        settings = load_settings()
    interval = settings["feed_interval_minutes"]
    counts = today_stats(entries)
    recent = list(reversed(entries[-50:]))
    daily = daily_stats(entries)
    hourly = hourly_stats(entries)
    return jsonify({
        "counts": counts,
        "recent": recent,
        "daily": daily,
        "hourly": hourly,
        "feed_interval_minutes": interval,
        "next_feed_iso": next_feed_iso(entries, interval),
    })


@app.route("/devices")
def list_devices():
    """Debug endpoint — only active when NURSERY_DEBUG=1 is set in the environment."""
    if not os.environ.get("NURSERY_DEBUG"):
        from flask import abort
        abort(404)
    if not EVDEV_AVAILABLE:
        return jsonify({"error": "evdev not available"}), 500
    result = []
    for path in evdev.list_devices():
        try:
            dev = evdev.InputDevice(path)
            keys = dev.capabilities().get(evdev.ecodes.EV_KEY, [])
            result.append({
                "path": path,
                "name": dev.name,
                "has_f13": evdev.ecodes.KEY_F13 in keys,
            })
        except Exception as e:
            result.append({"path": path, "error": str(e)})
    return jsonify(result)


if __name__ == "__main__":
    logging.info("Storage: %s", "Neon Postgres" if USE_DB else "local log.json (set DATABASE_URL to use Postgres)")
    if EVDEV_AVAILABLE:
        t = threading.Thread(target=keypad_listener, daemon=True)
        t.start()
    app.run(host="0.0.0.0", port=8080, threaded=True)
