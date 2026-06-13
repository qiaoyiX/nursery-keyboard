"""
Sleep monitoring daemon — runs as a separate systemd service (nursery-sleep-monitor).

Reads the RTSP stream from a TAPO C110/C210 camera at ~1fps, detects motion via
frame differencing, and maintains a sleep/awake state machine. Sleep sessions are
written to the shared storage layer (Postgres or sleep_sessions.json).

Configuration (settings.json):
  camera_rtsp_url        — rtsp://user:pass@IP:554/stream2
  sleep_motion_threshold — fraction of pixels that must change (default 0.02)
  sleep_min_minutes      — stillness minutes before marking asleep (default 10)
  sleep_wake_seconds     — sustained motion seconds before marking awake (default 20)
"""

import logging
import os
import time
from datetime import datetime

import cv2
import numpy as np

from storage import (
    end_sleep_session,
    get_open_sleep_session,
    load_settings,
    start_sleep_session,
    write_sleep_heartbeat,
)

try:
    from huckleberry_sync import push_sleep
    HUCKLEBERRY_AVAILABLE = True
except ImportError:
    HUCKLEBERRY_AVAILABLE = False

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [sleep_monitor] %(message)s",
)

STATE_AWAKE  = "awake"
STATE_ASLEEP = "asleep"

PIXEL_THRESHOLD   = 25    # per-pixel diff to count as "changed"
LIGHTING_CEILING  = 0.80  # fraction above which we assume IR/light toggle, not real motion
FRAME_INTERVAL    = 1.0   # seconds between sampled frames


# ── Motion analysis ───────────────────────────────────────────────────────────

def compute_motion_fraction(prev_gray, curr_gray):
    diff = cv2.absdiff(prev_gray, curr_gray)
    _, thresh = cv2.threshold(diff, PIXEL_THRESHOLD, 1, cv2.THRESH_BINARY)
    return float(thresh.sum()) / float(thresh.size)


def is_lighting_change(fraction):
    """IR night-vision switches and sudden lamp-on events flip nearly every pixel."""
    return fraction > LIGHTING_CEILING


# ── RTSP capture ──────────────────────────────────────────────────────────────

def open_capture(rtsp_url):
    # Force TCP transport to avoid UDP packet loss on a home LAN
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


# ── State machine ─────────────────────────────────────────────────────────────

def run_state_machine(rtsp_url, motion_threshold, sleep_min_seconds, wake_seconds):
    cap = open_capture(rtsp_url)
    if cap is None:
        logging.warning("Could not open RTSP stream: %s", rtsp_url)
        return

    logging.info("RTSP stream opened: %s", rtsp_url)

    # Resume an open session if the daemon was restarted mid-sleep
    open_session = get_open_sleep_session()
    if open_session:
        state = STATE_ASLEEP
        current_session_id = open_session["id"]
        logging.info("Resuming open sleep session id=%s started %s",
                     current_session_id, open_session["start_time"])
    else:
        state = STATE_AWAKE
        current_session_id = None

    still_since         = None   # datetime when continuous stillness streak began
    motion_since        = None   # datetime when continuous motion streak began
    current_sleep_start = None   # backdated start of current sleep session
    prev_gray           = None

    try:
        while True:
            loop_start = time.monotonic()

            curr_gray = read_frame_gray(cap)
            if curr_gray is None:
                logging.warning("Frame read failed — RTSP connection dropped")
                break

            write_sleep_heartbeat()

            if prev_gray is None:
                prev_gray = curr_gray
                time.sleep(max(0.0, FRAME_INTERVAL - (time.monotonic() - loop_start)))
                continue

            fraction  = compute_motion_fraction(prev_gray, curr_gray)

            if is_lighting_change(fraction):
                # Skip lighting transients; don't reset hysteresis streaks
                prev_gray = curr_gray
                time.sleep(max(0.0, FRAME_INTERVAL - (time.monotonic() - loop_start)))
                continue

            is_motion = fraction > motion_threshold
            now = datetime.now()

            if state == STATE_AWAKE:
                if not is_motion:
                    if still_since is None:
                        still_since  = now
                        motion_since = None
                    elif (now - still_since).total_seconds() >= sleep_min_seconds:
                        # Backdate sleep start to when stillness actually began
                        current_sleep_start = still_since
                        current_session_id  = start_sleep_session(still_since)
                        state               = STATE_ASLEEP
                        still_since         = None
                        logging.info("ASLEEP — session %s started at %s",
                                     current_session_id, current_sleep_start)
                else:
                    still_since = None  # any motion resets the stillness streak

            elif state == STATE_ASLEEP:
                if is_motion:
                    if motion_since is None:
                        motion_since = now
                        still_since  = None
                    elif (now - motion_since).total_seconds() >= wake_seconds:
                        # Backdate wake time to when motion actually started
                        wake_time           = motion_since
                        ended_id            = current_session_id
                        sleep_start_for_hb  = current_sleep_start
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
                    motion_since = None  # stillness resets the motion streak

            prev_gray = curr_gray
            elapsed   = time.monotonic() - loop_start
            time.sleep(max(0.0, FRAME_INTERVAL - elapsed))

    finally:
        cap.release()


# ── Reconnect loop ────────────────────────────────────────────────────────────

def main():
    RECONNECT_DELAY = 10

    while True:
        settings       = load_settings()
        rtsp_url       = settings.get("camera_rtsp_url", "")

        if not rtsp_url:
            logging.info("No camera_rtsp_url in settings.json — set it and restart. Retrying in 30s.")
            time.sleep(30)
            continue

        motion_threshold  = float(settings.get("sleep_motion_threshold", 0.02))
        sleep_min_seconds = int(settings.get("sleep_min_minutes", 10)) * 60
        wake_seconds      = int(settings.get("sleep_wake_seconds", 20))

        try:
            run_state_machine(rtsp_url, motion_threshold, sleep_min_seconds, wake_seconds)
        except Exception as exc:
            logging.error("Unexpected error: %s", exc, exc_info=True)

        logging.info("Reconnecting in %ds…", RECONNECT_DELAY)
        time.sleep(RECONNECT_DELAY)


if __name__ == "__main__":
    main()
