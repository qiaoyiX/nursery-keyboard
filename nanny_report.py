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
  - day metrics, all clipped to the care window and all deduplicated across
    cameras: sleep (with a crib-monitor vs camera cross-check), care activities
    by category, and attendance (minutes an awake baby had nobody with them)
  - a short day narrative from one cheap Gemini text call over the hourly
    summaries (the report still writes with narrative=null if that fails)

Cleanup: evidence clips older than NANNY_CLIP_RETENTION_DAYS (default 14) and
stray lowres transients are pruned at the end of each run.

Run with no arguments, this is the production behaviour above. For maintenance
there is a CLI — see parse_args(); `--dry-run --date YYYY-MM-DD` rebuilds a day
to stdout without sweeping, calling Gemini, writing, or pruning anything.
"""

import argparse
import json
import logging
import os
import shutil
import sys
import time
from datetime import date, datetime, time as dtime, timedelta

from nanny_common import (
    CHUNKS_DIR, CLIPS_DIR, LOWRES_DIR, REPORTS_DIR,
    atomic_write_json, context_age_days, disk_status, ensure_dirs, load_camera_rooms,
    load_cameras, load_context, load_days, load_window, update_status,
)
from nanny_analyze import ACTIVITY_CATEGORIES, DEFAULT_MODEL, analyze_pending

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
    return sum(((e - s).total_seconds() / 60 for s, e in intervals), 0.0)


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

CONFIDENCE_RANK = {"low": 0, "medium": 1, "high": 2}
# Most specific wins when two angles disagree: a camera that saw the baby in the
# caregiver's arms knows more than one that simply had no baby in frame.
CONTEXT_RANK = {"while_holding_baby": 5, "baby_unattended": 4, "baby_nearby_awake": 3,
                "baby_napping": 2, "unclear": 1, "baby_not_in_frame": 0}
# Scored unless positively attributed to someone else — "unclear" outranks
# "other_adult" so an unattributed event is never quietly excused.
PERSON_RANK = {"caregiver": 2, "unclear": 1, "other_adult": 0}
MERGE_GAP_SECONDS = 30


def merge_phone_events(events, gap_seconds=MERGE_GAP_SECONDS):
    """One event per real occurrence, not one per camera that happened to see it.

    Two cameras sharing a room are two angles on one scene, so a single phone
    pickup arrived twice — two rows on the page and two evidence clips of the
    same moment. The *minutes* were already right (classify_phone_use unions
    the intervals), but `event_count` was counting camera-observations and the
    reader got the same video twice from different angles.

    Merging is per room on purpose: a caregiver cannot be in two rooms at once,
    so simultaneous events in different rooms are genuinely separate
    observations and must stay separate.
    """
    parsed, passthrough = [], []
    for ev in events:
        span = _span(ev)
        if span:
            parsed.append((span, ev))
        else:
            passthrough.append(ev)      # unusable timestamps: never dropped

    merged = []
    by_room = {}
    for span, ev in parsed:
        by_room.setdefault(ev.get("room") or ev.get("camera"), []).append((span, ev))
    for _, items in sorted(by_room.items(), key=lambda kv: str(kv[0])):
        items.sort(key=lambda it: it[0][0])
        group, run_end = [], None
        for span, ev in items:
            # run_end, not the last span's end: a chain of staggered overlaps
            # is one event, and the second view may be shorter than the first.
            if group and span[0] <= run_end + timedelta(seconds=gap_seconds):
                group.append((span, ev))
                run_end = max(run_end, span[1])
            else:
                if group:
                    merged.append(_collapse(group))
                group, run_end = [(span, ev)], span[1]
        if group:
            merged.append(_collapse(group))

    merged.extend(passthrough)
    merged.sort(key=lambda e: e["start_iso"])
    return merged


def _collapse(group):
    """One merged event from several cameras' views of the same moment."""
    spans = [s for s, _ in group]
    evs = [e for _, e in group]
    start, end = min(s for s, _ in spans), max(e for _, e in spans)
    # The clip to keep: most confident first, then the longest look at it.
    primary = max(group, key=lambda it: (CONFIDENCE_RANK.get(it[1].get("confidence"), 0),
                                         (it[0][1] - it[0][0]).total_seconds()))[1]
    best = dict(primary)
    best["start_iso"], best["end_iso"] = start.isoformat(), end.isoformat()
    best["confidence"] = max((e.get("confidence", "low") for e in evs),
                             key=lambda c: CONFIDENCE_RANK.get(c, 0))
    best["context"] = max((e.get("context", "unclear") for e in evs),
                          key=lambda c: CONTEXT_RANK.get(c, 1))
    best["person"] = max((e.get("person", "unclear") for e in evs),
                         key=lambda p: PERSON_RANK.get(p, 1))
    others = sorted({e.get("camera") for e in evs} - {primary.get("camera")} - {None})
    if others:
        best["also_seen_by"] = others
    return best


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


