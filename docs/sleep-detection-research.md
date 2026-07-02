# Sleep Detection Research — Why It Failed, What We Learned, and the v5 Algorithm

**Status:** research complete, v5 ("event-gated latched presence") implemented 2026-07-02.
**Audience:** any agent or human picking up this work. This doc is self-contained; read it before
touching `sleep_monitor.py`. See also `sleep-monitor-algorithm.md` (mechanical spec of the code),
`architecture.md` ADR-004…007 (decision records), `backlog.md` H-2/TODO-1 (the overcount bug).

---

## 1. Problem statement

Detect, from a TAPO C110 RTSP camera over a crib, three states — AWAY (crib empty), AWAKE (baby in
crib, moving), ASLEEP (baby in crib, still) — and log sleep sessions with reasonably accurate
start/end times. Constraints:

- **Raspberry Pi 4, no GPU/TPU.** CPU budget ~1 fps of light OpenCV work.
- **H.264 low-bitrate stream + IR night vision:** macroblock artifacts, IR grain, hard day/night
  toggles that change >80% of pixels in one frame.
- **Top-down view of a swaddled/blanketed infant** — the worst case for pretrained person detectors.
- No wearables, no extra sensors (by choice).

The feature shipped four algorithm iterations and was then **disabled on the dashboard** (commit
`72217e2`) because the reported state was "always wrong."

---

## 2. History: four iterations, four failures

| # | Approach (commit) | How it decided presence | Why it failed |
|---|---|---|---|
| 1 | Static background frame (`75524b3`) | absdiff vs one saved empty-crib frame | If the frame was captured with the baby present, the baby was baked into the reference and never detected. |
| 2 | Adaptive running-average background (`4077e00`, `94d78d8`) | absdiff vs continuously blended background | The blend gradually **absorbed the still baby** → false AWAY. Freezing the blend caused an ASLEEP→AWAY deadlock instead. |
| 3 | MOG2 + Farneback optical flow (`dc95371`) | MOG2 foreground blobs; mean flow magnitude for motion | (a) MOG2 absorbed a still baby into its model in ~20 s → false AWAY. (b) Farneback mean-flow noise floor on this low-bitrate IR stream sat **above** any usable threshold → "always moving" → never ASLEEP. |
| 4 | Reference-frame differencing, current pre-v5 (`ade9490`, `25deea1`, `9cc0f05`, `964a9eb`) | `active_fraction(current, reference_frame.npy)` > threshold, per frame, with 2-frame hysteresis | See §3. Sessions never closed (overcounting), phantom naps on empty cribs; a 14 h force-end cap was added as a backstop. Dashboard UI disabled. |

**What iteration 4 got right (keep all of this):**
- `active_fraction(a, b)` = absdiff → per-pixel threshold (20 gray levels) → `MORPH_OPEN` 3×3 →
  changed-pixel fraction. Bounded, cheap, and the *only* motion metric found to be robust to
  H.264/IR noise. `MORPH_OPEN`, not `CLOSE` — CLOSE bridges noise specks into false blobs.
- The IR-toggle guard (skip frames where >80% of pixels change).
- Per-frame INFO logging of every signal + threshold, so tuning happens from `journalctl` numbers
  instead of guesswork.
- The 14 h max-session cap and the manual "📷 Crib is empty" calibration button as backstops.

---

## 3. Root-cause analysis of iteration 4

Every failure traces to one design flaw: **presence was re-decided from scratch on every frame by
comparing the current frame to an absolute reference.** That makes the system only as good as the
reference is *right now*, and the reference goes stale constantly:

1. **Bedding changes.** Parent puts baby down and rearranges the blanket → empty-crib reference no
   longer matches the crib even when empty → after pickup, `presence_frac` stays above threshold →
   phantom "present" → session never closes → overcount (the exact H-2 bug).
2. **Lighting drift.** Sunrise/sunset shifts pixels slowly; the triple-gated drift (lr = 0.02) only
   updates while AWAY, so a long night of ASLEEP lets the reference drift out of date.
3. **Bootstrap poisoning.** The auto-bootstrap (median of first 5 frames) runs whether or not the
   baby is in the crib. Bootstrapping with the baby present *inverts* the logic: empty crib then
   reads as "present" (differs from baby-containing reference) forever.
