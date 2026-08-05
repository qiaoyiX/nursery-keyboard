#!/usr/bin/env python3
"""Neon schema migration: richer sleep sessions + the upsert key backup_sync needs.

Adds start_detected_at / end_detected_at / end_reason to sleep_sessions, and the
UNIQUE(start_time) constraint that backup_sync.py's ON CONFLICT clause targets.
Without the constraint the upsert raises; run this once before the first sync on
the new code.

Idempotent — safe to re-run. Run it with the backup-only credentials:

    sudo -E env $(sudo cat /etc/nursery-tracker/backup.env | xargs) \
        python3 migrate_sleep_schema.py

Duplicate start_time values would block the UNIQUE constraint. They should not
exist (the monitor opens one session at a time), but six-hourly DELETE+reinsert
history means it is worth checking rather than assuming, so this reports them and
stops instead of guessing which row to drop.
"""
import sys

from storage import USE_DB, db


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
        sys.exit("DATABASE_URL is not set (or psycopg2 missing) — nothing to migrate.")

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