def asleep_intervals(naps, asleep_by_room):
    """The whole-house "the baby is asleep" union: crib-monitor naps ∪ whatever
    the cameras scored asleep.

    Asleep is deliberately NOT a per-room fact — wherever the baby sleeps, the
    baby is asleep, and the bedroom has no crib monitor at all, so there the
    cameras are the only evidence there is. The phone policy and the sleep
    metrics both judge against this one definition; two definitions left to
    drift apart would let one report call the same minute both phone-allowed
    (baby asleep) and awake.
    """
    return union_intervals(
        list(naps) + [iv for ivs in asleep_by_room.values() for iv in ivs])


def classify_phone_use(phone_events, chunks, rooms, naps):
    """Split phone time into asleep-OK / unauthorized / unclear and annotate each
    event in place with `room`, `authorization` and `unauthorized_minutes`.

    Returns (stats_dict, unauthorized_intervals)."""
    awake_by_room, asleep_by_room = baby_state_by_room(chunks, rooms)
    asleep_all = asleep_intervals(naps, asleep_by_room)

    spans, with_baby = [], []
    for ev in phone_events:
        try:
            span = (datetime.fromisoformat(ev["start_iso"]),
                    datetime.fromisoformat(ev["end_iso"]))
        except (KeyError, ValueError):
            continue
        room = rooms.get(ev.get("camera")) or ev.get("room") or ev.get("camera")
        ev["room"] = room
        if ev.get("person") == "other_adult":
            # A parent or visitor on their own phone. Still shown on the page —
            # it explains what is on camera — but this report measures the
            # caregiver's hours, so it counts toward nothing. Only a positive
            # attribution excuses an event; "unclear" is scored as usual.
            ev["authorization"] = "not_caregiver"
            ev["unauthorized_minutes"] = 0.0
            continue
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
        if (ev.get("confidence") in FLAGGABLE_CONFIDENCE or "room" not in ev
                or ev.get("person") == "other_adult"):
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
        if "room" not in ev or ev.get("person") == "other_adult":
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
        "event_count": sum(1 for e in phone_events if e.get("person") != "other_adult"),
        "other_adult_event_count": sum(
            1 for e in phone_events if e.get("person") == "other_adult"),
        "unauthorized_event_count": sum(
            1 for e in phone_events if e.get("authorization") == "unauthorized"),
    }
    return stats, unauthorized


# ── Day metrics ───────────────────────────────────────────────────────────────
#
# Everything here is clipped to the care window. A nanny report is about the
# hours the nanny worked: overnight sleep the parents handled would otherwise
# dominate the sleep totals, and minutes outside the window are not this
# report's business at all.
#
# All three families union their intervals across cameras BEFORE totalling, so
# an hour watched by two cameras counts once — the same treatment phone minutes
# already get.

def window_bounds(day, window):
    """The care window on `day`, as a one-element interval list ready to clip."""
    start_t, end_t = window
    return [(datetime.combine(day, start_t), datetime.combine(day, end_t))]


def _span(d, start_key="start_iso", end_key="end_iso"):
    """One (start, end) tuple from a chunk record, or None if unusable."""
    try:
        s = datetime.fromisoformat(d[start_key])
        e = datetime.fromisoformat(d[end_key])
    except (KeyError, TypeError, ValueError):
        return None
    return (s, e) if e > s else None


def _longest(intervals):
    return round(max((total_minutes([iv]) for iv in intervals), default=0.0), 1)


def analyzed_intervals(chunks):
    """Union of every analyzed segment across all cameras — the span of the day
    anyone was actually watching. Distinguishes 'nothing happened' from 'nobody
    was looking', which is the difference between a fact and a blind spot."""
    segs = []
    for c in chunks:
        try:
            s = datetime.fromisoformat(c["segment_start_iso"])
        except (KeyError, TypeError, ValueError):
            continue
        segs.append((s, s + timedelta(minutes=c.get("segment_minutes", 60) or 0)))
    return union_intervals(segs)


