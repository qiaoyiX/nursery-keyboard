"""One-time migration: import log.json into Neon Postgres.

Usage (run on the Pi with DATABASE_URL set):
    python migrate_log.py [path/to/log.json]

Defaults to log.json in the same directory as this script.
Skips rows that already exist (matches on exact timestamp).
"""
import json
import os
import sys
from datetime import datetime

import psycopg2

DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    print("ERROR: DATABASE_URL environment variable not set")
    sys.exit(1)

log_file = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(__file__), "log.json")

with open(log_file) as f:
    entries = json.load(f)

conn = psycopg2.connect(DATABASE_URL)
cur = conn.cursor()

inserted = skipped = 0
for e in entries:
    try:
        dt = datetime.fromisoformat(e["time"])
    except (ValueError, KeyError):
        print(f"  skip malformed entry: {e}")
        skipped += 1
        continue

    cur.execute("SELECT 1 FROM events WHERE time = %s AND type = %s", (dt, e["type"]))
    if cur.fetchone():
        skipped += 1
        continue

    cur.execute("INSERT INTO events (type, time) VALUES (%s, %s)", (e["type"], dt))
    inserted += 1

conn.commit()
cur.close()
conn.close()

print(f"Done — inserted {inserted}, skipped {skipped} (already existed or malformed)")
