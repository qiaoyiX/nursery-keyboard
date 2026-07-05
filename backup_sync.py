"""
Snapshot-sync the local JSON store (primary) up to Neon Postgres (backup).

Local-first architecture (see CLAUDE.md "Storage backends"): the services read and
write only the local JSON files; Neon exists solely to survive SD-card death. This
script mirrors the local files to Neon as a full snapshot in ONE transaction:

    DELETE FROM events;         INSERT <all of log.json>;
    DELETE FROM sleep_sessions; INSERT <all of sleep_sessions.json>;

Why snapshot, not incremental: it is idempotent by construction (a crashed run rolls
back to the previous consistent snapshot), and dashboard edits/deletes/clear-today
need no tombstones or stable keys. At this data volume (<10k rows) it is sub-second.

Safety guard: since this TRUNCATEs the backup, it refuses to run if the local files
look damaged (missing/unparseable log.json, or local row count collapsed below half
of what Neon holds). A broken Pi must never be replicated over a good backup.

Scheduling: a systemd timer runs this every 6 hours (installed by install.sh); each
run wakes Neon for seconds (+ its fixed ~5 min idle-before-suspend), so backup costs
~2-3 compute-hours/month instead of the ~180 that live reads were burning.

Usage (needs DATABASE_URL — the timer unit provides it via EnvironmentFile):
    DATABASE_URL=postgresql://... python3 backup_sync.py

Writes last_sync.json (timestamp + row counts) on success. Exits nonzero on any
failure; local data is never touched by this script.
"""

import json
import logging
import os
import sys
from datetime import datetime

from storage import DATA_FILE, SLEEP_FILE, USE_DB, db

LAST_SYNC_FILE = os.path.join(os.path.dirname(__file__), "last_sync.json")

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s [backup_sync] %(message)s")


def load_local(path, required):
    if not os.path.exists(path):
        if required:
            sys.exit(f"ABORT: {os.path.basename(path)} is missing — refusing to overwrite "
                     "the Neon backup with nothing.")
        return []
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        sys.exit(f"ABORT: {os.path.basename(path)} is unreadable ({e}) — refusing to sync.")


def guard_shrinkage(kind, local_n, remote_n, floor):
    """A collapsed local count means local damage, not history — protect the backup."""
    if remote_n > floor and local_n < remote_n * 0.5:
        sys.exit(f"ABORT: local {kind} count ({local_n}) is less than half of the Neon "
                 f"backup ({remote_n}). This looks like local data damage — refusing to "
                 "replicate it. Investigate, or restore with export_db.py --force.")


def main():
    if not USE_DB:
        # Not configured = backup intentionally disabled; exit clean so the
        # systemd timer shows skipped work, not a failing unit.
        logging.info("DATABASE_URL not set — off-site backup disabled, nothing to do.")
        return

    events   = load_local(DATA_FILE, required=True)
    sessions = load_local(SLEEP_FILE, required=False)

    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM events")
            remote_events = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM sleep_sessions")
            remote_sessions = cur.fetchone()[0]

            guard_shrinkage("event", len(events), remote_events, floor=20)
            guard_shrinkage("sleep-session", len(sessions), remote_sessions, floor=10)

            # One transaction: db() commits on clean exit, rolls back on exception,
            # so Neon always holds a complete snapshot — old or new, never partial.
            cur.execute("DELETE FROM events")
            cur.executemany(
                "INSERT INTO events (type, time) VALUES (%s, %s)",
                [(e["type"], e["time"]) for e in events],
            )
            cur.execute("DELETE FROM sleep_sessions")
            cur.executemany(
                """INSERT INTO sleep_sessions (start_time, end_time, duration_minutes)
                   VALUES (%s, %s, %s)""",
                [(s["start_time"], s["end_time"], s["duration_minutes"]) for s in sessions],
            )

    stamp = {"synced_at": datetime.now().isoformat(),
             "events": len(events), "sleep_sessions": len(sessions)}
    tmp = LAST_SYNC_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(stamp, f)
    os.replace(tmp, LAST_SYNC_FILE)

    logging.info("Backup snapshot complete: %d events, %d sleep sessions -> Neon",
                 len(events), len(sessions))


if __name__ == "__main__":
    main()
