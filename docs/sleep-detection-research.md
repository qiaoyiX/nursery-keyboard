# Sleep Detection Research — Why It Failed, What We Learned, and the v5 Algorithm

**Status:** v5 ("event-gated latched presence") implemented 2026-07-02; validated against three
real recordings (§6a): 2 h empty+put-down (7/2), overnight awake-baby pickup (7/3), and a full
put-down-to-ASLEEP cycle (7/4, which drove the stir-tolerant stillness rule). Empty crib,
put-down, active/quiet sleep, awake baby, pickup, bedding changes, and
bootstrap-poisoned-reference self-healing all match ground truth in simulation. The one path no
recording exercises — a pickup that starts from confirmed ASLEEP — failed live on 2026-07-07
(unconditional arousal-resume swallowed missed pickups → multi-hour phantom sessions); fixed by
resume-on-probation and locked by the synthetic regression `tests/test_arousal_probation.py`.
Still wanted: real footage of an ASLEEP→pickup to confirm on camera.
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
| disturbance | `motion_frac ≥ sleep_disturbance_fraction` (default **0.30**) for ≥2 consecutive frames | parent-scale event (above awake-baby squirming 0.10–0.17, below pickup peaks 0.57–1.0) |
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
  bootstrap-poisoned), so demand life evidence: sustained motion, or ≥2 micro-motion episodes
  separated by ≥60 s, within `sleep_probation_minutes` (default 15) — otherwise conclude the
  "presence" was a bedding ghost → **AWAY**, backdated to the disturbance end, and refresh the
  reference. *This makes a
  poisoned reference self-healing: the first pickup after a bad bootstrap ends in probation → no
  micro-motion → AWAY + correct reference saved.* **While probation is pending, the AWAKE→ASLEEP
  stillness timer is held** — a sleep session must never start on unconfirmed occupancy (observed
  on real footage: 10 min of empty-crib stillness during probation produced a phantom ASLEEP
  before this guard existed).

**Path 2 — Life-evidence override (AWAY → AWAKE).** While AWAY, **sustained motion** (the
60%-density test — an awake baby; an empty crib can produce at most 1–3 stray frames at a time,
never density) flips to AWAKE outright; **≥2 micro-motion episodes separated by ≥60 s of quiet**
within a rolling 10-minute window flips to AWAKE **on probation** — weaker evidence (two parent
reach-ins could fake it), so the probation machinery guards the flip and a false fire self-corrects
to AWAY (prior P2). Catches: daemon started with baby already in crib, a
placement whose disturbance was missed (camera reconnecting), bootstrap-with-baby. The episode
separation came from real footage (§6): a brief parent reach-in produces a single 2–4 s motion
cluster whose magnitude (~0.004–0.017) overlaps the sleeping-baby twitch range (~0.002–0.012) —
magnitude cannot separate them, temporal spread can. (Episodes, not calendar-minute buckets: a
single cluster straddling a minute boundary must not count as two pieces of evidence.) Probation
clearing (Path 1) uses the same evidence with a lower bar: sustained motion or **≥2 separated
episodes**. Two hardening rules on episode evidence (both from real footage, 2026-07-06):
an episode earns credit only after ending quietly for `EPISODE_CONFIRM_COOLDOWN` (15 s) —
micro-motion that escalates straight into a disturbance is a **parent approaching**, not the baby
(observed to falsely clear probation 11 s before a blanket-removal disturbance and mint a 28-min
phantom session in regression); and a probation deadline reached with **exactly 1** episode is
ambiguous (empty cribs measured 0 episodes ×3 clips, 1 ×1; sleeping baby averages 1 per ~3 min) —
it extends once rather than ruling the crib empty.

**Path 3 — Manual calibration.** The "📷 Crib is empty" button: save reference, force AWAY, end any
open session. Unchanged; still authoritative.

**Path 4 — Max-session cap.** `sleep_max_session_hours` (14 h) force-end. Unchanged; now expected
to ~never fire.

### Waking from ASLEEP is deliberately hard (actigraphy rescoring)

