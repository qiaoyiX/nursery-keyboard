"""
Export Neon Postgres → local JSON files (the reverse of the old migrate_log.py).

Two jobs:
  1. One-time switchover to local-first storage: pull everything out of Neon into
     log.json / sleep_sessions.json, after which the services run without DATABASE_URL.
  2. Disaster recovery: restore local files from the Neon backup after an SD-card death.

Usage (needs DATABASE_URL in the environment):
    DATABASE_URL=postgresql://... python3 export_db.py [--force]

Refuses to overwrite non-empty local files unless --force is given — the local files
are the primary store after switchover, and this script must never eat them by accident.

Run with both services STOPPED so no keypress lands between export and switchover:
    sudo systemctl stop nursery-tracker nursery-sleep-monitor
"""

import argparse
import json
import os
import sys

from storage import DATA_FILE, FOODS_FILE, SLEEP_FILE, USE_DB, db

REPORTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "nanny", "reports")


def _refuse_overwrite(path, force):
    if force or not os.path.exists(path):
        return
    try:
        with open(path) as f:
            existing = json.load(f)
    except Exception:
        return  # corrupt/unreadable — overwriting is a rescue, not a loss
    if existing:
        sys.exit(f"REFUSING: {os.path.basename(path)} already has {len(existing)} entries. "
                 "These local files are the primary store — rerun with --force only if you "
                 "really mean to replace them with the Neon backup.")


def _atomic_write(path, payload):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(payload, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def main():
    ap = argparse.ArgumentParser(description="Export Neon Postgres to local JSON files")
    ap.add_argument("--force", action="store_true",
                    help="overwrite non-empty local files (restore mode)")
    args = ap.parse_args()

    if not USE_DB:
        sys.exit("DATABASE_URL is not set (or psycopg2 missing) — nothing to export from.")

    _refuse_overwrite(DATA_FILE, args.force)
    _refuse_overwrite(SLEEP_FILE, args.force)
    _refuse_overwrite(FOODS_FILE, args.force)

    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT type, time FROM events ORDER BY time")
            events = [{"type": t, "time": ts.isoformat()} for t, ts in cur.fetchall()]

            cur.execute("""SELECT start_time, end_time, duration_minutes,
                                  start_detected_at, end_detected_at, end_reason
                           FROM sleep_sessions ORDER BY start_time""")
            sessions = [{
                "id":                i,
                "start_time":        start.isoformat(),
                "end_time":          end.isoformat() if end is not None else None,
                "duration_minutes":  float(dur) if dur is not None else None,
                "start_detected_at": sdet.isoformat() if sdet is not None else None,
                "end_detected_at":   edet.isoformat() if edet is not None else None,
                "end_reason":        reason,
            } for i, (start, end, dur, sdet, edet, reason) in enumerate(cur.fetchall())]

            foods = []
            try:
                cur.execute("""SELECT name, first_tried, last_offered, times_offered,
                                      reaction FROM foods ORDER BY last_offered""")
                foods = [{
                    "name": n,
                    "first_tried": ft.isoformat() if ft else None,
                    "last_offered": lo.isoformat() if lo else None,
                    "times_offered": int(times or 1),
                    "reaction": rx,
                } for n, ft, lo, times, rx in cur.fetchall()]
            except Exception:
                # Table only exists once migrate_foods_schema.py has run; its
                # absence is not a failed restore.
                conn.rollback()
                print("No foods table in Neon — skipping the food list.")

            reports = []
            try:
                cur.execute("SELECT report_date, report FROM nanny_reports "
                            "ORDER BY report_date")
                reports = cur.fetchall()
            except Exception:
                # The table only exists once the nanny feature has been backed
                # up at least once; its absence is not a failed restore.
                conn.rollback()
                print("No nanny_reports table in Neon — skipping nanny reports.")

    _atomic_write(DATA_FILE, events)
    _atomic_write(SLEEP_FILE, sessions)
    if foods:
        _atomic_write(FOODS_FILE, foods)

    written = 0
    if reports:
        os.makedirs(REPORTS_DIR, exist_ok=True)
        for day, payload in reports:
            path = os.path.join(REPORTS_DIR, f"{day.isoformat()}.json")
            if os.path.exists(path) and not args.force:
                continue          # local wins without --force, same as above
            _atomic_write(path, payload if isinstance(payload, (dict, list))
                          else json.loads(payload))
            written += 1

    print(f"Exported {len(events)} events -> {DATA_FILE}")
    print(f"Exported {len(sessions)} sleep sessions -> {SLEEP_FILE}")
    if foods:
        print(f"Exported {len(foods)} foods -> {FOODS_FILE}")
    if reports:
        print(f"Exported {written} of {len(reports)} nanny reports -> {REPORTS_DIR}")
    print("Verify these counts against Neon, then remove DATABASE_URL from both service units.")


if __name__ == "__main__":
    main()
