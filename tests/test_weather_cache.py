"""The weather cache: TTL, the stale path, and the shaping the page depends on.

The network is the part that will fail — a Pi on hotel wifi, an API blip, a half-written
file after a power cut. None of those may blank the page or raise into a request, so the
tests here are mostly about what happens when the fetch does *not* work.

Nothing in this file touches the network: `weather.fetch` is stubbed throughout.

Run:  venv/bin/python tests/test_weather_cache.py
"""
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import weather  # noqa: E402

FAILURES = []


def check(name, cond, detail=""):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def fresh():
    """Point the cache at a path that doesn't exist yet."""
    fd, path = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    os.remove(path)
    weather.CACHE_FILE = path
    return path


def payload(base_hour=9, temps=None, day="2026-09-13"):
    """A miniature Open-Meteo response: 24 hourly rows starting at base_hour."""
    times, t2m, app = [], [], []
    for i in range(24):
        h = (base_hour + i) % 24
        d = day if base_hour + i < 24 else "2026-09-14"
        times.append(f"{d}T{h:02d}:00")
        temp = temps[i] if temps and i < len(temps) else 60 + i
        t2m.append(temp)
        app.append(temp - 2)
    return {
        "current": {"temperature_2m": 68, "apparent_temperature": 66, "weather_code": 3,
                    "wind_speed_10m": 6, "is_day": 1},
        "hourly": {"time": times, "temperature_2m": t2m, "apparent_temperature": app,
                   "precipitation_probability": [10] * 24, "weather_code": [3] * 24,
                   "wind_speed_10m": [5] * 24, "uv_index": [2] * 24, "is_day": [1] * 24},
        "daily": {"temperature_2m_min": [55, 52], "temperature_2m_max": [75, 72],
                  "sunrise": ["2026-09-13T06:30"], "sunset": ["2026-09-13T19:20"]},
    }


def stub_fetch(result=None, fail=False):
    calls = []

    def _fetch(lat, lon):
        calls.append((lat, lon))
        if fail:
            raise OSError("no route to host")
        return result if result is not None else payload()
    weather.fetch = _fetch
    return calls


def test_refresh_writes_then_reuses():
    print("a fresh cache is not refetched")
    fresh()
    calls = stub_fetch()
    entry = weather.refresh(40.7, -74.0)
    check("first call fetches", len(calls) == 1)
    check("payload stored", entry["payload"]["current"]["temperature_2m"] == 68)
    check("coordinates stored", (entry["lat"], entry["lon"]) == (40.7, -74.0))

    weather.refresh(40.7, -74.0)
    check("second call is served from disk", len(calls) == 1)

    weather.refresh(40.7, -74.0, force=True)
    check("force refetches", len(calls) == 2)


def test_ttl_and_moving():
    print("the cache expires, and follows you")
    fresh()
    calls = stub_fetch()
    weather.refresh(40.7, -74.0)
    later = datetime.now() + timedelta(seconds=weather.REFRESH_SECONDS + 60)
    weather.refresh(40.7, -74.0, now=later)
    check("an old cache refetches", len(calls) == 2)

    weather.refresh(37.8, -122.4)
    check("a new location refetches immediately", len(calls) == 3)
    weather.refresh(37.8, -122.4)
    check("...and then settles", len(calls) == 3)


def test_failure_keeps_the_old_copy():
    print("a failed fetch keeps what we had")
    fresh()
    stub_fetch()
    good = weather.refresh(40.7, -74.0)
    stub_fetch(fail=True)
    later = datetime.now() + timedelta(seconds=weather.REFRESH_SECONDS + 60)
    entry = weather.refresh(40.7, -74.0, now=later)
    check("still returns weather", entry is not None and entry["payload"] == good["payload"])
    check("fetched_at is unchanged, so the page can say how old it is",
          entry["fetched_at"] == good["fetched_at"])

    on_disk = json.load(open(weather.CACHE_FILE))
    check("nothing half-written on disk", on_disk["payload"] == good["payload"])


def test_cold_cache_and_corruption():
    print("a missing or corrupt cache is a cold cache, not a crash")
    path = fresh()
    check("no file reads as None", weather.read_cache() is None)

    with open(path, "w") as f:
        f.write("{not json")
    check("corrupt file reads as None", weather.read_cache() is None)

    with open(path, "w") as f:
        json.dump({"fetched_at": "2026-09-13T09:00:00"}, f)   # no payload
    check("a payload-less file reads as None", weather.read_cache() is None)

    stub_fetch(fail=True)
    check("a failed first fetch returns None rather than raising",
          weather.refresh(40.7, -74.0) is None)


def test_memo_follows_the_file():
    """The /data poll reads this every 8 seconds, so reads are memoised on mtime."""
    print("memoised reads still see new writes")
    fresh()
    stub_fetch()
    weather.refresh(40.7, -74.0)
    first = weather.read_cache()
    check("a second read is the same object", weather.read_cache() is first)

    weather.write_cache(40.7, -74.0, payload(base_hour=5))
    second = weather.read_cache()
    check("a rewrite is picked up", second is not first)
    check("...with the new contents",
          second["payload"]["hourly"]["time"][0] == "2026-09-13T05:00")

    os.remove(weather.CACHE_FILE)
    check("a deleted file reads as None", weather.read_cache() is None)


