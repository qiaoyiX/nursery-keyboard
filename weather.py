"""Weather for the clothing page: fetch once in a while, serve from disk always.

Open-Meteo needs no API key and no account, which is the whole reason it was picked —
there is no secret to put in the Pi's EnvironmentFile and nothing to rotate.

Two rules shape this module:

  * **No request handler ever waits on the network.** The dashboard polls `/data`
    every 8 seconds; a slow upstream call in that path would stall event logging,
    which is the one thing this Pi must never do. A daemon thread refreshes the cache
    on a timer and every handler reads the file.
  * **Offline degrades, never blanks.** A Pi with no internet still shows this
    morning's weather, dimmed, with the age on screen. `stale` is part of the payload,
    not an exception.
"""
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

import clothing

CACHE_FILE = os.path.join(os.path.dirname(__file__), "weather_cache.json")
API_URL = "https://api.open-meteo.com/v1/forecast"
TIMEOUT_S = 8
REFRESH_SECONDS = 30 * 60      # the forecast is hourly; half an hour keeps the top of
                               # each hour fresh without hammering a free service
STALE_AFTER_SECONDS = 90 * 60  # three missed refreshes = say so on screen

cache_lock = threading.Lock()

HOURLY_FIELDS = ("temperature_2m", "apparent_temperature", "precipitation_probability",
                 "weather_code", "wind_speed_10m", "uv_index", "is_day")


# ── Fetch ─────────────────────────────────────────────────────────────────────

def fetch(lat, lon):
    """One GET to Open-Meteo. Raises on failure — callers decide what stale means."""
    params = {
        "latitude": lat,
        "longitude": lon,
        "current": "temperature_2m,apparent_temperature,precipitation,weather_code,"
                   "wind_speed_10m,is_day",
        "hourly": ",".join(HOURLY_FIELDS),
        "daily": "temperature_2m_min,temperature_2m_max,sunrise,sunset",
        "temperature_unit": "fahrenheit",
        "wind_speed_unit": "mph",
        "precipitation_unit": "inch",
        "timezone": "auto",
        "forecast_days": 2,
    }
    url = API_URL + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "nursery-tracker/1.0"})
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
        return json.loads(resp.read().decode("utf-8"))


# ── Cache ─────────────────────────────────────────────────────────────────────

_memo = {"key": None, "entry": None}


def read_cache():
    """Last stored payload, or None. A corrupt file is a cold cache, not a crash.

    Memoised on the file's identity and mtime, because the dashboard's /data poll hits
    this every 8 seconds and the file only changes twice an hour.
    """
    try:
        st = os.stat(CACHE_FILE)
        key = (CACHE_FILE, st.st_mtime_ns, st.st_size)
        if _memo["key"] == key:
            return _memo["entry"]
    except FileNotFoundError:
        return None
    except OSError:
        key = None

    try:
        with open(CACHE_FILE) as f:
            data = json.load(f)
    except FileNotFoundError:
        return None
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as e:
        logging.warning("weather_cache.json unreadable (%s) — refetching", e)
        return None
    if not isinstance(data, dict) or "payload" not in data:
        return None
    if key:
        _memo["key"], _memo["entry"] = key, data
    return data


def write_cache(lat, lon, payload):
    tmp = CACHE_FILE + ".tmp"
    body = {"fetched_at": datetime.now().isoformat(timespec="seconds"),
            "lat": lat, "lon": lon, "payload": payload}
    with open(tmp, "w") as f:
        json.dump(body, f)
    os.replace(tmp, CACHE_FILE)
    return body


def cache_age(entry, now=None):
    if not entry or not entry.get("fetched_at"):
        return None
    try:
        fetched = datetime.fromisoformat(entry["fetched_at"])
    except ValueError:
        return None
    return (now or datetime.now()) - fetched


