import threading
import logging
import os
import time
from datetime import datetime, timedelta
from flask import Flask, render_template, jsonify, request

from storage import (
    USE_DB,
    log_lock, settings_lock, sleep_lock,
    get_entries, add_entry, clear_today, delete_entry,
    load_settings, save_settings,
    get_sleep_sessions_today, get_open_sleep_session, read_sleep_status,
)

try:
    from huckleberry_sync import push_event
    HUCKLEBERRY_AVAILABLE = True
except ImportError:
    HUCKLEBERRY_AVAILABLE = False
    logging.warning("huckleberry_sync not available — Huckleberry sync disabled")

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

KEYPAD_KEYS = {
    "KEY_SPACE":  "Wet",
    "KEY_PAGEUP": "Dirty",
    "KEY_DOWN":   "Play",
    "KEY_UP":     "Feed",
}


# ── Keypad listener ───────────────────────────────────────────────────────────

def find_all_sayodevices():
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
                                    if HUCKLEBERRY_AVAILABLE:
                                        push_event(label, datetime.now())
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


# ── Stats helpers ─────────────────────────────────────────────────────────────

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


def today_sleep_stats(sessions):
    """Summarise today's sleep sessions for the /data endpoint."""
    now = datetime.now()
    total_minutes = 0.0
    sessions_out = []

    for s in sessions:
        start_raw = s["start_time"]
        start = start_raw if isinstance(start_raw, datetime) else datetime.fromisoformat(str(start_raw))

        end_raw = s.get("end_time")
        if end_raw is None:
            end = now
            is_open = True
        elif isinstance(end_raw, datetime):
            end = end_raw
            is_open = False
        else:
            end = datetime.fromisoformat(str(end_raw))
            is_open = False

        dur = (end - start).total_seconds() / 60
        total_minutes += dur
        sessions_out.append({
            "id":               s["id"],
            "start_iso":        start.isoformat(),
            "end_iso":          None if is_open else end.isoformat(),
            "duration_minutes": round(dur, 1),
            "is_open":          is_open,
        })

    return {
        "sessions":            sessions_out,
        "total_sleep_minutes": round(total_minutes, 1),
        "nap_count":           len(sessions_out),
    }


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
def clear_today_route():
    clear_today()
    return jsonify({"ok": True})


@app.route("/log/entry", methods=["DELETE"])
def delete_entry_route():
    data = request.get_json(silent=True) or {}
    entry_id = data.get("id")
    if not isinstance(entry_id, int):
        return jsonify({"error": "missing id"}), 400
    deleted = delete_entry(entry_id)
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
    if HUCKLEBERRY_AVAILABLE:
        push_event(event_type, datetime.now())
    return jsonify({"ok": True})


@app.route("/data")
def get_data():
    entries = get_entries()
    with settings_lock:
        settings = load_settings()
    interval = settings["feed_interval_minutes"]
    counts   = today_stats(entries)
    recent   = list(reversed(entries[-50:]))
    daily    = daily_stats(entries)
    hourly   = hourly_stats(entries)

    sessions_today = get_sleep_sessions_today()
    sleep_summary  = today_sleep_stats(sessions_today)
    sleep_status   = read_sleep_status()

    current_start_iso = None
    if sleep_status == "sleeping":
        open_s = get_open_sleep_session()
        if open_s:
            t = open_s["start_time"]
            current_start_iso = t.isoformat() if isinstance(t, datetime) else str(t)

    return jsonify({
        "counts":                counts,
        "recent":                recent,
        "daily":                 daily,
        "hourly":                hourly,
        "feed_interval_minutes": interval,
        "next_feed_iso":         next_feed_iso(entries, interval),
        "sleep": {
            "status":              sleep_status,
            "current_start_iso":   current_start_iso,
            "total_sleep_minutes": sleep_summary["total_sleep_minutes"],
            "nap_count":           sleep_summary["nap_count"],
            "sessions_today":      sleep_summary["sessions"],
        },
    })


@app.route("/devices")
def list_devices():
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
