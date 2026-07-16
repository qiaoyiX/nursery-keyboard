"""
Synthetic regression test for the ASLEEP → pickup path (the one path no recording
exercises — docs/sleep-detection-research.md §6a end: "Still unexercised on real footage:
ASLEEP → pickup → session close").

Reproduces the 2026-07-07 live failure: a pickup from ASLEEP whose settled frame differs
from the empty-crib reference (bedding rearranged — measured 0.127 presence over an empty
crib on 7/4) was resumed as a nap with no probation, so the session could only end at the
max-hours cap. The fix resumes the nap ON PROBATION: a real baby re-confirms via her
~3-min micro-motion cadence; an empty crib produces zero episodes and probation expiry
closes the session backdated to the disturbance (the real pickup) + refreshes the
reference.

Scenarios D–F reproduce the 2026-07-09 missed pickup (pickup_miss_1.log): the departure
happened inside a >80% frame change (old lighting guard silently skipped it — no
disturbance ever fired), the settle verdict was taken while the parent still filled the
ROI (presence 0.888 read as "occupied"), and the parent's later return (motion 0.72!)
was counted as micro-motion life evidence, falsely clearing probation over an empty crib.

Run:  venv/bin/python tests/test_arousal_probation.py
Scenarios:
  A  missed pickup from ASLEEP (bedding ghost)  → session must close via probation expiry
  B  true in-sleep startle, baby stays          → same nap continues, no fragmentation
  C  clean pickup (settled frame matches ref)   → session closes backdated (unchanged path)
  D  pickup hidden in a guard-scale scene change, settle attempted while parent blocks
     the ROI → verdict deferred, departure opens a disturbance, session closes backdated
  E  parent visits during probation (disturbance-scale spikes + micro flanks) must NOT
     clear probation → session closes at expiry, backdated to the pickup
  F  probation over a crib that matches the empty reference → affirmative-empty fast
     path closes the session in ~1 min instead of waiting out the deadline
  G  faint put-down (2026-07-14 shape): a baby whose settled presence is only ~1.7× the
     default threshold must be ruled occupied and reach ASLEEP; the same footage under
     the incident Pi's sleep_presence_threshold=0.05 settles as "empty" (AWAY, no
     session, reference poisoned) — locking the default at 0.02

Scenarios H–K lock the 2026-07-15/16 night incidents (night_0715.log):
  H  silent gentle pickup (02:42: peak motion 0.13, no disturbance possible) → the
     silent-departure close ends the session on a sustained trusted-empty match; the
     same run under an UNTRUSTED reference must hold the latch
  I  bedding-ghost phantom with zero micro-motion (session 394 ran 9h on this) → the
     reference-free liveness backstop closes at sleep_liveness_minutes, backdated
  J  parent partially in the ROI during arousal probation (09:00:38: every "sustained
     motion" frame read presence 0.27–0.30 against a 0.04 anchor) → presence-raising
     motion is an intruder, not evidence; probation must expire and close at the pickup
  K  slow put-back (04:41: one isolated 0.849 frame, other ≥0.30 frames 14–17s apart) →
     the solo-frame rule opens the disturbance and the put-down is recorded
"""

import logging
import os
import sys
from datetime import datetime, timedelta

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sleep_monitor import (  # noqa: E402
    DISTURBANCE_SOLO_FRACTION,
    EVIDENCE_PRESENCE_DELTA,
    LATCHED_EMPTY_MATCH_SECONDS,
    LIGHTING_CEILING,
    SETTLE_BLOCKED_PRESENCE,
    STATE_ASLEEP,
    STATE_AWAY,
    SleepStateMachine,
    active_fraction,
    build_cfg,
)

SIZE = 60  # synthetic ROI is SIZE×SIZE

EMPTY   = np.zeros((SIZE, SIZE), dtype=np.uint8)
BEDDING = EMPTY.copy();  BEDDING[48:60, :] = 150   # crumpled swaddle left behind (~20% of ROI)
BABY    = EMPTY.copy();  BABY[5:25, :]     = 200   # swaddled baby (~33% of ROI)
BABY2   = EMPTY.copy();  BABY2[10:30, :]   = 200   # baby after a position shift
# Faint baby: presence ~0.033 — between the default presence threshold (0.02) and the
# 2026-07-14 incident Pi's override (0.05). Real sleeping-baby frames measured
# 0.06–0.075 against the live reference (pickup_miss_1.log), so a slightly stale
# reference or a small swaddle puts a real put-down exactly in this band.
BABY_FAINT = EMPTY.copy();  BABY_FAINT[5:10, 18:42] = 200

