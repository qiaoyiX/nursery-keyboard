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

Run:  venv/bin/python tests/test_arousal_probation.py
Scenarios:
  A  missed pickup from ASLEEP (bedding ghost)  → session must close via probation expiry
  B  true in-sleep startle, baby stays          → same nap continues, no fragmentation
  C  clean pickup (settled frame matches ref)   → session closes backdated (unchanged path)
"""

import logging
import os
import sys
from datetime import datetime, timedelta

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sleep_monitor import (  # noqa: E402
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

# Parent-scale disturbance: two frames differing in ~40% of pixels, alternated.
DIST_A = EMPTY.copy();  DIST_A[0:24, :] = 255
DIST_B = EMPTY.copy();  DIST_B[24:48, :] = 255


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
    d.still(BABY, 15)                    # settle → occupied → AWAKE on probation
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


def main():
    cfg = build_cfg({})   # storage.DEFAULT_SETTINGS
    cfg["crib_roi"] = [0, 0, 1, 1]
    check_magnitudes(cfg)
    print("Synthetic ASLEEP→pickup regression (cfg = DEFAULT_SETTINGS):")
    scenario_a(cfg)
    scenario_b(cfg)
    scenario_c(cfg)
    print("All scenarios pass.")


if __name__ == "__main__":
    main()
