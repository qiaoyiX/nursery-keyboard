"""
Daily nanny-report merge: combine the day's per-segment chunk JSONs (all
cameras) into one report consumed by the dashboard's /nanny page.

Runs as a oneshot systemd timer (nursery-nanny-report.timer, 18:45,
Persistent=true). It first re-runs the analyzer as a straggler sweep (the
18:05 analyzer run can still be uploading at 18:45), then merges EVERY
chunk-date that has no report yet — deliberately not "today", so a Pi that was
off at 18:45 catches up on yesterday when the Persistent timer fires at boot.

Per report:
  - wall-clock-sorted cross-camera timeline (camera-tagged; no cross-camera
    dedup in v1 — the same event seen by two cameras appears twice)
  - phone totals by interval UNION across cameras (double coverage must not
    double-count), split against the baby's nap windows from storage
    (the crib monitor already records them)
  - per-camera coverage vs the care window, with an explicit gap list
  - a short day narrative from one cheap Gemini text call over the hourly
    summaries (the report still writes with narrative=null if that fails)

Cleanup: evidence clips older than NANNY_CLIP_RETENTION_DAYS (default 14) and
stray lowres transients are pruned at the end of each run.
"""

import json
import logging
import os
import shutil
import sys
from datetime import date, datetime, timedelta

from nanny_common import (
    CHUNKS_DIR, CLIPS_DIR, LOWRES_DIR, REPORTS_DIR,
    atomic_write_json, ensure_dirs, load_cameras, load_window, update_status,
)
from nanny_analyze import DEFAULT_MODEL, analyze_pending

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s [nanny_report] %(message)s")


# ── Interval math ─────────────────────────────────────────────────────────────

def union_intervals(intervals):
    """[(start_dt, end_dt), ...] → merged, sorted, non-overlapping list."""
    merged = []
    for s, e in sorted(i for i in intervals if i[1] > i[0]):
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    return merged


def intersect_minutes(intervals, windows):
    """Total minutes of `intervals` (already unioned) overlapping `windows`."""
    total = 0.0
    for s, e in intervals:
        for ws, we in windows:
            lo, hi = max(s, ws), min(e, we)
            if hi > lo:
                total += (hi - lo).total_seconds() / 60
    return total


def total_minutes(intervals):
    return sum((e - s).total_seconds() / 60 for s, e in intervals)


# ── Nap windows from the crib monitor ─────────────────────────────────────────

def nap_windows_for(day):
    """Closed sleep sessions (from storage) clipped to `day`."""
    import storage
    days_back = (date.today() - day).days + 1
    day_start = datetime.combine(day, datetime.min.time())
    day_end   = day_start + timedelta(days=1)
    windows = []
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
            windows.append((lo, hi))
    return union_intervals(windows)


# ── Narrative ─────────────────────────────────────────────────────────────────

def day_narrative(day, hour_summaries, phone_stats):
    """One cheap text-only Gemini call. Returns None on any failure."""
    if not os.environ.get("GEMINI_API_KEY") or not hour_summaries:
        return None
    try:
        from nanny_analyze import make_client
        client = make_client()
        model = os.environ.get("GEMINI_MODEL", DEFAULT_MODEL)
        lines = "\n".join(f"- {s}" for s in hour_summaries)
        prompt = (
            f"These are hourly observation notes from home cameras on {day.isoformat()} "
            f"while a nanny cared for an infant:\n{lines}\n\n"
            f"Phone use totals: {phone_stats['total_minutes']:.0f} min overall, "
            f"{phone_stats['while_baby_awake_minutes']:.0f} min while the baby was awake, "
            f"{phone_stats['during_naps_minutes']:.0f} min during naps.\n\n"
            "Write a neutral, factual 4-6 sentence summary of the day for the parents: "
            "overall rhythm, care activities, and phone usage in context. No bullet "
            "points, no advice, no speculation beyond the notes."
        )
        resp = client.models.generate_content(model=model, contents=prompt)
        return (resp.text or "").strip() or None
    except Exception as e:
        logging.warning("Narrative generation failed (%s) — report will have none", e)
        return None


# ── Merge one day ─────────────────────────────────────────────────────────────

def load_chunks(day_dir):
    chunks = []
    for name in sorted(os.listdir(day_dir)):
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(day_dir, name)) as f:
                chunks.append(json.load(f))
        except (json.JSONDecodeError, OSError) as e:
            logging.warning("Skipping unreadable chunk %s: %s", name, e)
    return chunks


def coverage_for(chunks, cameras, window):
    """Per-camera analyzed segments vs the care window; explicit gap list."""
    start_t, end_t = window
    window_minutes = (datetime.combine(date.min, end_t)
                      - datetime.combine(date.min, start_t)).total_seconds() / 60
    cov = {}
    names = set(cameras) | {c["camera"] for c in chunks}
    for cam in sorted(names):
        segs = [(datetime.fromisoformat(c["segment_start_iso"]),
                 datetime.fromisoformat(c["segment_start_iso"])
                 + timedelta(minutes=c.get("segment_minutes", 60)))
                for c in chunks if c["camera"] == cam]
        analyzed = union_intervals(segs)
        analyzed_min = total_minutes(analyzed)
        gaps = []
        if segs:
            day0 = segs[0][0].date()
            cursor = datetime.combine(day0, start_t)
            w_end = datetime.combine(day0, end_t)
            for s, e in analyzed:
                if s > cursor:
                    gaps.append({"start_iso": cursor.isoformat(),
                                 "end_iso": min(s, w_end).isoformat()})
                cursor = max(cursor, e)
            if cursor < w_end:
                gaps.append({"start_iso": cursor.isoformat(),
                             "end_iso": w_end.isoformat()})
        else:
            gaps.append({"whole_day": True})
        cov[cam] = {"analyzed_minutes": round(analyzed_min),
                    "window_minutes": round(window_minutes),
                    "gaps": gaps}
    return cov