# Parent-scale disturbance: two frames differing in ~40% of pixels, alternated.
DIST_A = EMPTY.copy();  DIST_A[0:24, :] = 255
DIST_B = EMPTY.copy();  DIST_B[24:48, :] = 255

# Parent leaning over the crib, filling the ROI (measured presence 0.77–0.89 live;
# here 1.0). Entering/leaving over a sleeping baby changes ~100% of pixels — above the
# LIGHTING_CEILING guard, exactly how the 2026-07-09 pickup went unseen.
PARENT = np.full((SIZE, SIZE), 255, dtype=np.uint8)

# Parent partially in frame (~60% presence): above SETTLE_BLOCKED_PRESENCE, below the
# guard ceiling, and a single-frame entry/exit jump reads as one disturbance-scale frame.
PARENT_HALF = EMPTY.copy();  PARENT_HALF[0:36, :] = 255

# A brief parent reach-in over the post-pickup bedding: one disturbance-scale frame
# (like the isolated 0.72 / 0.80 spikes at 06:50 in pickup_miss_1.log — too sparse to
# open an episode) whose flank frames are micro-scale.
REACH = BEDDING.copy();  REACH[0:24, :] = 255

# Gentle night pickup (2026-07-16 02:42 shape): the baby's footprint shrinks a few rows
# per frame — every step well below the 0.30 disturbance bar (measured peak 0.13).
GENTLE = [EMPTY.copy() for _ in range(4)]
for _i in range(4):
    GENTLE[_i][5 + 4 * (_i + 1):25, :] = 200

# Slow put-back (2026-07-16 04:41 shape): ONE isolated person-scale frame (measured 0.849
# live; here ~0.6 — above DISTURBANCE_SOLO_FRACTION, below the lighting guard), no second
# ≥0.30 frame anywhere near it.
PARENT_SOLO = EMPTY.copy();  PARENT_SOLO[0:36, :] = 255

# Parent PARTIALLY in frame over post-pickup bedding (2026-07-15 09:00:38 shape: evidence
# frames read presence 0.27–0.30 against a settle anchor of 0.04). Two jiggle variants
# give continuous sub-disturbance motion while presence sits ≫ anchor but < person-scale.
PARENT_BIT_A = BEDDING.copy();  PARENT_BIT_A[0:15, :] = 255
PARENT_BIT_B = BEDDING.copy();  PARENT_BIT_B[3:18, :] = 255


def twitch(base):
    """A living-thing micro-motion frame: 4×4 block toggled (micro, below 'moving')."""
    f = base.copy()
    f[40:44, 28:32] = 255 if base[40, 28] == 0 else 0
    return f


