"""
Offline replay: run the sleep monitor's exact per-frame signal pipeline over recorded
video (from record_camera.sh) instead of the live RTSP stream.

This is how thresholds get tuned from real data without touching the live daemon:

  1. On the Pi:  bash record_camera.sh 120        (capture 2h; do it once with the crib
     empty for a while, once with the baby asleep, and ideally across a put-down/pickup)
  2. Copy recordings/ to any machine with opencv installed (pip install opencv-python)
  3. python3 replay_sleep.py recordings/<ts>/*.mp4 --csv out.csv
  4. Read the summary: it reports the observed motion/presence distributions per file and
     suggests where sleep_micromotion_fraction / sleep_motion_fraction /
     sleep_disturbance_fraction should sit. Annotate what actually happened in each
     recording (empty / asleep / awake / pickup) — the numbers only mean something
     against ground truth you know.

The pipeline (resize → gray → ROI → active_fraction) is imported from sleep_monitor.py,
so replay numbers match live daemon numbers exactly.
"""

import argparse
import json
import os
import sys

import cv2
import numpy as np

from sleep_monitor import active_fraction, roi_pixels


def to_roi_gray(frame, roi):
    small = cv2.resize(frame, (320, 240), interpolation=cv2.INTER_AREA)
    gray  = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
    r0, r1, c0, c1 = roi
    return gray[r0:r1, c0:c1]


def replay_file(path, roi, reference, sample_interval, csv_out):
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        print(f"!! could not open {path}", file=sys.stderr)
        return None
    fps = cap.get(cv2.CAP_PROP_FPS) or 15.0
    step = max(1, int(round(fps * sample_interval)))  # sample at ~1 fps like the daemon

    motions, presences = [], []
    prev = None
    frame_idx = 0
    while True:
        ret, frame = cap.read()
        if not ret:
            break
        if frame_idx % step:
            frame_idx += 1
            continue
        gray = to_roi_gray(frame, roi)
        t = frame_idx / fps
        motion   = active_fraction(prev, gray) if prev is not None else 0.0
        presence = active_fraction(gray, reference) if reference is not None else float("nan")
        motions.append(motion)
        presences.append(presence)
        if csv_out:
            csv_out.write(f"{os.path.basename(path)},{t:.1f},{motion:.5f},{presence:.5f}\n")
        prev = gray
        frame_idx += 1
    cap.release()
    return np.array(motions[1:]), np.array(presences)  # drop the seeded 0.0 motion


def pct(a, p):
    return float(np.percentile(a, p)) if len(a) else float("nan")


def main():
    ap = argparse.ArgumentParser(description="Replay recorded footage through the sleep-monitor pipeline")
    ap.add_argument("videos", nargs="+", help="video files from record_camera.sh")
    ap.add_argument("--reference", help="empty-crib reference: a .npy (from the daemon) or an image file")
    ap.add_argument("--settings", default=os.path.join(os.path.dirname(__file__), "settings.json"),
                    help="settings.json to take sleep_crib_roi from (default: repo settings.json)")
    ap.add_argument("--interval", type=float, default=1.0, help="sampling interval seconds (default 1.0)")
    ap.add_argument("--csv", help="write per-frame file,t,motion,presence rows here")
    args = ap.parse_args()

    roi_frac = [0.0, 0.0, 1.0, 1.0]
    if os.path.exists(args.settings):
        with open(args.settings) as f:
            roi_frac = json.load(f).get("sleep_crib_roi", roi_frac)
    roi = roi_pixels(roi_frac)
    print(f"ROI rows {roi[0]}–{roi[1]}, cols {roi[2]}–{roi[3]} (fractions {roi_frac})")

    reference = None
    if args.reference:
        if args.reference.endswith(".npy"):
            reference = np.load(args.reference)
        else:
            img = cv2.imread(args.reference)
            reference = to_roi_gray(img, roi)
        if tuple(reference.shape) != (roi[1] - roi[0], roi[3] - roi[2]):
            sys.exit(f"reference shape {reference.shape} does not match ROI — was it saved with a different sleep_crib_roi?")
    else:
        print("(no --reference: presence column will be NaN; motion analysis still works)")

    csv_out = open(args.csv, "w") if args.csv else None
    if csv_out:
        csv_out.write("file,t_seconds,motion,presence\n")

    print(f"\n{'file':<28} {'frames':>6} | motion p50/p90/p99/max          | presence p50/p90")
    all_motion = []
    for path in args.videos:
        result = replay_file(path, roi, reference, args.interval, csv_out)
        if result is None:
            continue
        m, p = result
        all_motion.extend(m)
        pres = f"{pct(p,50):.4f} / {pct(p,90):.4f}" if reference is not None else "—"
        print(f"{os.path.basename(path):<28} {len(m):>6} | "
              f"{pct(m,50):.5f} / {pct(m,90):.5f} / {pct(m,99):.5f} / {m.max() if len(m) else 0:.5f} | {pres}")

    if csv_out:
        csv_out.close()
        print(f"\nPer-frame data written to {args.csv}")

    m = np.array(all_motion)
    if len(m):
        print(f"""
How to read this against the current thresholds:
  sleep_micromotion_fraction — must sit ABOVE the empty-crib motion p99 (noise floor)
                               and BELOW typical sleeping-baby twitch values.
  sleep_motion_fraction      — should sit between sleeping-baby p90 and awake-baby p50.
  sleep_disturbance_fraction — should be BELOW pickup/put-down peaks (check max on a
                               recording with a real pickup) and above awake-baby p99.
Overall motion percentiles across all files:
  p50={pct(m,50):.5f}  p90={pct(m,90):.5f}  p99={pct(m,99):.5f}  max={m.max():.5f}""")


if __name__ == "__main__":
    main()
