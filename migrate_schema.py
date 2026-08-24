#!/usr/bin/env python3
"""Bring the Neon backup schema up to date. One command, safe to re-run.

Replaces the per-feature migration scripts. Everything here is idempotent
(CREATE TABLE IF NOT EXISTS / ADD COLUMN IF NOT EXISTS), so running it when some
or all of it has already been applied is a no-op:

    sudo venv/bin/python migrate_schema.py

Use the project venv, not system python3: the services run venv/bin/python and
that is where psycopg2 lives (Raspberry Pi OS refuses system-wide pip, PEP 668).
DATABASE_URL is read from /etc/nursery-tracker/backup.env, which is chmod 600 root
— hence sudo — or from the environment if you already have it set. Point it
elsewhere with NURSERY_ENV_FILE=/path/to/file.

Until this runs, backup_sync.py logs a warning and skips whichever tables are
missing; events and sleep sessions still back up.
"""
import os
import sys

from envfile import load_env_file

# Loaded before importing storage, which decides USE_DB from DATABASE_URL at import
# time — doing it afterwards would have no effect. Hence the deferred import below.
ENV_FILE = os.environ.get("NURSERY_ENV_FILE", "/etc/nursery-tracker/backup.env")
load_env_file(ENV_FILE)

from storage import DATABASE_URL, USE_DB, db  # noqa: E402


COLUMNS = [
    ("sleep_sessions", "start_detected_at", "TIMESTAMP"),
    ("sleep_sessions", "end_detected_at",   "TIMESTAMP"),
    ("sleep_sessions", "end_reason",        "TEXT"),
]

TABLES = [
    # Within-nap detail. session_start references sleep_sessions.start_time by
    # value rather than by FK: backup_sync upserts the tables independently and
    # an FK would make ordering matter.
    ("sleep_events", """CREATE TABLE IF NOT EXISTS sleep_events (
                            id            BIGSERIAL PRIMARY KEY,
                            session_start TIMESTAMP,
                            at            TIMESTAMP NOT NULL,
                            kind          TEXT NOT NULL,
                            duration_s    NUMERIC(10,1),
                            settled_back  BOOLEAN,
                            UNIQUE (session_start, at, kind))"""),
    # name is the natural key, matching add_food()'s case-insensitive dedupe.
    # This list is the allergy record and must never shrink.
    ("foods", """CREATE TABLE IF NOT EXISTS foods (
                     id            BIGSERIAL PRIMARY KEY,
                     name          TEXT NOT NULL UNIQUE,
                     first_tried   TIMESTAMP,
                     last_offered  TIMESTAMP,
                     times_offered INTEGER NOT NULL DEFAULT 1,
                     reaction      TEXT)"""),
    # Parent verdicts on detected sleep, plus sleep the detector missed, in one
    # table: kind='verdict' (ref = session start_time, value = the verdict) or
    # kind='missed' (ref/ends = the interval). These rows are hand-made and the
    # most expensive thing on the Pi to recreate.
    ("sleep_truth", """CREATE TABLE IF NOT EXISTS sleep_truth (
                           id         BIGSERIAL PRIMARY KEY,
                           kind       TEXT NOT NULL,
                           ref        TEXT NOT NULL,
                           ends       TEXT,
                           value      TEXT,
                           labeled_at TIMESTAMP,
                           UNIQUE (kind, ref))"""),
]


def main():
    if not USE_DB:
        # Two very different causes, and guessing between them wastes real time
        # when the fix is a one-liner either way.
        if not DATABASE_URL:
            sys.exit(f"No DATABASE_URL found in {ENV_FILE} or the environment.\n"
                     "The file is chmod 600 root — run this with sudo.")
        venv_py = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "venv", "bin", "python")
        hint = (f"    sudo {venv_py} {os.path.basename(__file__)}"
                if os.path.exists(venv_py) else
                "    (no venv found — re-run install.sh)")
        sys.exit(f"psycopg2 is not installed for {sys.executable}.\n"
                 "The services use the project venv, which already has it:\n" + hint)

    with db() as conn:
        with conn.cursor() as cur:
            for table, ddl in TABLES:
                cur.execute(ddl)
                print(f"table  {table}: ok")

            for table, column, coltype in COLUMNS:
                cur.execute(f"ALTER TABLE {table} "
                            f"ADD COLUMN IF NOT EXISTS {column} {coltype}")
                print(f"column {table}.{column}: ok")

            # Duplicate start_time values would block the UNIQUE constraint that
            # backup_sync's sleep upsert targets. They should not exist (the monitor
            # opens one session at a time), but the old six-hourly DELETE+reinsert
            # history makes it worth checking rather than assuming.
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

    print("\nSchema up to date. backup_sync.py can sync everything.")


if __name__ == "__main__":
    main()