def sleep_metrics(day, chunks, rooms, naps, window):
    """Baby sleep during the care window, and a crib-monitor/camera cross-check.

    nap_count counts intervals of the MERGED union, so two naps a few minutes
    apart read as one nap. That is the honest reading of merged evidence: once
    the crib monitor and two cameras are unioned there is no principled way to
    re-split a contiguous block back into the sessions that formed it.

    The crib-only / camera-only split is the monitoring half. Large
    camera_only_minutes is normally a bedroom nap the crib monitor cannot see;
    large crib_only_minutes means either the cameras missed it or the crib
    monitor is inventing sleep — the latter is worth chasing in
    docs/sleep-detection-research.md terms.
    """
    win = window_bounds(day, window)
    _, asleep_by_room = baby_state_by_room(chunks, rooms)
    camera_all = union_intervals(
        [iv for ivs in asleep_by_room.values() for iv in ivs])

    crib     = intersect_intervals(union_intervals(list(naps)), win)
    camera   = intersect_intervals(camera_all, win)
    combined = intersect_intervals(asleep_intervals(naps, asleep_by_room), win)
    awake_gaps = subtract_intervals(win, combined)

    total = total_minutes(combined)
    return {
        "total_sleep_minutes": round(total, 1),
        "nap_count": len(combined),
        "longest_nap_minutes": _longest(combined),
        "average_nap_minutes": round(total / len(combined), 1) if combined else 0.0,
        "first_sleep_start_iso": combined[0][0].isoformat() if combined else None,
        "last_wake_iso": combined[-1][1].isoformat() if combined else None,
        "longest_awake_stretch_minutes": _longest(awake_gaps),
        "window_minutes": round(total_minutes(win), 1),
        # Cross-check between the two independent sources of "asleep".
        "crib_monitor_minutes": round(total_minutes(crib), 1),
        "camera_observed_minutes": round(total_minutes(camera), 1),
        "agreement_minutes": round(total_minutes(intersect_intervals(crib, camera)), 1),
        "crib_only_minutes": round(total_minutes(subtract_intervals(crib, camera)), 1),
        "camera_only_minutes": round(total_minutes(subtract_intervals(camera, crib)), 1),
        "naps": [{"start_iso": s.isoformat(), "end_iso": e.isoformat(),
                  "duration_minutes": round(total_minutes([(s, e)]), 1)}
                 for s, e in combined],
    }


def activity_metrics(day, chunks, rooms, window):
    """Minutes and counts per ACTIVITY_CATEGORIES, deduplicated across cameras."""
    win = window_bounds(day, window)
    by_cat = {}
    for c in chunks:
        for a in c.get("activities", []):
            cat = a.get("category")
            if cat not in ACTIVITY_CATEGORIES:
                continue
            span = _span(a)
            if span:
                by_cat.setdefault(cat, []).append(span)

    merged = {cat: intersect_intervals(union_intervals(spans), win)
              for cat, spans in by_cat.items()}
    minutes = {cat: round(total_minutes(ivs), 1)
               for cat, ivs in merged.items() if ivs}
    counts  = {cat: len(ivs) for cat, ivs in merged.items() if ivs}

    active = intersect_intervals(
        union_intervals([iv for cat in ("feeding", "play", "holding_baby")
                         for iv in merged.get(cat, [])]), win)
    return {
        "minutes_by_category": minutes,
        "event_counts": counts,
        "feeding_count": counts.get("feeding", 0),
        "diaper_count": counts.get("diaper", 0),
        "held_minutes": minutes.get("holding_baby", 0.0),
        "active_care_minutes": round(total_minutes(active), 1),
    }


