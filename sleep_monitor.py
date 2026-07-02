"""
Sleep monitoring daemon — runs as a separate systemd service (nursery-sleep-monitor).

v5 algorithm — event-gated latched presence (see docs/sleep-detection-research.md for the
full rationale and the failure history of v1–v4):

  Presence (baby in crib vs empty) is a LATCHED state, never re-decided per frame. A baby
  cannot enter or leave the crib without a parent-scale disturbance, so presence changes
  only through four explicit paths:

    1. Settle evaluation — after a disturbance (motion ≥ sleep_disturbance_fraction for
       2+ frames) quiets down for sleep_settle_seconds: if the settled frame matches the
       empty-crib reference → AWAY (and the reference is refreshed from the settled frame);
       otherwise → AWAKE on PROBATION: micro-motion must appear within
       sleep_probation_minutes or the "presence" is ruled a bedding ghost → AWAY.
    2. Micro-motion override — while AWAY, repeated micro-motion frames (living-thing
       evidence, needs no reference) flip to AWAKE.
    3. Manual "📷 Crib is empty" button — saves reference, forces AWAY.
    4. sleep_max_session_hours cap — backstop force-end.

  Between those events the latch holds no matter what the reference comparison says, which
  is what makes stale/poisoned references self-healing instead of fatal.

  Both signals use one robust primitive: active_fraction(a, b) = fraction of pixels that
  meaningfully changed (absdiff → per-pixel threshold → MORPH_OPEN speck removal), computed
  inside the crib ROI (sleep_crib_roi). Motion = vs previous frame; presence = vs reference.

States: AWAY (not in crib) → AWAKE (in crib, moving) → ASLEEP (in crib, still)

Every frame logs all metrics + thresholds at INFO so you can tune from real numbers:
  sudo journalctl -u nursery-sleep-monitor -f
Tuning order (details in docs/sleep-detection-research.md §6): noise floor first, then
micro-motion sensitivity, then disturbance detection, then one full nap end-to-end.

Configuration (settings.json):
  camera_rtsp_url            — rtsp://user:pass@IP:554/stream2
  sleep_crib_roi             — [x0, y0, x1, y1] crib region as 0–1 fractions (default full frame)
  sleep_presence_threshold   — ROI fraction differing from reference = "occupied" hint (default 0.02)
  sleep_motion_fraction      — ROI fraction changed vs prev frame = "moving/awake" (default 0.01)
  sleep_micromotion_fraction — ROI fraction = living-thing micro-motion (default 0.002)
  sleep_disturbance_fraction — ROI fraction = parent-scale disturbance (default 0.10)
  sleep_settle_seconds       — quiet seconds ending a disturbance episode (default 10)
  sleep_probation_minutes    — micro-motion deadline after an ambiguous settle (default 15)
  sleep_min_minutes          — stillness minutes before marking asleep (default 10)
  sleep_wake_seconds         — sustained motion seconds before marking awake (default 20)
  sleep_max_session_hours    — force-end cap on an open session (default 14)
"""

import json
import logging
import os
import time
from collections import deque
from datetime import datetime, timedelta

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
REFERENCE_META_FILE  = os.path.join(os.path.dirname(__file__), "reference_frame_meta.json")
DENOISE_KERNEL       = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
REFERENCE_UPDATE_LR  = 0.02   # slow drift of reference toward current during trusted-empty periods
BOOTSTRAP_FRAMES     = 5      # frames median-averaged on startup to build initial reference (~5s)
DISTURBANCE_FRAMES   = 2      # consecutive disturbance-level frames to open an episode
MICRO_OVERRIDE_EVENTS = 3     # micro-motion frames within the window to flip AWAY → AWAKE
MICRO_OVERRIDE_WINDOW = 600   # seconds (rolling) for the AWAY micro-motion override


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


# ── Reference frame I/O (with metadata + shape validation) ────────────────────

