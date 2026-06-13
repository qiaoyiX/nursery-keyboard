import threading
import json
import logging
import os
import time
from datetime import datetime
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


def find_keypad():
    """Find the SayoDevice keyboard interface by name.

    Matches 'sayodevice' + 'keyboard' in the device name so we get the
    keyboard HID interface (e.g. 'SayoDevice 1x4P Keyboard') and not the
    mouse or raw interfaces. Avoids capability checks because SayoDevice
    doesn't advertise its keys in the HID descriptor capabilities list.
    """
    for path in evdev.list_devices():
        try:
            dev = evdev.InputDevice(path)
            name = dev.name.lower()
            if "sayodevice" in name and "keyboard" in name:
                return dev
        except Exception:
            continue
    return None


def keypad_listener():
    if not EVDEV_AVAILABLE:
        return
    while True:
        try:
            keypad = find_keypad()
            if keypad is None:
                logging.info("Keypad not found, retrying in 5s...")
                time.sleep(5)
                continue

            logging.info("Keypad found: %s at %s", keypad.name, keypad.path)
            keypad.grab()  # exclusively capture — F13-F16 won't reach the OS
            try:
                for event in keypad.read_loop():
                    if event.type == evdev.ecodes.EV_KEY:
                        key_event = evdev.categorize(event)
                        if key_event.keystate == evdev.KeyEvent.key_down:
                            key_name = key_event.keycode
                            if isinstance(key_name, list):
                                key_name = key_name[0]
                            logging.info("Key event raw: %s", key_name)
                            label = KEYPAD_KEYS.get(key_name)
                            if label:
                                add_entry(label)
                                logging.info("Logged: %s", label)
            finally:
                try:
                    keypad.ungrab()
                except Exception:
                    pass

        except Exception as e:
            logging.error("Keypad error: %s", e)
            time.sleep(2)


def today_stats(entries):
    today = datetime.now().date().isoformat()
    today_entries = [e for e in entries if e["time"].startswith(today)]
    counts = {label: 0 for label in ["Wet", "Dirty", "Both", "Feed"]}
    for e in today_entries:
        if e["type"] in counts:
            counts[e["type"]] += 1
    return counts


@app.route("/")
def index():
    with log_lock:
        entries = load_log()
    counts = today_stats(entries)
    recent = list(reversed(entries[-50:]))
    return render_template("index.html", counts=counts, recent=recent)


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
    return jsonify({"counts": counts, "recent": recent})


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
