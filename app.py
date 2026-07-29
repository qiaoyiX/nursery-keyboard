import threading
import json
import logging
import os
import errno
import time
from datetime import datetime, timedelta
from flask import Flask, render_template, jsonify, request

from storage import (
    USE_DB,
    CALIBRATE_FLAG,
    log_lock, settings_lock, sleep_lock,
    get_entries, add_entry, clear_today, delete_entry, update_entry,
    load_settings, update_setting,
    get_sleep_sessions_today, get_sleep_sessions_range, get_open_sleep_session,
    read_sleep_status,
)

try:
    from huckleberry_sync import push_event, test_connection
    HUCKLEBERRY_AVAILABLE = True
except Exception:
    HUCKLEBERRY_AVAILABLE = False
    logging.warning("huckleberry_sync not available — Huckleberry sync disabled", exc_info=True)

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

PLAY_DOUBLE_PRESS_SECONDS = 3.0   # second Play press within this window = Probiotic


# ── Keypad listener ───────────────────────────────────────────────────────────

def log_keypad_event(label):
    """Shared log path for keypad events: debounce → store → Huckleberry."""
    try:
        if is_debounced(label):
            logging.info("Debounced: %s — discarded (repeat within window)", label)
            return
        add_entry(label)
        logging.info("Logged: %s", label)
        if HUCKLEBERRY_AVAILABLE:
            push_event(label, datetime.now())
    except Exception as db_err:
        logging.error("DB write failed for %s: %s — event dropped", label, db_err)


# The 4-key pad has no free key for Probiotic, so Play is overloaded: a single
# press logs Play (deferred by the double-press window, so its timestamp lands up
# to PLAY_DOUBLE_PRESS_SECONDS late), a second press within the window logs
# Probiotic instead. The lock is required: key events arrive on one thread per
# SayoDevice interface and the deferred fire runs on a Timer thread.
_play_timer = None
_play_timer_lock = threading.Lock()


def handle_play_press():
    global _play_timer
    with _play_timer_lock:
        if _play_timer is not None:           # second press → Probiotic
            _play_timer.cancel()
            _play_timer = None
            probiotic = True
        else:                                 # first press → hold for the window
            _play_timer = threading.Timer(PLAY_DOUBLE_PRESS_SECONDS, _fire_play)
            _play_timer.daemon = True
            _play_timer.start()
            probiotic = False
    if probiotic:
        log_keypad_event("Probiotic")


def _fire_play():
    global _play_timer
    with _play_timer_lock:
        _play_timer = None
    log_keypad_event("Play")

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
                            if label == "Play":
                                handle_play_press()
                            elif label:
                                log_keypad_event(label)
            finally:
                try:
                    dev.ungrab()
                except Exception:
                    pass

        except OSError as e:
            if e.errno == errno.ENODEV:
                logging.error("Device %s disappeared (ENODEV) — exiting thread for rescan", dev.path)
                return
            logging.error("Error on %s: %s — retrying in 2s", dev.path, e)
            time.sleep(2)
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


# ── Debounce ──────────────────────────────────────────────────────────────────

def is_debounced(event_type, now=None):
    """True if an entry of this type was logged within its debounce window (→ discard).

    Per-type windows come from settings (`debounce_minutes`, minutes; 0 = off). Applies to
    both the keypad and the web /log path so rapid repeats are dropped before add_entry/push.
    """
    now = now or datetime.now()
    with settings_lock:
        window = load_settings().get("debounce_minutes", {}).get(event_type, 0)
    if not window:
        return False
    for e in reversed(get_entries()):   # entries are time-ordered; last match = newest
        if e["type"] == event_type:
            last = datetime.fromisoformat(e["time"])
            return (now - last).total_seconds() < window * 60
    return False


# ── Stats helpers ─────────────────────────────────────────────────────────────

def today_stats(entries):
    today = datetime.now().date().isoformat()
    today_entries = [e for e in entries if e["time"].startswith(today)]
    counts = {label: 0 for label in ["Wet", "Dirty", "Play", "Feed", "Probiotic"]}
    for e in today_entries:
        if e["type"] in counts:
            counts[e["type"]] += 1
    return counts


def daily_stats(entries, days=7):
    today = datetime.now().date()
    result = []
    for i in range(days - 1, -1, -1):
        d = (today - timedelta(days=i)).isoformat()
        counts = {label: 0 for label in ["Wet", "Dirty", "Play", "Feed", "Probiotic"]}
        for e in entries:
            if e["time"].startswith(d) and e["type"] in counts:
                counts[e["type"]] += 1
        result.append({"date": d, **counts})
    return result


