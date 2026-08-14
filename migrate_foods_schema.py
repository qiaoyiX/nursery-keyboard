#!/usr/bin/env python3
"""Neon schema migration: the foods table behind the dashboard's food list.

Creates `foods` only — no existing table is altered, and log.json keeps its current
shape (a Solid event is an ordinary event; the food name lives only in this table).

Until this runs, backup_sync.py logs a warning and skips foods; everything else still
backs up. Idempotent — safe to re-run:

    sudo python3 migrate_foods_schema.py

It reads DATABASE_URL from /etc/nursery-tracker/backup.env itself (hence sudo — the
file is chmod 600 root), or from the environment if you already have it set. Point it
elsewhere with NURSERY_ENV_FILE=/path/to/file.
"""
import os
import sys

from envfile import load_env_file

# Loaded before importing storage, which decides USE_DB from DATABASE_URL at import
# time — doing it afterwards would have no effect. Hence the deferred import below.
ENV_FILE = os.environ.get("NURSERY_ENV_FILE", "/etc/nursery-tracker/backup.env")
load_env_file(ENV_FILE)

from storage import DATABASE_URL, USE_DB, db  # noqa: E402


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
            # name is the natural key, matching add_food()'s case-insensitive
            # dedupe, so the 6-hourly sync can upsert instead of DELETE+reinsert.
            # This list is the allergy record and must never shrink.
            cur.execute("""CREATE TABLE IF NOT EXISTS foods (
                               id            BIGSERIAL PRIMARY KEY,
                               name          TEXT NOT NULL UNIQUE,
                               first_tried   TIMESTAMP,
                               last_offered  TIMESTAMP,
                               times_offered INTEGER NOT NULL DEFAULT 1,
                               reaction      TEXT)""")
            print("table foods: ok")

    print("\nMigration complete. backup_sync.py can now sync foods.")


if __name__ == "__main__":
    main()