def refresh(lat, lon, force=False, now=None):
    """Refetch if the cache is old, moved, or missing. Returns the cache entry or None.

    Never raises: a failed refresh leaves the old entry in place, which is exactly what
    the page wants to show.
    """
    with cache_lock:
        entry = read_cache()
        if not force and entry:
            age = cache_age(entry, now)
            moved = (round(entry.get("lat", 0), 3), round(entry.get("lon", 0), 3)) != \
                    (round(lat, 3), round(lon, 3))
            if not moved and age is not None and age.total_seconds() < REFRESH_SECONDS:
                return entry
        try:
            payload = fetch(lat, lon)
        except (urllib.error.URLError, urllib.error.HTTPError, OSError,
                json.JSONDecodeError, TimeoutError) as e:
            logging.warning("weather refresh failed (%s) — keeping cached copy", e)
            return entry
        logging.info("weather refreshed for %.3f,%.3f", lat, lon)
        return write_cache(lat, lon, payload)


def start_refresher(get_location):
    """Daemon thread: keep weather_cache.json warm. `get_location()` → (lat, lon)."""
    def loop():
        while True:
            try:
                lat, lon = get_location()
                refresh(lat, lon)
            except Exception:  # a background thread must outlive its own bugs
                logging.exception("weather refresher iteration failed")
            time.sleep(60)  # cheap tick; refresh() itself decides when to actually fetch

    t = threading.Thread(target=loop, daemon=True, name="weather-refresher")
    t.start()
    return t


# ── Shaping ───────────────────────────────────────────────────────────────────

def _parse(ts):
    try:
        return datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return None


def hourly_rows(payload):
    """The hourly block as a list of dicts, one per hour, instead of parallel arrays."""
    hourly = (payload or {}).get("hourly") or {}
    times = hourly.get("time") or []
    rows = []
    for i, ts in enumerate(times):
        row = {"time": ts}
        for field in HOURLY_FIELDS:
            series = hourly.get(field) or []
            row[field] = series[i] if i < len(series) else None
        rows.append(row)
    return rows


def overnight_low(payload, now=None):
    """Lowest temperature between this evening and tomorrow morning.

    The daily minimum is the wrong number: before midnight it describes the night that
    just ended, and she is not wearing that one.
    """
    now = now or datetime.now()
    start = now.replace(hour=19, minute=0, second=0, microsecond=0)
    if now.hour < 7:               # it is already tonight
        start = start - timedelta(days=1)
    end = start + timedelta(hours=13)   # 19:00 → 08:00

    temps = []
    for row in hourly_rows(payload):
        at = _parse(row["time"])
        if at and start <= at <= end and row.get("temperature_2m") is not None:
            temps.append(row["temperature_2m"])
    if temps:
        return min(temps)

    daily = (payload or {}).get("daily") or {}
    mins = daily.get("temperature_2m_min") or []
    return mins[-1] if mins else None


def upcoming_hours(payload, count=8, now=None):
    """The next `count` hours from now, as shaped rows."""
    now = now or datetime.now()
    cutoff = now.replace(minute=0, second=0, microsecond=0)
    out = []
    for row in hourly_rows(payload):
        at = _parse(row["time"])
        if at and at >= cutoff:
            out.append(row)
        if len(out) >= count:
            break
    return out


def sky_key(code, is_day):
    """A class name for the hero gradient: time of day crossed with the condition."""
    cond = clothing.condition_for(code)
    if cond == "storm":
        cond = "rain"
    if cond == "fog":
        cond = "cloud"
    return ("day" if is_day else "night") + "-" + cond


# ── Geocoding ─────────────────────────────────────────────────────────────────

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"


def geocode(name, count=5):
    """Place name → [{name, latitude, longitude}]. Same service, still no key."""
    url = GEOCODE_URL + "?" + urllib.parse.urlencode(
        {"name": name, "count": count, "language": "en", "format": "json"})
    req = urllib.request.Request(url, headers={"User-Agent": "nursery-tracker/1.0"})
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    out = []
    for r in data.get("results") or []:
        label = ", ".join(x for x in (r.get("name"), r.get("admin1"), r.get("country_code")) if x)
        out.append({"name": label,
                    "latitude": r.get("latitude"),
                    "longitude": r.get("longitude")})
    return out