def hourly_stats(entries):
    today = datetime.now().date().isoformat()
    result = [{"hour": h, "Wet": 0, "Dirty": 0, "Play": 0, "Feed": 0, "Probiotic": 0} for h in range(24)]
    for e in entries:
        if not e["time"].startswith(today):
            continue
        try:
            h = datetime.fromisoformat(e["time"]).hour
            if e["type"] in ("Wet", "Dirty", "Play", "Feed", "Probiotic"):
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


def today_sleep_stats(sessions, max_open_minutes=None):
    """Summarise today's sleep sessions for the /data endpoint.

    max_open_minutes clamps an *open* (still-running) session's counted duration so a
    stuck-open session can't balloon the displayed total before the daemon force-ends it.
    """
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
        if is_open and max_open_minutes is not None:
            dur = min(dur, max_open_minutes)
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


def weekly_pattern_stats(entries, sessions, days=7, max_open_minutes=None):
    """Per-day sleep segments + feed times for the weekly pattern chart.

    Midnight-spanning sessions are split into per-day segments HERE so the client
    positions everything by minute-of-day (a segment ending at midnight is minute
    1440, which the client's minuteOfDay() cannot express). start_iso / end_iso /
    duration_minutes always describe the WHOLE session (for the tap toast);
    is_open marks only the growing segment of a still-running session. Feeds ship
    as raw ISO strings — point events don't split.
    """
    now = datetime.now()
    day_dates = [now.date() - timedelta(days=days - 1 - i) for i in range(days)]
    buckets = {d.isoformat(): {"date": d.isoformat(), "sleep": [], "feeds": []}
               for d in day_dates}

    for s in sessions:
        start_raw = s["start_time"]
        start = start_raw if isinstance(start_raw, datetime) else datetime.fromisoformat(str(start_raw))
        end_raw = s.get("end_time")
        is_open = end_raw is None
        if is_open:
            end = now
            if max_open_minutes is not None:
                end = min(end, start + timedelta(minutes=max_open_minutes))
        else:
            end = end_raw if isinstance(end_raw, datetime) else datetime.fromisoformat(str(end_raw))
        if end <= start:
            continue
        duration = round((end - start).total_seconds() / 60, 1)

        day = start.date()
        while day <= end.date():
            key = day.isoformat()
            day_start = datetime.combine(day, datetime.min.time())
            seg_start = max(start, day_start)
            seg_end   = min(end, day_start + timedelta(days=1))
            if key in buckets and seg_end > seg_start:
                start_min = int((seg_start - day_start).total_seconds() // 60)
                end_min   = int((seg_end - day_start).total_seconds() // 60)
                buckets[key]["sleep"].append({
                    "start_min": start_min,
                    # a sub-minute sliver still gets 1 minute so it renders
                    "end_min":   min(1440, max(end_min, start_min + 1)),
                    "is_open":   is_open and day == end.date(),
                    "start_iso": start.isoformat(),
                    "end_iso":   None if is_open else end.isoformat(),
                    "duration_minutes": duration,
                })
            day += timedelta(days=1)

    for e in entries:
        if e["type"] == "Feed":
            bucket = buckets.get(str(e["time"])[:10])
            if bucket:
                bucket["feeds"].append(e["time"])

    return {"days": [buckets[d.isoformat()] for d in day_dates]}


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
        # Write only the changed key — saving the merged dict bakes every current
        # default into settings.json and freezes it against future tuning.
        update_setting("feed_interval_minutes", interval)
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


@app.route("/log/entry", methods=["PATCH"])
def update_entry_route():
    data = request.get_json(silent=True) or {}
    entry_id = data.get("id")
    event_type = data.get("type")
    time = data.get("time")
    if not isinstance(entry_id, int):
        return jsonify({"error": "missing id"}), 400
    if event_type not in ["Wet", "Dirty", "Play", "Feed", "Probiotic"]:
        return jsonify({"error": "invalid type"}), 400
    try:
        time = datetime.fromisoformat(time).isoformat()
    except (TypeError, ValueError):
        return jsonify({"error": "invalid time"}), 400
    updated = update_entry(entry_id, event_type, time)
    if updated == 0:
        return jsonify({"error": "not found"}), 404
    return jsonify({"ok": True})


@app.route("/log", methods=["POST"])
def log_event():
    data = request.get_json(silent=True) or {}
    event_type = data.get("type")
    if event_type not in ["Wet", "Dirty", "Play", "Feed", "Probiotic"]:
        return jsonify({"error": "invalid type"}), 400
    if is_debounced(event_type):
        return jsonify({"ok": True, "discarded": True, "reason": "debounced"})
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
    max_open_min   = float(settings.get("sleep_max_session_hours", 14)) * 60
    sleep_summary  = today_sleep_stats(sessions_today, max_open_minutes=max_open_min)
    sleep_status   = read_sleep_status()

    current_start_iso = None
    if sleep_status == "asleep":
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
        "week": weekly_pattern_stats(entries, get_sleep_sessions_range(7),
                                     max_open_minutes=max_open_min),
    })