def check_magnitudes(cfg):
    """The synthetic frames must land in the same signal bands as the real footage."""
    tw = active_fraction(BABY, twitch(BABY))
    assert cfg["micromotion_fraction"] < tw < cfg["motion_fraction"], \
        f"twitch {tw:.4f} not in micro band"
    d = active_fraction(DIST_A, DIST_B)
    assert d >= cfg["disturbance_fraction"], f"disturbance {d:.3f} too small"
    for frame, name in [(BEDDING, "bedding"), (BABY, "baby"), (BABY2, "baby2")]:
        p = active_fraction(frame, EMPTY)
        assert p > cfg["presence_threshold"], f"{name} presence {p:.4f} too small"
    assert active_fraction(EMPTY, EMPTY) == 0.0
    # 2026-07-09 incident bands (scenarios D–F)
    for a, b in [(BABY, PARENT), (EMPTY, PARENT)]:
        g = active_fraction(a, b, pixel_thresh=25)
        assert g > LIGHTING_CEILING, f"guard-scale jump only {g:.2f}"
    for frame, name in [(PARENT, "parent"), (PARENT_HALF, "parent_half")]:
        p = active_fraction(frame, EMPTY)
        assert p >= SETTLE_BLOCKED_PRESENCE, f"{name} presence {p:.2f} below blocked bar"
    ph = active_fraction(EMPTY, PARENT_HALF, pixel_thresh=25)
    assert cfg["disturbance_fraction"] <= ph <= LIGHTING_CEILING, \
        f"parent_half jump {ph:.2f} not in disturbance band"
    r = active_fraction(REACH, BEDDING)
    assert cfg["disturbance_fraction"] <= r, f"reach spike {r:.2f} below disturbance"
    assert active_fraction(REACH, BEDDING, pixel_thresh=25) <= LIGHTING_CEILING
    # Scenario G band: above the default threshold, below the incident override.
    pf = active_fraction(BABY_FAINT, EMPTY)
    assert cfg["presence_threshold"] < pf < 0.05, f"faint baby {pf:.4f} out of band"
    tw = active_fraction(BABY_FAINT, twitch(BABY_FAINT))
    assert cfg["micromotion_fraction"] < tw < cfg["motion_fraction"], \
        f"faint-baby twitch {tw:.4f} not in micro band"
    # Scenario H band: every gentle-pickup frame step is moving but sub-disturbance.
    seq = [BABY] + GENTLE + [EMPTY]
    for a, b in zip(seq, seq[1:]):
        g = active_fraction(a, b)
        assert cfg["motion_fraction"] < g < cfg["disturbance_fraction"], \
            f"gentle step {g:.3f} not in the sub-disturbance band"
    # Scenario K band: one isolated solo-scale frame, below the lighting guard.
    solo = active_fraction(EMPTY, PARENT_SOLO)
    assert DISTURBANCE_SOLO_FRACTION <= solo, f"solo frame {solo:.2f} below solo bar"
    assert active_fraction(EMPTY, PARENT_SOLO, pixel_thresh=25) <= LIGHTING_CEILING
    # Scenario J bands: presence ≫ anchor but sub-person-scale; jiggle is sub-disturbance.
    anchor = active_fraction(BEDDING, EMPTY)
    pb = active_fraction(PARENT_BIT_A, EMPTY)
    assert pb - anchor > EVIDENCE_PRESENCE_DELTA, \
        f"parent-bit presence delta {pb - anchor:.2f} too small to test the rule"
    assert pb < SETTLE_BLOCKED_PRESENCE, f"parent-bit {pb:.2f} is person-scale"
    jig = active_fraction(PARENT_BIT_A, PARENT_BIT_B)
    assert cfg["motion_fraction"] < jig < cfg["disturbance_fraction"], \
        f"jiggle {jig:.3f} not in the moving band"
    entry = active_fraction(BEDDING, PARENT_BIT_A)
    assert entry < cfg["disturbance_fraction"], f"parent-bit entry {entry:.2f} would disturb"


class Driver:
    def __init__(self, cfg):
        self.sessions = []
        log = logging.getLogger("test")
        log.setLevel(logging.WARNING)
        self.m = SleepStateMachine(
            cfg, reference=EMPTY.copy(), log=log,
            on_session_start=self._start, on_session_end=self._end)
        self.m.prev = EMPTY.copy()
        self.t = datetime(2026, 7, 7, 12, 0, 0)

    def _start(self, t):
        self.sessions.append({"start": t, "end": None})
        return len(self.sessions) - 1

    def _end(self, sid, t):
        if sid is not None and self.sessions[sid]["end"] is None:
            self.sessions[sid]["end"] = t

    def still(self, frame, seconds):
        for _ in range(int(seconds)):
            self.m.step(frame, self.t)
            self.t += timedelta(seconds=1)

    def disturbance(self, seconds):
        for i in range(int(seconds)):
            self.m.step(DIST_A if i % 2 == 0 else DIST_B, self.t)
            self.t += timedelta(seconds=1)

    def twitch_cluster(self, base, seconds=3):
        for i in range(int(seconds)):
            self.m.step(twitch(base) if i % 2 == 0 else base, self.t)
            self.t += timedelta(seconds=1)
        self.still(base, 1)


