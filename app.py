import threading
import json
import logging
import os
import time
from datetime import datetime, timedelta
from flask import Flask, render_template, jsonify, request

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

DATA_FILE = os.path.join(os.path.dirname(__file__), "log.json")
KEYPAD_KEYS = {
    "KEY_SPACE":  "Wet",
    "KEY_PAGEUP": "Dirty",
    "KEY_DOWN":   "Both",
    "KEY_UP":     "Feed",
}

log_lock = threading.Lock()


def load_log():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE) as f:
            return json.load(f)
    return []


def save_log(entries):
    with open(DATA_FILE, "w") as f:
        json.dump(entries, f)


def add_entry(event_type):
    with log_lock:
        entries = load_log()
        entries.append({"type": event_type, "time": datetime.now().isoformat()})
        save_log(entries)


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
                                add_entry(label)
                                logging.info("Logged: %s", label)
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

        # Re-scan when all interface threads die (e.g. device unplugged)
        for t in threads:
            t.join()
        logging.info("All SayoDevice interfaces died — rescanning in 2s")
        time.sleep(2)


def today_stats(entries):
    today = datetime.now().date().isoformat()
    today_entries = [e for e in entries if e["time"].startswith(today)]
    counts = {label: 0 for label in ["Wet", "Dirty", "Both", "Feed"]}
    for e in today_entries:
        if e["type"] in counts:
            counts[e["type"]] += 1
    return counts


def daily_stats(entries, days=7):
    today = datetime.now().date()
    result = []
    for i in range(days - 1, -1, -1):
        d = (today - timedelta(days=i)).isoformat()
        counts = {label: 0 for label in ["Wet", "Dirty", "Both", "Feed"]}
        for e in entries:
            if e["time"].startswith(d) and e["type"] in counts:
                counts[e["type"]] += 1
        result.append({"date": d, **counts})
    return result


def hourly_stats(entries):
    today = datetime.now().date().isoformat()
    result = [{"hour": h, "Wet": 0, "Dirty": 0, "Both": 0, "Feed": 0} for h in range(24)]
    for e in entries:
        if not e["time"].startswith(today):
            continue
        try:
            h = datetime.fromisoformat(e["time"]).hour
            if e["type"] in ("Wet", "Dirty", "Both", "Feed"):
                result[h][e["type"]] += 1
        except Exception:
            pass
    return result


@app.route("/")
def index():
    with log_lock:
        entries = load_log()
    counts = today_stats(entries)
    recent = list(reversed(entries[-50:]))
    return render_template("index.html", counts=counts, recent=recent)


@app.route("/log", methods=["DELETE"])
def clear_log():
    with log_lock:
        save_log([])
    return jsonify({"ok": True})


@app.route("/log", methods=["POST"])
def log_event():
    data = request.get_json(silent=True) or {}
    event_type = data.get("type")
    if event_type not in ["Wet", "Dirty", "Both", "Feed"]:
        return jsonify({"error": "invalid type"}), 400
    add_entry(event_type)
    return jsonify({"ok": True})


@app.route("/data")
def get_data():
    with log_lock:
        entries = load_log()
    counts = today_stats(entries)
    recent = list(reversed(entries[-50:]))
    daily = daily_stats(entries)
    hourly = hourly_stats(entries)
    return jsonify({"counts": counts, "recent": recent, "daily": daily, "hourly": hourly})


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
    if EVDEV_AVAILABLE:
        t = threading.Thread(target=keypad_listener, daemon=True)
        t.start()
    app.run(host="0.0.0.0", port=8080)