@app.route("/history")
def history():
    """Filtered history search across ALL entries (not just today's recent 50).

    Query params (both optional):
      date — YYYY-MM-DD, matches the entry's local date
      type — one of Wet/Dirty/Play/Feed
    Returns newest-first, capped to HISTORY_LIMIT.
    """
    HISTORY_LIMIT = 200
    entries = get_entries()

    etype = request.args.get("type")
    if etype in ("Wet", "Dirty", "Play", "Feed", "Probiotic"):
        entries = [e for e in entries if e["type"] == etype]

    date = request.args.get("date")
    if date:
        entries = [e for e in entries if e["time"].startswith(date)]

    entries = list(reversed(entries))[:HISTORY_LIMIT]
    return jsonify({"entries": entries})


@app.route("/huckleberry/test")
def huckleberry_test():
    if not HUCKLEBERRY_AVAILABLE:
        return jsonify({"ok": False, "error": "huckleberry_sync module not available (import failed)"}), 503
    with settings_lock:
        s = load_settings()
    if not s.get("huckleberry_email") or not s.get("huckleberry_password"):
        return jsonify({"ok": False, "error": "credentials not set — add huckleberry_email and huckleberry_password to settings.json"}), 400
    try:
        result = test_connection()
        return jsonify({"ok": True, "child_count": result["child_count"]})
    except Exception as exc:
        logging.exception("Huckleberry connection test failed")
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/nanny")
def nanny_page():
    return render_template("nanny.html")


@app.route("/nanny/data")
def nanny_data():
    """Daily nanny report JSON. ?date=YYYY-MM-DD, defaults to the newest report."""
    from nanny_common import REPORTS_DIR, STATUS_FILE
    dates = []
    if os.path.isdir(REPORTS_DIR):
        for name in os.listdir(REPORTS_DIR):
            if name.endswith(".json"):
                try:
                    datetime.strptime(name[:-5], "%Y-%m-%d")
                    dates.append(name[:-5])
                except ValueError:
                    continue
    dates.sort(reverse=True)

    requested = request.args.get("date")
    if requested is not None:
        try:
            datetime.strptime(requested, "%Y-%m-%d")
        except ValueError:
            return jsonify({"error": "invalid date"}), 400
    target = requested or (dates[0] if dates else None)

    report = None
    if target in dates:
        try:
            with open(os.path.join(REPORTS_DIR, target + ".json")) as f:
                report = json.load(f)
        except (OSError, ValueError):
            return jsonify({"error": "report unreadable"}), 500

    # Last-run bookkeeping from the analyzer and the report merge. Without it a
    # missing day is a mystery on the page and only explicable from journalctl.
    status = {}
    try:
        with open(STATUS_FILE) as f:
            status = json.load(f)
    except (OSError, ValueError):
        pass
    return jsonify({"dates": dates, "report": report, "status": status})


@app.route("/nanny/clips/<day>/<path:filename>")
def nanny_clip(day, filename):
    """Serve a phone-use evidence clip. Both parts are validated; Flask's
    send_from_directory refuses path traversal on top."""
    from flask import abort, send_from_directory
    from nanny_common import CLIPS_DIR
    try:
        datetime.strptime(day, "%Y-%m-%d")
    except ValueError:
        abort(404)
    if os.sep in filename or not filename.endswith(".mp4"):
        abort(404)
    return send_from_directory(os.path.join(CLIPS_DIR, day), filename)


@app.route("/sleep/calibrate", methods=["POST"])
def sleep_calibrate():
    with open(CALIBRATE_FLAG, "w") as f:
        f.write(datetime.now().isoformat())
    return jsonify({"ok": True})


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