def attendance_metrics(day, chunks, rooms, naps, window):
    """Minutes an AWAKE baby was left with no caregiver in view.

    These minutes describe a real person's conduct, so this inherits the phone
    policy's two biases exactly (see the block comment above classify_phone_use):

      * Presence evidence outranks absence evidence. Any caregiver-present
        activity — any category that is not out_of_frame, from any camera —
        clears the span. Cameras disagreeing clears rather than accuses.
      * Only confidence-gated evidence can flag. A `baby_unattended` phone
        context carries the model's own confidence, so FLAGGABLE_CONFIDENCE
        applies. An out_of_frame activity has no confidence field and can only
        flag when the same room independently shows an awake baby.

    And one addition the phone policy does not need: minutes nobody analyzed are
    subtracted out. An hour with no footage must never read as an hour the baby
    was alone — that is the difference between a finding and a blind spot.
    """
    win = window_bounds(day, window)
    awake_by_room, asleep_by_room = baby_state_by_room(chunks, rooms)
    asleep_all = asleep_intervals(naps, asleep_by_room)
    awake_all = union_intervals([iv for ivs in awake_by_room.values() for iv in ivs])

    present, strong, weak = [], [], []
    for c in chunks:
        for a in c.get("activities", []):
            span = _span(a)
            if not span:
                continue
            room = chunk_room(c, rooms)
            if a.get("category") == "out_of_frame":
                # Only this room's own awake-baby evidence turns "the caregiver
                # stepped out of frame" into "the baby was left alone".
                corroborated = intersect_intervals([span], awake_by_room.get(room, []))
                strong.extend(corroborated)
                weak.extend(subtract_intervals([span], corroborated))
            else:
                present.append(span)
        for p in c.get("phone_use", []):
            if p.get("context") != "baby_unattended":
                continue
            span = _span(p)
            if not span:
                continue
            (strong if p.get("confidence") in FLAGGABLE_CONFIDENCE
             else weak).append(span)

    present = union_intervals(present)
    observed = intersect_intervals(analyzed_intervals(chunks), win)
    uncovered = subtract_intervals(win, observed)

    # A sleeping baby alone in a crib is the normal state of affairs, not a
    # finding — restrict to minutes the baby was awake somewhere.
    unattended = intersect_intervals(
        subtract_intervals(
            subtract_intervals(
                intersect_intervals(union_intervals(strong), awake_all),
                present),
            asleep_all),
        observed)
    unclear = union_intervals(
        uncovered + subtract_intervals(
            subtract_intervals(union_intervals(weak), present), unattended))

    return {
        "unattended_minutes": round(total_minutes(unattended), 1),
        "longest_unattended_stretch_minutes": _longest(unattended),
        "unattended_intervals": [{"start_iso": s.isoformat(), "end_iso": e.isoformat()}
                                 for s, e in unattended],
        "unclear_minutes": round(total_minutes(intersect_intervals(unclear, win)), 1),
        "caregiver_present_minutes": round(
            total_minutes(intersect_intervals(present, win)), 1),
        "baby_awake_minutes": round(total_minutes(intersect_intervals(awake_all, win)), 1),
        "observed_minutes": round(total_minutes(observed), 1),
        "uncovered_minutes": round(total_minutes(uncovered), 1),
    }


# ── Verdict ───────────────────────────────────────────────────────────────────

# Coverage below this fraction of the care window makes the day's numbers weak
# evidence rather than a finding — a clean day nobody watched is not a clean day.
MIN_TRUSTED_COVERAGE = 0.75


def day_verdict(report):
    """The one line a parent should be able to read and then stop.

    Computed here, in Python, from the already-classified numbers — deliberately
    NOT by the model. `narrative` is a second LLM pass over the first LLM's
    summaries; it is useful prose but it is under no obligation to agree with
    what classify_phone_use() actually concluded, and the page must not let a
    soothing paragraph stand in for the verdict.

    Levels, highest first: concern > attention > degraded > clear.
    """
    reasons = []
    safety = [n for n in report.get("notable_events", [])
              if n.get("type") == "safety_concern"]
    phone = report.get("phone_use", {})
    flagged = phone.get("unauthorized_minutes", 0) or 0

    coverage = report.get("coverage") or {}
    covered = [c for c in coverage.values() if c.get("window_minutes")]
    worst = min((c["analyzed_minutes"] / c["window_minutes"] for c in covered),
                default=1.0)

    degraded = []
    if report.get("no_analysis"):
        degraded.append("no footage was analyzed at all")
    if report.get("config_errors"):
        degraded.append(f"{len(report['config_errors'])} configuration problem(s)")
    if report.get("failures"):
        degraded.append(f"{len(report['failures'])} hour(s) of footage lost")
    if covered and worst < MIN_TRUSTED_COVERAGE:
        degraded.append(f"one camera covered only {worst * 100:.0f}% of the window")

    if safety:
        level = "concern"
        headline = (f"{len(safety)} safety concern reported"
                    if len(safety) == 1 else
                    f"{len(safety)} safety concerns reported")
        reasons = [n.get("description", "") for n in safety]
    elif flagged > 0:
        level = "attention"
        headline = (f"{flagged:.0f} min of phone use while with an awake baby")
        reasons = [f"{phone.get('unauthorized_event_count', 0)} flagged event(s)"]
    elif degraded:
        level = "degraded"
        headline = "Not enough was reviewed to call this day clear"
    else:
        level = "clear"
        headline = "Nothing flagged today"

    # Degradation never *replaces* a finding, but it always qualifies one: a
    # flag found in 40% coverage and a flag found in full coverage are not the
    # same claim.
    reasons.extend(degraded)
    return {"level": level, "headline": headline,
            "reasons": [r for r in reasons if r],
            "worst_coverage_pct": round(worst * 100)}


# ── Narrative ─────────────────────────────────────────────────────────────────

