"""
Offline replay: run the sleep monitor's exact per-frame pipeline over recorded video
(from record_camera.sh) instead of the live RTSP stream.

Two modes:

  Analysis (default) — per-frame motion/presence numbers + percentiles, for setting
  thresholds from measurements:
    python3 replay_sleep.py recordings/<ts>/*.mp4 --csv out.csv

  Simulation (--simulate) — drive the real SleepStateMachine over the footage with the
  video clock and print the state timeline + detected sleep sessions, for validating the
  whole algorithm against footage whose ground truth you know:
    python3 replay_sleep.py recordings/<ts>/*.mp4 --simulate --roi 0.10 0.07 0.80 1.00

The pipeline (resize → gray → ROI → active_fraction) and the state machine are imported
from sleep_monitor.py, so replay behavior matches the live daemon exactly. Segments are
assumed contiguous (as record_camera.sh produces them); the wall-clock base is parsed
from the recording directory name (YYYYmmdd_HHMMSS) when possible.

Workflow + measured numbers from real footage: docs/sleep-detection-research.md §6.
"""

import argparse
import json
import logging
import os
import re
import sys
from datetime import datetime, timedelta

import cv2
import numpy as np

from sleep_monitor import (
    BOOTSTRAP_FRAMES,
    SleepStateMachine,
    active_fraction,
    build_cfg,
    roi_pixels,
)


def to_roi_gray(frame, roi):
    small = cv2.resize(frame, (320, 240), interpolation=cv2.INTER_AREA)
    gray  = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
    r0, r1, c0, c1 = roi
    return gray[r0:r1, c0:c1]


def sampled_frames(path, interval):
    """Yield (t_seconds_within_file, gray_full_frame) at ~1/interval fps, decoding cheaply."""
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        print(f"!! could not open {path}", file=sys.stderr)
        return
    fps  = cap.get(cv2.CAP_PROP_FPS) or 15.0
    step = max(1, int(round(fps * interval)))
    i = 0
    while True:
        if i % step == 0:
            if not cap.grab():
                break
            ret, frame = cap.retrieve()
            if not ret:
                break
            yield i / fps, frame
        else:
            if not cap.grab():
                break
        i += 1
    cap.release()


def base_time_for(videos):
    """Parse YYYYmmdd_HHMMSS from the recording directory name, else epoch-ish default."""
    m = re.search(r"(\d{8}_\d{6})", os.path.dirname(os.path.abspath(videos[0])))
    if m:
        return datetime.strptime(m.group(1), "%Y%m%d_%H%M%S")
    return datetime(2000, 1, 1)


# ── Analysis mode ─────────────────────────────────────────────────────────────

def pct(a, p):
    return float(np.percentile(a, p)) if len(a) else float("nan")


def run_analysis(videos, roi, reference, interval, csv_path):
    csv_out = open(csv_path, "w") if csv_path else None
    if csv_out:
        csv_out.write("file,t_seconds,motion,presence\n")

    print(f"\n{'file':<28} {'frames':>6} | motion p50/p90/p99/max          | presence p50/p90")
    all_motion = []
    for path in videos:
        motions, presences, prev = [], [], None
        for t, frame in sampled_frames(path, interval):
            gray = to_roi_gray(frame, roi)
            motion   = active_fraction(prev, gray) if prev is not None else 0.0
            presence = active_fraction(gray, reference) if reference is not None else float("nan")
            motions.append(motion)
            presences.append(presence)
            if csv_out:
                csv_out.write(f"{os.path.basename(path)},{t:.1f},{motion:.5f},{presence:.5f}\n")
            prev = gray
        m = np.array(motions[1:])
        all_motion.extend(m)
        pres = (f"{pct(np.array(presences),50):.4f} / {pct(np.array(presences),90):.4f}"
                if reference is not None else "—")
        print(f"{os.path.basename(path):<28} {len(m):>6} | "
              f"{pct(m,50):.5f} / {pct(m,90):.5f} / {pct(m,99):.5f} / "
              f"{m.max() if len(m) else 0:.5f} | {pres}")

    if csv_out:
        csv_out.close()
        print(f"\nPer-frame data written to {csv_path}")

    m = np.array(all_motion)
    if len(m):
        print(f"""
How to read this against the current thresholds:
  sleep_micromotion_fraction — must sit ABOVE the empty-crib motion noise (with a correct
                               crib ROI the measured floor is ~zero) and BELOW sleeping-baby
                               twitch values (~0.002–0.012 measured).
  sleep_motion_fraction      — between sleeping-baby p90 and awake-baby p50.
  sleep_disturbance_fraction — below put-down/pickup peaks (measured 0.6–1.0), above baby
                               twitches AND above brief parent reach-ins (~0.017 measured).
Overall motion percentiles across all files:
  p50={pct(m,50):.5f}  p90={pct(m,90):.5f}  p99={pct(m,99):.5f}  max={m.max():.5f}""")


# ── Simulation mode ───────────────────────────────────────────────────────────