def build_report(day, chunks, cameras, window):
    timeline, phone_events, notable, summaries = [], [], [], []
    parse_errors = 0
    for c in chunks:
        cam = c["camera"]
        if c.get("parse_error"):
            parse_errors += 1
        if c.get("summary"):
            summaries.append(f"[{cam} {c['segment_start_iso'][11:16]}] {c['summary']}")
        for a in c.get("activities", []):
            timeline.append({**a, "camera": cam})
        for p in c.get("phone_use", []):
            phone_events.append({**p, "camera": cam})
        for n in c.get("notable_events", []):
            notable.append({**n, "camera": cam})

    timeline.sort(key=lambda x: x["start_iso"])
    phone_events.sort(key=lambda x: x["start_iso"])
    notable.sort(key=lambda x: x["time_iso"])

    phone_intervals = union_intervals(
        [(datetime.fromisoformat(p["start_iso"]), datetime.fromisoformat(p["end_iso"]))
         for p in phone_events])
    naps = nap_windows_for(day)
    during_naps = intersect_minutes(phone_intervals, naps)
    total = total_minutes(phone_intervals)
    phone_stats = {
        "total_minutes": round(total, 1),
        "during_naps_minutes": round(during_naps, 1),
        "while_baby_awake_minutes": round(total - during_naps, 1),
        "event_count": len(phone_events),
    }

    return {
        "date": day.isoformat(),
        "generated_at": datetime.now().isoformat(),
        "cameras": sorted(set(cameras) | {c["camera"] for c in chunks}),
        "coverage": coverage_for(chunks, cameras, window),
        "parse_errors": parse_errors,
        "narrative": day_narrative(day, summaries, phone_stats),
        "timeline": timeline,
        "phone_use": {"events": phone_events, **phone_stats},
        "notable_events": notable,
        "naps": [{"start_iso": s.isoformat(), "end_iso": e.isoformat()}
                 for s, e in naps],
    }


def unreported_dates(today):
    """Chunk-date dirs without a report, excluding today before window end
    (today is merged by tonight's run, not a Persistent catch-up at noon)."""
    if not os.path.isdir(CHUNKS_DIR):
        return []
    out = []
    for name in sorted(os.listdir(CHUNKS_DIR)):
        try:
            day = date.fromisoformat(name)
        except ValueError:
            continue
        if os.path.exists(os.path.join(REPORTS_DIR, f"{name}.json")):
            continue
        if day == today and datetime.now().time() < load_window()[1]:
            continue
        out.append(day)
    return out


def cleanup():
    retention = int(os.environ.get("NANNY_CLIP_RETENTION_DAYS", "14"))
    floor = date.today() - timedelta(days=retention)
    if os.path.isdir(CLIPS_DIR):
        for name in os.listdir(CLIPS_DIR):
            try:
                day = date.fromisoformat(name)
            except ValueError:
                continue
            if day < floor:
                shutil.rmtree(os.path.join(CLIPS_DIR, name), ignore_errors=True)
                logging.info("Pruned clips for %s (older than %dd retention)",
                             name, retention)
    if os.path.isdir(LOWRES_DIR):
        for root, _, files in os.walk(LOWRES_DIR):
            for f in files:
                os.remove(os.path.join(root, f))


def main():
    ensure_dirs()
    try:
        cameras = load_cameras()
        window = load_window()
    except ValueError as e:
        sys.exit(f"ABORT: bad configuration: {e}")

    # Straggler sweep: the 18:05 analyzer run may not have finished (or run).
    analyze_pending()

    days = unreported_dates(date.today())
    if not days:
        logging.info("No unreported days — nothing to merge.")
        cleanup()
        return

    for day in days:
        chunks = load_chunks(os.path.join(CHUNKS_DIR, day.isoformat()))
        if not chunks:
            logging.info("%s: chunk dir exists but holds no readable chunks — skipping.", day)
            continue
        report = build_report(day, chunks, cameras, window)
        atomic_write_json(os.path.join(REPORTS_DIR, f"{day.isoformat()}.json"), report)
        logging.info("%s: report written — %d timeline spans, %.0f phone min "
                     "(%.0f awake / %.0f naps), %d camera(s)",
                     day, len(report["timeline"]),
                     report["phone_use"]["total_minutes"],
                     report["phone_use"]["while_baby_awake_minutes"],
                     report["phone_use"]["during_naps_minutes"],
                     len(report["cameras"]))
        update_status("report", date=day.isoformat(),
                      phone_minutes=report["phone_use"]["total_minutes"])

    cleanup()


if __name__ == "__main__":
    main()
