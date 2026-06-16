"""
Sleep monitoring daemon — runs as a separate systemd service (nursery-sleep-monitor).

Algorithm — one robust primitive for both signals:
  active_fraction(a, b) = fraction of pixels that meaningfully changed between two frames,
  after per-pixel thresholding and MORPH_OPEN speck removal. Bounded [0,1], interpretable,
  cheap, and robust to H.264 compression / IR-grain noise (which defeats mean optical flow).

  1. Presence — active_fraction(current, reference_empty_crib): high = baby present.
  2. Motion   — active_fraction(current, previous_frame): high = baby moving.

States: AWAY (not in crib) → AWAKE (in crib, moving) → ASLEEP (in crib, still)

Every frame logs both metrics + thresholds at INFO so you can tune from real numbers:
  sudo journalctl -u nursery-sleep-monitor -f
Watch with the crib empty vs baby-in-crib, then set the thresholds in settings.json to
sit between the observed values. No code change needed to tune.

Calibration: the "📷 Crib is empty" button saves the current frame as the empty-crib
reference. (The button is served by the nursery-tracker Flask service — restart that
service after deploying template changes, not just this daemon.)

Configuration (settings.json):
  camera_rtsp_url          — rtsp://user:pass@IP:554/stream2
  sleep_presence_threshold — fraction of frame differing from reference to count as present (default 0.02)
  sleep_motion_fraction    — fraction of pixels changed vs previous frame to count as moving (default 0.01)
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

LIGHTING_CEILING     = 0.80   # frame-diff fraction above which we assume IR toggle, skip frame
FRAME_INTERVAL       = 1.0    # seconds between sampled frames (~1 fps)
PIXEL_THRESH         = 20     # per-pixel gray-level delta to count as "changed"
REFERENCE_FRAME_FILE = os.path.join(os.path.dirname(__file__), "reference_frame.npy")
DENOISE_KERNEL       = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
REFERENCE_UPDATE_LR  = 0.02   # slow drift of reference toward current (tracks lighting changes)
NOISE_FLOOR_FRACTION = 0.005  # motion below this = scene genuinely still (for reference refine gate)
BOOTSTRAP_FRAMES     = 5      # frames median-averaged on startup to build initial reference (~5s)


# ── Core primitive ────────────────────────────────────────────────────────────

def active_fraction(gray_a, gray_b, pixel_thresh=PIXEL_THRESH):
    """
    Fraction of pixels that meaningfully changed between two frames.
    absdiff → per-pixel threshold → MORPH_OPEN (erode→dilate removes isolated noise specks)
    → count. Robust to compression / IR-grain noise; bounded [0, 1].
    """
    diff = cv2.absdiff(gray_a, gray_b)
    _, mask = cv2.threshold(diff, pixel_thresh, 255, cv2.THRESH_BINARY)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, DENOISE_KERNEL)
    return float(mask.sum() / 255) / float(mask.size)


# ── Reference frame I/O ───────────────────────────────────────────────────────

def save_reference_frame(gray_frame):
    np.save(REFERENCE_FRAME_FILE, gray_frame)


def load_reference_frame():
    if os.path.exists(REFERENCE_FRAME_FILE):
        try:
            return np.load(REFERENCE_FRAME_FILE)
        except Exception:
            return None
    return None


def bootstrap_reference(cap):
    """Median of the first BOOTSTRAP_FRAMES frames — quick startup reference (~5s)."""
    frames = []
    for _ in range(BOOTSTRAP_FRAMES):
        g = read_frame_gray(cap)
        if g is not None:
            frames.append(g.astype(np.float32))
        time.sleep(FRAME_INTERVAL)
    if not frames:
        return None
    ref = np.median(np.stack(frames), axis=0).astype(np.uint8)
    save_reference_frame(ref)
    return ref


def maybe_update_reference(reference_gray, curr_gray, state, motion_frac, presence_threshold):
    """
    Triple-gated slow drift: refine the reference only during trusted-empty periods so it
    tracks lighting changes without ever absorbing a present baby.
      Gate 1: state == AWAY (never absorb a present baby)
      Gate 2: scene genuinely still (motion at noise floor)
      Gate 3: current already closely matches reference (prevents inversion entrenchment)
    """
    if state != STATE_AWAY:
        return reference_gray
    if motion_frac >= NOISE_FLOOR_FRACTION:
        return reference_gray
    if active_fraction(curr_gray, reference_gray) > presence_threshold:
        return reference_gray
    return cv2.addWeighted(reference_gray, 1.0 - REFERENCE_UPDATE_LR,
                           curr_gray,       REFERENCE_UPDATE_LR, 0)


# ── Per-frame signals ─────────────────────────────────────────────────────────

def compute_presence(curr_gray, reference_gray, min_fraction):
    """Active fraction vs empty-crib reference > min_fraction. No reference → AWAY."""
    if reference_gray is None:
        return 0.0, False
    frac = active_fraction(curr_gray, reference_gray)
    return frac, frac > min_fraction


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

def run_state_machine(rtsp_url, presence_threshold, motion_fraction,
                      sleep_min_seconds, wake_seconds, max_session_seconds):
    cap = open_capture(rtsp_url)
    if cap is None:
        logging.warning("Could not open RTSP stream: %s", rtsp_url)
        return

    logging.info("RTSP stream opened: %s", rtsp_url)

    # ── Load or bootstrap reference frame ─────────────────────────────────────
    reference_gray = load_reference_frame()
    if reference_gray is None:
        logging.info("No saved reference — building provisional reference from %d frames (~%ds)…",
                     BOOTSTRAP_FRAMES, BOOTSTRAP_FRAMES)
        reference_gray = bootstrap_reference(cap)
        if reference_gray is not None:
            logging.info("Provisional reference built. Self-corrects during empty-crib periods; "
                         "press 'Crib is empty' to override.")
        else:
            logging.warning("Could not bootstrap reference — camera may not be ready")
    else:
        logging.info("Reference frame loaded from disk")

    # ── Resume open session from DB (daemon restart) ──────────────────────────
    open_session = get_open_sleep_session()
    if open_session:
        state               = STATE_ASLEEP
        current_session_id  = open_session["id"]
        t                   = open_session["start_time"]
        current_sleep_start = t if isinstance(t, datetime) else datetime.fromisoformat(str(t))
        logging.info("Resuming open sleep session id=%s started %s",
                     current_session_id, current_sleep_start)
    else:
        state               = STATE_AWAY
        current_session_id  = None
        current_sleep_start = None

    still_since     = None
    motion_since    = None
    away_since      = datetime.now() if state == STATE_AWAY else None
    prev_gray       = read_frame_gray(cap)  # seed motion diff
    presence_streak = 0   # consecutive frames with baby detected
    absence_streak  = 0   # consecutive frames without baby

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
                    save_reference_frame(curr_gray)
                    reference_gray  = curr_gray
                    logging.info("Reference frame saved manually — empty crib baseline updated")
                    if state == STATE_ASLEEP and current_session_id:
                        end_sleep_session(current_session_id, datetime.now())
                        current_session_id  = None
                        current_sleep_start = None
                    state           = STATE_AWAY
                    still_since     = None
                    motion_since    = None
                    away_since      = datetime.now()
                    presence_streak = 0
                    absence_streak  = 0
                    prev_gray       = curr_gray
                except Exception as e:
                    logging.warning("Calibration error: %s", e)

            now = datetime.now()

            # ── Lighting-change guard (skip IR-toggle frames entirely) ─────────
            if prev_gray is not None:
                if active_fraction(prev_gray, curr_gray, pixel_thresh=25) > LIGHTING_CEILING:
                    prev_gray = curr_gray
                    write_sleep_heartbeat(state)
                    time.sleep(max(0.0, FRAME_INTERVAL - (time.monotonic() - loop_start)))
                    continue

            # ── Presence + motion (same robust primitive) ─────────────────────
            presence_frac, raw_present = compute_presence(curr_gray, reference_gray, presence_threshold)
            motion_frac = active_fraction(prev_gray, curr_gray) if prev_gray is not None else 0.0
            is_motion   = motion_frac > motion_fraction

            # Per-frame visibility for data-driven tuning
            logging.info("state=%s presence=%.4f (thr %.3f) motion=%.4f (thr %.3f)",
                         state, presence_frac, presence_threshold, motion_frac, motion_fraction)

            if raw_present:
                presence_streak += 1
                absence_streak   = 0
            else:
                absence_streak  += 1
                presence_streak  = 0

            # 2-frame hysteresis: avoids single-frame noise flips
            baby_present = presence_streak >= 2
            baby_absent  = absence_streak  >= 2

            # ── Reference self-correction (lighting drift, empty crib only) ───
            reference_gray = maybe_update_reference(
                reference_gray, curr_gray, state, motion_frac, presence_threshold
            )

            # ── State transitions ─────────────────────────────────────────────

            if baby_absent:
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

            elif baby_present:
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
                    # Sanity cap: a session open longer than the cap is almost certainly a
                    # phantom nap on an empty crib (or a missed wake) — force-end it so the
                    # daily total can't balloon. Sized for overnight, not a nap.
                    if (current_sleep_start is not None
                            and (now - current_sleep_start).total_seconds() >= max_session_seconds):
                        ended_id           = current_session_id
                        sleep_start_for_hb = current_sleep_start
                        end_sleep_session(ended_id, now)
                        if HUCKLEBERRY_AVAILABLE and sleep_start_for_hb:
                            push_sleep(sleep_start_for_hb, now)
                        state               = STATE_AWAKE
                        current_session_id  = None
                        current_sleep_start = None
                        motion_since        = None
                        still_since         = None
                        logging.warning("Session %s exceeded %.1fh cap — force-ended at %s",
                                        ended_id, max_session_seconds / 3600, now.isoformat())
                    elif is_motion:
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

        presence_threshold  = float(settings.get("sleep_presence_threshold", 0.02))
        motion_fraction     = float(settings.get("sleep_motion_fraction",    0.01))
        sleep_min_seconds   = int(settings.get("sleep_min_minutes",          10)) * 60
        wake_seconds        = int(settings.get("sleep_wake_seconds",         20))
        max_session_seconds = float(settings.get("sleep_max_session_hours",  14)) * 3600

        try:
            run_state_machine(rtsp_url, presence_threshold, motion_fraction,
                              sleep_min_seconds, wake_seconds, max_session_seconds)
        except Exception as exc:
            logging.error("Unexpected error: %s", exc, exc_info=True)

        logging.info("Reconnecting in %ds…", RECONNECT_DELAY)
        time.sleep(RECONNECT_DELAY)


if __name__ == "__main__":
    main()
