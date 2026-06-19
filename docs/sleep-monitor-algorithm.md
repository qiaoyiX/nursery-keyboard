# Sleep-Monitor Algorithm Specification

How `sleep_monitor.py` decides whether the baby is AWAY, AWAKE, or ASLEEP from a camera feed.

See also: [`architecture.md`](architecture.md) (ADR-004/005/006/007 cover the design rationale)
and [`backlog.md`](backlog.md) (open issues, including the sleep-overcount diagnosis).

---

## Hardware context

- **Platform:** Raspberry Pi 4, no GPU.
- **Camera:** TAPO C110, RTSP, H.264, automatic IR day/night switching.
- **Frame rate:** ~1 fps (`FRAME_INTERVAL = 1.0` s).
- **Resolution:** every frame resized to 320×240, converted to grayscale.
- **Noise:** H.264 macroblock artifacts; IR grain; periodic IR-toggle frames where >80% of pixels
  change at once.

---

## Core signal primitive: `active_fraction(a, b)`

```
active_fraction(gray_a, gray_b, pixel_thresh=PIXEL_THRESH):
    diff = absdiff(gray_a, gray_b)
    mask = threshold(diff, pixel_thresh, THRESH_BINARY)
    mask = MORPH_OPEN(mask, 3x3 ellipse kernel)   # erode then dilate: remove noise specks
    return (count of white pixels) / (total pixels)   -> [0, 1]
```

`PIXEL_THRESH = 20` gray levels. `MORPH_OPEN` removes isolated noise specks without bridging nearby
pixels into false blobs (the reason it is preferred over `MORPH_CLOSE`).

Both detection signals reuse this one primitive:

| Signal   | Frame pair                                   | Interpretation                                   |
|----------|----------------------------------------------|--------------------------------------------------|
| Presence | `active_fraction(current, reference_empty)`  | High → frame differs from empty crib → present   |
| Motion   | `active_fraction(current, previous_frame)`   | High → scene changed between frames → moving      |

---

## Startup: reference-frame bootstrap

On daemon start the code checks for `reference_frame.npy`:

- **File exists:** load it; used immediately.
- **File absent:** `bootstrap_reference(cap)` collects `BOOTSTRAP_FRAMES = 5` frames at 1 s
  intervals, computes the pixel-wise median (float32 → uint8), saves to disk. ~5 s, noise-averaged.
  A warning is logged that it is provisional and "Crib is empty" should be pressed with the crib
  confirmed empty.

If the camera is not ready, `bootstrap_reference` may return `None`. `compute_presence` returns
`(0.0, False)` when the reference is `None`, so the machine starts safely in `STATE_AWAY`.

---

## Reference update: triple-gated slow drift (`maybe_update_reference`)

The reference drifts toward the current frame at `REFERENCE_UPDATE_LR = 0.02` per frame **only when
all three gates pass**:

| Gate | Condition                                          | Rationale                                            |
|------|----------------------------------------------------|------------------------------------------------------|
| 1    | `state == STATE_AWAY`                              | Never absorb a present baby into the reference        |
| 2    | `motion_frac < NOISE_FLOOR_FRACTION (0.005)`       | Scene genuinely still — nothing moving in empty crib  |
| 3    | `active_fraction(curr, reference) <= presence_thr` | Current already matches reference; blocks inversion    |

When all pass: `reference = 0.98 * reference + 0.02 * current` (`cv2.addWeighted`). This tracks slow
lighting change without re-absorbing a baby.

---

## Lighting-change guard (IR toggle)

Before computing presence/motion each frame:

```
if active_fraction(prev, curr, pixel_thresh=25) > LIGHTING_CEILING (0.80):
    write heartbeat; advance prev_gray; sleep; continue
```

An IR day/night toggle changes nearly the whole frame in one step. Skipping these frames prevents
the lighting event from registering as massive motion or corrupting the reference-update gates.
`pixel_thresh=25` here (vs the default 20) reduces false triggering on ordinary high-motion frames.

---

## Manual calibration ("Crib is empty" button)

1. User presses the button on the dashboard.
2. `app.py` (`POST /sleep/calibrate`) writes a timestamp to `calibrate.flag`.
3. On the next frame loop, `sleep_monitor.py` detects and deletes the flag, saves the current frame
   as `reference_frame.npy`, and loads it into `reference_gray`.
4. Any open sleep session is force-ended at `now`.
5. State resets to `STATE_AWAY`; `still_since`, `motion_since`, `presence_streak`, `absence_streak`
   all clear.

This is the authoritative way to establish a correct empty-crib baseline (see ADR-004 for why an
automatic solution is not possible).

---

## State machine

**States:** `STATE_AWAY`, `STATE_AWAKE`, `STATE_ASLEEP`.

**Per-frame sequence in `run_state_machine`:**