def settle_into_sleep(d):
    """Shared preamble: empty crib → put-down → probation confirmed → ASLEEP."""
    d.still(EMPTY, 30)
    d.disturbance(20)                    # put-down
    d.still(BABY, 60)                    # settle → occupied → AWAKE on probation; wait out
                                         # the EVIDENCE_SUPPRESS window before twitching
    d.twitch_cluster(BABY); d.still(BABY, 100)   # episode 1, then quiet ≥ gap
    d.twitch_cluster(BABY); d.still(BABY, 30)    # episode 2 → probation cleared
    d.still(BABY, 11 * 60)               # stillness → ASLEEP (backdated)
    assert d.m.state == STATE_ASLEEP, f"preamble failed: state={d.m.state}"
    assert len(d.sessions) == 1 and d.sessions[0]["end"] is None


def scenario_a(cfg):
    """Missed pickup: settled frame is a bedding ghost (≠ reference). The session must
    close via probation expiry, backdated to the pickup disturbance."""
    d = Driver(cfg)
    settle_into_sleep(d)
    pickup_at = d.t
    d.disturbance(15)                    # pickup
    d.still(BEDDING, 25 * 60)            # empty crib, zero micro-motion, forever-still
    s = d.sessions[0]
    assert s["end"] is not None, \
        "BUG REPRODUCED: session never closed after a missed pickup (stuck until max-cap)"
    err = abs((s["end"] - pickup_at).total_seconds())
    assert err <= cfg["settle_seconds"] + 5, f"end not backdated to pickup (off by {err:.0f}s)"
    assert d.m.state == STATE_AWAY, f"state={d.m.state}, want away"
    assert active_fraction(d.m.reference, BEDDING) == 0.0, "reference not self-healed"
    print(f"  A missed pickup: session closed {err:.0f}s from pickup, state=away, "
          f"reference healed  ✓")


def scenario_b(cfg):
    """True startle: baby stays. The SAME nap must continue (no fragmentation, no close)."""
    d = Driver(cfg)
    settle_into_sleep(d)
    d.disturbance(6)                     # startle / limb-fling
    d.still(BABY2, 15)                   # settle → still occupied → resume on probation
    for _ in range(6):                   # her ~2–3 min episode cadence re-confirms
        d.twitch_cluster(BABY2)
        d.still(BABY2, 120)
    assert len(d.sessions) == 1, f"nap fragmented into {len(d.sessions)} sessions"
    assert d.sessions[0]["end"] is None, "nap wrongly ended after a survivable startle"
    assert d.m.state == STATE_ASLEEP, f"state={d.m.state}, want asleep"
    assert d.m.probation_deadline is None, "probation not cleared by micro-motion"
    print("  B true startle: same nap continues, probation cleared, no fragmentation  ✓")


def scenario_c(cfg):
    """Clean pickup: settled frame matches the reference — ends immediately, backdated."""
    d = Driver(cfg)
    settle_into_sleep(d)
    pickup_at = d.t
    d.disturbance(15)
    d.still(EMPTY, 60)                   # settle → empty → departure
    s = d.sessions[0]
    assert s["end"] is not None and d.m.state == STATE_AWAY
    err = abs((s["end"] - pickup_at).total_seconds())
    assert err <= cfg["settle_seconds"] + 5, f"end not backdated to pickup (off by {err:.0f}s)"
    print(f"  C clean pickup: session closed {err:.0f}s from pickup (path unchanged)  ✓")


def scenario_d(cfg):
    """2026-07-09 pickup shape: parent fills the ROI (entry > guard ceiling), stands
    quietly long enough to trigger a settle attempt (which must be DEFERRED — presence is
    the parent, not the crib), then leaves with the baby in one guard-scale jump (which
    must open a disturbance, not be skipped). Session closes backdated to the arrival."""
    d = Driver(cfg)
    settle_into_sleep(d)
    pickup_at = d.t
    d.still(PARENT, 60)                  # arrival >80% change; then leaning quietly
    assert d.sessions[0]["end"] is None, "settle verdict taken while parent blocked the ROI"
    d.still(EMPTY, 40)                   # departure with baby: another >80% jump
    s = d.sessions[0]
    assert s["end"] is not None, \
        "BUG REPRODUCED: guard swallowed the departure — no disturbance, session stuck"
    err = abs((s["end"] - pickup_at).total_seconds())
    assert err <= 5, f"end not backdated to parent arrival (off by {err:.0f}s)"
    assert d.m.state == STATE_AWAY, f"state={d.m.state}, want away"
    assert active_fraction(d.m.reference, EMPTY) == 0.0, "reference not refreshed"
    print(f"  D blocked settle + guard-scale departure: closed {err:.0f}s from pickup  ✓")


