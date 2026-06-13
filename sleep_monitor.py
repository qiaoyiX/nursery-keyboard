"""
Sleep monitoring daemon — runs as a separate systemd service (nursery-sleep-monitor).

Two-stage detection per frame:
  1. Presence:  compare frame vs saved empty-crib background
               → baby is in crib if enough pixels differ from background
  2. Motion:    compare frame vs previous frame
               → baby is awake if pixels are changing

States: AWAY (not in crib) → AWAKE (in crib, moving) → ASLEEP (in crib, still)

Background management:
  - Auto-captured from first 15 frames on startup (assumes crib empty at boot)
  - Auto-refreshed after 5 min in AWAY state (handles day/night lighting changes)
  - Force-refresh: press "Crib is empty" in dashboard → creates calibrate.flag

Configuration (settings.json):
  camera_rtsp_url          — rtsp://user:pass@IP:554/stream2
  sleep_motion_threshold   — frame-to-frame diff fraction (default 0.02)
  sleep_presence_threshold — vs-background diff fraction (default 0.05)
  sleep_min_minutes        — stillness minutes before marking asleep (default 10)
  sleep_wake_seconds       — motion seconds before marking awake (default 20)
"""

import logging
import os
import time
from datetime import datetime

import cv2
import numpy as np

from storage import (
    BACKGROUND_FILE,
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
except ImportError:
    HUCKLEBERRY_AVAILABLE = False

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [sleep_monitor] %(message)s",
)

STATE_AWAY   = "away"
STATE_AWAKE  = "awake"
STATE_ASLEEP = "asleep"

PIXEL_THRESHOLD   = 25    # per-pixel brightness change to count as "different"
LIGHTING_CEILING  = 0.80  # fraction above which we assume IR/light toggle, not motion
FRAME_INTERVAL    = 1.0   # seconds between sampled frames (~1 fps)
WARMUP_FRAMES     = 15    # frames averaged to build initial background
BG_SAVE_INTERVAL  = 30    # seconds between background persistence to disk


# ── Frame helpers ─────────────────────────────────────────────────────────────

def compute_diff_fraction(frame_a, frame_b):
    """Fraction of pixels that differ by more than PIXEL_THRESHOLD."""
    diff = cv2.absdiff(frame_a, frame_b)
    _, thresh = cv2.threshold(diff, PIXEL_THRESHOLD, 1, cv2.THRESH_BINARY)
    return float(thresh.sum()) / float(thresh.size)


def is_lighting_change(fraction):
    """IR night-vision switches and sudden light-on events change nearly every pixel."""
    return fraction > LIGHTING_CEILING


# ── Background management ─────────────────────────────────────────────────────

def load_background():
    if os.path.exists(BACKGROUND_FILE):
        return np.load(BACKGROUND_FILE).astype(np.float32)
    return None


def save_background(gray_frame):
    np.save(BACKGROUND_FILE, gray_frame.astype(np.float32))
    logging.info("Background frame saved to %s", BACKGROUND_FILE)


def background_to_uint8(background):
    return np.clip(background, 0, 255).astype(np.uint8)


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


# ── State machine ─────────────────────────────────────────────────────────────

def run_state_machine(rtsp_url, motion_threshold, presence_threshold,
                      sleep_min_seconds, wake_seconds):
    cap = open_capture(rtsp_url)
    if cap is None:
        logging.warning("Could not open RTSP stream: %s", rtsp_url)
        return

    logging.info("RTSP stream opened: %s", rtsp_url)

    # ── Background warmup ─────────────────────────────────────────────────────
    background = load_background()
    if background is None:
        logging.info("No background found — collecting %d warmup frames (crib should be empty)…",
                     WARMUP_FRAMES)
        warmup = []
        while len(warmup) < WARMUP_FRAMES:
            frame = read_frame_gray(cap)
            if frame is None:
                cap.release()
                return
            warmup.append(frame.astype(np.float32))
            time.sleep(FRAME_INTERVAL)
        background = np.mean(warmup, axis=0)
        save_background(background)
        logging.info("Initial background captured from %d frames", WARMUP_FRAMES)

    bg_uint8 = background_to_uint8(background)

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

    still_since   = None
    motion_since  = None
    away_since    = datetime.now() if state == STATE_AWAY else None
    prev_gray     = None
    last_bg_save  = datetime.now()

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
                    save_background(curr_gray)
                    background = curr_gray.astype(np.float32)
                    bg_uint8   = background_to_uint8(background)
                    logging.info("Background updated via calibration request")
                    # Reset to AWAY since crib is declared empty
                    if state == STATE_ASLEEP and current_session_id:
                        end_sleep_session(current_session_id, datetime.now())
                        current_session_id  = None
                        current_sleep_start = None
                    state        = STATE_AWAY
                    still_since  = None
                    motion_since = None
                    away_since   = datetime.now()
                except Exception as e:
                    logging.warning("Calibration error: %s", e)

            now = datetime.now()

            # ── Lighting-change guard (skip IR toggle frames) ─────────────────
            if prev_gray is not None:
                frame_diff = compute_diff_fraction(prev_gray, curr_gray)
                if is_lighting_change(frame_diff):
                    prev_gray = curr_gray
                    write_sleep_heartbeat(state)
                    time.sleep(max(0.0, FRAME_INTERVAL - (time.monotonic() - loop_start)))
                    continue

            # ── Presence detection ────────────────────────────────────────────
            presence_fraction = compute_diff_fraction(curr_gray, bg_uint8)
            baby_present      = presence_fraction > presence_threshold

            # ── Motion detection (frame-to-frame) ────────────────────────────
            if prev_gray is not None:
                motion_fraction = compute_diff_fraction(prev_gray, curr_gray)
                is_motion       = motion_fraction > motion_threshold
            else:
                is_motion = False
                prev_gray = curr_gray

            # ── State transitions ─────────────────────────────────────────────

            if not baby_present:
                # Baby not in frame
                if state == STATE_ASLEEP and current_session_id is not None:
                    # Baby was picked up while sleeping — close the session now
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
                # Baby is present
                away_since = None

                if state == STATE_AWAY:
                    state       = STATE_AWAKE
                    still_since = None
                    logging.info("Baby detected in frame — AWAKE")

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

            # Adaptive background update — rate depends on state
            if state == STATE_AWAY:
                bg_alpha = 0.02      # fast: syncs to current empty-crib lighting in ~1 min
            elif state == STATE_AWAKE:
                bg_alpha = 0.0005    # slow: lighting drift only
            else:                    # STATE_ASLEEP
                bg_alpha = 0.0001    # very slow: allows self-healing if background is wrong
                                     # (e.g. background was captured with baby in crib)
                                     # ~4.7 hr half-life → normal naps safe, deadlock breaks

            if bg_alpha > 0:
                background = (1 - bg_alpha) * background + bg_alpha * curr_gray.astype(np.float32)
                bg_uint8   = background_to_uint8(background)

            if (now - last_bg_save).total_seconds() >= BG_SAVE_INTERVAL:
                save_background(background)
                last_bg_save = now

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

        motion_threshold   = float(settings.get("sleep_motion_threshold",   0.02))
        presence_threshold = float(settings.get("sleep_presence_threshold", 0.05))
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