def day_narrative(day, hour_summaries, phone_stats):
    """One cheap text-only Gemini call. Returns None on any failure.

    It runs seconds after the straggler sweep's last video call, so it is the
    one request most likely to meet a still-exhausted per-minute quota — hence
    the retries. A missing narrative is cosmetic, the report ships either way.
    """
    if not os.environ.get("GEMINI_API_KEY") or not hour_summaries:
        return None
    try:
        from nanny_analyze import is_retryable, make_client, retry_delay_seconds
        client = make_client()
        model = os.environ.get("GEMINI_MODEL", DEFAULT_MODEL)
        lines = "\n".join(f"- {s}" for s in hour_summaries)
        context = load_context()
        household = (f"Standing context about this household:\n{context}\n\n"
                     if context else "")
        prompt = (
            f"{household}"
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
        for attempt in range(1, 4):
            try:
                resp = client.models.generate_content(model=model, contents=prompt)
                return (resp.text or "").strip() or None
            except Exception as e:
                if attempt == 3 or not is_retryable(e):
                    raise
                wait = min(retry_delay_seconds(e) or 20 * attempt, 120)
                logging.warning("Narrative attempt %d failed (%s) — retrying in %.0fs",
                                attempt, e, wait)
                time.sleep(wait)
    except Exception as e:
        logging.warning("Narrative generation failed (%s) — report will have none", e)
        return None


# ── Merge one day ─────────────────────────────────────────────────────────────

def load_chunks(day_dir):
    chunks = []
    if not os.path.isdir(day_dir):
        return chunks          # a care day on which nothing was ever analyzed
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
        segs, lost = [], []
        for c in chunks:
            if c["camera"] != cam:
                continue
            start = datetime.fromisoformat(c["segment_start_iso"])
            segs.append((start, start + timedelta(minutes=c.get("segment_minutes", 60))))
            # A piece that failed inside an otherwise-fine hour is a real gap.
            # Without this the segment counts as fully reviewed and coverage —
            # the number that calibrates trust in every other figure on the
            # page — overstates what anyone actually looked at.
            for iv in c.get("unanalyzed_intervals") or []:
                span = _span(iv)
                if span:
                    lost.append(span)
        analyzed = subtract_intervals(union_intervals(segs), union_intervals(lost))
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


def build_report(day, chunks, cameras, window, rooms=None, config_errors=None,
                 with_narrative=True):
    rooms = rooms or {}
    timeline, phone_events, notable, summaries = [], [], [], []
    failures = []
    parse_errors = 0
    for c in chunks:
        cam, room = c["camera"], chunk_room(c, rooms)
        if c.get("parse_error"):
            parse_errors += 1
        if c.get("error"):
            # An hour we know we lost, and why — as opposed to a plain coverage
            # gap, which is indistinguishable from the camera being off.
            failures.append({"camera": cam, "room": room,
                             "segment_start_iso": c["segment_start_iso"],
                             "error": c["error"],
                             "detail": c.get("error_detail", "")})
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
    # Before classification, so every downstream stat sees one event per
    # occurrence rather than one per camera that watched it.
    phone_events = merge_phone_events(phone_events)
    notable.sort(key=lambda x: x["time_iso"])

    naps = nap_windows_for(day)
    phone_stats, unauthorized = classify_phone_use(phone_events, chunks, rooms, naps)

    report = {
        "date": day.isoformat(),
        "generated_at": datetime.now().isoformat(),
        "cameras": sorted(set(cameras) | {c["camera"] for c in chunks}),
        "rooms": {cam: rooms.get(cam, cam)
                  for cam in sorted(set(cameras) | {c["camera"] for c in chunks})},
        "coverage": coverage_for(chunks, cameras, window),
        "parse_errors": parse_errors,
        "failures": sorted(failures, key=lambda f: f["segment_start_iso"]),
        # Both are about the pipeline, not the day: an empty report and a
        # misconfigured one must be legible as such on the page.
        "no_analysis": not chunks,
        "config_errors": list(config_errors or []),
        "narrative": (day_narrative(day, summaries, phone_stats)
                      if with_narrative else None),
        "sleep": sleep_metrics(day, chunks, rooms, naps, window),
        "care": activity_metrics(day, chunks, rooms, window),
        "attendance": attendance_metrics(day, chunks, rooms, naps, window),
        "timeline": timeline,
        "phone_use": {"events": phone_events, **phone_stats,
                      "unauthorized_intervals": [
                          {"start_iso": s.isoformat(), "end_iso": e.isoformat()}
                          for s, e in unauthorized]},
        "notable_events": notable,
        "naps": [{"start_iso": s.isoformat(), "end_iso": e.isoformat()}
                 for s, e in naps],
        # Things worth knowing that are not broken config: kept separate so a
        # stale context file never reads as a pipeline failure.
        "warnings": pipeline_warnings(),
        "storage": storage_status(),
    }
    report["verdict"] = day_verdict(report)
    return report


def unreported_dates(today, force=False, reports_dir=None):
    """Chunk-date dirs without a report, excluding today before window end
    (today is merged by tonight's run, not a Persistent catch-up at noon)."""
    reports_dir = reports_dir or REPORTS_DIR
    if not os.path.isdir(CHUNKS_DIR):
        return []
    out = []
    for name in sorted(os.listdir(CHUNKS_DIR)):
        try:
            day = date.fromisoformat(name)
        except ValueError:
            continue
        if not force and os.path.exists(os.path.join(reports_dir, f"{name}.json")):
            continue
        if day == today and datetime.now().time() < load_window()[1]:
            continue
        out.append(day)
    return out


CONTEXT_MAX_AGE_DAYS = 90


def pipeline_warnings():
    """Not-broken-but-worth-knowing. Deliberately not config_errors, which mean
    something is actually wrong and degrade the report."""
    warnings = []
    age = context_age_days()
    if age is None:
        warnings.append(
            "No household context file — the model cannot tell the caregiver from "
            "a parent, and will describe everyone as 'a person'.")
    elif age > CONTEXT_MAX_AGE_DAYS:
        warnings.append(
            f"The household context has not been edited in {age} days. If anyone's "
            "role changed, every report since is miscasting people.")
    return warnings


def storage_status():
    """Disk headroom + unanalyzed backlog, so a building problem is visible
    before purge_raw_under_disk_pressure() starts deleting unwatched hours."""
    status = disk_status()
    status["dropped_for_disk_pressure"] = 0
    if os.path.isdir(CHUNKS_DIR):
        for day_name in os.listdir(CHUNKS_DIR):
            day_dir = os.path.join(CHUNKS_DIR, day_name)
            if not os.path.isdir(day_dir):
                continue
            for name in os.listdir(day_dir):
                if not name.endswith(".json"):
                    continue
                try:
                    with open(os.path.join(day_dir, name)) as f:
                        if json.load(f).get("error") == "disk_pressure":
                            status["dropped_for_disk_pressure"] += 1
                except (OSError, ValueError):
                    continue
    return status


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
    prune_superseded_clips()
    prune_old_chunks()
    prune_old_reports()
    if os.path.isdir(LOWRES_DIR):
        for root, _, files in os.walk(LOWRES_DIR):
            for f in files:
                os.remove(os.path.join(root, f))


def prune_old_chunks():
    """Chunks die with the clips that evidence them.

    Not a disk measure — a report is ~2 KB and chunks run ~0.5 MB/day; raw
    video is the only thing that ever threatens the card. This is a privacy
    one: the granular per-camera record of a person's day should not outlive
    the 14-day clip trail that could substantiate it.

    Only for dates that already have a report, for the same reason
    prune_superseded_clips() checks: chunks are the report's input, so pruning
    an unmerged day is silent data loss dressed up as housekeeping.
    """
    days = int(os.environ.get("NANNY_CHUNK_RETENTION_DAYS",
                              os.environ.get("NANNY_CLIP_RETENTION_DAYS", "14")))
    floor = date.today() - timedelta(days=days)
    if not os.path.isdir(CHUNKS_DIR):
        return
    for name in sorted(os.listdir(CHUNKS_DIR)):
        try:
            day = date.fromisoformat(name)
        except ValueError:
            continue
        if day >= floor:
            continue
        if not os.path.exists(os.path.join(REPORTS_DIR, f"{name}.json")):
            logging.info("Keeping chunks for %s past retention — no report merged "
                         "them yet", name)
            continue
        shutil.rmtree(os.path.join(CHUNKS_DIR, name), ignore_errors=True)
        logging.info("Pruned chunks for %s (older than %dd)", name, days)


def prune_old_reports():
    """Local reports are a window; Neon (via backup_sync.py) is the archive.

    A year of reports is ~5 MB, so this is not about space either — it keeps
    the local record bounded while the trend view, which reads only local
    files, still has far more history than its 28-day window needs.
    """
    days = int(os.environ.get("NANNY_REPORT_RETENTION_DAYS", "365"))
    floor = date.today() - timedelta(days=days)
    if not os.path.isdir(REPORTS_DIR):
        return
    for name in sorted(os.listdir(REPORTS_DIR)):
        if not name.endswith(".json"):
            continue
        try:
            day = date.fromisoformat(name[:-5])
        except ValueError:
            continue
        if day < floor:
            os.remove(os.path.join(REPORTS_DIR, name))
            logging.info("Pruned local report %s (older than %dd; Neon keeps the "
                         "archive)", name, days)


def prune_superseded_clips():
    """Delete clips no report references.

    merge_phone_events() keeps one clip per event, so the other angles of the
    same moment were cut by the analyzer and are now dead weight for the whole
    retention window.

    Only ever touches a day that HAS a report: clips are cut before the report
    is built, so sweeping an unreported day would delete the day's evidence
    before anything had a chance to reference it.
    """
    if not os.path.isdir(CLIPS_DIR):
        return
    for name in sorted(os.listdir(CLIPS_DIR)):
        day_dir = os.path.join(CLIPS_DIR, name)
        report_path = os.path.join(REPORTS_DIR, f"{name}.json")
        if not os.path.isdir(day_dir) or not os.path.exists(report_path):
            continue
        try:
            with open(report_path) as f:
                report = json.load(f)
        except (OSError, ValueError):
            continue          # unreadable report: keep every clip
        # Notable events carry clips too — leaving them out of this set would
        # delete the evidence for the highest-stakes findings on the page.
        referenced = {os.path.basename(e["clip"])
                      for e in (list(report.get("phone_use", {}).get("events", []))
                                + list(report.get("notable_events", [])))
                      if e.get("clip")}
        for f in os.listdir(day_dir):
            if f not in referenced:
                os.remove(os.path.join(day_dir, f))
                logging.info("Pruned unreferenced clip %s/%s", name, f)


def load_config():
    """Every setting, each with its own fallback. Returns (cameras, rooms,
    window, days, errors).

    Nothing here aborts. A typo in one env line used to exit(1) before the
    straggler sweep even ran, so the whole day silently never reached the
    dashboard — a config error must degrade the report, never delete it. The
    errors travel into the report so the page can show what is misconfigured
    instead of leaving the reader to guess at a missing date.
    """
    errors = []

    def attempt(name, fn, fallback):
        try:
            return fn()
        except ValueError as e:
            logging.error("%s is invalid (%s) — falling back to %r", name, e, fallback)
            errors.append(f"{name}: {e}")
            return fallback

    cameras = attempt("NANNY_CAM_*", load_cameras, {})
    # Chunks carry their own camera and room, so a broken map costs the "silent
    # camera" list and cross-room fusion — not the report.
    rooms   = attempt("NANNY_CAM_ROOMS", lambda: load_camera_rooms(cameras), {})
    window  = attempt("NANNY_WINDOW", load_window, (dtime(10, 0), dtime(18, 0)))
    days    = attempt("NANNY_DAYS", load_days, {0, 1, 2, 3, 4})
    return cameras, rooms, window, days, errors


def care_day_awaiting_report(today, days, window, force=False, reports_dir=None):
    """Is today a care day whose window has closed and that has no report yet?

    Without this a day with zero chunks — analysis down all day, cameras
    unplugged — never appears in unreported_dates() and vanishes from the date
    picker entirely. A day that produced nothing must still say so.
    """
    reports_dir = reports_dir or REPORTS_DIR
    if today.weekday() not in days:
        return False
    if datetime.now().time() < window[1]:
        return False
    if force:
        return True
    return not os.path.exists(os.path.join(reports_dir, f"{today.isoformat()}.json"))


# ── CLI ───────────────────────────────────────────────────────────────────────
#
# With no arguments this is exactly the production run the systemd unit invokes:
# sweep, merge every unreported day, prune. The flags exist so the report can be
# iterated on WITHOUT the three side effects that make the production run
# unsafe to repeat — a paid straggler sweep, cleanup() deleting clips and
# lowres, and the refusal to rebuild a day that already has a report.

def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog="nanny_report.py",
        description="Merge analyzed chunks into daily nanny reports.")
    parser.add_argument("--date", action="append", metavar="YYYY-MM-DD",
                        help="rebuild exactly this day (repeatable) instead of "
                             "auto-selecting unreported days")
    parser.add_argument("--force", action="store_true",
                        help="rebuild days that already have a report")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the report JSON to stdout and change nothing "
                             "on disk; implies --no-sweep and --no-narrative")
    parser.add_argument("--no-sweep", action="store_true",
                        help="skip the analyzer straggler sweep (spends no quota)")
    parser.add_argument("--no-narrative", action="store_true",
                        help="skip the Gemini narrative call")
    parser.add_argument("--narrative", action="store_true",
                        help="generate the narrative even under --dry-run")
    parser.add_argument("--out", metavar="DIR",
                        help="write reports here instead of the live reports dir")
    parser.add_argument("--list", action="store_true", dest="list_days",
                        help="list chunk dates and whether each has a report, then exit")
    args = parser.parse_args(argv)

    # A dry run must never cost money. The sweep is the obvious spender; the
    # narrative is the sneaky one — it is a real Gemini call on every single
    # build, so tuning the report over a dozen runs would quietly bill a dozen
    # times and can trip the per-minute quota.
    if args.dry_run:
        args.no_sweep = True
        if not args.narrative:
            args.no_narrative = True

    args.dates = []
    for raw in args.date or []:
        try:
            args.dates.append(date.fromisoformat(raw))
        except ValueError:
            parser.error(f"--date {raw!r} is not YYYY-MM-DD")
    return args