Infants spend ~50% of sleep in active/REM sleep — startles, Moro reflexes, limb-flings, rooting,
position shifts, squirms — **without waking**. Wrist-actigraphy sleep scoring (Cole-Kripke's 7-min
weighted window; Sadeh; [Webster rescoring](https://pmc.ncbi.nlm.nih.gov/articles/PMC12697920/):
"a short wake block embedded in sleep is rescored back to sleep") exists precisely because a single
active epoch does not mean awake — only a *run* of active epochs over minutes does. v5's original
wake rule (motion in ≥60% of a **20 s** window, or any 2-frame ≥0.30 disturbance) was far too
twitchy by that standard and scored ordinary in-sleep movement as waking. Two changes bring it in
line:

1. **A disturbance while ASLEEP is a candidate arousal, not an automatic wake.** It no longer closes
   the session on the spot. The settle evaluation decides: crib now empty → real pickup, end the nap
   (backdated to the disturbance start); still occupied → resume the *same* nap **on probation**, the
   burst rescored as sleep (no fragmentation, end-time untouched). A startle/limb-fling, however
   large, is absorbed once the baby's micro-motion cadence re-confirms occupancy.

   ⚠️ **2026-07-07 field failure — why the probation is not optional.** As first shipped, the resume
   was unconditional, and "still occupied" comes from the reference — which *always lies after a
   pickup* (bedding rearranged; measured 0.127 presence over an empty crib on 7/4, vs the 0.02
   threshold). Every missed pickup therefore resumed a nap over an empty crib, and since an empty
   crib produces no active epochs and no further disturbance, nothing could end the session but the
   14 h cap. Worse, each missed pickup also skipped the settle-time reference refresh, so the
   reference grew staler and the next miss *more* likely — self-worsening, and the "📷 Crib is
   empty" button only patched one incident at a time. Live symptom: multi-hour phantom "sleep"
   periods. The resume-on-probation restores Path 1's self-healing guarantee: a sleeping baby
   re-confirms at her ~3-min episode cadence (7/6 measurements) and the nap continues seamlessly;
   zero evidence → probation expiry closes the session **backdated to the disturbance start** (the
   real pickup) and refreshes the reference. Regression-locked by
   `tests/test_arousal_probation.py` (synthetic ASLEEP→pickup, the one path no recording contains).
2. **A self-wake (no pickup) ends the nap only on sustained multi-epoch motion.** Frames are scored
   into `WAKE_EPOCH_SECONDS` (30 s) epochs; an epoch is "active" if ≥ `WAKE_EPOCH_ACTIVE_FRAC` (0.5,
   i.e. ≥15 s) of it moved; waking requires `sleep_wake_minutes` (default 3) of *consecutive* active
   epochs, backdated to the start of that run. Active-sleep bouts (measured: 1–7 s runs, ≤1 active
   epoch) never reach it; a genuinely waking baby sustaining motion for minutes does. This is
   separate from the short 20 s `sustained` test, which is kept for the fast probation / AWAY life-
   evidence checks (there we *want* to detect presence quickly).

Validation: all four prior recordings regress identically (put-down still reaches ASLEEP with the
same backdated start; both parent pickups still end as departures; empty crib unchanged, zero
phantom sessions). Because no recording contains a baby *self-waking without a pickup*, the new wake
path was proven with a synthetic driver — brief startle → nap continues; repeated active-sleep
squirms → stays asleep; 3.5 min of continuous motion → wakes with correctly backdated end; pickup →
ends as departure. **`sleep_wake_minutes` = 3 is a sleep-science default, not yet tuned to a real
false-wake clip** — pending footage of the reported failure (baby scored awake while asleep).

### ASLEEP/AWAKE (stir-tolerant stillness, density-based wakefulness)

Stillness ≥ `sleep_min_minutes` → ASLEEP (start backdated to stillness start); **sustained** motion
→ AWAKE (end backdated to the first frame of the sustained burst). "Sustained" is a rolling density
test (`MOTION_DENSITY = 0.6`): motion frames must fill ≥60% of the trailing `sleep_wake_seconds`
window. This came from 2026-07-04 footage: a newborn in *active sleep* stirs for 1–7 s every 1–3
minutes (longest fully-still stretch observed: 7.5 min), so the earlier rule — any single moving
frame resets the stillness timer — kept a visibly sleeping baby "awake" forever. Awake squirming
fills ~98% of frames, so density separates the two cleanly where magnitude cannot (stirs reach
0.077, awake p50 is 0.104). Isolated stirs neither reset the stillness timer nor wake a session.
Further rules:
- **The AWAKE→ASLEEP transition is gated on probation being clear** — a session must never start
  on unconfirmed occupancy (10 min of empty-crib stillness during probation once produced a
  phantom ASLEEP). The stillness *timer* keeps running through probation, so once micro-motion
  confirms the baby, the session start is still backdated to when stillness truly began.
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

## 6a. Measured results (2026-07-02 footage — thresholds are now measurements, not guesses)

2 h recorded via `record_camera.sh` (12 × 10-min segments, 1280×720 @ 15 fps, IR mode).
Ground truth from visual frame inspection: **empty crib 18:04→19:55** (with three brief parent
visits), **put-down 19:54:40–19:55:00**, **swaddled baby asleep 19:55→20:04**. Analysis at 1 fps
through the exact daemon pipeline, crib ROI `[0.10, 0.07, 0.80, 1.00]`:

| Signal (crib ROI) | Measured | Threshold it validates |
|---|---|---|
| Empty-crib motion noise floor | p50 = p90 = **0.00000**, quiet-period max ≈ 0.0005 | `sleep_micromotion_fraction` 0.002 has real margin — **but only inside the ROI** |
| Full-frame motion (OSD clock!) | constant 0.0001–0.0017 | The TAPO timestamp overlay alone nearly reaches the micro-motion threshold: **the ROI must exclude it** (or disable the OSD in the Tapo app) |
| Sleeping-baby twitches | 3 frames > 0.002 in 9 min (0.0027–0.012), spread across minutes | Probation confirm (≥2 distinct minutes in 15 min) clears; `sleep_motion_fraction` 0.01 correctly reads the baby as "still" |
| Parent reach-in (19:01) | one 3 s cluster, 0.004–**0.017** | Below disturbance (0.10), overlaps twitch range → motivated the distinct-minutes rules |
| Put-down / parent-over-crib | 0.57–**1.00** | `sleep_disturbance_fraction` 0.10 catches every real event with huge margin |
| Baby presence vs empty reference | **0.116** stable | 5.8× above `sleep_presence_threshold` 0.02 — clean separation |
| Empty-crib presence drift | 0.005 → 0.0116 over 2 h | Would near the 0.02 threshold in ~4 h if static — the settle-time refresh + AWAY drift keep it pinned in practice |

**Night footage (2026-07-03 00:40–01:10, awake baby → pickup at 00:41 → empty)** added the
occupied-awake side and a real pickup:

| Signal (crib ROI) | Measured | Consequence |
|---|---|---|
| Awake baby squirming | p50 = **0.104**, max 0.17 | Overlapped the old 0.10 disturbance threshold — half of awake frames opened bogus "parent" episodes. **`sleep_disturbance_fraction` raised to 0.30** (pickups peak 0.57–1.0; clean gap both ways). |
| Pickup | first crossing 0.53, peak 0.94 | Fires the 0.30 threshold with margin |
| Empty crib at night (IR, 20 min) | motion max **0.00000** | Noise floor holds at night |

This recording also exercised the **bootstrap-poisoned reference end-to-end**: the recording opens
with the baby in frame, so the simulated bootstrap baked the baby into the reference; after the
pickup, settle evaluation read "occupied" (empty crib ≠ baby-reference), probation got zero
micro-motion, ruled AWAY, and refreshed the reference — self-healing exactly as designed. It also
exposed the **phantom-ASLEEP bug**: the stillness timer ran during that unconfirmed probation and
declared ASLEEP on an empty crib at the 10-min mark. Fixed by holding the stillness timer while
probation is pending (§5 Path 1).

**Put-down footage (2026-07-04 17:19–17:49, empty-with-blanket → put-down 17:25 → swaddled baby
asleep from ~17:32)** added the settle-into-sleep phase:

| Signal (crib ROI) | Measured | Consequence |
|---|---|---|
| Active-sleep stirs | 0.010–0.077 in 1–7 s clusters, every 1–3 min; longest fully-still stretch **7.5 min** | Single-frame stillness resets meant ASLEEP never fired → replaced with the 60%-density sustained-motion rule |
| Put-down + swaddling | three disturbance episodes 17:25–17:32, peaks 0.49–0.78 | Multi-episode put-downs handled; probation restarted per settle, cleared at 17:35 by real micro-motion |

Simulated result after the fix: AWAKE at 17:25:34, **ASLEEP at 17:42:07 with the session backdated
to 17:32:06** — the same minute the parent finished swaddling per the frames. The bedding change
across this put-down (draped blanket removed) also confirmed reference refreshes handle scene
changes.

**Pickup footage (2026-07-04 18:15–18:45, awake baby → pickup 18:16 → empty crib with the unwrapped
swaddle left crumpled → blanket removed 18:35)** exercised the bedding-ghost pickup and exposed the
last holes in the evidence rules:

| Finding | Measured | Consequence |
|---|---|---|
| Stray micro-motion on empty crib during probation | one frame, 0.0046, at 18:21:27 (≈1 per 15–35 min across recordings) | A second stray would have falsely confirmed occupancy — and with probation cleared, empty-crib stillness would have produced a phantom ASLEEP session. **Confirm rules hardened.** |
| Minute-bucket boundary bug | (analysis) a single 2–4 s cluster straddling a minute boundary counted as 2 "distinct minutes" | Replaced calendar-minute buckets with **episodes separated by ≥60 s of quiet** — one cluster can never be two pieces of evidence |
| Awake baby = strongest signal | sustained 60%-density motion detected occupancy in 16 s | **Sustained motion now clears probation / fires the AWAY override instantly**; an empty crib cannot produce it |
| Blanket left behind at pickup | settle read presence 0.127 vs poisoned ref; after blanket removal 0.028 vs blanket-ref | Bedding ghosts correctly resolved by probation expiry + reference refresh, at the cost of one bounded false-AWAKE window (≤ `sleep_probation_minutes`) per bedding change |

**Double-pickup footage (2026-07-05 21:53–22:53: asleep baby → pickup 21:58 → put back awake 22:01
→ pickup 22:04 → empty 49 min)** validated the arousal-rescoring release end-to-end and set the
probation window from data:

| Finding | Measured | Consequence |
|---|---|---|
| Empty crib, 49 min | **zero** micro-motion frames | Cleanest empty data yet; stray rate lower than earlier estimates |
| Awake baby after put-back | p50 0.089, 79% frames moving — probation cleared in **13 s** (sustained) | Real-baby confirms are fast |
| Probation confirm times across all clips | 13 s / 3.2 min / 4.2 min (real baby); empty-crib windows never reached 2 episodes | **`sleep_probation_minutes` 15 → 10** — phantom-awake windows a third shorter, 2.4× margin over slowest observed confirm |
| Pickup peaks | 0.57–0.88 | 0.30 disturbance threshold margin re-confirmed |

Both pickups resolved correctly through a baby-poisoned bootstrap reference; zero phantom sessions.
Note: this footage contains **no in-sleep arousal or self-wake**, so `sleep_wake_minutes` (=3)
remains a sleep-science default awaiting a real false-wake clip. Also fixed here: `build_cfg()` had
its own hardcoded fallbacks that silently diverged from `storage.DEFAULT_SETTINGS` — it now merges
`DEFAULT_SETTINGS` as the single source of truth.

**Sleeping-baby night footage (2026-07-06 01:00–01:30, IR, asleep throughout)** — the first
sleeping-baby-only data; it validated the arousal work and drove the empty-vs-asleep separation
tuning:

| Finding | Measured | Consequence |
|---|---|---|
| Night sleep micro-motion | **10 episodes / 30 min** (~1 per 3 min; peaks 0.014–0.157, biggest bout 75 frames) vs empty crib **0 in 49 min** | The discriminator is enormous — rules were the bottleneck, not the signal |
| Worst 10-min window | exactly **2** episodes | Old bars (2 to confirm probation, 3 to override) sat at/over her quiet-night rate → `MICRO_OVERRIDE_EPISODES` 3→2 (landing on probation), probation gains a one-shot extension on partial evidence |
| Old 20 s sustained wake test | fires on **98 frames** of this *sleeping* baby | Reproduces the original false-wake complaint on real data |
| New epoch wake test | longest run 3 active epochs vs 6 needed | **`sleep_wake_minutes` = 3 validated with 2× margin** — no longer just a sleep-science default |
| Parent-approach contamination | micro-frames 11 s before a disturbance falsely cleared probation in regression (7/4 clip) → 28-min phantom | Episodes must end quietly for 15 s before counting (`EPISODE_CONFIRM_COOLDOWN`) |

**End-to-end simulation** (`replay_sleep.py --simulate`, the real `SleepStateMachine` over all 2 h):
state stayed AWAY through the empty period (both parent visits resolved back to AWAY within
minutes via settle evaluation, one after a self-healing probation); the 19:01 reach-in caused **no**
transition; the put-down produced AWAKE with probation cleared by genuine baby micro-motion; zero
phantom sessions. With `sleep_min_minutes` lowered to 5 (footage ends 9 min after put-down), the
ASLEEP transition fired with a correctly backdated session start. **Still unexercised on real
footage: ASLEEP → pickup → session close** — capture a pickup in the next recording. (2026-07-07:
this exact gap failed live — see the ⚠️ note in §"actigraphy" above. Now covered synthetically by
`tests/test_arousal_probation.py`; real footage still wanted.)

## 6b. Tuning & validation plan

**Data collection tooling (added with v5):** `record_camera.sh [minutes]` on the Pi captures the
RTSP stream as 10-minute .mp4 segments with `-c copy` (no transcode, ~0% CPU; 2 h of the 640×360
sub-stream ≈ 400–700 MB) into `recordings/`. Needs `sudo apt install ffmpeg`. `replay_sleep.py`
then runs the **identical** resize→gray→ROI→`active_fraction` pipeline over the recordings on any
machine with opencv and prints per-file motion/presence percentiles plus threshold guidance —
record with the crib empty, with the baby asleep, and across a real put-down/pickup, and the
numbers below become measurements instead of guesses. Recordings are also the training corpus if
the custom-detector option (§7) is ever pursued.

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
