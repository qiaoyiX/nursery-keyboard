# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Raspberry Pi baby-tracking app. A 4-key USB keypad (SayoDevice) is plugged into the Pi; pressing a key logs a diaper-change or feeding event to storage and the dashboard updates instantly. The dashboard is a mobile-first Flask web app served on port 8080.

## Running locally (dev machine, no Pi)

```bash
python3 -m venv venv && source venv/bin/activate
pip install flask psycopg2-binary   # evdev is Linux-only, skip it
python app.py
# → http://localhost:8080
```

`evdev` import failure is handled gracefully — the keypad listener is disabled but the web dashboard runs normally.

## On the Pi (production)

```bash
# Deploy a change
git pull && sudo systemctl restart nursery-tracker

# Watch live logs (key presses, errors)
sudo journalctl -u nursery-tracker -f

# Service management
sudo systemctl status | restart | stop nursery-tracker
```

The service is installed by `install.sh` and runs `python app.py` from a venv at the repo root.

## Architecture

Three files form the core:

**`storage.py`** — Shared storage layer imported by both `app.py` and `sleep_monitor.py`.
- Dual-mode: `USE_DB = bool(DATABASE_URL and PSYCOPG2_AVAILABLE)`. `_pg_*` functions use Neon Postgres; `_json_*` functions use local files.
- Public API: `get_entries()`, `add_entry()`, `clear_today()`, `delete_entry()`, `load_settings()`, `save_settings()`.
- Sleep API: `start_sleep_session()`, `end_sleep_session()`, `get_sleep_sessions_today()`, `get_open_sleep_session()`, `write_sleep_heartbeat()`, `read_sleep_status()`.

**`app.py`** — Flask backend + keypad listener in one process.
- **Keypad thread**: `keypad_listener()` scans for all SayoDevice `/dev/input/event*` interfaces, spawns a `listen_one_interface()` thread per interface, grabs them exclusively, and calls `add_entry()` on key-down events. Runs as a daemon thread alongside Flask.
- **Stat helpers** (`today_stats`, `daily_stats`, `hourly_stats`, `next_feed_iso`, `today_sleep_stats`) accept in-memory lists and are storage-agnostic.
- **Settings**: always `settings.json` (local file, not backed up to Postgres).

**`sleep_monitor.py`** — Standalone sleep detection daemon (second systemd service).
- Reads RTSP stream from TAPO C110 at ~1fps using OpenCV.
- One robust primitive for both signals: `active_fraction(a, b)` = `absdiff → per-pixel threshold → MORPH_OPEN denoise → fraction of changed pixels`. Bounded [0,1], robust to H.264/IR-grain noise (which defeats mean optical flow). `MORPH_OPEN` (not CLOSE) is essential — it removes isolated noise specks rather than bridging them into false blobs.
- **Presence** (AWAY vs PRESENT) = `active_fraction(current, reference_frame)` against a stored empty-crib baseline (`reference_frame.npy`). Immune to MOG2-style still-object absorption.
- **Motion** (AWAKE vs ASLEEP) = `active_fraction(current, previous_frame)`.
- Every frame logs `state / presence / motion` + thresholds at INFO — tune `sleep_presence_threshold` & `sleep_motion_fraction` in `settings.json` from the real numbers (`journalctl -u nursery-sleep-monitor -f`), no code change needed.
- Reference auto-bootstraps from the median of the first 5 frames; `maybe_update_reference` slowly drifts it (lr=0.02) for lighting changes, triple-gated so it only refines during confirmed-empty periods. "📷 Crib is empty" button saves an authoritative reference (served by `nursery-tracker` — restart that service after template changes).
- 2-frame hysteresis on presence transitions prevents single-frame noise from flipping AWAY state.
- Writes sleep sessions via `storage.py`; writes heartbeat file so Flask can detect if daemon is offline.

**`templates/index.html`** — Single-page dashboard. Pure HTML/CSS/JS, no build step.
- Polls `GET /data` every 8 seconds; `refresh()` updates counts, history, next-feed card, sleep cards, and all three Chart.js charts in one pass.
- Chart.js 4.5.1 loaded from CDN with SHA-384 SRI.
- Event type colors and sleep color are defined as CSS custom properties in `:root` and must match `COLORS` in the JS `<script>` block.

## Key mapping

`KEYPAD_KEYS` in `app.py` maps evdev key names to event types:

| evdev key | Event type |
|-----------|-----------|
| KEY_SPACE  | Wet |
| KEY_PAGEUP | Dirty |
| KEY_DOWN   | Play |
| KEY_UP     | Feed |

To add or rename an event type, update `KEYPAD_KEYS`, the validation list in `log_event()`, the counts dicts in all three stat helpers, the `TYPES` array and `COLORS` object in `index.html`, and the CSS color rules (`.type-*`, `.btn-*`, `.count-card.*`).

## Debounce

