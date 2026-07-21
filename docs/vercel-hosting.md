# Hosting the dashboard on Vercel — feasibility & risk analysis

**Status: analysis only, not implemented — and now stale relative to a later architecture
decision.** This documents what hosting on Vercel would actually require and everything that
could go wrong, so the decision is made with eyes open. It was written 2026-06-19, before the
2026-07-04 storage migration (`ff628d8`, see `neon-backup-migration.md`) made storage
**local-first**: `nursery-tracker`/`nursery-sleep-monitor` now must run with `DATABASE_URL`
unset, and Postgres is written to only by a 6-hourly batch backup job. Checklist item 1 below
("Force the Postgres backend... JSON fallback must never be the active path") directly
contradicts that decision — `CLAUDE.md` now explicitly warns against ever setting `DATABASE_URL`
on the live services, since it wakes Neon's free-tier compute on every 8-second dashboard poll.
**This plan would need rework before it's actionable again**, not just a revisit — treat it as
historical analysis of the tradeoffs, not a ready-to-execute path.

## TL;DR

Vercel can host **only the dashboard web UI**, and only after migrating *all* state to the
shared Neon Postgres and adding authentication. The **keypad listener and the camera sleep
monitor must stay on the Raspberry Pi** — they are bound to physical hardware (USB HID, local
RTSP camera) and need a long-running process, neither of which exists on Vercel's stateless
serverless platform. This is a re-architecture, not a deployment.

## Why the current app can't "just deploy" to Vercel

Vercel runs **stateless, ephemeral, short-lived serverless functions**. The app violates that in
four fundamental ways:

| App feature | Needs | Vercel reality |
|-------------|-------|----------------|
| Keypad listener (`keypad_listener` thread reading `/dev/input/event*` via evdev) | Physical USB device + a persistent daemon thread | No USB, no evdev, no long-lived process. **Impossible on Vercel.** |
| Sleep monitor (`sleep_monitor.py`, ~1 fps RTSP from a TAPO camera on the home LAN) | Continuous background process + LAN access to the camera | No background workers; can't reach a device on your home network. **Impossible on Vercel.** |
| Local-file storage (`log.json`, `settings.json`, `reference_frame.npy`, `sleep_state.json`) | A writable, persistent filesystem | Vercel's filesystem is read-only/ephemeral; writes vanish between invocations. |
| Inter-process file signals (`calibrate.flag`, `sleep_state.json` heartbeat) | A filesystem shared between the web app and the Pi daemons | No shared filesystem; serverless invocations share nothing. |

## The only viable shape: a split architecture

```
   Raspberry Pi (stays as-is)                 Vercel (new, UI only)
   ┌─────────────────────────┐                ┌──────────────────────┐
   │ nursery-tracker          │                │ Flask dashboard as a  │
   │   • keypad listener  ────┼──┐         ┌───┼─ serverless function  │
   │ nursery-sleep-monitor    │  │         │   │   (read/write events) │
   │   • camera detection ────┼──┤         │   └──────────────────────┘
   └─────────────────────────┘  │         │
                                 ▼         ▼
                        ┌────────────────────────────┐
                        │  Neon Postgres (shared)     │
                        │  events / sleep_sessions /  │
                        │  heartbeat / calibrate sig  │
                        └────────────────────────────┘
```

The Pi keeps doing all hardware work and writes to Neon. Vercel serves the UI and reads/writes
the same Neon DB. This is plausible **because `storage.py` already supports Postgres** — but only
events and sleep sessions live in the DB today; several things are still local files (below).

## What must change before it would work (checklist)

1. **Force the Postgres backend on Vercel.** Set `DATABASE_URL` (Neon) as a Vercel env var.
   `USE_DB` already keys off it. JSON fallback must never be the active path on Vercel.
2. **Move heartbeat + sleep status into the DB.** `read_sleep_status()` / `write_sleep_heartbeat()`
   use the local `sleep_state.json`. With the daemon on the Pi and the UI on Vercel, that file
   isn't shared — Vercel would always show "Camera offline." Needs a DB-backed heartbeat row.
