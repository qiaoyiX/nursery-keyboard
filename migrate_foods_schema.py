#!/usr/bin/env python3
"""Neon schema migration: the foods table behind the dashboard's food list.

Creates `foods` only — no existing table is altered, and log.json keeps its current
shape (a Solid event is an ordinary event; the food name lives only in this table).

Until this runs, backup_sync.py logs a warning and skips foods; everything else still
backs up. Idempotent — safe to re-run. Use the backup-only credentials:

    sudo -E env $(sudo cat /etc/nursery-tracker/backup.env | xargs) \
        python3 migrate_foods_schema.py
"""
import sys

from storage import USE_DB, db


def main():
    if not USE_DB:
        sys.exit("DATABASE_URL is not set (or psycopg2 missing) — nothing to migrate.")

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