def test_cache_age():
    print("cache age")
    fresh()
    stub_fetch()
    entry = weather.refresh(40.7, -74.0)
    age = weather.cache_age(entry, datetime.now() + timedelta(minutes=20))
    check("age is measured from fetched_at", 19 <= age.total_seconds() / 60 <= 21)
    check("a garbled timestamp is not an age",
          weather.cache_age({"fetched_at": "whenever"}) is None)
    check("no entry is not an age", weather.cache_age(None) is None)


def test_overnight_low():
    print("overnight low is tonight's, not the calendar day's")
    # 19:00 onward: temps fall from 60 to 46, then climb again after 08:00.
    temps = [60, 58, 56, 54, 52, 50, 48, 47, 46, 47, 50, 55, 60, 65, 70, 72,
             74, 75, 74, 70, 66, 62, 60, 58]
    p = payload(base_hour=19, temps=temps)
    evening = datetime(2026, 9, 13, 20, 30)
    check("uses the hourly trough", weather.overnight_low(p, now=evening) == 46,
          weather.overnight_low(p, now=evening))

    # At 2am the night is already underway — the window must look backward to 19:00,
    # not forward to tomorrow evening.
    small_hours = datetime(2026, 9, 14, 2, 0)
    check("still tonight at 2am", weather.overnight_low(p, now=small_hours) == 46,
          weather.overnight_low(p, now=small_hours))

    check("falls back to the daily minimum with no hourly rows",
          weather.overnight_low({"daily": {"temperature_2m_min": [55, 52]}},
                                now=evening) == 52)
    check("no data at all is None", weather.overnight_low({}, now=evening) is None)


def test_upcoming_hours():
    print("the hourly ribbon starts at this hour")
    p = payload(base_hour=9)
    now = datetime(2026, 9, 13, 11, 40)
    rows = weather.upcoming_hours(p, count=8, now=now)
    check("eight of them", len(rows) == 8, len(rows))
    check("starts at the current hour", rows[0]["time"] == "2026-09-13T11:00", rows[0]["time"])
    check("rows carry every field",
          all(f in rows[0] for f in weather.HOURLY_FIELDS))
    check("nothing from earlier today", all(r["time"] >= "2026-09-13T11:00" for r in rows))


def test_sky_keys():
    print("sky keys")
    check("clear day", weather.sky_key(0, True) == "day-clear")
    check("clear night", weather.sky_key(0, False) == "night-clear")
    check("rain", weather.sky_key(63, True) == "day-rain")
    check("a storm gets the rain sky", weather.sky_key(95, True) == "day-rain")
    check("fog gets the cloud sky", weather.sky_key(45, False) == "night-cloud")
    check("snow", weather.sky_key(73, True) == "day-snow")


def test_feels_like_matches_nws():
    print("feels-like (NWS)")
    fl = weather.feels_like
    # 2026-09-24, NYC: 64°F, 15 mph. Open-Meteo said 58; every phone said 64.
    check("mild + windy is just the temperature",
          fl({"temperature_2m": 64.2, "wind_speed_10m": 15, "apparent_temperature": 58.1}) == 64.2)
    check("wind chill at 40°F / 15 mph ≈ 32", abs(fl({"temperature_2m": 40, "wind_speed_10m": 15}) - 31.8) < 0.5,
          fl({"temperature_2m": 40, "wind_speed_10m": 15}))
    check("no wind chill in calm air", fl({"temperature_2m": 40, "wind_speed_10m": 2}) == 40)
    check("heat index at 90°F / 60% ≈ 100", abs(fl({"temperature_2m": 90, "relative_humidity_2m": 60}) - 100) < 1.5,
          fl({"temperature_2m": 90, "relative_humidity_2m": 60}))
    check("hot without humidity falls back to apparent",
          fl({"temperature_2m": 90, "apparent_temperature": 95}) == 95)
    check("no temperature falls back to apparent", fl({"apparent_temperature": 50}) == 50)


def main():
    print("Weather cache:")
    original_file, original_fetch = weather.CACHE_FILE, weather.fetch
    try:
        for fn in (test_refresh_writes_then_reuses, test_ttl_and_moving,
                   test_failure_keeps_the_old_copy, test_cold_cache_and_corruption,
                   test_memo_follows_the_file, test_cache_age, test_overnight_low, test_upcoming_hours, test_sky_keys,
                   test_feels_like_matches_nws):
            fn()
    finally:
        weather.CACHE_FILE, weather.fetch = original_file, original_fetch
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S): {', '.join(FAILURES)}")
        sys.exit(1)
    print("All weather-cache tests passed.")


if __name__ == "__main__":
    main()
