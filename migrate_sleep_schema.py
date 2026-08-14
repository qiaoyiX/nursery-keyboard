#!/usr/bin/env python3
"""Neon schema migration: richer sleep sessions + the upsert key backup_sync needs.

Adds start_detected_at / end_detected_at / end_reason to sleep_sessions, and the
UNIQUE(start_time) constraint that backup_sync.py's ON CONFLICT clause targets.
Without the constraint the upsert raises; run this once before the first sync on
the new code.

Idempotent — safe to re-run:

    sudo python3 migrate_sleep_schema.py

It reads DATABASE_URL from /etc/nursery-tracker/backup.env itself (hence sudo — the
file is chmod 600 root), or from the environment if you already have it set. Point it
elsewhere with NURSERY_ENV_FILE=/path/to/file.

Duplicate start_time values would block the UNIQUE constraint. They should not
exist (the monitor opens one session at a time), but six-hourly DELETE+reinsert
history means it is worth checking rather than assuming, so this reports them and
stops instead of guessing which row to drop.
"""
import os
import sys

from envfile import load_env_file

# Loaded before importing storage, which decides USE_DB from DATABASE_URL at import
# time — doing it afterwards would have no effect. Hence the deferred import below.
ENV_FILE = os.environ.get("NURSERY_ENV_FILE", "/etc/nursery-tracker/backup.env")
load_env_file(ENV_FILE)

from storage import DATABASE_URL, USE_DB, db  # noqa: E402


DDL = [
    ("start_detected_at",
     "ALTER TABLE sleep_sessions ADD COLUMN IF NOT EXISTS start_detected_at TIMESTAMP"),
    ("end_detected_at",
     "ALTER TABLE sleep_sessions ADD COLUMN IF NOT EXISTS end_detected_at TIMESTAMP"),
    ("end_reason",
     "ALTER TABLE sleep_sessions ADD COLUMN IF NOT EXISTS end_reason TEXT"),
]


def main():
    if not USE_DB:
        # Two very different causes, and guessing between them wastes real time
        # when the fix is a one-liner either way.
        if not DATABASE_URL:
            sys.exit(f"No DATABASE_URL found in {ENV_FILE} or the environment.\n"
                     "The file is chmod 600 root — run this with sudo.")
        sys.exit("psycopg2 is not installed — pip install -r requirements.txt")

    with db() as conn:
        with conn.cursor() as cur:
            for name, ddl in DDL:
                cur.execute(ddl)
                print(f"column {name}: ok")

            # Within-nap detail. session_start references sleep_sessions.start_time
            # by value rather than by FK: backup_sync upserts the two tables
            # independently and an FK would make ordering matter.
            cur.execute("""CREATE TABLE IF NOT EXISTS sleep_events (
                               id            BIGSERIAL PRIMARY KEY,
                               session_start TIMESTAMP,
                               at            TIMESTAMP NOT NULL,
                               kind          TEXT NOT NULL,
                               duration_s    NUMERIC(10,1),
                               settled_back  BOOLEAN,
                               UNIQUE (session_start, at, kind))""")
            print("table sleep_events: ok")

            cur.execute("""SELECT start_time, count(*) FROM sleep_sessions
                           GROUP BY start_time HAVING count(*) > 1
                           ORDER BY start_time""")
            dupes = cur.fetchall()
            if dupes:
                print("\nABORT: duplicate start_time values block UNIQUE(start_time):")
                for start, n in dupes:
                    print(f"  {start}  ×{n}")
                sys.exit("\nResolve these first, then re-run.")

            # Constraints have no IF NOT EXISTS; check the catalog instead.
            cur.execute("""SELECT 1 FROM pg_constraint
                           WHERE conname = 'sleep_sessions_start_time_key'""")
            if cur.fetchone():
                print("constraint sleep_sessions_start_time_key: already present")
            else:
                cur.execute("""ALTER TABLE sleep_sessions
                               ADD CONSTRAINT sleep_sessions_start_time_key
                               UNIQUE (start_time)""")
                print("constraint sleep_sessions_start_time_key: created")

    print("\nMigration complete. backup_sync.py can now upsert.")


if __name__ == "__main__":
    main()
