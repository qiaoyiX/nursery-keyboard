#!/usr/bin/env python3
"""Score the crib monitor against the cameras, and say which failure mode is to blame.

The nanny report already publishes a crib/camera cross-check (`sleep_metrics()`), and
it is damning: on 2026-07-30 the two sources agreed for 1.1 minutes out of ~100, and
97-101 min/day of "crib-only" sleep appeared on 07-30 and 08-04. But `crib_only` is
not usable as an error signal on its own, for two reasons this module fixes:

  1. It is whole-house. The crib monitor watches ONE crib in ONE room; a bedroom nap
     it structurally cannot see is not a detector error, and counting it as one would
     tune the thresholds toward garbage.
  2. It conflates "a camera watched the same room and saw an AWAKE baby" with "no
     camera scored anything either way". Only the first is evidence of invented sleep.

So the buckets here are:

    agreement    crib asleep  ∩ camera asleep (same room)   — both agree
    contradicted crib asleep  ∩ camera AWAKE  (same room)   — hard false-sleep evidence
    unconfirmed  crib asleep, room observed, no camera verdict — weak, reported separately
    missed       camera asleep ∩ ¬crib, in-room             — detector missed real sleep

`contradicted` is the number to tune against. Each contradicted minute is attributed
to the end_reason of the session that produced it, so a sweep can tell "the detector
invents sleep and only the liveness backstop ever catches it" from "the detector is
fine but wake detection is late".

Scope limits, which the output states rather than hides:
  * Camera truth is a vision model's reading, not ground truth. It is a second opinion
    that happens to be independent of the motion detector, which is what makes the
    disagreement informative — not a label to trust absolutely.
  * Coverage is the nanny window (~8 h), so nights are unscored. Most sleep is at night.
  * Sessions predating the end_reason field (everything before 2026-08-05) attribute to
    "unknown" — expected, and the reason backfill is impossible for them.

Run on the Pi (needs chunks + sleep_sessions.json):

    python3 sleep_score.py --date 2026-08-04
    python3 sleep_score.py --days 14 --json
    python3 sleep_score.py --date 2026-08-04 --verify   # reproduce the report's own numbers
"""
import argparse
import json
import os
import sys
from datetime import date, datetime, timedelta

from nanny_common import CHUNKS_DIR, REPORTS_DIR, load_camera_rooms, load_window
from nanny_report import (
    baby_state_by_room,
    chunk_room,
    intersect_intervals,
    load_chunks,
    subtract_intervals,
    total_minutes,
    union_intervals,
    window_bounds,
)

# The room the crib monitor can actually see. Everything outside it is out of scope
# for scoring: the bedroom has no crib monitor, so a bedroom nap is a known blind
# spot, not a miss. Override if the crib camera moves rooms.
CRIB_ROOM = os.environ.get("SLEEP_MONITOR_ROOM", "nursery")


def crib_sessions_for(day):
    """Closed sleep sessions clipped to `day`, keeping end_reason for attribution.

    nanny_report.nap_windows_for() does the same clipping but returns bare intervals;
    the reason is exactly what this module needs, so it is re-derived here rather than
    widening that function's contract for one caller.
    """
    import storage

    days_back = (date.today() - day).days + 1
    day_start = datetime.combine(day, datetime.min.time())
    day_end = day_start + timedelta(days=1)
    out = []
    for s in storage.get_sleep_sessions_range(max(days_back, 1)):
        if not s.get("end_time"):
            continue
        try:
            ws = datetime.fromisoformat(str(s["start_time"]))
            we = datetime.fromisoformat(str(s["end_time"]))
        except ValueError:
            continue
        lo, hi = max(ws, day_start), min(we, day_end)
        if hi > lo:
            out.append({"start": lo, "end": hi,
                        "reason": s.get("end_reason") or "unknown",
                        "id": s.get("id")})
    return out