`is_debounced()` in `app.py` drops a repeat press of the same type within a per-type window, before `add_entry()`/`push_event()`. It guards both the keypad path (`listen_one_interface`) and the `/log` route (which returns `{"ok": true, "discarded": true}`). Windows are in `settings.json` `debounce_minutes` (minutes; `0` = off), default `{"Feed": 5, "Wet": 1, "Dirty": 1, "Play": 5}`.

## Storage backends

**JSON (default — no config needed)**
- Events in `log.json`, sleep sessions in `sleep_sessions.json`, daemon heartbeat in `sleep_state.json`.
- `_json_delete_entry` uses list index as `id`; indices shift after any deletion, but the client always gets a fresh list before showing delete buttons, so this is safe.

**Postgres (Neon)**
- Set `DATABASE_URL=postgresql://...` in the systemd unit (`sudo systemctl edit nursery-tracker`, add `Environment=DATABASE_URL=...` under `[Service]`).
- Also set it in `nursery-sleep-monitor.service` the same way.
- Required schema:
  ```sql
  CREATE TABLE events (id BIGSERIAL PRIMARY KEY, type VARCHAR(10) NOT NULL, time TIMESTAMP NOT NULL);
  CREATE INDEX ON events (time);
  CREATE TABLE sleep_sessions (
      id BIGSERIAL PRIMARY KEY, start_time TIMESTAMP NOT NULL,
      end_time TIMESTAMP, duration_minutes NUMERIC(8,2));
  CREATE INDEX ON sleep_sessions (start_time);
  ```
- `migrate_log.py` imports an existing `log.json` into Postgres (idempotent).
- `db()` context manager in `storage.py`: commits on clean exit, rolls back on exception, always closes.

## Sleep monitoring

`sleep_monitor.py` is a standalone daemon that detects baby sleep via camera motion analysis.

**Service management:**
```bash
sudo systemctl status | start | stop | restart nursery-sleep-monitor
sudo journalctl -u nursery-sleep-monitor -f
```

**Configuration (in `settings.json`):**
| Key | Default | Description |
|-----|---------|-------------|
| `camera_rtsp_url` | `""` | `rtsp://user:pass@IP:554/stream2` — TAPO camera account credentials |
| `sleep_motion_fraction` | `0.01` | Fraction of pixels changed vs the previous frame above which baby is "moving" |
| `sleep_presence_threshold` | `0.02` | Fraction of 320×240 frame that must differ from the empty-crib reference to count as "present" |
| `sleep_min_minutes` | `10` | Stillness minutes before marking asleep |
| `sleep_wake_seconds` | `20` | Sustained motion seconds before marking awake |

**How it works:** Startup loads `reference_frame.npy` (saved empty-crib baseline) or bootstraps one from the first 5 frames. Then: AWAY → (baby detected for 2 consecutive frames) → AWAKE → (still ≥ `sleep_min_minutes`) → ASLEEP → (motion ≥ `sleep_wake_seconds`) → AWAKE. Start/end times are backdated to when each streak began. Lighting-change guard: frame diff > 80% (IR night-vision flip) skips the frame entirely. Heartbeat written every frame; Flask shows "Camera offline" if stale > 60s. "📷 Crib is empty" button saves the current frame as the new reference baseline (crib must be empty when pressed).

**Tuning:** Every frame logs `presence` and `motion` fractions with their thresholds. Watch `journalctl -u nursery-sleep-monitor -f` with the crib empty vs. baby-in-crib (still and moving), then set `sleep_presence_threshold` / `sleep_motion_fraction` in `settings.json` to sit between the observed values. The monitor re-reads settings on reconnect (or restart the service).

## API routes

| Method | Path | Description |
|--------|------|-------------|
| GET | `/` | Dashboard HTML (server-rendered initial counts) |
| GET | `/data` | JSON: counts, recent 50 entries, hourly/daily stats, next feed |
| POST | `/log` | Add event `{"type": "Wet\|Dirty\|Play\|Feed"}` |
| DELETE | `/log/today` | Remove all of today's entries |
| DELETE | `/log/entry` | Remove one entry `{"id": <int>}` |
| GET | `/settings` | Get `feed_interval_minutes` |
| POST | `/settings` | Set `feed_interval_minutes` (multiple of 15, 15–720) |
| POST | `/sleep/calibrate` | Save current frame as empty-crib reference baseline (crib must be empty) |
| GET | `/devices` | Debug: list input devices (only with `NURSERY_DEBUG=1`) |

## Debugging the keypad

```bash
# Confirm the keypad is visible to the app
bash find_device.sh

# See raw key names as reported by evdev
sudo journalctl -u nursery-tracker -f
# Look for: [/dev/input/eventN] Key event raw: KEY_SPACE

# List all input devices (requires NURSERY_DEBUG=1 in service env)
curl http://raspberrypi.local:8080/devices
```

If key presses appear in the log but don't match any `KEYPAD_KEYS` entry, the device is sending different key names than expected — update the mapping.
