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

Everything lives in two files:

**`app.py`** — Flask backend + keypad listener in one process.
- **Keypad thread**: `keypad_listener()` scans for all SayoDevice `/dev/input/event*` interfaces, spawns a `listen_one_interface()` thread per interface, grabs them exclusively, and calls `add_entry()` on key-down events. Runs as a daemon thread alongside Flask.
- **Storage layer**: dual-mode, selected at startup by `USE_DB = bool(DATABASE_URL and PSYCOPG2_AVAILABLE)`. `_pg_*` functions use Neon Postgres (psycopg2); `_json_*` functions use `log.json` on disk. Public API: `get_entries()`, `add_entry()`. All stat helpers (`today_stats`, `daily_stats`, `hourly_stats`, `next_feed_iso`) accept a plain list of `{"id", "type", "time"}` dicts and are storage-agnostic.
- **Settings**: always `settings.json` (local file, not backed up to Postgres). Stores `feed_interval_minutes`.

**`templates/index.html`** — Single-page dashboard. Pure HTML/CSS/JS, no build step.
- Polls `GET /data` every 8 seconds; `refresh()` updates counts, history, next-feed card, and all three Chart.js charts in one pass.
- Chart.js 4.5.1 loaded from CDN with SHA-384 SRI.
- Event type colors are defined as CSS custom properties in `:root` and must match `COLORS` in the JS `<script>` block.

## Key mapping

`KEYPAD_KEYS` in `app.py` maps evdev key names to event types:

| evdev key | Event type |
|-----------|-----------|
| KEY_SPACE  | Wet |
| KEY_PAGEUP | Dirty |
| KEY_DOWN   | Play |
| KEY_UP     | Feed |

To add or rename an event type, update `KEYPAD_KEYS`, the validation list in `log_event()`, the counts dicts in all three stat helpers, the `TYPES` array and `COLORS` object in `index.html`, and the CSS color rules (`.type-*`, `.btn-*`, `.count-card.*`).

## Storage backends

**JSON (default — no config needed)**
- Data lives in `log.json` at the repo root.
- `_json_delete_entry` uses list index as `id`; indices shift after any deletion, but the client always gets a fresh list from `/data` before showing delete buttons, so this is safe.

**Postgres (Neon)**
- Set `DATABASE_URL=postgresql://...` in the systemd unit (`sudo systemctl edit nursery-tracker`, add `Environment=DATABASE_URL=...` under `[Service]`).
- Required schema:
  ```sql
  CREATE TABLE events (id BIGSERIAL PRIMARY KEY, type VARCHAR(10) NOT NULL, time TIMESTAMP NOT NULL);
  CREATE INDEX ON events (time);
  ```
- `migrate_log.py` imports an existing `log.json` into Postgres (idempotent).
- `db()` context manager: commits on clean exit, rolls back on exception, always closes.

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