def rooms_observed(chunks, rooms):
    """{room: unioned intervals a camera in that room was actually analyzed}.

    Without this, a gap in recording reads as "the cameras disagree with the crib
    monitor" when in truth nobody was looking.
    """
    seen = {}
    for c in chunks:
        try:
            span = (datetime.fromisoformat(c["start_iso"]),
                    datetime.fromisoformat(c["end_iso"]))
        except (KeyError, ValueError, TypeError):
            continue
        seen.setdefault(chunk_room(c, rooms), []).append(span)
    return {r: union_intervals(v) for r, v in seen.items()}


def attribute(intervals, sessions):
    """Minutes of `intervals` grouped by the end_reason of the session covering them."""
    by_reason = {}
    for s in sessions:
        overlap = intersect_intervals(intervals, [(s["start"], s["end"])])
        if overlap:
            by_reason[s["reason"]] = round(
                by_reason.get(s["reason"], 0.0) + total_minutes(overlap), 1)
    return dict(sorted(by_reason.items(), key=lambda kv: -kv[1]))


def score_day(day, chunks, rooms, window):
    """Crib-monitor error for one day, restricted to the room the monitor can see."""
    win = window_bounds(day, window)
    sessions = crib_sessions_for(day)
    crib = intersect_intervals(
        union_intervals([(s["start"], s["end"]) for s in sessions]), win)

    awake_by_room, asleep_by_room = baby_state_by_room(chunks, rooms)
    cam_asleep = intersect_intervals(asleep_by_room.get(CRIB_ROOM, []), win)
    cam_awake = intersect_intervals(awake_by_room.get(CRIB_ROOM, []), win)
    observed = intersect_intervals(rooms_observed(chunks, rooms).get(CRIB_ROOM, []), win)

    agreement = intersect_intervals(crib, cam_asleep)
    contradicted = intersect_intervals(crib, cam_awake)
    # Crib says asleep, the room was being watched, and no camera committed either way.
    unconfirmed = intersect_intervals(
        subtract_intervals(subtract_intervals(crib, cam_asleep), cam_awake), observed)
    missed = subtract_intervals(cam_asleep, crib)

    crib_min = total_minutes(crib)
    return {
        "date": day.isoformat(),
        "room": CRIB_ROOM,
        "crib_minutes": round(crib_min, 1),
        "camera_asleep_minutes": round(total_minutes(cam_asleep), 1),
        "observed_minutes": round(total_minutes(observed), 1),
        "agreement_minutes": round(total_minutes(agreement), 1),
        "contradicted_minutes": round(total_minutes(contradicted), 1),
        "unconfirmed_minutes": round(total_minutes(unconfirmed), 1),
        "missed_minutes": round(total_minutes(missed), 1),
        # Share of crib-scored sleep a camera actively disputes. The headline error
        # rate: 0 is a detector that never invents sleep, 1 never gets it right.
        "false_sleep_rate": round(total_minutes(contradicted) / crib_min, 3) if crib_min else None,
        "contradicted_by_reason": attribute(contradicted, sessions),
        "session_count": len(sessions),
        "sessions_missing_reason": sum(1 for s in sessions if s["reason"] == "unknown"),
    }


def verify_against_report(day, chunks, rooms, window):
    """Recompute the published report's own whole-house numbers as an algebra check.

    If this disagrees with the stored `sleep` block, the fault is in how this module
    drives the shared interval helpers — worth knowing before trusting anything above.
    """
    from nanny_report import nap_windows_for, sleep_metrics

    path = os.path.join(REPORTS_DIR, f"{day.isoformat()}.json")
    if not os.path.exists(path):
        return None
    with open(path) as f:
        published = (json.load(f) or {}).get("sleep")
    if not published:
        return None

    naps = nap_windows_for(day)
    recomputed = sleep_metrics(day, chunks, rooms, naps, window)
    keys = ("crib_monitor_minutes", "camera_observed_minutes", "agreement_minutes",
            "crib_only_minutes", "camera_only_minutes")
    return {k: {"published": published.get(k), "recomputed": recomputed.get(k),
                "match": abs((published.get(k) or 0) - (recomputed.get(k) or 0)) < 0.05}
            for k in keys}


