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
    double-count), classified against the house rule: phone is allowed while
    the baby sleeps, not allowed while the caregiver is with an awake baby.
    "Asleep" fuses the crib monitor's nap windows with what the cameras saw;
    "with the baby" fuses the cameras that share a room, so a caregiver alone
    in one room while the baby is with nobody is never flagged on the strength
    of a single camera's view. See classify_phone_use().
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
    atomic_write_json, ensure_dirs, load_camera_rooms, load_cameras, load_window,
    update_status,
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


def intersect_intervals(a, b):
    """Overlap of two unioned interval lists (a ∩ b)."""
    out, i, j = [], 0, 0
    while i < len(a) and j < len(b):
        lo, hi = max(a[i][0], b[j][0]), min(a[i][1], b[j][1])
        if hi > lo:
            out.append((lo, hi))
        if a[i][1] < b[j][1]:
            i += 1
        else:
            j += 1
    return out


def subtract_intervals(a, b):
    """a minus b (both unioned)."""
    out = []
    for s, e in a:
        cur = s
        for bs, be in b:
            if be <= cur:
                continue
            if bs >= e:
                break
            if bs > cur:
                out.append((cur, bs))
            cur = max(cur, be)
            if cur >= e:
                break
        if cur < e:
            out.append((cur, e))
    return out


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


# ── Phone-use policy ──────────────────────────────────────────────────────────
#
# House rule: the phone is fine while the baby is asleep, and NOT fine while the
# caregiver is with an awake baby (nursery or bedroom — every camera watches a
# room the baby is cared for in). Everything else is "unclear", never a flag.
#
# Two deliberate biases, because these minutes are about a real person's conduct:
#   * asleep evidence outranks with-baby evidence, so a disagreement between two
#     cameras in the same room clears rather than accuses;
#   * only medium/high-confidence phone detections can produce flagged minutes
#     (they are also the ones that get an evidence clip). Low-confidence ones are
#     reported separately as unconfirmed.

WITH_BABY_CONTEXTS = {"while_holding_baby", "baby_nearby_awake", "baby_unattended"}
ASLEEP_CONTEXTS    = {"baby_napping"}
FLAGGABLE_CONFIDENCE = {"medium", "high"}


def chunk_room(chunk, rooms):
    """Live config wins (re-labelling a camera fixes past days too); the room
    stored at analysis time is the fallback for cameras no longer configured."""
    return rooms.get(chunk.get("camera")) or chunk.get("room") or chunk.get("camera")


def baby_state_by_room(chunks, rooms):
    """(awake, asleep) → {room: unioned intervals}, fused across the cameras that
    share a room. Cross-room fusion is what makes 'the baby is elsewhere with the
    other camera on them' distinguishable from 'the baby is alone'."""
    awake, asleep = {}, {}
    for c in chunks:
        room = chunk_room(c, rooms)
        for a in c.get("activities", []):
            state = a.get("baby_state")
            if state not in ("awake", "asleep"):
                continue
            try:
                span = (datetime.fromisoformat(a["start_iso"]),
                        datetime.fromisoformat(a["end_iso"]))
            except (KeyError, ValueError):
                continue
            (awake if state == "awake" else asleep).setdefault(room, []).append(span)
        # A phone event's own context is per-room evidence about the baby too.
        for p in c.get("phone_use", []):
            if p.get("context") not in ASLEEP_CONTEXTS:
                continue
            try:
                span = (datetime.fromisoformat(p["start_iso"]),
                        datetime.fromisoformat(p["end_iso"]))
            except (KeyError, ValueError):
                continue
            asleep.setdefault(room, []).append(span)
    return ({r: union_intervals(v) for r, v in awake.items()},
            {r: union_intervals(v) for r, v in asleep.items()})