def scenario_e(cfg):
    """Parent visits during post-pickup probation (isolated disturbance-scale spikes with
    micro-scale flanks — the 06:50 pattern) must NOT count as life evidence."""
    d = Driver(cfg)
    settle_into_sleep(d)
    pickup_at = d.t
    d.disturbance(15)                    # pickup
    d.still(BEDDING, 15)                 # settle → bedding ghost → resume on probation
    for _ in range(2):                   # two parent visits, 90s apart (the 06:50 pattern:
        d.m.step(REACH, d.t)             # isolated disturbance-scale frames — enter,
        d.t += timedelta(seconds=1)      # lean motionless, leave — too sparse to open
        d.still(REACH, 8)                # an episode, exactly like the real footage)
        d.m.step(BEDDING, d.t)
        d.t += timedelta(seconds=1)
        d.twitch_cluster(BEDDING)        # micro-scale flank right after the spike
        d.still(BEDDING, 90)
    d.still(BEDDING, 20 * 60)            # empty crib, probation must expire
    s = d.sessions[0]
    assert s["end"] is not None, \
        "BUG REPRODUCED: parent visits faked micro-motion evidence, probation cleared"
    err = abs((s["end"] - pickup_at).total_seconds())
    assert err <= cfg["settle_seconds"] + 5, f"end not backdated to pickup (off by {err:.0f}s)"
    assert d.m.state == STATE_AWAY, f"state={d.m.state}, want away"
    print(f"  E parent visits during probation: not evidence, closed {err:.0f}s from pickup  ✓")


def scenario_f(cfg):
    """Affirmative-empty fast path: probation over a crib that MATCHES the empty
    reference must close in ~PROBATION_EMPTY_MATCH_SECONDS, not the full deadline.
    Path: pickup → settle reads a bedding ghost → nap resumes on probation → the
    blanket slumps flat in one sub-disturbance frame → crib matches the reference →
    fast close, backdated to the pickup. (The old shape of this scenario — a parent
    half-in-frame whose single-frame departure went unseen — is now caught upstream
    by the DISTURBANCE_SOLO_FRACTION rule and closes as a plain departure.)"""
    d = Driver(cfg)
    settle_into_sleep(d)
    pickup_at = d.t
    d.disturbance(15)                    # pickup
    d.still(BEDDING, 15)                 # settle → bedding ghost → resume on probation
    assert d.sessions[0]["end"] is None
    d.still(EMPTY, 120)                  # blanket settles flat (one 0.2 frame, sub-
                                         # disturbance) → perfect reference match
    s = d.sessions[0]
    # only the fast path can close within 120s — probation expiry is minutes away
    assert s["end"] is not None, "empty-match fast path did not close the session"
    err = abs((s["end"] - pickup_at).total_seconds())
    assert err <= cfg["settle_seconds"] + 5, f"end not backdated to pickup (off by {err:.0f}s)"
    assert d.m.state == STATE_AWAY, f"state={d.m.state}, want away"
    assert active_fraction(d.m.reference, EMPTY) == 0.0, "reference not refreshed"
    print(f"  F empty-match fast path: closed within probation, backdated {err:.0f}s  ✓")