4. **Threshold flicker.** When `presence_frac` sits *at* the threshold, the 2-frame hysteresis means
   neither `baby_present` nor `baby_absent` ever latches, and the state (often ASLEEP) is held
   indefinitely — overcount candidate (b) in the backlog.

Meanwhile the **motion** signal (`active_fraction(current, previous)`) has no reference to go stale
against and was observed to work. The asymmetry is the lesson: *relative* signals are reliable on
this hardware; *absolute* signals are not.

---

## 4. External research (2026-07-02)

### 4a. Pretrained ML person detection — confirmed dead end for this scene

The [Frigate discussion #15050](https://github.com/blakeblackshear/frigate/discussions/15050)
documents exactly our scenario in the wild: with a COCO-trained detector on an IR nursery camera,
**the blanket was detected as a person at 75% confidence while the actual baby in the crib was
missed** (≤40%). Root cause per the maintainers: COCO contains almost no top-down IR imagery and no
swaddled infants. The recommended workaround in that thread is *motion-only detection for the crib
zone* — independent confirmation of this repo's ADR-004.

Speed is not the blocker — [TFLite EfficientDet-Lite0 benchmarks](https://github.com/tensorflow/examples/blob/master/lite/examples/object_detection/raspberry_pi/README.md)
and [community benchmarks](https://www.ejtech.io/learn/tflite-object-detection-model-comparison)
show INT8 detectors run fine on a Pi 4 at ~1 fps — **accuracy on this scene is.** A custom-trained
model (e.g. Frigate+ style fine-tuning on this camera's own footage) would work but needs hundreds
of labeled frames from this exact camera; parked as a future option (§7).

### 4b. Abandoned/removed-object detection literature — the right framing

Our problem is isomorphic to the classic surveillance problem of "abandoned vs. removed object":
distinguish a *still* foreground object from an empty scene. The standard solution is
[dual background models with different learning rates](https://ijisae.org/index.php/IJISAE/article/view/4298)
([also](https://pmc.ncbi.nlm.nih.gov/articles/PMC11510867/),
[illumination-robust variant](https://www.ncbi.nlm.nih.gov/pmc/articles/PMC6928649/)): a fast
background absorbs still objects quickly, a slow one doesn't; disagreement between the two flags a
static change. Two takeaways:

1. Even the literature can't classify "object added vs. removed" from backgrounds alone — modern
   papers bolt on Mask R-CNN / YOLO for that step, which we can't use (§4a). So **any purely
   pixel-based absolute presence decision has a floor on reliability.** Stop trying to raise it.
2. The dual-background insight — *changes worth evaluating are the ones that arrive as events* — is
   directly usable without ML, because our domain adds priors the surveillance case lacks (§4c).

### 4c. Domain priors that substitute for ML

These are the facts about a nursery that a generic CV pipeline doesn't know:

- **P1 — No teleportation.** A baby cannot enter or leave the crib without a parent-scale
  disturbance: an adult leaning over the crib changes a large fraction of the ROI for several
  seconds. Presence *transitions* are therefore only possible during/after large motion events.
- **P2 — Living things move; bedding doesn't.** Over minutes, an occupied crib always shows
  micro-motion (breathing repositioning, startles, sighs, head turns — infant quiet-sleep bouts run
  ~20 min and are punctuated by movements every few minutes; active sleep is ~half of infant sleep
  and full of movement). An empty crib shows *zero* real motion. Integrated over a window,
  micro-motion is a presence oracle that **needs no reference frame at all**.
- **P3 — A confirmed-empty crib is a free calibration.** The moment we conclude AWAY with high
  confidence, the current frame *is* the correct empty-crib reference, current bedding and lighting
  included. The reference should be refreshed at every such moment, not drifted at lr = 0.02.

Commercial camera-only monitors (Nanit, Miku, Cubo) solve this with infant-specific trained models
plus (for breathing) special blankets or radar — not reachable here, but their public materials
confirm camera-only *sleep/wake* classification is fundamentally motion-based too.

---

## 5. The v5 algorithm: event-gated latched presence

**Design rule: presence is a *latched* state, never re-decided per frame. It changes only through
four explicit paths, each anchored to an event. Between events, the latch holds no matter what the
reference comparison says.** Motion (relative, trustworthy) is primary; reference comparison
(absolute, fragile) is demoted to a hint that is always cross-checked by micro-motion.

### Signals (all computed on the crib ROI, see below)

| Signal | Definition | Threshold (setting) |
|---|---|---|
| `motion_frac` | `active_fraction(curr, prev)` | — |
| disturbance | `motion_frac ≥ sleep_disturbance_fraction` (default **0.10**) for ≥2 consecutive frames | parent-scale event |
| micro-motion | `motion_frac > sleep_micromotion_fraction` (default **0.002**) | living-thing evidence |
| "moving" (awake) | `motion_frac > sleep_motion_fraction` (default 0.01) | unchanged from v4 |
| `presence_frac` | `active_fraction(curr, reference)` | `sleep_presence_threshold` (0.02) |

New: **crib ROI** — `sleep_crib_roi` = `[x0, y0, x1, y1]` as 0–1 fractions of the 320×240 frame
(default full frame). Everything (motion, presence, reference) is computed inside it. Excludes
parents walking past, mobiles, curtains.

### The four presence-transition paths

**Path 1 — Settle evaluation (the main path).** A disturbance episode starts when disturbance-level
motion holds for 2 frames, and ends after `sleep_settle_seconds` (default 10) of sub-disturbance
motion. At the moment it ends:
- If `presence_frac ≤ threshold` → **AWAY**. Any open sleep session ends **backdated to the
  disturbance start**. The settled frame is **saved as the new reference** (prior P3 — this is what
  keeps the reference from ever going stale for long).
- Else → **AWAKE, on probation**: presence says occupied, but the reference might be lying (stale /
  bootstrap-poisoned), so demand corroboration: if no micro-motion frame occurs within
  `sleep_probation_minutes` (default 15), conclude the "presence" was a bedding ghost → **AWAY**,
  backdated to the disturbance end, and refresh the reference. One micro-motion frame clears
  probation. *This makes a poisoned reference self-healing: the first pickup after a bad bootstrap
  ends in probation → no micro-motion → AWAY + correct reference saved.*

**Path 2 — Micro-motion override (AWAY → AWAKE).** While AWAY, ≥3 micro-motion frames within a
rolling 10-minute window flips to AWAKE (prior P2). Catches: daemon started with baby already in
crib, a placement whose disturbance was missed (camera reconnecting), bootstrap-with-baby.

**Path 3 — Manual calibration.** The "📷 Crib is empty" button: save reference, force AWAY, end any
open session. Unchanged; still authoritative.

**Path 4 — Max-session cap.** `sleep_max_session_hours` (14 h) force-end. Unchanged; now expected
to ~never fire.

### ASLEEP/AWAKE (unchanged mechanics, two adjustments)

Stillness ≥ `sleep_min_minutes` → ASLEEP (start backdated to stillness start); sustained motion ≥
`sleep_wake_seconds` → AWAKE (end backdated to motion start). Adjustments:
- A **disturbance while ASLEEP ends the session immediately**, backdated to the disturbance start —
  a parent handling the crib is a wake by definition, and we shouldn't wait out `wake_seconds` of
  ambiguity. If the baby stays asleep after a blanket-adjust, the stillness timer simply restarts
  and backdates a new session; a few minutes of accuracy lost, correctness kept.
- While a disturbance is in progress, normal transitions are suspended (the parent's motion must
  not read as "baby moving").
- After an RTSP reconnect while occupied, probation restarts (we may have missed a pickup during
  the outage).

### Why each v4 failure mode is now closed

| v4 failure | v5 answer |
|---|---|
| Stale reference after bedding change → session never closes | Pickup is a disturbance → settle eval; even if reference lies, probation + no micro-motion → AWAY. Reference refreshed on every confirmed-empty. |
| Bootstrap with baby present → inverted logic forever | First pickup self-heals via probation (Path 1); AWAY-state reference refresh writes a correct baseline. |
| Presence flicker at threshold → hysteresis never latches | Presence is latched; per-frame `presence_frac` cannot flip state at all. Streak/hysteresis code deleted. |
| Lighting drift during a long night | Irrelevant while latched-occupied; reference is refreshed at the next confirmed-empty settle. Triple-gated slow drift kept for long AWAY stretches. |
| Baby-still-for-20-min misread | Micro-motion threshold (0.002) is 5× more sensitive than the "moving" threshold; probation windows are tunable; sessions are never ended by stillness alone outside probation. |

### Known residual risks (be honest with the next agent)

1. **Unusually still baby during probation** (15 min, right after a parent adjusted the crib with
   the baby left in): false AWAY, corrected by Path 2 at the next twitch → a session gap. Tunable
   via `sleep_probation_minutes` / `sleep_micromotion_fraction`.
2. **Micro-motion threshold vs. sensor noise floor**: 0.002 was chosen below the v4 "moving"
   threshold but must sit *above* this camera's empty-crib noise. **Validate from logs** (§6) —
   if empty-crib frames log `motion` above 0.002, raise the setting.
3. **Pet/sibling enters crib area**: micro-motion can't tell a cat from a baby. ROI helps; nothing
   else does without ML.
4. A disturbance that never settles (party in the nursery) postpones evaluation indefinitely —
   harmless; the state simply holds.

---

## 6. Tuning & validation plan

Per-frame logs now include the disturbance/probation flags:
`state=asleep presence=0.0312 (thr 0.020) motion=0.0007 (thr 0.010) micro=0.002 dist=0.100 [probation]`

1. **Noise floor** (do first): crib empty, lights as at night, watch
   `journalctl -u nursery-sleep-monitor -f` for 10 min. `motion` should log ≈0.0000; if it exceeds
   0.002, raise `sleep_micromotion_fraction` above the observed ceiling.
2. **Micro-motion sensitivity**: baby asleep in crib, watch 15 min; micro-motion frames
   (`motion` > threshold) should appear at least every few minutes. If not, lower the threshold
   toward the noise floor.
3. **Disturbance detection**: do a real put-down and a real pickup; each must log a
   `Disturbance started/ended` pair. If a gentle pickup stays under 0.10, lower
   `sleep_disturbance_fraction` (floor: comfortably above the awake-baby "moving" range).
4. **End-to-end**: one full nap cycle — put-down → settle eval says occupied → probation cleared by
   micro-motion → ASLEEP after stillness → pickup → disturbance → settle eval says AWAY, session
   closed with sane times, reference refreshed (log line confirms).
5. Watch the daily total for a week against reality; the 14 h cap warning should never appear.

## 7. Future options if v5 still isn't good enough

- **Custom-trained detector on this camera's footage** (Frigate+ approach): collect ~500 labeled
  frames (empty / baby / parent), fine-tune a MobileNet-SSD or YOLO-nano, run at 0.2 fps as a slow
  corrector of the latch. Highest-effort, highest-ceiling.
- **Higher fps breathing detection** in a chest sub-ROI (temporal FFT over 10 fps, look for
  0.5–1.5 Hz energy): the definitive occupied-and-alive signal, but likely defeated by H.264
  bitrate and IR grain — prototype offline against recorded clips first.
- **mmWave radar presence sensor** (~$20, e.g. LD2410) beside the crib feeding a GPIO: hardware
  answer to presence; camera then only does awake/asleep. Abandons the camera-only constraint.

## Sources

- [Frigate discussion #15050 — blanket detected as person, baby missed](https://github.com/blakeblackshear/frigate/discussions/15050)
- [Frigate motion detection docs](https://docs.frigate.video/configuration/motion_detection/)
- [TFLite object detection on Raspberry Pi (official example, EfficientDet-Lite0)](https://github.com/tensorflow/examples/blob/master/lite/examples/object_detection/raspberry_pi/README.md)
- [TFLite model performance comparison on Pi](https://www.ejtech.io/learn/tflite-object-detection-model-comparison)
- [Abandoned object detection with dual background models + YOLO-NAS](https://ijisae.org/index.php/IJISAE/article/view/4298)
- [Adaptive dual-background modeling + SAO-YOLO (2024)](https://pmc.ncbi.nlm.nih.gov/articles/PMC11510867/)
- [Illumination-robust abandoned object detection](https://www.ncbi.nlm.nih.gov/pmc/articles/PMC6928649/)
- [Non-contact infant sleep apnea detection on a Pi (arXiv 1910.04725)](https://arxiv.org/pdf/1910.04725)
