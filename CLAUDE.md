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
- Reads RTSP stream from TAPO C110 at ~1fps using OpenCV; all analysis inside the crib ROI (`sleep_crib_roi`).
- **v5 "event-gated latched presence"** — read `docs/sleep-detection-research.md` before changing anything here; it records why four previous algorithms failed. Presence is a latched state that changes only via: (1) settle evaluation after a parent-scale disturbance (with a micro-motion probation window when the reference says "occupied"), (2) micro-motion override while AWAY, (3) the "📷 Crib is empty" button, (4) the max-session cap. Per-frame reference comparison never flips state directly.
- One robust primitive for both signals: `active_fraction(a, b)` = `absdiff → per-pixel threshold → MORPH_OPEN denoise → fraction of changed pixels`. Bounded [0,1], robust to H.264/IR-grain noise (which defeats mean optical flow). `MORPH_OPEN` (not CLOSE) is essential — it removes isolated noise specks rather than bridging them into false blobs.
- The empty-crib reference (`reference_frame.npy` + shape-validated metadata sidecar) auto-refreshes every time a settle evaluation confirms the crib empty, plus slow triple-gated drift for lighting. A stale or baby-poisoned reference self-heals via probation.
- Every frame logs `state / presence / motion / micro / dist` + thresholds at INFO — tune the `sleep_*` settings from the real numbers (`journalctl -u nursery-sleep-monitor -f`), no code change needed. To tune offline: `record_camera.sh` (on the Pi) captures footage, `replay_sleep.py` runs the identical pipeline over it anywhere.
- Writes sleep sessions via `storage.py`; writes heartbeat file so Flask can detect if daemon is offline.

**`templates/index.html`** — Single-page dashboard. Pure HTML/CSS/JS, no build step.
- Polls `GET /data` every 8 seconds; `refresh()` updates counts, history, next-feed card, and all three Chart.js charts in one pass.
- The next-feed card sits at the top (above the count cards).
- Sleep UI (status card + timeline) is live again (re-enabled 2026-07-02 together with the v5 detection algorithm). `updateSleepCard`/`updateSleepTimeline` consume `data.sleep`; the "📷 Crib is empty" button POSTs `/sleep/calibrate`.
- History rows have an edit (✎) and delete (✕) button. Edit opens a modal (`#editOverlay`) to change an entry's type + time, sent via `PATCH /log/entry`.
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
| `sleep_crib_roi` | `[0,0,1,1]` | Crib region `[x0, y0, x1, y1]` as 0–1 fractions of the frame; everything outside is ignored |
| `sleep_presence_threshold` | `0.02` | ROI fraction differing from the empty-crib reference for a settle evaluation to say "occupied" |
| `sleep_motion_fraction` | `0.01` | ROI fraction changed vs the previous frame above which baby is "moving" (awake) |
| `sleep_micromotion_fraction` | `0.002` | ROI fraction counting as living-thing micro-motion; must sit above the camera noise floor |
| `sleep_disturbance_fraction` | `0.30` | ROI fraction = parent-scale disturbance; presence is only re-evaluated after one settles. Measured: awake-baby squirming 0.10–0.17, pickups 0.57–1.0 |
| `sleep_settle_seconds` | `10` | Quiet seconds that end a disturbance episode and trigger the settle evaluation |
| `sleep_probation_minutes` | `15` | After an ambiguous settle (reference says occupied), micro-motion must appear within this window or the crib is ruled empty |
| `sleep_min_minutes` | `10` | Stillness minutes before marking asleep. Brief sleep stirs (1–7 s clusters) do NOT reset the timer |
| `sleep_wake_seconds` | `20` | Window for the sustained-motion test: motion in ≥60% of its frames = genuinely awake (wakes a session / resets stillness) |
| `sleep_max_session_hours` | `14` | Force-end backstop for a stuck-open session |

**How it works (v5 — full rationale in `docs/sleep-detection-research.md`):** Presence is latched; it changes only at events. A parent-scale disturbance (motion ≥ `sleep_disturbance_fraction`) opens an episode; when it settles, the frame is compared to the empty-crib reference: match → AWAY + reference refreshed from the settled frame; differ → AWAKE on probation (micro-motion must confirm within `sleep_probation_minutes`, else AWAY + reference refreshed — this self-heals stale/poisoned references). While AWAY, repeated micro-motion flips to AWAKE without needing any reference. AWAKE ↔ ASLEEP uses stillness/motion timers with backdated start/end times; a disturbance during ASLEEP ends the session at the disturbance start. Lighting-change guard: frame diff > 80% (IR flip) skips the frame. Heartbeat written every frame; Flask shows "Camera offline" if stale > 60s.

**Tuning:** Measured thresholds from real footage live in `docs/sleep-detection-research.md` §6a — start there. For new footage: `bash record_camera.sh 120` on the Pi (needs `sudo apt install ffmpeg`; writes 10-min segments to `recordings/`), then anywhere with opencv: `python3 replay_sleep.py <segs>` prints motion/presence percentiles, and `python3 replay_sleep.py <segs> --simulate` drives the real `SleepStateMachine` over the footage and prints the state timeline + sessions to compare against ground truth. **The crib ROI must exclude the TAPO OSD timestamp** (top-left) — its per-second digit change reads as constant motion. The monitor re-reads settings on reconnect (or restart the service).

## API routes

| Method | Path | Description |
|--------|------|-------------|
| GET | `/` | Dashboard HTML (server-rendered initial counts) |
| GET | `/data` | JSON: counts, recent 50 entries, hourly/daily stats, next feed |
| POST | `/log` | Add event `{"type": "Wet\|Dirty\|Play\|Feed"}` |
| DELETE | `/log/today` | Remove all of today's entries |
| DELETE | `/log/entry` | Remove one entry `{"id": <int>}` |
| PATCH | `/log/entry` | Edit one entry `{"id": <int>, "type": "Wet\|Dirty\|Play\|Feed", "time": <ISO>}` |
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