3. **Move the calibrate signal into the DB.** `POST /sleep/calibrate` writes `calibrate.flag` for
   the Pi daemon to consume. On Vercel that flag never reaches the Pi. Needs a DB flag the Pi polls.
4. **Decide where `settings.json` lives.** It's currently local and per-machine. Either keep
   settings Pi-only (Vercel UI can't edit them) or move to a DB table (then both can read/write).
5. **Add authentication.** All routes are currently unauthenticated. On a public Vercel URL,
   anyone could read the baby's data, log fake events, or delete history. This is a hard blocker
   for public hosting — needs at least a shared password / token, ideally real auth.
6. **Package as a Vercel Python function.** Add `vercel.json` + an `api/` entrypoint exposing the
   Flask WSGI app; `app.run()`/`__main__` is ignored by Vercel. Guard the keypad-thread startup so
   it never attempts to run in the serverless context.
7. **Use Neon's pooled connection string.** `db()` opens a fresh `psycopg2.connect()` per call.
   Serverless cold-starts open a new connection every invocation and can exhaust Neon's
   connection limit. Use the Neon **pooler** endpoint (PgBouncer) or the serverless driver.
8. **Fix timezone handling.** Stat helpers use naive `datetime.now()`. Vercel functions run in
   **UTC**; the Pi runs in local time. "Today"/hourly buckets would disagree between the two,
   bucketing events into the wrong day. Pin an explicit timezone everywhere.

## What could go wrong (the failure modes to expect)

- **Split-brain "today" boundaries.** UTC on Vercel vs local time on the Pi → the dashboard's
  daily counts, "next feed," and hourly chart silently disagree with reality around midnight.
  (Most likely subtle bug; fix tz before anything else.)
- **Neon connection exhaustion.** The 8s dashboard poll × every open browser tab × per-invocation
  connections can blow through Neon's limit, causing intermittent 500s that are hard to reproduce.
- **Cost / rate from chatty polling.** `refresh()` hits `/data` every 8 seconds. On serverless
  that's ~10,800 invocations/day per open tab, each a cold-or-warm function call — usage and
  latency implications. Consider increasing the poll interval or moving to SSE/websockets (the
  latter doesn't fit Vercel functions well either).
- **Security exposure.** A public, unauthenticated URL with `DELETE /log/today` and `/log/entry`
  is a real data-loss/abuse risk. Search engines/bots will find it.
- **Heartbeat staleness false alarms.** If the DB-backed heartbeat isn't written/read carefully
  (clock skew, write latency), the UI flaps between "asleep" and "Camera offline."
- **Calibrate button becomes a no-op** until the DB-signal rework lands — users press it, nothing
  happens on the Pi, trust erodes.
- **Secrets sprawl.** `DATABASE_URL` and Huckleberry credentials now live in Vercel env vars *and*
  systemd units — two places to rotate, easy to leave one stale.
- **Cold-start latency** on the first request after idle — a few seconds of blank dashboard.
- **Dependency surprises.** `opencv-python-headless`, `numpy`, `evdev` are in `requirements.txt`;
  Vercel would try to install them and either bloat the bundle or fail (evdev needs Linux headers;
  it's useless on Vercel anyway). The Vercel deployment needs a **trimmed** requirements set
  (flask + psycopg2 only), separate from the Pi's.

## Recommendation

If the goal is **remote access to the dashboard**, hosting the full app on Vercel is the wrong
tool — too much of it is hardware-bound and stateful. Cheaper, lower-risk alternatives that avoid
every issue above:

1. **Tailscale / WireGuard VPN** to the Pi — reach `http://<pi>:8080` from anywhere, zero
   re-architecture, stays private. **Recommended.**
2. **Cloudflare Tunnel** (`cloudflared`) — exposes the Pi's port over HTTPS with optional
   Cloudflare Access auth; no code changes, just add auth at the edge.

If the goal specifically is **a Vercel-hosted UI** (e.g. for a polished public URL), treat it as
the split-architecture project above: do items 1–8 first, with **authentication and timezone**
as non-negotiable prerequisites, and keep all hardware work on the Pi.