def scenario_g(cfg):
    """2026-07-14 missed put-down: baby deposited, dashboard said the crib was empty.
    The Pi ran sleep_presence_threshold=0.05 (baked into settings.json long ago) against
    sleeping-baby presence measured 0.06–0.075 — so a faint settled frame reads "empty",
    the machine goes AWAY, and the reference is refreshed WITH the baby in it. A quiet
    newborn then never produces enough micro-motion for the Path-2 override, and the
    dashboard shows an empty crib all nap. The default 0.02 must keep this put-down."""
    def run(c):
        d = Driver(c)
        d.still(EMPTY, 30)
        d.disturbance(20)                                 # put-down
        d.still(BABY_FAINT, 60)                           # settle verdict on faint baby;
                                                          # wait out evidence suppression
        d.twitch_cluster(BABY_FAINT); d.still(BABY_FAINT, 100)   # sparse early twitches
        d.twitch_cluster(BABY_FAINT); d.still(BABY_FAINT, 30)
        for _ in range(2):                                # a quiet newborn still stirs every
            d.still(BABY_FAINT, 11 * 60)                  # ~11 min: under the 20-min liveness
            d.twitch_cluster(BABY_FAINT)                  # backstop, but too sparse for the
        d.still(BABY_FAINT, 5 * 60)                       # Path-2 override (needs 2 in 10 min)
        return d
    d = run(cfg)
    assert d.m.state == STATE_ASLEEP, f"faint put-down lost: state={d.m.state}"
    assert len(d.sessions) == 1 and d.sessions[0]["end"] is None, "nap not recorded"

    bad = dict(cfg)
    bad["presence_threshold"] = 0.05      # the incident Pi's settings.json override
    d2 = run(bad)
    assert d2.m.state == STATE_AWAY and not d2.sessions, \
        "incident shape no longer reproduces under 0.05 — update this scenario's bands"
    print("  G faint put-down: kept at default threshold (ASLEEP, session open); "
          "incident override 0.05 loses the baby as documented  ✓")


def gentle_pickup(d, end_frame):
    """Motion entirely below the disturbance threshold (2026-07-16 02:42: peak 0.13),
    ending on end_frame. Returns the time the crib became still again."""
    for f in [*GENTLE, end_frame]:
        d.m.step(f, d.t)
        d.t += timedelta(seconds=1)
    return d.t


def scenario_h(cfg):
    """2026-07-16 02:42 silent pickup: a gentle lift never fires a disturbance, but the
    crib then MATCHES the trusted-empty reference at zero micro-motion. The silent-
    departure close must end the session; without reference trust it must NOT fire
    (the reference could contain the baby)."""
    d = Driver(cfg)
    settle_into_sleep(d)
    d.m.reference_trusted = True         # as if the pre-put-down settle was a confirmed
                                         # empty (it was: the preamble starts on EMPTY)
    empty_at = gentle_pickup(d, EMPTY)   # no frame reaches the disturbance bar
    d.still(EMPTY, LATCHED_EMPTY_MATCH_SECONDS + 60)
    s = d.sessions[0]
    assert s["end"] is not None, \
        "BUG REPRODUCED: silent pickup left the session open (2026-07-16 02:42 shape)"
    err = abs((s["end"] - empty_at).total_seconds())
    assert err <= 5, f"end not backdated to the empty-match start (off by {err:.0f}s)"
    assert d.m.state == STATE_AWAY, f"state={d.m.state}, want away"

    # Trust gate: the identical run with an untrusted reference must hold the latch.
    d2 = Driver(cfg)
    settle_into_sleep(d2)
    gentle_pickup(d2, EMPTY)
    d2.still(EMPTY, LATCHED_EMPTY_MATCH_SECONDS + 60)
    assert d2.sessions[0]["end"] is None, \
        "silent-departure close fired against an UNTRUSTED reference"
    print(f"  H silent pickup: closed {err:.0f}s from empty-match start via trusted "
          f"reference; untrusted reference correctly holds  ✓")


def scenario_i(cfg):
    """2026-07-15 phantom-afternoon shape: after a missed pickup the bedding ghost
    (presence ≫ threshold) defeats every reference test, but the crib produces ZERO
    micro-motion. The liveness backstop must close the session at ~sleep_liveness_minutes,
    backdated to the last life sign."""
    d = Driver(cfg)
    settle_into_sleep(d)
    last_life = gentle_pickup(d, BEDDING)   # unseen pickup; ghost stays behind
    d.still(BEDDING, cfg["liveness_seconds"] + 120)
    s = d.sessions[0]
    assert s["end"] is not None, \
        "BUG REPRODUCED: bedding-ghost phantom survived (session 394 ran 9h on this)"
    err = abs((s["end"] - last_life).total_seconds())
    assert err <= 5, f"end not backdated to the last life sign (off by {err:.0f}s)"
    assert d.m.state == STATE_AWAY, f"state={d.m.state}, want away"
    print(f"  I liveness backstop: ghost phantom closed at {cfg['liveness_seconds'] // 60}m, "
          f"backdated {err:.0f}s from last life sign  ✓")