def save_reference_frame(gray_frame):
    np.save(REFERENCE_FRAME_FILE, gray_frame)
    try:
        with open(REFERENCE_META_FILE, "w") as f:
            json.dump({"saved_at": datetime.now().isoformat(),
                       "shape": list(gray_frame.shape)}, f)
    except OSError as e:
        logging.warning("Could not write reference metadata: %s", e)


def load_reference_frame(expected_shape):
    """Load the saved reference; discard it if its shape doesn't match the current ROI."""
    if not os.path.exists(REFERENCE_FRAME_FILE):
        return None
    try:
        ref = np.load(REFERENCE_FRAME_FILE)
    except Exception:
        return None
    if expected_shape is not None and tuple(ref.shape) != tuple(expected_shape):
        logging.warning("Saved reference shape %s != current ROI %s (ROI or camera changed) "
                        "— discarding, will bootstrap fresh", ref.shape, expected_shape)
        return None
    return ref


def bootstrap_reference(cap, roi):
    """Median of the first BOOTSTRAP_FRAMES frames — quick startup reference (~5s)."""
    frames = []
    for _ in range(BOOTSTRAP_FRAMES):
        g = read_frame_gray(cap, roi)
        if g is not None:
            frames.append(g.astype(np.float32))
        time.sleep(FRAME_INTERVAL)
    if not frames:
        return None
    ref = np.median(np.stack(frames), axis=0).astype(np.uint8)
    save_reference_frame(ref)
    return ref


def maybe_update_reference(reference_gray, curr_gray, state, motion_frac,
                           micromotion_fraction, presence_threshold):
    """
    Triple-gated slow drift: refine the reference only during trusted-empty periods so it
    tracks lighting changes without ever absorbing a present baby.
      Gate 1: state == AWAY (never absorb a present baby)
      Gate 2: scene genuinely still (motion below the micro-motion floor)
      Gate 3: current already closely matches reference (prevents inversion entrenchment)
    """
    if state != STATE_AWAY:
        return reference_gray
    if motion_frac >= micromotion_fraction:
        return reference_gray
    if active_fraction(curr_gray, reference_gray) > presence_threshold:
        return reference_gray
    return cv2.addWeighted(reference_gray, 1.0 - REFERENCE_UPDATE_LR,
                           curr_gray,       REFERENCE_UPDATE_LR, 0)


# ── RTSP capture ──────────────────────────────────────────────────────────────

def open_capture(rtsp_url):
    os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp")
    cap = cv2.VideoCapture(rtsp_url, cv2.CAP_FFMPEG)
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    return cap if cap.isOpened() else None


def roi_pixels(roi_fractions):
    """[x0, y0, x1, y1] fractions → pixel slice bounds on the 320×240 frame."""
    try:
        x0, y0, x1, y1 = [float(v) for v in roi_fractions]
    except (TypeError, ValueError):
        x0, y0, x1, y1 = 0.0, 0.0, 1.0, 1.0
    x0, y0 = max(0.0, min(x0, 1.0)), max(0.0, min(y0, 1.0))
    x1, y1 = max(0.0, min(x1, 1.0)), max(0.0, min(y1, 1.0))
    if x1 - x0 < 0.1 or y1 - y0 < 0.1:   # degenerate ROI → full frame
        x0, y0, x1, y1 = 0.0, 0.0, 1.0, 1.0
    return (int(round(y0 * 240)), int(round(y1 * 240)),
            int(round(x0 * 320)), int(round(x1 * 320)))


def read_frame_gray(cap, roi):
    ret, frame = cap.read()
    if not ret or frame is None:
        return None
    small = cv2.resize(frame, (320, 240), interpolation=cv2.INTER_AREA)
    gray  = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
    r0, r1, c0, c1 = roi
    return gray[r0:r1, c0:c1]


# ── State machine ─────────────────────────────────────────────────────────────

