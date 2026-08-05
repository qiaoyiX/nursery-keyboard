#!/usr/bin/env python3
"""Rolling retention of crib-camera footage, so detector errors can be replayed.

sleep_score can say the crib monitor invented 97 minutes of sleep on a given day.
It cannot say which threshold would have prevented it — that needs replay_sleep
--simulate over the footage of that exact window, and no such footage exists:
record_camera.sh is a manual time-boxed capture, and the nanny pipeline keeps only
notable clips of the three nanny cameras, which do not include the crib cam.

So this keeps a short rolling window of crib-cam segments. It is sized in days
rather than gigabytes because the useful unit is "the day the scorer flagged".
10-minute stream-copy segments off the substream: no re-encoding, so the CPU cost
is a memcpy and the Pi's 1 fps detection loop is untouched.

    python3 crib_retention.py --prune          # delete segments past retention
    python3 crib_retention.py --status         # what's on disk, and disk headroom

Recording itself is the systemd unit (see install.sh); this module owns only the
sizing, the pruning, and the disk-headroom check that stops it filling the card.
"""
import argparse
import os
import shutil
import sys
from datetime import datetime, timedelta

CRIB_FOOTAGE_DIR = os.environ.get(
    "CRIB_FOOTAGE_DIR", os.path.join(os.path.dirname(__file__), "crib_footage"))
RETENTION_DAYS = int(os.environ.get("CRIB_FOOTAGE_RETENTION_DAYS", "3"))
# Stop recording rather than fill the card: the sleep monitor, the nanny pipeline
# and the JSON stores all share this disk, and losing those to a footage buffer
# would be a bad trade for a diagnostic.
MIN_FREE_GB = float(os.environ.get("CRIB_FOOTAGE_MIN_FREE_GB", "4"))


def segment_days():
    """{day_dir: (bytes, file_count)} for each YYYY-MM-DD dir of segments."""
    out = {}
    if not os.path.isdir(CRIB_FOOTAGE_DIR):
        return out
    for name in sorted(os.listdir(CRIB_FOOTAGE_DIR)):
        path = os.path.join(CRIB_FOOTAGE_DIR, name)
        if not os.path.isdir(path):
            continue
        total, count = 0, 0
        for f in os.listdir(path):
            if f.endswith(".mp4"):
                total += os.path.getsize(os.path.join(path, f))
                count += 1
        out[name] = (total, count)
    return out


def free_gb(path):
    try:
        return shutil.disk_usage(path).free / 1e9
    except OSError:
        return float("inf")


def prune(retention_days=RETENTION_DAYS, today=None):
    """Drop day directories older than the retention window. Returns names removed."""
    today = today or datetime.now().date()
    floor = today - timedelta(days=retention_days)
    removed = []
    for name in list(segment_days()):
        try:
            day = datetime.strptime(name, "%Y-%m-%d").date()
        except ValueError:
            # Not a day directory — never delete what we cannot identify.
            continue
        if day < floor:
            shutil.rmtree(os.path.join(CRIB_FOOTAGE_DIR, name), ignore_errors=True)
            removed.append(name)
    return removed


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--prune", action="store_true", help="delete segments past retention")
    ap.add_argument("--status", action="store_true", help="show what is on disk")
    args = ap.parse_args()

    if not args.prune and not args.status:
        ap.error("choose --prune or --status")

    if args.prune:
        removed = prune()
        print(f"Pruned {len(removed)} day(s) past {RETENTION_DAYS}d retention"
              + (": " + ", ".join(removed) if removed else ""))

    days = segment_days()
    total = sum(b for b, _ in days.values())
    print(f"\n{CRIB_FOOTAGE_DIR}  ({RETENTION_DAYS}d retention)")
    if not days:
        print("  (no footage yet)")
    for name, (b, n) in sorted(days.items()):
        print(f"  {name}  {n:4d} segments  {b/1e9:6.2f} GB")
    if days:
        print(f"  {'total':<12} {total/1e9:19.2f} GB")

    free = free_gb(CRIB_FOOTAGE_DIR if os.path.isdir(CRIB_FOOTAGE_DIR) else ".")
    print(f"\nDisk free: {free:.1f} GB (recording stops below {MIN_FREE_GB:.0f} GB)")
    if free < MIN_FREE_GB:
        print("WARNING: below the floor — recording should be stopped or retention cut.")
        sys.exit(1)


if __name__ == "__main__":
    main()
