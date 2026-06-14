"""
Sleep monitoring daemon — runs as a separate systemd service (nursery-sleep-monitor).

Algorithm (two signals per frame):
  1. Presence  — MOG2 background subtractor with state-dependent learning rate
                 + morphological blob filtering (baby-sized blob in foreground = present)
  2. Motion    — Farneback optical flow mean magnitude
                 (actual motion vectors, not raw pixel differences)

States: AWAY (not in crib) → AWAKE (in crib, moving) → ASLEEP (in crib, still)

Why MOG2 with state-dependent learning rate:
  ASLEEP → lr=0.0   frozen: sleeping baby never gets absorbed into background
  AWAY   → lr=0.05  fast: empty-crib scene learned within ~20 frames
  AWAKE  → lr=-1    auto: standard adaptation

Configuration (settings.json):
  camera_rtsp_url          — rtsp://user:pass@IP:554/stream2
  sleep_motion_threshold   — mean optical flow magnitude in px/frame (default 0.5)
  sleep_presence_threshold — min foreground blob as fraction of frame (default 0.08)
  sleep_min_minutes        — stillness minutes before marking asleep (default 10)
  sleep_wake_seconds       — sustained motion seconds before marking awake (default 20)
"""

import logging
import os
import time
from datetime import datetime

import cv2
import numpy as np

from storage import (
    CALIBRATE_FLAG,
    end_sleep_session,
    get_open_sleep_session,
    load_settings,
    start_sleep_session,
    write_sleep_heartbeat,
)

try:
    from huckleberry_sync import push_sleep
    HUCKLEBERRY_AVAILABLE = True
except Exception:
    HUCKLEBERRY_AVAILABLE = False

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [sleep_monitor] %(message)s",
)

STATE_AWAY   = "away"
STATE_AWAKE  = "awake"
STATE_ASLEEP = "asleep"

LIGHTING_CEILING = 0.80   # frame-diff fraction above which we assume IR toggle, not motion
FRAME_INTERVAL   = 1.0    # seconds between sampled frames (~1 fps)
WARMUP_FRAMES    = 30     # frames fed at lr=1.0 to init/re-init MOG2 (~30 s)

MORPH_KERNEL = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (7, 7))


# ── MOG2 factory ──────────────────────────────────────────────────────────────

def create_mog2():
    return cv2.createBackgroundSubtractorMOG2(
        history=500,        # frames used for background model
        varThreshold=40,    # higher = less sensitive to small changes
        detectShadows=False
    )


# ── Per-frame signal functions ────────────────────────────────────────────────

def frame_diff_fraction(frame_a, frame_b, pixel_thresh=25):
    """Fraction of pixels that differ by more than pixel_thresh (for lighting guard only)."""
    diff = cv2.absdiff(frame_a, frame_b)
    _, thresh = cv2.threshold(diff, pixel_thresh, 1, cv2.THRESH_BINARY)
    return float(thresh.sum()) / float(thresh.size)


def compute_presence(curr_gray, mog2, lr, min_blob_pixels):
    """True if MOG2 foreground contains a blob at least min_blob_pixels in area."""
    fg = mog2.apply(curr_gray, learningRate=lr)
    fg = cv2.morphologyEx(fg, cv2.MORPH_CLOSE, MORPH_KERNEL)
    contours, _ = cv2.findContours(fg, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    return any(cv2.contourArea(c) > min_blob_pixels for c in contours)


def compute_optical_flow(prev_gray, curr_gray):
    """Mean optical flow magnitude (pixels/frame). 0 = no motion."""
    flow = cv2.calcOpticalFlowFarneback(
        prev_gray, curr_gray, None,
        pyr_scale=0.5, levels=3, winsize=15,
        iterations=3, poly_n=5, poly_sigma=1.2, flags=0
    )
    mag, _ = cv2.cartToPolar(flow[..., 0], flow[..., 1])
    return float(mag.mean())


# ── RTSP capture ──────────────────────────────────────────────────────────────

def open_capture(rtsp_url):
    os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp")
    cap = cv2.VideoCapture(rtsp_url, cv2.CAP_FFMPEG)
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    return cap if cap.isOpened() else None


def read_frame_gray(cap):
    ret, frame = cap.read()
    if not ret or frame is None:
        return None
    small = cv2.resize(frame, (320, 240), interpolation=cv2.INTER_AREA)
    return cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)