def run_state_machine(rtsp_url, cfg):
    roi = roi_pixels(cfg["crib_roi"])
    cap = open_capture(rtsp_url)
    if cap is None:
        logging.warning("Could not open RTSP stream: %s", rtsp_url)
        return

    logging.info("RTSP stream opened: %s (ROI rows %d–%d, cols %d–%d)", rtsp_url, *roi)

    prev_gray = read_frame_gray(cap, roi)  # seed motion diff + validate reference shape

    # ── Load or bootstrap reference frame ─────────────────────────────────────
    expected_shape = prev_gray.shape if prev_gray is not None else None
    reference_gray = load_reference_frame(expected_shape)
    if reference_gray is None:
        logging.info("No usable saved reference — building provisional reference from %d frames…",
                     BOOTSTRAP_FRAMES)
        reference_gray = bootstrap_reference(cap, roi)
        if reference_gray is not None:
            logging.info("Provisional reference built. If the crib was NOT empty just now, the "
                         "first pickup will self-correct it; or press 'Crib is empty' to override.")
        else:
            logging.warning("Could not bootstrap reference — camera may not be ready")
    else:
        logging.info("Reference frame loaded from disk")

    # ── Resume open session from DB (daemon restart / RTSP reconnect) ─────────
    open_session = get_open_sleep_session()
    if open_session:
        state               = STATE_ASLEEP
        current_session_id  = open_session["id"]
        t                   = open_session["start_time"]
        current_sleep_start = t if isinstance(t, datetime) else datetime.fromisoformat(str(t))
        # We may have missed a pickup while offline — demand micro-motion to keep the session.
        probation_deadline  = datetime.now() + timedelta(minutes=cfg["probation_minutes"])
        probation_anchor    = datetime.now()
        logging.info("Resuming open sleep session id=%s started %s (on probation until %s)",
                     current_session_id, current_sleep_start, probation_deadline.isoformat())
    else:
        state               = STATE_AWAY
        current_session_id  = None
        current_sleep_start = None
        probation_deadline  = None
        probation_anchor    = None

    still_since        = None
    motion_since       = None
    dist_streak        = 0      # consecutive disturbance-level frames
    in_disturbance     = False
    disturbance_start  = None
    settle_quiet_since = None   # first sub-disturbance frame after an episode
    micro_events       = deque()  # timestamps of micro-motion frames (AWAY override window)

    def close_session(end_time, why):
        nonlocal current_session_id, current_sleep_start
        if current_session_id is not None:
            end_sleep_session(current_session_id, end_time)
            if HUCKLEBERRY_AVAILABLE and current_sleep_start:
                push_sleep(current_sleep_start, end_time)
            logging.info("Sleep session %s ended at %s (%s)",
                         current_session_id, end_time.isoformat(), why)
        current_session_id  = None
        current_sleep_start = None

    try:
        while True:
            loop_start = time.monotonic()

            curr_gray = read_frame_gray(cap, roi)
            if curr_gray is None:
                logging.warning("Frame read failed — RTSP connection dropped")
                break

            now = datetime.now()

            # ── Calibration flag (user pressed "Crib is empty" button) ────────
            if os.path.exists(CALIBRATE_FLAG):
                try:
                    os.remove(CALIBRATE_FLAG)
                    save_reference_frame(curr_gray)
                    reference_gray = curr_gray
                    logging.info("Reference frame saved manually — empty crib baseline updated")
                    close_session(now, "manual calibration")
                    state              = STATE_AWAY
                    still_since        = None
                    motion_since       = None
                    dist_streak        = 0
                    in_disturbance     = False
                    settle_quiet_since = None
                    probation_deadline = None
                    micro_events.clear()
                    prev_gray          = curr_gray
                except Exception as e:
                    logging.warning("Calibration error: %s", e)

            # ── Lighting-change guard (skip IR-toggle frames entirely) ─────────
            if prev_gray is not None:
                if active_fraction(prev_gray, curr_gray, pixel_thresh=25) > LIGHTING_CEILING:
                    prev_gray = curr_gray
                    write_sleep_heartbeat(state)
                    time.sleep(max(0.0, FRAME_INTERVAL - (time.monotonic() - loop_start)))
                    continue

            # ── Per-frame signals ──────────────────────────────────────────────
            motion_frac   = active_fraction(prev_gray, curr_gray) if prev_gray is not None else 0.0
            presence_frac = (active_fraction(curr_gray, reference_gray)
                             if reference_gray is not None else 0.0)
            is_disturb    = motion_frac >= cfg["disturbance_fraction"]
            is_micro      = motion_frac > cfg["micromotion_fraction"]
            is_motion     = motion_frac > cfg["motion_fraction"]

            flags = []
            if in_disturbance:
                flags.append("disturbance")
            if probation_deadline is not None:
                flags.append("probation")
            logging.info("state=%s presence=%.4f (thr %.3f) motion=%.4f (thr %.3f) "
                         "micro=%.3f dist=%.3f%s",
                         state, presence_frac, cfg["presence_threshold"],
                         motion_frac, cfg["motion_fraction"],
                         cfg["micromotion_fraction"], cfg["disturbance_fraction"],
                         " [" + ",".join(flags) + "]" if flags else "")

            # ── Disturbance episode tracking (Path 1 trigger) ─────────────────
            if is_disturb:
                dist_streak       += 1
                settle_quiet_since = None
                if not in_disturbance and dist_streak >= DISTURBANCE_FRAMES:
                    in_disturbance    = True
                    disturbance_start = now - timedelta(seconds=(DISTURBANCE_FRAMES - 1) * FRAME_INTERVAL)
                    logging.info("Disturbance started (motion %.3f ≥ %.3f)",
                                 motion_frac, cfg["disturbance_fraction"])
                    # A parent handling the crib is a wake by definition.
                    if state == STATE_ASLEEP:
                        close_session(disturbance_start, "disturbance")
                        state = STATE_AWAKE
                    still_since        = None
                    motion_since       = None
                    probation_deadline = None
            else:
                dist_streak = 0
                if in_disturbance:
                    if settle_quiet_since is None:
                        settle_quiet_since = now
                    elif (now - settle_quiet_since).total_seconds() >= cfg["settle_seconds"]:
                        # ── Settle evaluation (Path 1) ─────────────────────────
                        in_disturbance     = False
                        settle_quiet_since = None
                        if reference_gray is not None and presence_frac <= cfg["presence_threshold"]:
                            close_session(now, "settled empty")  # safety; normally closed already
                            state = STATE_AWAY
                            save_reference_frame(curr_gray)
                            reference_gray = curr_gray
                            micro_events.clear()
                            still_since = motion_since = None
                            logging.info("Settle: crib empty (presence %.4f ≤ %.3f) — AWAY, "
                                         "reference refreshed", presence_frac, cfg["presence_threshold"])
                        else:
                            state              = STATE_AWAKE
                            still_since        = None
                            motion_since       = None
                            probation_deadline = now + timedelta(minutes=cfg["probation_minutes"])
                            probation_anchor   = now
                            logging.info("Settle: presence %.4f suggests occupied — AWAKE on "
                                         "probation until %s (micro-motion must confirm)",
                                         presence_frac, probation_deadline.isoformat())

            if in_disturbance:
                # Parent motion must not read as baby motion — suspend normal transitions.
                write_sleep_heartbeat(state)
                prev_gray = curr_gray
                time.sleep(max(0.0, FRAME_INTERVAL - (time.monotonic() - loop_start)))
                continue

            # ── Micro-motion bookkeeping ───────────────────────────────────────
            if is_micro:
                micro_events.append(now)
            while micro_events and (now - micro_events[0]).total_seconds() > MICRO_OVERRIDE_WINDOW:
                micro_events.popleft()

            # ── Probation (ambiguous settle / resumed session must be confirmed) ──
            if probation_deadline is not None:
                if is_micro:
                    logging.info("Probation cleared — micro-motion confirms occupancy")
                    probation_deadline = None
                elif now >= probation_deadline:
                    close_session(probation_anchor, "probation expired — no micro-motion")
                    state = STATE_AWAY
                    if reference_gray is not None:
                        save_reference_frame(curr_gray)
                        reference_gray = curr_gray
                    micro_events.clear()
                    still_since = motion_since = None
                    probation_deadline = None
                    logging.warning("Probation expired with zero micro-motion — ruling crib "
                                    "empty, reference refreshed")

            # ── Reference self-correction (lighting drift, empty crib only) ───
            if reference_gray is not None:
                reference_gray = maybe_update_reference(
                    reference_gray, curr_gray, state, motion_frac,
                    cfg["micromotion_fraction"], cfg["presence_threshold"]
                )

            # ── State transitions (presence is latched; only paths 2 & timers) ──
            if state == STATE_AWAY:
                # Path 2: living-thing evidence needs no reference frame.
                if len(micro_events) >= MICRO_OVERRIDE_EVENTS:
                    state       = STATE_AWAKE
                    still_since = None
                    micro_events.clear()
                    logging.info("Micro-motion override — baby detected, AWAKE")

            elif state == STATE_AWAKE:
                if not is_motion:
                    if still_since is None:
                        still_since  = now
                        motion_since = None
                    elif (now - still_since).total_seconds() >= cfg["sleep_min_seconds"]:
                        current_sleep_start = still_since
                        current_session_id  = start_sleep_session(still_since)
                        state               = STATE_ASLEEP
                        still_since         = None
                        logging.info("ASLEEP — session %s started at %s",
                                     current_session_id, current_sleep_start)
                else:
                    still_since = None

            elif state == STATE_ASLEEP:
                # Path 4: sanity cap — a session this old is detection loss, not sleep.
                if (current_sleep_start is not None
                        and (now - current_sleep_start).total_seconds() >= cfg["max_session_seconds"]):
                    logging.warning("Session exceeded %.1fh cap — force-ending",
                                    cfg["max_session_seconds"] / 3600)
                    close_session(now, "max-session cap")
                    state        = STATE_AWAKE
                    motion_since = None
                    still_since  = None
                elif is_motion:
                    if motion_since is None:
                        motion_since = now
                        still_since  = None
                    elif (now - motion_since).total_seconds() >= cfg["wake_seconds"]:
                        close_session(motion_since, "sustained motion")
                        state        = STATE_AWAKE
                        motion_since = None
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

        cfg = {
            "crib_roi":             settings.get("sleep_crib_roi", [0.0, 0.0, 1.0, 1.0]),
            "presence_threshold":   float(settings.get("sleep_presence_threshold",  0.02)),
            "motion_fraction":      float(settings.get("sleep_motion_fraction",     0.01)),
            "micromotion_fraction": float(settings.get("sleep_micromotion_fraction", 0.002)),
            "disturbance_fraction": float(settings.get("sleep_disturbance_fraction", 0.10)),
            "settle_seconds":       float(settings.get("sleep_settle_seconds",      10)),
            "probation_minutes":    float(settings.get("sleep_probation_minutes",   15)),
            "sleep_min_seconds":    int(settings.get("sleep_min_minutes",           10)) * 60,
            "wake_seconds":         int(settings.get("sleep_wake_seconds",          20)),
            "max_session_seconds":  float(settings.get("sleep_max_session_hours",   14)) * 3600,
        }

        try:
            run_state_machine(rtsp_url, cfg)
        except Exception as exc:
            logging.error("Unexpected error: %s", exc, exc_info=True)

        logging.info("Reconnecting in %ds…", RECONNECT_DELAY)
        time.sleep(RECONNECT_DELAY)


if __name__ == "__main__":
    main()