class _DropPerFrame(logging.Filter):
    def filter(self, record):
        return not record.getMessage().startswith("state=")


def run_simulation(videos, roi, interval, verbose, cfg):
    log = logging.getLogger("sim")
    log.setLevel(logging.INFO)
    h = logging.StreamHandler()
    h.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    if not verbose:
        h.addFilter(_DropPerFrame())
    log.addHandler(h)
    log.propagate = False

    base = base_time_for(videos)
    print(f"Simulation clock starts at {base.isoformat()}")

    sessions = []

    def on_start(t, detected_at):
        sessions.append({"start": t, "end": None, "detected": detected_at,
                         "end_detected": None, "reason": None})
        return len(sessions) - 1

    def on_end(sid, t, detected_at, reason):
        if sid is not None and sessions[sid]["end"] is None:
            sessions[sid]["end"] = t
            sessions[sid]["end_detected"] = detected_at
            sessions[sid]["reason"] = reason

    machine = SleepStateMachine(cfg, reference=None, log=log,
                                on_session_start=on_start, on_session_end=on_end)

    transitions = []
    offset = 0.0
    boot = []
    last_state = machine.state
    for path in videos:
        file_dur = 0.0
        for t, frame in sampled_frames(path, interval):
            now  = base + timedelta(seconds=offset + t)
            gray = to_roi_gray(frame, roi)
            if machine.reference is None:
                boot.append(gray.astype(np.float32))          # mirror bootstrap_reference
                if len(boot) >= BOOTSTRAP_FRAMES:
                    machine.reference = np.median(np.stack(boot), axis=0).astype(np.uint8)
                    machine.reference_trusted = False         # bootstrap may contain the baby
                    machine.on_reference_save(machine.reference, False)
                machine.prev = gray
                file_dur = t
                continue
            state = machine.step(gray, now)
            if state != last_state:
                transitions.append((now, last_state, state))
                last_state = state
            file_dur = t
        offset += file_dur + interval
        print(f"  processed {os.path.basename(path)} (sim clock {base + timedelta(seconds=offset)})")

    print("\n── State timeline ──")
    print(f"  {base.strftime('%H:%M:%S')}  start: away")
    for ts, frm, to in transitions:
        print(f"  {ts.strftime('%H:%M:%S')}  {frm} → {to}")
    print(f"  final state: {machine.state}")

    print("\n── Sleep sessions ──")
    if not sessions:
        print("  (none)")
    for s in sessions:
        end = s["end"].strftime("%H:%M:%S") if s["end"] else "OPEN"
        dur = ((s["end"] or base + timedelta(seconds=offset)) - s["start"]).total_seconds() / 60
        print(f"  {s['start'].strftime('%H:%M:%S')} → {end}  ({dur:.1f} min)")


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Replay recorded footage through the sleep-monitor pipeline")
    ap.add_argument("videos", nargs="+", help="video files from record_camera.sh (in order)")
    ap.add_argument("--simulate", action="store_true",
                    help="run the full SleepStateMachine and print timeline + sessions")
    ap.add_argument("--roi", nargs=4, type=float, metavar=("X0", "Y0", "X1", "Y1"),
                    help="crib ROI fractions; default from settings.json sleep_crib_roi")
    ap.add_argument("--reference", help="analysis mode: empty-crib reference (.npy or image)")
    ap.add_argument("--settings", default=os.path.join(os.path.dirname(__file__), "settings.json"),
                    help="settings.json for ROI + thresholds (default: repo settings.json)")
    ap.add_argument("--interval", type=float, default=1.0, help="sampling interval seconds (default 1.0)")
    ap.add_argument("--csv", help="analysis mode: write per-frame file,t,motion,presence rows here")
    ap.add_argument("--verbose", action="store_true", help="simulate mode: keep per-frame log lines")
    args = ap.parse_args()

    settings = {}
    if os.path.exists(args.settings):
        with open(args.settings) as f:
            settings = json.load(f)
    roi_frac = args.roi if args.roi else settings.get("sleep_crib_roi", [0.0, 0.0, 1.0, 1.0])
    roi = roi_pixels(roi_frac)
    print(f"ROI rows {roi[0]}–{roi[1]}, cols {roi[2]}–{roi[3]} (fractions {roi_frac})")

    if args.simulate:
        cfg = build_cfg(settings)
        cfg["crib_roi"] = roi_frac
        run_simulation(args.videos, roi, args.interval, args.verbose, cfg)
        return

    reference = None
    if args.reference:
        if args.reference.endswith(".npy"):
            reference = np.load(args.reference)
        else:
            reference = to_roi_gray(cv2.imread(args.reference), roi)
        if tuple(reference.shape) != (roi[1] - roi[0], roi[3] - roi[2]):
            sys.exit(f"reference shape {reference.shape} does not match ROI — was it saved "
                     "with a different sleep_crib_roi?")
    else:
        print("(no --reference: presence column will be NaN; motion analysis still works)")

    run_analysis(args.videos, roi, reference, args.interval, args.csv)


if __name__ == "__main__":
    main()