1. Read frame; resize to 320×240; grayscale. On read failure, break inner loop → reconnect.
2. Consume `calibrate.flag` if present.
3. IR guard: if `active_fraction(prev, curr, 25) > 0.80`, heartbeat, advance, sleep, `continue`.
4. `presence_frac, raw_present = compute_presence(curr, reference, presence_threshold)`.
5. `motion_frac = active_fraction(prev, curr)`; `is_motion = motion_frac > motion_fraction`.
6. Log INFO: `state=... presence=... (thr ...) motion=... (thr ...)`.
7. Update streaks: if `raw_present` → `presence_streak += 1`, `absence_streak = 0`; else the inverse.
8. Hysteresis: `baby_present = presence_streak >= 2`; `baby_absent = absence_streak >= 2`.
9. `maybe_update_reference(...)`.
10. Run transitions (table below).
11. `write_sleep_heartbeat(state)`.
12. `prev_gray = curr_gray`.
13. Sleep the remainder of `FRAME_INTERVAL`.

**2-frame hysteresis note:** if presence flickers (raw_present alternates every frame), *both*
`baby_present` and `baby_absent` stay False, so neither transition branch runs and the state is held
for that frame. This is the mechanism behind overcount candidate (b) in the backlog.

**Transition table:**

| Condition                                   | State            | Action |
|---------------------------------------------|------------------|--------|
| `baby_absent`                               | any              | If ASLEEP with open session: `end_sleep_session(id, now)`, push Huckleberry. → AWAY; clear `still_since`/`motion_since`; set `away_since=now` if unset. |
| `baby_present`                              | AWAY             | → AWAKE; clear `still_since`. |
| `baby_present`, `not is_motion`             | AWAKE            | If `still_since is None`: set it, clear `motion_since`. Elif still ≥ `sleep_min_seconds`: `start_sleep_session(still_since)`, → ASLEEP, clear `still_since`. |
| `baby_present`, `is_motion`                 | AWAKE            | Clear `still_since`. |
| `baby_present`, session age ≥ cap           | ASLEEP           | `end_sleep_session(id, now)`, push Huckleberry. → AWAKE; clear session + `motion_since`/`still_since`; log warning. |
| `baby_present`, `is_motion` (cap not hit)   | ASLEEP           | If `motion_since is None`: set it, clear `still_since`. Elif motion ≥ `wake_seconds`: `end_sleep_session(id, motion_since)`, push Huckleberry. → AWAKE. |
| `baby_present`, `not is_motion` (cap not hit)| ASLEEP          | Clear `motion_since`; session continues. |

**Backdating:**
- Sleep **start** is backdated to `still_since` (first frame of the stillness streak).
- Wake **end** is backdated to `motion_since` (first frame of the motion streak).
- Force-end from the cap, and end from `baby_absent`, use `now` (the earlier moment is unknown).

---

## Daemon restart recovery

On startup, `get_open_sleep_session()` is checked. If an open session exists, the daemon resumes in
`STATE_ASLEEP` with `current_session_id`/`current_sleep_start` from storage — preventing a gap in a
genuine overnight sleep across a restart. The trade-off: a restart on an empty crib (where the prior
session was never closed) resumes the phantom session; the 14h cap is the backstop.

---

## Tunables (read from `settings.json` at startup; defaults in `storage.DEFAULT_SETTINGS`)

| Key                        | Default | Unit          | Meaning |
|----------------------------|---------|---------------|---------|
| `camera_rtsp_url`          | `""`    | —             | RTSP URL. Empty: daemon polls every 30 s. |
| `sleep_presence_threshold` | `0.02`  | fraction      | Min fraction differing from reference to count as present. |
| `sleep_motion_fraction`    | `0.01`  | fraction      | Min fraction changed vs previous frame to count as moving. |
| `sleep_min_minutes`        | `10`    | minutes       | Stillness before AWAKE → ASLEEP. |
| `sleep_wake_seconds`       | `20`    | seconds       | Motion before ASLEEP → AWAKE. |
| `sleep_max_session_hours`  | `14`    | hours         | Force-end an open session past this; also clamps displayed duration. |

**Non-tunable constants** (code change required): `PIXEL_THRESH=20`, `LIGHTING_CEILING=0.80`,
`FRAME_INTERVAL=1.0`, `REFERENCE_UPDATE_LR=0.02`, `NOISE_FLOOR_FRACTION=0.005`, `BOOTSTRAP_FRAMES=5`,
`DENOISE_KERNEL` = 3×3 ellipse.

---

## Known limitations

1. **Empty-crib vs still-sleeping-baby** is unsolvable from a single static frame by any vision
   technique. The manual calibration button is the designed resolution.
2. **1 fps granularity:** transitions have ±1 s precision; streaks need `wake_seconds` /
   `sleep_min_minutes*60` consecutive non-skipped frames.
3. **No reference metadata:** `reference_frame.npy` stores raw pixels only. A repositioned camera
   makes the reference silently wrong — no age/dimension check at load (see backlog H-3/H-4).
4. **Restart resumes the last session:** correct for overnight; creates a phantom session on an
   empty crib. The 14h cap is the backstop.
5. **Per-frame INFO logging:** ~86,400 lines/day; set journal size limits on small SD cards.