def scenario_j(cfg):
    """2026-07-15 09:00:38 false clear: during an in-sleep arousal probation, a parent
    partially in the ROI produced 'sustained motion' whose every frame read presence far
    above the settle anchor. Evidence that RAISES presence is an intruder, not the baby —
    it must not clear probation; expiry closes the session backdated to the pickup."""
    d = Driver(cfg)
    settle_into_sleep(d)
    pickup_at = d.t
    d.disturbance(15)                    # pickup
    d.still(BEDDING, 60)                 # settle → ghost → resume on probation (anchor =
                                         # BEDDING presence); wait out taint suppression
    for i in range(90):                  # parent hovers 90s: continuous sub-disturbance
        d.m.step(PARENT_BIT_A if i % 2 == 0 else PARENT_BIT_B, d.t)
        d.t += timedelta(seconds=1)      # motion, presence ≫ anchor — old code cleared
    d.m.step(BEDDING, d.t)               # this as 'sustained motion' within ~20s
    d.t += timedelta(seconds=1)
    d.still(BEDDING, 25 * 60)            # empty crib: probation must expire
    s = d.sessions[0]
    assert s["end"] is not None, \
        "BUG REPRODUCED: presence-raising motion cleared probation (09:00:38 shape)"
    err = abs((s["end"] - pickup_at).total_seconds())
    assert err <= cfg["settle_seconds"] + 5, f"end not backdated to pickup (off by {err:.0f}s)"
    assert d.m.state == STATE_AWAY, f"state={d.m.state}, want away"
    print(f"  J presence-jump evidence: parent hover not evidence, closed {err:.0f}s "
          f"from pickup  ✓")


def scenario_k(cfg):
    """2026-07-16 04:41 slow put-back: ONE person-scale frame (0.849 live), every other
    frame sub-disturbance — the old pair rule (2 ≥0.30 frames in 4s) never fired and the
    put-back was invisible. The solo rule must open a disturbance so the settle machinery
    rules 'occupied' and a session eventually starts."""
    d = Driver(cfg)
    d.still(EMPTY, 30)
    d.m.step(PARENT_SOLO, d.t)           # the single big frame
    d.t += timedelta(seconds=1)
    assert d.m.in_disturbance, "solo person-scale frame did not open a disturbance"
    d.m.step(BABY, d.t)                  # parent lowers baby, leaves
    d.t += timedelta(seconds=1)
    d.still(BABY, 60)                    # settle → occupied → AWAKE on probation
    d.twitch_cluster(BABY); d.still(BABY, 100)
    d.twitch_cluster(BABY); d.still(BABY, 30)    # probation cleared
    d.still(BABY, 11 * 60)               # stillness → ASLEEP
    assert d.m.state == STATE_ASLEEP, f"state={d.m.state}, want asleep"
    assert len(d.sessions) == 1 and d.sessions[0]["end"] is None, "put-back nap not recorded"
    print("  K slow put-back: solo frame opened the disturbance, session recorded  ✓")


def main():
    cfg = build_cfg({})   # storage.DEFAULT_SETTINGS
    cfg["crib_roi"] = [0, 0, 1, 1]
    check_magnitudes(cfg)
    print("Synthetic ASLEEP→pickup regression (cfg = DEFAULT_SETTINGS):")
    scenario_a(cfg)
    scenario_b(cfg)
    scenario_c(cfg)
    scenario_d(cfg)
    scenario_e(cfg)
    scenario_f(cfg)
    scenario_g(cfg)
    scenario_h(cfg)
    scenario_i(cfg)
    scenario_j(cfg)
    scenario_k(cfg)
    print("All scenarios pass.")


if __name__ == "__main__":
    main()