SCORES_DIR = os.path.join(os.path.dirname(__file__), "nanny", "sleep_scores")


def save_score(row):
    """Persist one day's score so the error trend outlives chunk retention.

    Chunks are pruned on the nanny pipeline's schedule, so a day becomes unscoreable
    a couple of weeks after the fact. The scores are tiny and are what a sweep needs
    as its baseline, so they are kept independently of the footage that produced them.
    """
    os.makedirs(SCORES_DIR, exist_ok=True)
    path = os.path.join(SCORES_DIR, f"{row['date']}.json")
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(row, f, indent=1)
    os.replace(tmp, path)
    return path


def load_day(day):
    day_dir = os.path.join(CHUNKS_DIR, day.isoformat())
    if not os.path.isdir(day_dir):
        return None
    return load_chunks(day_dir)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--date", help="YYYY-MM-DD (default: yesterday)")
    ap.add_argument("--days", type=int, help="score the last N days instead of one")
    ap.add_argument("--json", action="store_true", help="emit JSON instead of a table")
    ap.add_argument("--verify", action="store_true",
                    help="also recompute the report's own numbers and compare")
    ap.add_argument("--save", action="store_true",
                    help="also write each day's score to nanny/sleep_scores/<date>.json")
    args = ap.parse_args()

    if args.days:
        days = [date.today() - timedelta(days=i) for i in range(1, args.days + 1)]
    elif args.date:
        days = [datetime.strptime(args.date, "%Y-%m-%d").date()]
    else:
        days = [date.today() - timedelta(days=1)]

    # No camera list to validate against here — the mapping is read as configured, and
    # a camera missing from it falls back to its own name as a room (chunk_room).
    rooms = load_camera_rooms([])
    try:
        window = load_window()
    except ValueError as e:
        sys.exit(f"NANNY_WINDOW is unusable ({e}) — scoring needs the same care window "
                 "the reports were built with.")

    results = []
    for day in sorted(days):
        chunks = load_day(day)
        if not chunks:
            results.append({"date": day.isoformat(), "no_chunks": True})
            continue
        row = score_day(day, chunks, rooms, window)
        if args.verify:
            row["verify"] = verify_against_report(day, chunks, rooms, window)
        if args.save:
            save_score(row)
        results.append(row)

    if args.json:
        print(json.dumps(results, indent=2))
        return

    scored = [r for r in results if not r.get("no_chunks")]
    if not scored:
        print("No chunks for any requested day — nothing to score.")
        return

    print(f"Crib monitor vs cameras, room={CRIB_ROOM} "
          f"(nanny window only — nights are unscored)\n")
    print(f"{'date':12} {'crib':>7} {'agree':>7} {'contra':>7} {'unconf':>7} "
          f"{'missed':>7} {'false%':>7}")
    for r in scored:
        rate = "—" if r["false_sleep_rate"] is None else f"{r['false_sleep_rate']*100:.0f}%"
        print(f"{r['date']:12} {r['crib_minutes']:7.1f} {r['agreement_minutes']:7.1f} "
              f"{r['contradicted_minutes']:7.1f} {r['unconfirmed_minutes']:7.1f} "
              f"{r['missed_minutes']:7.1f} {rate:>7}")

    worst = {}
    for r in scored:
        for reason, mins in r["contradicted_by_reason"].items():
            worst[reason] = round(worst.get(reason, 0.0) + mins, 1)
    if worst:
        print("\nContradicted minutes by end_reason:")
        for reason, mins in sorted(worst.items(), key=lambda kv: -kv[1]):
            print(f"  {reason:24} {mins:8.1f}")

    stale = sum(r["sessions_missing_reason"] for r in scored)
    if stale:
        print(f"\n{stale} session(s) predate the end_reason field — shown as 'unknown'.")
    for r in results:
        if r.get("no_chunks"):
            print(f"{r['date']}: no chunks on disk, not scored.")


if __name__ == "__main__":
    main()