def list_days(reports_dir):
    """What is on disk: every chunk date, its chunk count, and its report."""
    if not os.path.isdir(CHUNKS_DIR):
        print(f"No chunks directory at {CHUNKS_DIR}")
        return
    names = sorted(n for n in os.listdir(CHUNKS_DIR)
                   if os.path.isdir(os.path.join(CHUNKS_DIR, n)))
    if not names:
        print(f"No chunk dates in {CHUNKS_DIR}")
        return
    print(f"{'date':<12} {'chunks':>6}  report")
    for name in names:
        n = len([f for f in os.listdir(os.path.join(CHUNKS_DIR, name))
                 if f.endswith(".json")])
        has = os.path.exists(os.path.join(reports_dir, f"{name}.json"))
        print(f"{name:<12} {n:>6}  {'yes' if has else 'no'}")


def main(argv=None):
    args = parse_args(argv)
    ensure_dirs()
    cameras, rooms, window, days, config_errors = load_config()
    for err in config_errors:
        # load_config() already logs these, but a dry run is exactly when you
        # want them in front of you rather than buried in journalctl.
        print(f"config: {err}", file=sys.stderr)

    out_dir = args.out or REPORTS_DIR
    if args.list_days:
        list_days(out_dir)
        return

    if not args.no_sweep:
        # Straggler sweep: the last analyzer run may not have finished (or run).
        # Uncapped on purpose — the analyzer's per-run cap exists to spread a
        # backlog over the day, but by report time everything must be in.
        analyze_pending(limit=None)

    today = date.today()
    if args.dates:
        targets = list(args.dates)
    else:
        targets = list(unreported_dates(today, args.force, out_dir))
        if (care_day_awaiting_report(today, days, window, args.force, out_dir)
                and today not in targets):
            # No chunks at all today. Report it as an empty day rather than
            # letting the date disappear from the dashboard.
            targets.append(today)
    if not targets:
        logging.info("No unreported days — nothing to merge.")
        if not args.dry_run:
            cleanup()
        return

    for day in sorted(targets):
        chunks = load_chunks(os.path.join(CHUNKS_DIR, day.isoformat()))
        if not chunks:
            logging.warning("%s: nothing was analyzed for this day — writing an empty "
                            "report so the day is visible with its coverage gaps.", day)
        report = build_report(day, chunks, cameras, window, rooms, config_errors,
                              with_narrative=not args.no_narrative)
        if args.dry_run:
            json.dump(report, sys.stdout, indent=2)
            sys.stdout.write("\n")
        else:
            os.makedirs(out_dir, exist_ok=True)
            atomic_write_json(os.path.join(out_dir, f"{day.isoformat()}.json"), report)
        logging.info("%s: report %s — %d timeline spans, %.0f phone min "
                     "(%.0f while asleep / %.0f flagged / %.0f unclear), "
                     "%.0f min sleep, %.0f min unattended, %d camera(s)",
                     day, "built (dry run)" if args.dry_run else "written",
                     len(report["timeline"]),
                     report["phone_use"]["total_minutes"],
                     report["phone_use"]["while_baby_asleep_minutes"],
                     report["phone_use"]["unauthorized_minutes"],
                     report["phone_use"]["unclear_minutes"],
                     report["sleep"]["total_sleep_minutes"],
                     report["attendance"]["unattended_minutes"],
                     len(report["cameras"]))
        # Status is production bookkeeping about the live report. A dry run
        # wrote nothing, and --out wrote somewhere else; neither may claim one.
        if not args.dry_run and not args.out:
            update_status("report", date=day.isoformat(),
                          phone_minutes=report["phone_use"]["total_minutes"],
                          unauthorized_minutes=report["phone_use"]["unauthorized_minutes"],
                          no_analysis=report["no_analysis"],
                          config_errors=config_errors)

    if not args.dry_run:
        cleanup()


if __name__ == "__main__":
    main()
