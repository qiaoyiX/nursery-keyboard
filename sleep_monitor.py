"""
Sleep monitoring daemon — runs as a separate systemd service (nursery-sleep-monitor).

v5 algorithm — event-gated latched presence (see docs/sleep-detection-research.md for the
full rationale, the failure history of v1–v4, and the 2026-07-02 footage measurements
that set the thresholds):

  Presence (baby in crib vs empty) is a LATCHED state, never re-decided per frame. A baby
  cannot enter or leave the crib without a parent-scale disturbance, so presence changes
  only through four explicit paths:

    1. Settle evaluation — after a disturbance (motion ≥ sleep_disturbance_fraction on
       2+ frames within DISTURBANCE_WINDOW, or a persistent >LIGHTING_CEILING scene
       change) quiets down for sleep_settle_seconds: if the settled frame matches the
       empty-crib reference → AWAY (and the reference is refreshed from the settled frame);
       otherwise → AWAKE on PROBATION: life evidence — sustained motion, or ≥2 micro-motion
       episodes separated by ≥60s of quiet — must appear within sleep_probation_minutes or
       the "presence" is ruled a bedding ghost → AWAY. The verdict is deferred (capped)
       while presence reads person-scale: the parent is still over the crib. Probation has
       an affirmative-empty fast path: a sustained wide-margin match to the empty
       reference closes the question immediately (see EMPTY_MATCH_MARGIN). Micro-motion
       evidence is a band, not a floor — parent-scale frames and their flanks never count
       (2026-07-09 missed-pickup lessons, all four).
    2. Micro-motion override — while AWAY, sustained motion (awake baby) flips to AWAKE
       outright; ≥2 separated micro-motion episodes within a 10-minute window flips to
       AWAKE **on probation** (weaker evidence — two parent reach-ins could fake it, so the
       probation machinery guards the flip; a sleeping baby confirms with her ~3-minute
       episode cadence, measured 2026-07-06). The episode-separation requirement is what
       distinguishes a real baby (twitches spread over minutes) from a brief parent reach-in
       (one 2–4s cluster whose motion overlaps the baby-twitch range — observed in real
       footage; magnitude alone cannot separate them).
    3. Manual "📷 Crib is empty" button — saves reference, forces AWAY.
    4. sleep_max_session_hours cap — backstop force-end.

  Between those events the latch holds no matter what the reference comparison says, which
  is what makes stale/poisoned references self-healing instead of fatal.

  Waking from ASLEEP is deliberately hard to trigger (actigraphy-style, see the WAKE_EPOCH
  constants): infants spend ~half of sleep in active/REM sleep — startles, limb-flings,
  squirms — without waking. A disturbance while asleep is a CANDIDATE arousal, not an
  automatic wake: the settle evaluation ends the nap only if the crib is now empty (a real
  pickup); if still occupied the nap resumes ON PROBATION (arousal rescored as sleep once
  micro-motion re-confirms; zero evidence means the "occupied" verdict was a bedding ghost
  from a missed pickup, and probation expiry closes the session backdated to the
  disturbance and refreshes the reference). A self-wake (no pickup) ends the nap only when
  motion is sustained across several consecutive epochs spanning sleep_wake_minutes — a
  brief arousal never reaches it.

  Both signals use one robust primitive: active_fraction(a, b) = fraction of pixels that
  meaningfully changed (absdiff → per-pixel threshold → MORPH_OPEN speck removal), computed
  inside the crib ROI (sleep_crib_roi). Motion = vs previous frame; presence = vs reference.
  The ROI must exclude the TAPO OSD timestamp (top-left) — its per-second digit changes
  register as constant motion on the full frame.

States: AWAY (not in crib) → AWAKE (in crib, moving) → ASLEEP (in crib, still)

The per-frame decision logic lives in SleepStateMachine so the identical code runs live
(this daemon) and offline over recorded footage (replay_sleep.py --simulate). Every frame
logs all metrics + thresholds at INFO:  sudo journalctl -u nursery-sleep-monitor -f

Configuration (settings.json):
  camera_rtsp_url            — rtsp://user:pass@IP:554/stream2
  sleep_crib_roi             — [x0, y0, x1, y1] crib region as 0–1 fractions (default full frame)
  sleep_presence_threshold   — ROI fraction differing from reference = "occupied" hint (default 0.02)
  sleep_motion_fraction      — ROI fraction changed vs prev frame = "moving/awake" (default 0.01)
  sleep_micromotion_fraction — ROI fraction = living-thing micro-motion (default 0.002)
  sleep_disturbance_fraction — ROI fraction = parent-scale disturbance (default 0.30; awake-baby
                               squirming measured 0.10–0.17, pickups 0.57–1.0)
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
    DEFAULT_SETTINGS,
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

LIGHTING_CEILING     = 0.80   # frame-diff fraction above which per-frame motion is meaningless:
                              # an IR day/night toggle, a corrupt decode, OR a pickup whose whole
                              # departure landed on one frame gap. Judgment is deferred one frame
                              # (see step()): a glitch reverts, a real scene change persists and
                              # opens a disturbance episode. The old skip-and-forget behavior let a
                              # parent leave with the baby invisibly (2026-07-09 missed pickup: the
                              # departure coincided with an h264 hiccup, diff 0.86 → frame skipped,
                              # no disturbance ever fired, session ran 3h over an empty crib).
FRAME_INTERVAL       = 1.0    # seconds between sampled frames (~1 fps)
PIXEL_THRESH         = 20     # per-pixel gray-level delta to count as "changed"
REFERENCE_FRAME_FILE = os.path.join(os.path.dirname(__file__), "reference_frame.npy")
REFERENCE_META_FILE  = os.path.join(os.path.dirname(__file__), "reference_frame_meta.json")
DENOISE_KERNEL       = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
REFERENCE_UPDATE_LR  = 0.02   # slow drift of reference toward current during trusted-empty periods
BOOTSTRAP_FRAMES     = 5      # frames median-averaged on startup to build initial reference (~5s)
DISTURBANCE_FRAMES   = 2      # disturbance-level frames within DISTURBANCE_WINDOW to open an episode
DISTURBANCE_WINDOW   = 4      # seconds. Was "2 consecutive frames", but decode hiccups drop frames
                              # exactly when the scene is busiest (H.264 bitstream stress), and a
                              # brief parent reach-in at ~1fps can alternate disturb/quiet frames —
                              # observed 2026-07-09: spikes 0.72 and 0.80 thirteen seconds apart
                              # never opened an episode, so the settle machinery never re-evaluated.
MICRO_OVERRIDE_EPISODES = 2   # micro-motion episodes to flip AWAY → AWAKE-on-probation …
MICRO_OVERRIDE_WINDOW   = 600  # … within this rolling window (seconds).
                              # 2 (not 3): a sleeping baby's worst measured 10-min window has
                              # exactly 2 episodes, while an empty crib has never produced 2 in
                              # any window — and the episode-path override lands on probation,
                              # so even a false fire self-corrects to AWAY.
PROBATION_CONFIRM_EPISODES = 2  # micro-motion episodes to clear probation
PROBATION_EXTEND_ON_PARTIAL = True  # exactly 1 episode at the deadline = ambiguous → extend once
                                    # (empty-crib probations measured 0 episodes ×3, 1 episode ×1)
EPISODE_CONFIRM_COOLDOWN = 15  # an episode counts as life evidence only after ending quietly for
                               # this many seconds. Micro-motion that escalates straight into a
                               # disturbance is a PARENT APPROACHING, not the baby — observed to
                               # falsely clear probation 11s before a blanket-removal disturbance
                               # and mint a phantom session. Baby episodes all end quietly.
MICRO_EPISODE_GAP       = 60  # quiet seconds separating two micro-motion episodes.
                              # Counting episodes (not calendar-minute buckets) means a single
                              # 2–4s reach-in can never look like two pieces of evidence, no
                              # matter where it falls relative to a minute boundary.

# Parent-scale motion is NEVER life evidence, and it taints the micro-motion around it:
# a reach-in's flank frames (measured 0.04 right after a 0.80 spike, 2026-07-09) are the
# same parent event, not baby twitches. Frames ≥ sleep_disturbance_fraction are excluded
# from the micro band outright; micro events within MICRO_EPISODE_GAP before a
# disturbance-scale frame are purged, and collection is suppressed for
# EPISODE_CONFIRM_COOLDOWN after it. Without this, two parent visits during probation
# read as two separated micro-motion episodes and falsely confirm occupancy of an empty
# crib (the 2026-07-09 missed pickup ran 3h on exactly that).

# Settle evaluations are meaningless while a person fills the crib ROI: a parent leaning
# over the crib below disturbance-level motion for settle_seconds triggers a settle whose
# presence is the PARENT (measured 0.77–0.89), not the crib contents (baby ≈ 0.07–0.12).
# Defer the verdict while presence ≥ SETTLE_BLOCKED_PRESENCE, up to SETTLE_DEFER_LIMIT
# (an IR flip against a stale reference can also read ≥0.5 forever — the cap guarantees
# an eventual verdict, and the probation machinery absorbs a wrong one).
SETTLE_BLOCKED_PRESENCE = 0.5
SETTLE_DEFER_LIMIT      = 180  # seconds

# Affirmative-empty fast path during probation: "differs from reference" can lie (bedding
# ghost), but a sustained match to the known-empty reference at wide margin cannot be a
# baby. If presence stays ≤ presence_threshold × EMPTY_MATCH_MARGIN for
# PROBATION_EMPTY_MATCH_SECONDS, rule the crib empty immediately instead of waiting out
# the probation deadline (2026-07-09: presence read 0.001 for nine straight minutes while
# the state machine waited for micro-motion evidence that a parent visit then faked).
# Also the fast recovery for daemon-restart resumes that missed a pickup while offline.
# Measured margins: empty crib 0.0006–0.0022, baby present 0.06–0.12, bar = 0.025.
PROBATION_EMPTY_MATCH_SECONDS = 60
EMPTY_MATCH_MARGIN            = 0.5

# ── Wake confirmation (actigraphy-style) ──────────────────────────────────────
# A sleeping infant spends ~50% of sleep in active/REM sleep: startles, limb-flings,
# position shifts, squirms — all WITHOUT waking. Wrist-actigraphy scoring (Cole-Kripke,
# Sadeh, Webster rescoring) never scores one epoch alone: a genuine wake is a RUN of
# active epochs lasting minutes, and short active blocks embedded in sleep are rescored
# back to sleep. We mirror that: end an ASLEEP session only when motion is sustained
# across several consecutive epochs — a brief arousal is absorbed back into the nap.
WAKE_EPOCH_SECONDS      = 30    # epoch length for wake scoring
WAKE_EPOCH_ACTIVE_FRAC  = 0.5   # an epoch is "active" if ≥ this fraction of its frames moved
                                # (≥15s of motion in a 30s epoch — well above active-sleep stirs)
MOTION_DENSITY          = 0.6  # fraction of the trailing wake_seconds window that must be
                               # motion frames to count as "sustained" (genuinely awake).
                               # Sleep stirs are 1–7s clusters minutes apart (density ~0.05);
                               # awake squirming fills ~98% of frames — measured 2026-07-04.


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


# ── State machine (pure decision logic — shared by daemon and replay) ─────────

class SleepStateMachine:
    """
    Per-frame decision core. Feed it ROI-cropped grayscale frames via step(); it calls
    back on session start/end and reference saves. No I/O of its own, so the identical
    logic runs against the live RTSP stream and recorded footage (replay_sleep.py).
    """

    def __init__(self, cfg, reference=None, log=logging,
                 on_session_start=None, on_session_end=None, on_reference_save=None):
        self.cfg  = cfg
        self.log  = log
        self.on_session_start  = on_session_start or (lambda t: None)
        self.on_session_end    = on_session_end or (lambda sid, t: None)
        self.on_reference_save = on_reference_save or (lambda ref: None)

        self.reference = reference
        self.prev      = None
        self.state     = STATE_AWAY
        self.session_id    = None
        self.sleep_start   = None
        self.still_since   = None
        self.motion_frames = deque()          # timestamps of recent "moving" frames
        self.disturb_times      = deque()     # timestamps of recent disturbance-scale frames
        self.in_disturbance     = False
        self.disturbance_start  = None
        self.settle_quiet_since = None
        self.settle_blocked_since = None      # first blocked-scene settle attempt (deferral)
        self.pending_guard      = None        # time a >LIGHTING_CEILING frame change fired
        self.micro_suppress_until = None      # micro collection tainted by parent-scale motion
        self.empty_match_since  = None        # probation: first frame of a sustained empty match
        self.probation_deadline = None
        self.probation_anchor   = None
        self.probation_micro    = []          # micro-motion timestamps since probation start
        self.probation_extended = False       # one-shot partial-evidence extension used?
        self.micro_events = deque()           # micro-motion timestamps (AWAY override)
        # Wake scoring (ASLEEP → AWAKE): rolling epoch verdicts + current-epoch accumulation
        self.wake_epochs        = deque()     # (epoch_start, active_bool) recent epochs
        self._epoch_start       = None        # start time of the epoch being accumulated
        self._epoch_frames      = 0
        self._epoch_motion      = 0
        # True when a disturbance interrupted an ASLEEP nap: on settle-to-occupied the nap
        # resumes (arousal rescored as sleep) instead of the session ending.
        self.arousal_from_sleep = False

    # ── external controls ─────────────────────────────────────────────────────

    def resume_session(self, session_id, sleep_start, now):
        """Daemon restart/reconnect with an open session: resume ASLEEP, on probation
        (we may have missed a pickup while offline)."""
        self.state       = STATE_ASLEEP
        self.session_id  = session_id
        self.sleep_start = sleep_start
        self._start_probation(now)
        self.log.info("Resumed open sleep session id=%s started %s (on probation until %s)",
                      session_id, sleep_start, self.probation_deadline.isoformat())

    def calibrate(self, curr_gray, now):
        """Manual 'Crib is empty': authoritative reference + forced AWAY."""
        self._set_reference(curr_gray)
        self._close_session(now, "manual calibration")
        self._to_away()
        # Cancel any in-flight disturbance episode: the button is often pressed right
        # after walking away from the crib, and a pending settle evaluation against the
        # just-saved reference would immediately re-open probation over an empty crib.
        self.in_disturbance     = False
        self.disturb_times.clear()
        self.settle_quiet_since = None
        self.settle_blocked_since = None
        self.disturbance_start  = None
        self.pending_guard      = None
        self.micro_suppress_until = None
        self.prev = curr_gray
        self.log.info("Reference frame saved manually — empty crib baseline updated")

    # ── internals ─────────────────────────────────────────────────────────────

    def _set_reference(self, gray):
        self.reference = gray
        self.on_reference_save(gray)

    def _close_session(self, end_time, why):
        if self.session_id is not None:
            self.on_session_end(self.session_id, end_time)
            self.log.info("Sleep session %s ended at %s (%s)",
                          self.session_id, end_time.isoformat(), why)
        self.session_id  = None
        self.sleep_start = None

    def _to_away(self):
        self.state = STATE_AWAY
        self.still_since = None
        self.motion_frames.clear()
        self.probation_deadline = self.probation_anchor = None
        self.probation_micro.clear()
        self.micro_events.clear()
        self.arousal_from_sleep = False
        self.empty_match_since  = None
        self._reset_wake_epochs()

    def _start_probation(self, now):
        self.probation_deadline = now + timedelta(minutes=self.cfg["probation_minutes"])
        self.probation_anchor   = now
        self.probation_micro.clear()
        self.probation_extended = False
        self.empty_match_since  = None

    def _open_disturbance(self, start_time):
        """Open a disturbance episode: suspend transitions until it settles."""
        self.in_disturbance     = True
        self.disturbance_start  = start_time
        self.settle_quiet_since = None
        self.settle_blocked_since = None
        # A disturbance while ASLEEP is a *candidate* arousal, NOT an automatic wake:
        # a startle/limb-fling is a sleep phenomenon. Keep the session open and let the
        # settle evaluation decide — crib empty ⇒ real pickup (end); still occupied ⇒
        # resume the nap (arousal rescored as sleep).
        self.arousal_from_sleep = (self.state == STATE_ASLEEP)
        self.still_since = None
        self.motion_frames.clear()
        self.probation_deadline = None
        self.probation_micro.clear()
        self.empty_match_since  = None

    @staticmethod
    def _count_episodes(timestamps, now=None):
        """
        Number of micro-motion episodes: groups separated by ≥ MICRO_EPISODE_GAP of quiet.
        With `now`, only credits episodes that ended ≥ EPISODE_CONFIRM_COOLDOWN ago —
        motion still running (or escalating into a disturbance) is not yet evidence.
        """
        episodes = []   # last-frame timestamp of each episode
        prev = None
        for t in timestamps:
            if prev is None or (t - prev).total_seconds() >= MICRO_EPISODE_GAP:
                episodes.append(t)
            else:
                episodes[-1] = t
            prev = t
        if now is None:
            return len(episodes)
        return sum(1 for last in episodes
                   if (now - last).total_seconds() >= EPISODE_CONFIRM_COOLDOWN)

    def _sustained_motion(self, now, is_motion):
        """
        Rolling density test for genuine wakefulness: motion frames must fill
        ≥ MOTION_DENSITY of the trailing wake_seconds window. Brief sleep stirs
        (a newborn in active sleep moves 1–7 s every 1–3 min) never reach that;
        awake squirming (~98% of frames moving) reaches it within ~wake_seconds.
        Replaces both the old single-frame stillness reset (which kept an active-sleep
        baby "awake" forever) and the old consecutive-frames wake test (which one
        quiet frame could postpone).
        """
        if is_motion:
            self.motion_frames.append(now)
        window = self.cfg["wake_seconds"]
        while self.motion_frames and (now - self.motion_frames[0]).total_seconds() > window:
            self.motion_frames.popleft()
        needed = max(3, int(MOTION_DENSITY * window / FRAME_INTERVAL))
        return len(self.motion_frames) >= needed

    def _reset_wake_epochs(self):
        self.wake_epochs.clear()
        self._epoch_start  = None
        self._epoch_frames = 0
        self._epoch_motion = 0

    def _update_wake_epochs(self, now, is_motion):
        """
        Actigraphy-style wake scoring. Accumulate frames into fixed epochs; an epoch is
        "active" if ≥ WAKE_EPOCH_ACTIVE_FRAC of its frames moved. Wake is confirmed only
        after K consecutive active epochs spanning ≥ sleep_wake_minutes — a brief arousal
        (one active epoch, or scattered motion) never reaches it. Returns
        (wake_confirmed, bout_start) where bout_start is the first of the confirming run.
        """
        if self._epoch_start is None:
            self._epoch_start = now
        self._epoch_frames += 1
        if is_motion:
            self._epoch_motion += 1

        if (now - self._epoch_start).total_seconds() < WAKE_EPOCH_SECONDS:
            return False, None

        active = (self._epoch_frames > 0
                  and self._epoch_motion / self._epoch_frames >= WAKE_EPOCH_ACTIVE_FRAC)
        self.wake_epochs.append((self._epoch_start, active))
        self._epoch_start  = now
        self._epoch_frames = 0
        self._epoch_motion = 0

        need = max(2, round(self.cfg["wake_minutes"] * 60 / WAKE_EPOCH_SECONDS))
        while len(self.wake_epochs) > need:
            self.wake_epochs.popleft()
        if len(self.wake_epochs) == need and all(a for _, a in self.wake_epochs):
            return True, self.wake_epochs[0][0]
        return False, None

    def _maybe_update_reference(self, curr_gray, motion_frac):
        """Triple-gated slow drift: track lighting only during trusted-empty stillness."""
        if (self.state == STATE_AWAY
                and motion_frac < self.cfg["micromotion_fraction"]
                and self.reference is not None
                and active_fraction(curr_gray, self.reference) <= self.cfg["presence_threshold"]):
            self.reference = cv2.addWeighted(self.reference, 1.0 - REFERENCE_UPDATE_LR,
                                             curr_gray,       REFERENCE_UPDATE_LR, 0)

    # ── per-frame step ────────────────────────────────────────────────────────

    def step(self, curr_gray, now):
        """Process one ROI-cropped gray frame. Returns the state after the frame."""
        cfg = self.cfg

        if self.prev is None:
            self.prev = curr_gray
            return self.state

        # Scene-change guard: a near-total frame change (> LIGHTING_CEILING) is an IR
        # day/night toggle, a corrupt decode, or a pickup whose whole departure landed in
        # one frame gap. Per-frame motion is meaningless across it, so defer judgment one
        # frame instead of deciding now: a glitch reverts (next frame matches the old
        # scene again → process normally), while a real scene change persists → treat it
        # as a disturbance so the settle evaluation gets to rule on the new scene.
        if self.pending_guard is not None:
            fired_at = self.pending_guard
            self.pending_guard = None
            if active_fraction(self.prev, curr_gray, pixel_thresh=25) > LIGHTING_CEILING:
                self.log.info("Scene changed >%d%% and persisted (IR flip or unseen "
                              "pickup) — treating as a disturbance",
                              int(LIGHTING_CEILING * 100))
                if self.in_disturbance:
                    self.settle_quiet_since = None
                else:
                    self._open_disturbance(fired_at)
                self.prev = curr_gray
                return self.state
            # else: single-frame glitch — fall through, compare against the pre-glitch prev
        elif active_fraction(self.prev, curr_gray, pixel_thresh=25) > LIGHTING_CEILING:
            self.pending_guard = now      # judgment deferred one frame; prev unchanged
            return self.state

        motion_frac   = active_fraction(self.prev, curr_gray)
        presence_frac = (active_fraction(curr_gray, self.reference)
                         if self.reference is not None else 0.0)
        is_disturb = motion_frac >= cfg["disturbance_fraction"]
        micro_suppressed = (self.micro_suppress_until is not None
                            and now < self.micro_suppress_until)
        # Micro-motion is a band, not a floor: parent-scale frames are never life evidence.
        is_micro   = (not is_disturb and not micro_suppressed
                      and motion_frac > cfg["micromotion_fraction"])
        is_motion  = motion_frac > cfg["motion_fraction"]

        flags = ([""] if not (self.in_disturbance or self.probation_deadline) else
                 [" [" + ",".join(f for f, on in
                                  [("disturbance", self.in_disturbance),
                                   ("probation", self.probation_deadline is not None)] if on) + "]"])
        self.log.info("state=%s presence=%.4f (thr %.3f) motion=%.4f (thr %.3f) "
                      "micro=%.3f dist=%.3f%s",
                      self.state, presence_frac, cfg["presence_threshold"],
                      motion_frac, cfg["motion_fraction"],
                      cfg["micromotion_fraction"], cfg["disturbance_fraction"], flags[0])

        # ── Disturbance episode tracking (Path 1 trigger) ─────────────────────
        while self.disturb_times and (now - self.disturb_times[0]).total_seconds() > DISTURBANCE_WINDOW:
            self.disturb_times.popleft()
        if is_disturb:
            self.disturb_times.append(now)
            self.settle_quiet_since = None
            # Parent-scale motion taints nearby micro-motion: purge the trailing episode
            # gap (the flank frames were the same parent event) and suppress collection
            # while the event's tail plays out.
            self.micro_suppress_until = now + timedelta(seconds=EPISODE_CONFIRM_COOLDOWN)
            cutoff = now - timedelta(seconds=MICRO_EPISODE_GAP)
            self.probation_micro = [t for t in self.probation_micro if t < cutoff]
            while self.micro_events and self.micro_events[-1] >= cutoff:
                self.micro_events.pop()
            if not self.in_disturbance and len(self.disturb_times) >= DISTURBANCE_FRAMES:
                self._open_disturbance(self.disturb_times[0])
                self.log.info("Disturbance started (motion %.3f ≥ %.3f)",
                              motion_frac, cfg["disturbance_fraction"])
        elif self.in_disturbance:
            if self.settle_quiet_since is None:
                self.settle_quiet_since = now
            elif (now - self.settle_quiet_since).total_seconds() >= cfg["settle_seconds"]:
                # A settle verdict is about the crib contents; while a person still fills
                # the ROI (presence at parent scale) it would be about the person. Defer
                # until the scene clears — capped, so a stale-reference IR flip (which
                # also reads high forever) still gets an eventual verdict for the
                # probation machinery to absorb.
                blocked = (self.reference is not None
                           and presence_frac >= SETTLE_BLOCKED_PRESENCE)
                if blocked and self.settle_blocked_since is None:
                    self.settle_blocked_since = now
                    self.log.info("Settle deferred — presence %.2f is person-scale, the "
                                  "crib is still blocked; waiting up to %ds",
                                  presence_frac, SETTLE_DEFER_LIMIT)
                if not blocked or ((now - self.settle_blocked_since).total_seconds()
                                   >= SETTLE_DEFER_LIMIT):
                    self._settle_evaluation(curr_gray, presence_frac, now)

        if self.in_disturbance:
            # Parent motion must not read as baby motion — suspend normal transitions.
            self.prev = curr_gray
            return self.state

        # ── Micro-motion bookkeeping ──────────────────────────────────────────
        if is_micro:
            self.micro_events.append(now)
            if self.probation_deadline is not None:
                self.probation_micro.append(now)
        while self.micro_events and (now - self.micro_events[0]).total_seconds() > MICRO_OVERRIDE_WINDOW:
            self.micro_events.popleft()

        # Genuine wakefulness = sustained motion density, not isolated stirs.
        # Also the strongest life evidence: an empty crib produces at most 1–3
        # stray frames at a time, never 60% density over the wake window.
        sustained = self._sustained_motion(now, is_motion)

        # ── Probation: occupancy claimed by the reference must be confirmed ───
        if self.probation_deadline is not None:
            # Affirmative-empty fast path: "differs from reference" can lie (bedding
            # ghost), but a sustained wide-margin MATCH to the known-empty reference
            # cannot be a baby. Don't wait out the deadline — and don't leave a window
            # for a parent visit to fake the evidence (2026-07-09 missed pickup).
            # Gated on an OPEN session: while a nap runs, the reference predates it and
            # was validated empty, so a match refutes occupancy. Session-less probations
            # (AWAY override, put-down settle) may run under a provisional/bootstrap
            # reference that CONTAINS the baby — there "matches reference" ≠ "empty"
            # (replaying 20260709_165351 without this gate closed a real nap at 60s).
            if (self.session_id is not None
                    and self.reference is not None
                    and presence_frac <= cfg["presence_threshold"] * EMPTY_MATCH_MARGIN):
                if self.empty_match_since is None:
                    self.empty_match_since = now
                elif ((now - self.empty_match_since).total_seconds()
                        >= PROBATION_EMPTY_MATCH_SECONDS):
                    self._close_session(self.probation_anchor or now,
                                        "probation empty-match (crib matches empty reference)")
                    self.log.warning("Probation: presence ≤ %.4f sustained %ds — crib is "
                                     "empty, ruling AWAY now, reference refreshed",
                                     cfg["presence_threshold"] * EMPTY_MATCH_MARGIN,
                                     PROBATION_EMPTY_MATCH_SECONDS)
                    self._to_away()
                    self._set_reference(curr_gray)
                    self.prev = curr_gray
                    return self.state
            else:
                self.empty_match_since = None
            episodes = self._count_episodes(self.probation_micro, now)
            if sustained or episodes >= PROBATION_CONFIRM_EPISODES:
                self.log.info("Probation cleared at %s — %s confirms occupancy", now.isoformat(),
                              "sustained motion" if sustained
                              else f"{episodes} separated micro-motion episodes")
                self.probation_deadline = self.probation_anchor = None
                self.probation_micro.clear()
            elif now >= self.probation_deadline:
                if (PROBATION_EXTEND_ON_PARTIAL and episodes == 1
                        and not self.probation_extended):
                    # Exactly one episode is ambiguous — an empty crib almost never
                    # produces even one, a quiet sleeping baby averages one per ~3 min.
                    # Buy one more window rather than rule the crib empty on a coin flip.
                    self.probation_deadline = now + timedelta(
                        minutes=self.cfg["probation_minutes"])
                    self.probation_extended = True
                    self.log.info("Probation deadline reached with 1 micro-motion episode — "
                                  "ambiguous, extending once until %s",
                                  self.probation_deadline.isoformat())
                else:
                    self._close_session(self.probation_anchor,
                                        "probation expired — no micro-motion")
                    self.log.warning("Probation expired with %d micro-motion episode(s) — "
                                     "ruling crib empty, reference refreshed", episodes)
                    self._to_away()
                    if self.reference is not None:
                        self._set_reference(curr_gray)

        self._maybe_update_reference(curr_gray, motion_frac)

        # ── State transitions (presence is latched; only Path 2 & timers) ─────
        if self.state == STATE_AWAY:
            # Path 2: life evidence with no reference needed — sustained motion
            # (awake baby, unambiguous) or micro-motion episodes separated by ≥60s
            # of quiet (sleeping baby). A brief parent reach-in is a single episode
            # with magnitudes overlapping baby twitches and must NOT fire this.
            episodes = self._count_episodes(self.micro_events, now)
            if sustained:
                self.state = STATE_AWAKE
                self.still_since = None
                self.micro_events.clear()
                self.log.info("Override — sustained motion, AWAKE")
            elif episodes >= MICRO_OVERRIDE_EPISODES:
                # Episode evidence is weaker (2 reach-ins in 10 min could fake it),
                # so land on probation: a real baby confirms with her next episodes
                # (~every 3 min asleep); a false fire self-corrects to AWAY.
                self.state = STATE_AWAKE
                self.still_since = None
                self.micro_events.clear()
                self._start_probation(now)
                self.log.info("Override — %d separated micro-motion episodes, AWAKE on "
                              "probation until %s", episodes,
                              self.probation_deadline.isoformat())

        elif self.state == STATE_AWAKE:
            if sustained:
                self.still_since = None   # genuinely restless — not falling asleep
            elif self.still_since is None:
                if not is_motion:
                    self.still_since = now
            elif ((now - self.still_since).total_seconds() >= cfg["sleep_min_seconds"]
                    and self.probation_deadline is None):
                # The probation gate: a session must never start on unconfirmed
                # occupancy (observed: 10 min of empty-crib stillness during probation
                # produced a phantom ASLEEP). The timer itself keeps running through
                # probation so a confirmed baby's session start stays backdated to
                # when stillness truly began; probation expiry wipes the timer.
                self.sleep_start = self.still_since
                self.session_id  = self.on_session_start(self.still_since)
                self.state       = STATE_ASLEEP
                self.still_since = None
                self.log.info("ASLEEP — session %s started at %s",
                              self.session_id, self.sleep_start)

        elif self.state == STATE_ASLEEP:
            # Path 4: sanity cap — a session this old is detection loss, not sleep.
            if (self.sleep_start is not None
                    and (now - self.sleep_start).total_seconds() >= cfg["max_session_seconds"]):
                self.log.warning("Session exceeded %.1fh cap — force-ending",
                                 cfg["max_session_seconds"] / 3600)
                self._close_session(now, "max-session cap")
                self.state = STATE_AWAKE
                self.still_since = None
                self._reset_wake_epochs()
            else:
                # Wake requires motion sustained across several epochs (minutes), not a
                # brief arousal. Active-sleep squirms and startles score one active epoch
                # at most and are absorbed back into the nap (actigraphy rescoring).
                wake_confirmed, bout_start = self._update_wake_epochs(now, is_motion)
                if wake_confirmed:
                    self._close_session(bout_start, "sustained wake (%d epochs)"
                                        % len(self.wake_epochs))
                    self.state = STATE_AWAKE
                    self.still_since = None
                    self._reset_wake_epochs()
        else:
            self._reset_wake_epochs()

        self.prev = curr_gray
        return self.state

    def _settle_evaluation(self, curr_gray, presence_frac, now):
        """Path 1: a disturbance episode just ended — decide the state it settles into."""
        self.in_disturbance     = False
        self.settle_quiet_since = None
        self.settle_blocked_since = None
        was_arousal             = self.arousal_from_sleep
        self.arousal_from_sleep = False

        if self.reference is not None and presence_frac <= self.cfg["presence_threshold"]:
            # Crib empty — a real departure (pickup). Backdate a nap's end to the
            # disturbance start; if we weren't asleep this is a no-op close.
            self._close_session(self.disturbance_start or now, "settled empty (departure)")
            self._to_away()
            self._set_reference(curr_gray)
            self.log.info("Settle: crib empty (presence %.4f ≤ %.3f) — AWAY, reference "
                          "refreshed", presence_frac, self.cfg["presence_threshold"])
        elif was_arousal and self.session_id is not None:
            # Still occupied and we were asleep before the burst — a startle / position
            # shift, not a wake. Resume the SAME nap, but ON PROBATION: "still occupied"
            # comes from the reference, and after a pickup the reference always lies
            # (bedding rearranged ≫ presence threshold), so an unconditional resume turns
            # every missed pickup into a session that only the max-hours cap can end.
            # A sleeping baby re-confirms at her ~3-min micro-motion cadence (measured
            # 2026-07-06) and the nap continues unfragmented; an empty crib produces zero
            # episodes → probation expiry closes the session backdated to the disturbance
            # (the real pickup) and refreshes the reference — self-healing restored.
            self.state = STATE_ASLEEP
            self.still_since = None
            self._reset_wake_epochs()
            self._start_probation(now)
            if self.disturbance_start is not None:
                self.probation_anchor = self.disturbance_start
            self.log.info("Settle: presence %.4f still occupied after an in-sleep arousal — "
                          "resuming nap %s on probation until %s (life evidence must "
                          "confirm, else scored as a missed pickup)",
                          presence_frac, self.session_id,
                          self.probation_deadline.isoformat())
        else:
            self.state = STATE_AWAKE
            self.still_since = None
            self.motion_frames.clear()
            self._start_probation(now)
            self.log.info("Settle: presence %.4f suggests occupied — AWAKE on probation "
                          "until %s (sustained motion or %d micro-motion episodes must confirm)",
                          presence_frac, self.probation_deadline.isoformat(),
                          PROBATION_CONFIRM_EPISODES)


# ── Daemon loop ───────────────────────────────────────────────────────────────

def run_state_machine(rtsp_url, cfg):
    roi = roi_pixels(cfg["crib_roi"])
    cap = open_capture(rtsp_url)
    if cap is None:
        logging.warning("Could not open RTSP stream: %s", rtsp_url)
        return

    logging.info("RTSP stream opened: %s (ROI rows %d–%d, cols %d–%d)", rtsp_url, *roi)

    first_gray = read_frame_gray(cap, roi)  # validates reference shape, seeds motion diff

    reference = load_reference_frame(first_gray.shape if first_gray is not None else None)
    if reference is None:
        logging.info("No usable saved reference — building provisional reference from %d frames…",
                     BOOTSTRAP_FRAMES)
        reference = bootstrap_reference(cap, roi)
        if reference is not None:
            logging.info("Provisional reference built. If the crib was NOT empty just now, the "
                         "first pickup will self-correct it; or press 'Crib is empty' to override.")
        else:
            logging.warning("Could not bootstrap reference — camera may not be ready")
    else:
        logging.info("Reference frame loaded from disk")

    def on_session_end(sid, end_time):
        end_sleep_session(sid, end_time)
        if HUCKLEBERRY_AVAILABLE and machine.sleep_start:
            push_sleep(machine.sleep_start, end_time)

    machine = SleepStateMachine(
        cfg, reference=reference,
        on_session_start=start_sleep_session,
        on_session_end=on_session_end,
        on_reference_save=save_reference_frame,
    )
    machine.prev = first_gray

    open_session = get_open_sleep_session()
    if open_session:
        t = open_session["start_time"]
        machine.resume_session(
            open_session["id"],
            t if isinstance(t, datetime) else datetime.fromisoformat(str(t)),
            datetime.now(),
        )

    try:
        while True:
            loop_start = time.monotonic()

            curr_gray = read_frame_gray(cap, roi)
            if curr_gray is None:
                logging.warning("Frame read failed — RTSP connection dropped")
                break

            now = datetime.now()

            if os.path.exists(CALIBRATE_FLAG):
                try:
                    os.remove(CALIBRATE_FLAG)
                    machine.calibrate(curr_gray, now)
                except Exception as e:
                    logging.warning("Calibration error: %s", e)

            state = machine.step(curr_gray, now)
            write_sleep_heartbeat(state)

            elapsed = time.monotonic() - loop_start
            time.sleep(max(0.0, FRAME_INTERVAL - elapsed))
    finally:
        cap.release()


# ── Reconnect loop ────────────────────────────────────────────────────────────

def build_cfg(settings):
    # Single source of truth for defaults is storage.DEFAULT_SETTINGS — no per-key
    # fallbacks here (a duplicated default silently diverged once already).
    s = {**DEFAULT_SETTINGS, **settings}
    return {
        "crib_roi":             s["sleep_crib_roi"],
        "presence_threshold":   float(s["sleep_presence_threshold"]),
        "motion_fraction":      float(s["sleep_motion_fraction"]),
        "micromotion_fraction": float(s["sleep_micromotion_fraction"]),
        "disturbance_fraction": float(s["sleep_disturbance_fraction"]),
        "settle_seconds":       float(s["sleep_settle_seconds"]),
        "probation_minutes":    float(s["sleep_probation_minutes"]),
        "sleep_min_seconds":    int(s["sleep_min_minutes"]) * 60,
        "wake_seconds":         int(s["sleep_wake_seconds"]),
        "wake_minutes":         float(s["sleep_wake_minutes"]),
        "max_session_seconds":  float(s["sleep_max_session_hours"]) * 3600,
    }


def main():
    RECONNECT_DELAY = 10

    while True:
        settings = load_settings()
        rtsp_url = settings.get("camera_rtsp_url", "")

        if not rtsp_url:
            logging.info("No camera_rtsp_url in settings.json — retrying in 30s.")
            time.sleep(30)
            continue

        try:
            run_state_machine(rtsp_url, build_cfg(settings))
        except Exception as exc:
            logging.error("Unexpected error: %s", exc, exc_info=True)

        logging.info("Reconnecting in %ds…", RECONNECT_DELAY)
        time.sleep(RECONNECT_DELAY)


if __name__ == "__main__":
    main()