def classify_phone_use(phone_events, chunks, rooms, naps):
    """Split phone time into asleep-OK / unauthorized / unclear and annotate each
    event in place with `room`, `authorization` and `unauthorized_minutes`.

    Returns (stats_dict, unauthorized_intervals)."""
    awake_by_room, asleep_by_room = baby_state_by_room(chunks, rooms)

    # "Asleep" is a whole-house fact: wherever the baby sleeps, the phone is fine.
    asleep_all = union_intervals(
        list(naps) + [iv for ivs in asleep_by_room.values() for iv in ivs])

    spans, with_baby = [], []
    for ev in phone_events:
        try:
            span = (datetime.fromisoformat(ev["start_iso"]),
                    datetime.fromisoformat(ev["end_iso"]))
        except (KeyError, ValueError):
            continue
        room = rooms.get(ev.get("camera")) or ev.get("room") or ev.get("camera")
        ev["room"] = room
        spans.append(span)
        if ev.get("confidence") not in FLAGGABLE_CONFIDENCE:
            continue
        if ev.get("context") in WITH_BABY_CONTEXTS:
            with_baby.append(span)            # the model saw them together
        else:
            # Baby not in this frame (or unclear): only the room's own awake-baby
            # evidence, from either camera in that room, puts them together.
            with_baby.extend(intersect_intervals([span],
                                                 awake_by_room.get(room, [])))

    total = union_intervals(spans)
    unauthorized = intersect_intervals(
        subtract_intervals(union_intervals(with_baby), asleep_all), total)
    asleep_overlap = intersect_intervals(total, asleep_all)

    # Same shape as `unauthorized` but for the low-confidence detections, so a
    # borderline "is that a phone?" never lands in the flagged number.
    unconfirmed = []
    for ev in phone_events:
        if ev.get("confidence") in FLAGGABLE_CONFIDENCE or "room" not in ev:
            continue
        span = (datetime.fromisoformat(ev["start_iso"]),
                datetime.fromisoformat(ev["end_iso"]))
        if ev.get("context") in WITH_BABY_CONTEXTS:
            unconfirmed.append(span)
        else:
            unconfirmed.extend(intersect_intervals([span],
                                                   awake_by_room.get(ev["room"], [])))
    unconfirmed = subtract_intervals(
        subtract_intervals(union_intervals(unconfirmed), asleep_all), unauthorized)

    for ev in phone_events:
        if "room" not in ev:
            continue
        span = [(datetime.fromisoformat(ev["start_iso"]),
                 datetime.fromisoformat(ev["end_iso"]))]
        flagged = total_minutes(intersect_intervals(span, unauthorized))
        ev["unauthorized_minutes"] = round(flagged, 1)
        if flagged > 0:
            ev["authorization"] = "unauthorized"
        elif total_minutes(intersect_intervals(span, asleep_all)) > 0:
            ev["authorization"] = "allowed_baby_asleep"
        elif total_minutes(intersect_intervals(span, unconfirmed)) > 0:
            ev["authorization"] = "unconfirmed"
        else:
            ev["authorization"] = "unclear"

    total_min = total_minutes(total)
    asleep_min = total_minutes(asleep_overlap)
    unauth_min = total_minutes(unauthorized)
    stats = {
        "total_minutes": round(total_min, 1),
        "during_naps_minutes": round(total_minutes(intersect_intervals(total, naps)), 1),
        "while_baby_asleep_minutes": round(asleep_min, 1),
        "while_baby_awake_minutes": round(total_min - asleep_min, 1),
        "unauthorized_minutes": round(unauth_min, 1),
        "unauthorized_unconfirmed_minutes": round(total_minutes(unconfirmed), 1),
        "unclear_minutes": round(max(total_min - asleep_min - unauth_min, 0), 1),
        "event_count": len(phone_events),
        "unauthorized_event_count": sum(
            1 for e in phone_events if e.get("authorization") == "unauthorized"),
    }
    return stats, unauthorized


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
            f"while a nanny cared for an infant. Each note is tagged with the room and "
            f"camera it came from; several cameras cover the same hours at once, so the "
            f"same moment can appear more than once from different rooms:\n{lines}\n\n"
            f"Phone use totals (already de-duplicated across cameras): "
            f"{phone_stats['total_minutes']:.0f} min overall, "
            f"{phone_stats['while_baby_asleep_minutes']:.0f} min while the baby was "
            f"asleep (allowed under the house rule), and "
            f"{phone_stats['unauthorized_minutes']:.0f} min while the caregiver was "
            f"with an awake baby (not allowed under the house rule).\n\n"
            "Write a neutral, factual 4-6 sentence summary of the day for the parents: "
            "overall rhythm, care activities, and phone usage in context. Do not merge "
            "the rooms into a single narrative if they disagree. No bullet points, no "
            "advice, no speculation beyond the notes."
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


def build_report(day, chunks, cameras, window, rooms=None):
    rooms = rooms or {}
    timeline, phone_events, notable, summaries = [], [], [], []
    parse_errors = 0
    for c in chunks:
        cam, room = c["camera"], chunk_room(c, rooms)
        if c.get("parse_error"):
            parse_errors += 1
        if c.get("summary"):
            summaries.append(f"[{room} · {cam} {c['segment_start_iso'][11:16]}] "
                             f"{c['summary']}")
        for a in c.get("activities", []):
            timeline.append({**a, "camera": cam, "room": room})
        for p in c.get("phone_use", []):
            phone_events.append({**p, "camera": cam, "room": room})
        for n in c.get("notable_events", []):
            notable.append({**n, "camera": cam, "room": room})

    timeline.sort(key=lambda x: x["start_iso"])
    phone_events.sort(key=lambda x: x["start_iso"])
    notable.sort(key=lambda x: x["time_iso"])

    naps = nap_windows_for(day)
    phone_stats, unauthorized = classify_phone_use(phone_events, chunks, rooms, naps)

    return {
        "date": day.isoformat(),
        "generated_at": datetime.now().isoformat(),
        "cameras": sorted(set(cameras) | {c["camera"] for c in chunks}),
        "rooms": {cam: rooms.get(cam, cam)
                  for cam in sorted(set(cameras) | {c["camera"] for c in chunks})},
        "coverage": coverage_for(chunks, cameras, window),
        "parse_errors": parse_errors,
        "narrative": day_narrative(day, summaries, phone_stats),
        "timeline": timeline,
        "phone_use": {"events": phone_events, **phone_stats,
                      "unauthorized_intervals": [
                          {"start_iso": s.isoformat(), "end_iso": e.isoformat()}
                          for s, e in unauthorized]},
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
        rooms = load_camera_rooms(cameras)
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
        report = build_report(day, chunks, cameras, window, rooms)
        atomic_write_json(os.path.join(REPORTS_DIR, f"{day.isoformat()}.json"), report)
        logging.info("%s: report written — %d timeline spans, %.0f phone min "
                     "(%.0f while asleep / %.0f flagged / %.0f unclear), %d camera(s)",
                     day, len(report["timeline"]),
                     report["phone_use"]["total_minutes"],
                     report["phone_use"]["while_baby_asleep_minutes"],
                     report["phone_use"]["unauthorized_minutes"],
                     report["phone_use"]["unclear_minutes"],
                     len(report["cameras"]))
        update_status("report", date=day.isoformat(),
                      phone_minutes=report["phone_use"]["total_minutes"],
                      unauthorized_minutes=report["phone_use"]["unauthorized_minutes"])

    cleanup()


if __name__ == "__main__":
    main()
