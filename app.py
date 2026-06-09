import threading
import json
import os
from datetime import datetime
from flask import Flask, render_template, jsonify, request

try:
    import evdev
    EVDEV_AVAILABLE = True
except ImportError:
    EVDEV_AVAILABLE = False

app = Flask(__name__)

DATA_FILE = os.path.join(os.path.dirname(__file__), "log.json")
KEYPAD_KEYS = {
    "KEY_F13": "Wet",
    "KEY_F14": "Dirty",
    "KEY_F15": "Both",
    "KEY_F16": "Feed",
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


def keypad_listener():
    if not EVDEV_AVAILABLE:
        return
    while True:
        try:
            devices = [evdev.InputDevice(p) for p in evdev.list_devices()]
            keypad = next((d for d in devices if "sayo" in d.name.lower()), None)
            if keypad is None:
                import time
                time.sleep(5)
                continue
            for event in keypad.read_loop():
                if event.type == evdev.ecodes.EV_KEY:
                    key_event = evdev.categorize(event)
                    if key_event.keystate == evdev.KeyEvent.key_down:
                        key_name = key_event.keycode
                        if isinstance(key_name, list):
                            key_name = key_name[0]
                        label = KEYPAD_KEYS.get(key_name)
                        if label:
                            add_entry(label)
        except Exception:
            import time
            time.sleep(2)


@app.route("/")
def index():
    with log_lock:
        entries = load_log()
    today = datetime.now().date().isoformat()
    today_entries = [e for e in entries if e["time"].startswith(today)]
    counts = {label: 0 for label in ["Wet", "Dirty", "Both", "Feed"]}
    for e in today_entries:
        if e["type"] in counts:
            counts[e["type"]] += 1
    recent = list(reversed(entries[-50:]))
    return render_template("index.html", counts=counts, recent=recent)


@app.route("/log", methods=["POST"])
def log_event():
    data = request.get_json()
    event_type = data.get("type")
    if event_type not in ["Wet", "Dirty", "Both", "Feed"]:
        return jsonify({"error": "invalid type"}), 400
    add_entry(event_type)
    return jsonify({"ok": True})


@app.route("/data")
def get_data():
    with log_lock:
        entries = load_log()
    today = datetime.now().date().isoformat()
    today_entries = [e for e in entries if e["time"].startswith(today)]
    counts = {label: 0 for label in ["Wet", "Dirty", "Both", "Feed"]}
    for e in today_entries:
        if e["type"] in counts:
            counts[e["type"]] += 1
    recent = list(reversed(entries[-50:]))
    return jsonify({"counts": counts, "recent": recent})


if __name__ == "__main__":
    if EVDEV_AVAILABLE:
        t = threading.Thread(target=keypad_listener, daemon=True)
        t.start()
    app.run(host="0.0.0.0", port=5000)
