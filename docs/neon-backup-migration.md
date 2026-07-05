# Migration: Postgres-primary → local-first with Neon snapshot backup

One-time switchover on the Pi, introduced 2026-07-04 (commit `ff628d8`). Why: the dashboard's 8 s
poll kept Neon's free-tier compute awake 24/7 (~180 compute-hours/month; the ~5 min autosuspend is
not tunable on the free plan). After this migration the services touch only local JSON files and a
systemd timer snapshots them to Neon every 6 hours (~2–3 compute-hours/month). Details in
`CLAUDE.md` → "Storage: local-first, Neon as snapshot backup".

---

## Step 0 — Find your DATABASE_URL

You need it twice below. Two places to get it:

**Easiest — it's already on the Pi.** The current services were configured with it:

```bash
systemctl cat nursery-tracker | grep DATABASE_URL
# → Environment=DATABASE_URL=postgresql://user:password@ep-xxx-pooler.us-east-2.aws.neon.tech/neondb?sslmode=require
```

Copy everything after `Environment=DATABASE_URL=`. (If nothing prints, check the same for
`nursery-sleep-monitor`, or look in `/etc/systemd/system/nursery-tracker.service.d/override.conf`.)

**Canonical — the Neon console.** [console.neon.tech](https://console.neon.tech) → your project →
**Connect** button (top right of the project dashboard) → connection string. Pick the **pooled**
variant (host contains `-pooler`) and make sure `?sslmode=require` is on the end. If you've lost
the password, the same dialog has a **Reset password** option — do that *before* the migration,
since the old URL stops working the moment you reset.

---

## Migration steps (order matters — don't reorder)

```bash
cd ~/nursery-keyboard        # or wherever the repo lives
git pull

# 1. Stop both services so no keypress lands between export and switchover
sudo systemctl stop nursery-tracker nursery-sleep-monitor

# 2. Export current data out of Neon into the local JSON files
DATABASE_URL='postgresql://...' venv/bin/python export_db.py
```

`export_db.py` prints the exported row counts. **Verify they look right** (roughly: your total
events and sleep sessions to date). It refuses to overwrite non-empty local files — if it refuses,
the Pi already has local data; decide which side is truth before continuing (`--force` makes the
Neon side win).

```bash
# 3. Remove DATABASE_URL from BOTH service units
sudo systemctl edit nursery-tracker
#    → delete the line:  Environment=DATABASE_URL=...   then save/exit
sudo systemctl edit nursery-sleep-monitor
#    → same

# 4. Install the backup timer + env file scaffolding (also re-installs the services; harmless)
bash install.sh

# 5. Put the URL where ONLY the backup job reads it
sudo nano /etc/nursery-tracker/backup.env
#    → uncomment and complete:  DATABASE_URL=postgresql://...
#    (file is chmod 600 root — credentials live nowhere else anymore)

# 6. Start the services (now in local-JSON mode)
sudo systemctl start nursery-tracker nursery-sleep-monitor

# 7. Run one backup sync manually and confirm it worked
sudo systemctl start nursery-backup.service
journalctl -u nursery-backup -n 5
#    → expect: "Backup snapshot complete: N events, M sleep sessions -> Neon"
cat last_sync.json
```

## Verify

- Dashboard at `http://raspberrypi.local:8080` shows the same history/counts as before the
  migration (now served from `log.json`).
- A test keypress appears instantly (no Neon cold-start lag).
- `systemctl list-timers nursery-backup.timer` shows the next run (every 6 h, minute 20).
- In the Neon console, **Monitoring → Compute**: activity should collapse to ~4 brief episodes/day.
  Check the compute-hours meter again after 2–3 days — it should be nearly flat.

## Rollback / disaster recovery

- **Rollback** (return to Postgres-primary): re-add `Environment=DATABASE_URL=...` to both units
  via `sudo systemctl edit`, restart both services, and disable the timer
  (`sudo systemctl disable --now nursery-backup.timer`). Run one `backup_sync.py` first so Neon is
  current before it becomes primary again.
- **SD card died / fresh Pi**: clone the repo, run `bash install.sh`, put the URL in
  `/etc/nursery-tracker/backup.env`, then restore the data:
  `sudo systemctl stop nursery-tracker nursery-sleep-monitor &&
  DATABASE_URL='postgresql://...' venv/bin/python export_db.py --force` and start the services.
  You lose at most the events since the last 6-hourly sync.