def warmup_mog2(cap, mog2, n_frames=WARMUP_FRAMES):
    """Feed n_frames at learningRate=1.0 to quickly build background model."""
    for i in range(n_frames):
        f = read_frame_gray(cap)
        if f is None:
            return False
        mog2.apply(f, learningRate=1.0)
        time.sleep(FRAME_INTERVAL)
    return True


# ── State machine ─────────────────────────────────────────────────────────────

def run_state_machine(rtsp_url, motion_threshold, presence_threshold,
                      sleep_min_seconds, wake_seconds):
    cap = open_capture(rtsp_url)
    if cap is None:
        logging.warning("Could not open RTSP stream: %s", rtsp_url)
        return

    logging.info("RTSP stream opened: %s", rtsp_url)

    # min foreground blob in pixels (presence_threshold is fraction of 320×240)
    min_blob_pixels = int(presence_threshold * 320 * 240)

    # ── MOG2 warmup ───────────────────────────────────────────────────────────
    mog2 = create_mog2()
    logging.info("Initializing background model (%d warmup frames, ~%ds)…",
                 WARMUP_FRAMES, WARMUP_FRAMES)
    if not warmup_mog2(cap, mog2):
        cap.release()
        return
    logging.info("Background model ready — starting state machine")

    # ── Resume open session from DB (daemon restart) ──────────────────────────
    open_session = get_open_sleep_session()
    if open_session:
        state = STATE_ASLEEP
        current_session_id  = open_session["id"]
        t = open_session["start_time"]
        current_sleep_start = t if isinstance(t, datetime) else datetime.fromisoformat(str(t))
        logging.info("Resuming open sleep session id=%s started %s",
                     current_session_id, current_sleep_start)
    else:
        state               = STATE_AWAY
        current_session_id  = None
        current_sleep_start = None

    still_since  = None
    motion_since = None
    away_since   = datetime.now() if state == STATE_AWAY else None
    prev_gray    = None

    try:
        while True:
            loop_start = time.monotonic()

            curr_gray = read_frame_gray(cap)
            if curr_gray is None:
                logging.warning("Frame read failed — RTSP connection dropped")
                break

            # ── Calibration flag (user pressed "Crib is empty" button) ────────
            if os.path.exists(CALIBRATE_FLAG):
                try:
                    os.remove(CALIBRATE_FLAG)
                    logging.info("Calibration requested — re-initializing MOG2 (%d frames)…",
                                 WARMUP_FRAMES)
                    mog2 = create_mog2()
                    warmup_mog2(cap, mog2)
                    logging.info("MOG2 re-initialized via calibration")
                    if state == STATE_ASLEEP and current_session_id:
                        end_sleep_session(current_session_id, datetime.now())
                        current_session_id  = None
                        current_sleep_start = None
                    state        = STATE_AWAY
                    still_since  = None
                    motion_since = None
                    away_since   = datetime.now()
                    prev_gray    = None
                    curr_gray    = read_frame_gray(cap)
                    if curr_gray is None:
                        break
                except Exception as e:
                    logging.warning("Calibration error: %s", e)

            now = datetime.now()

            # ── Lighting-change guard (skip IR toggle frames) ─────────────────
            if prev_gray is not None:
                if frame_diff_fraction(prev_gray, curr_gray) > LIGHTING_CEILING:
                    prev_gray = curr_gray
                    write_sleep_heartbeat(state)
                    time.sleep(max(0.0, FRAME_INTERVAL - (time.monotonic() - loop_start)))
                    continue

            # ── Presence detection (MOG2 + blob filter) ───────────────────────
            if state == STATE_ASLEEP:
                mog2_lr = 0.0    # frozen: sleeping baby never absorbed into background
            elif state == STATE_AWAY:
                mog2_lr = 0.05   # fast: learn current empty-crib scene quickly
            else:
                mog2_lr = -1     # auto: standard MOG2 adaptation

            baby_present = compute_presence(curr_gray, mog2, mog2_lr, min_blob_pixels)

            # ── Motion detection (optical flow) ───────────────────────────────
            if prev_gray is not None:
                flow_mag  = compute_optical_flow(prev_gray, curr_gray)
                is_motion = flow_mag > motion_threshold
            else:
                flow_mag  = 0.0
                is_motion = False
                prev_gray = curr_gray

            # ── State transitions ─────────────────────────────────────────────

            if not baby_present:
                if state == STATE_ASLEEP and current_session_id is not None:
                    end_sleep_session(current_session_id, now)
                    if HUCKLEBERRY_AVAILABLE and current_sleep_start:
                        push_sleep(current_sleep_start, now)
                    logging.info("Baby left frame — ended sleep session %s at %s",
                                 current_session_id, now.isoformat())
                    current_session_id  = None
                    current_sleep_start = None

                if state != STATE_AWAY:
                    logging.info("Baby not detected — AWAY")
                state        = STATE_AWAY
                still_since  = None
                motion_since = None
                if away_since is None:
                    away_since = now

            else:
                away_since = None

                if state == STATE_AWAY:
                    state       = STATE_AWAKE
                    still_since = None
                    logging.info("Baby detected — AWAKE")

                if state == STATE_AWAKE:
                    if not is_motion:
                        if still_since is None:
                            still_since  = now
                            motion_since = None
                        elif (now - still_since).total_seconds() >= sleep_min_seconds:
                            current_sleep_start = still_since
                            current_session_id  = start_sleep_session(still_since)
                            state               = STATE_ASLEEP
                            still_since         = None
                            logging.info("ASLEEP — session %s started at %s",
                                         current_session_id, current_sleep_start)
                    else:
                        still_since = None

                elif state == STATE_ASLEEP:
                    if is_motion:
                        if motion_since is None:
                            motion_since = now
                            still_since  = None
                        elif (now - motion_since).total_seconds() >= wake_seconds:
                            wake_time          = motion_since
                            ended_id           = current_session_id
                            sleep_start_for_hb = current_sleep_start
                            end_sleep_session(ended_id, wake_time)
                            if HUCKLEBERRY_AVAILABLE and sleep_start_for_hb:
                                push_sleep(sleep_start_for_hb, wake_time)
                            state               = STATE_AWAKE
                            current_session_id  = None
                            current_sleep_start = None
                            motion_since        = None
                            logging.info("AWAKE — session %s ended at %s",
                                         ended_id, wake_time.isoformat())
                    else:
                        motion_since = None

            write_sleep_heartbeat(state)
            prev_gray = curr_gray
            elapsed   = time.monotonic() - loop_start
            time.sleep(max(0.0, FRAME_INTERVAL - elapsed))

    finally:
        cap.release()


# ── Reconnect loop ────────────────────────────────────────────────────────────

def main():
    RECONNECT_DELAY = 10

    while True:
        settings = load_settings()
        rtsp_url = settings.get("camera_rtsp_url", "")

        if not rtsp_url:
            logging.info("No camera_rtsp_url in settings.json — retrying in 30s.")
            time.sleep(30)
            continue

        motion_threshold   = float(settings.get("sleep_motion_threshold",   0.5))
        presence_threshold = float(settings.get("sleep_presence_threshold", 0.08))
        sleep_min_seconds  = int(settings.get("sleep_min_minutes",          10)) * 60
        wake_seconds       = int(settings.get("sleep_wake_seconds",         20))

        try:
            run_state_machine(rtsp_url, motion_threshold, presence_threshold,
                              sleep_min_seconds, wake_seconds)
        except Exception as exc:
            logging.error("Unexpected error: %s", exc, exc_info=True)

        logging.info("Reconnecting in %ds…", RECONNECT_DELAY)
        time.sleep(RECONNECT_DELAY)


if __name__ == "__main__":
    main()
