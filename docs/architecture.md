# Architecture Decision Records

Decisions behind the nursery-keyboard baby tracker. Each record states the decision, why it
was made, what was rejected, and the consequences we live with.

See also: [`sleep-monitor-algorithm.md`](sleep-monitor-algorithm.md) for the detection algorithm,
[`backlog.md`](backlog.md) for improvements and to-dos, and `../CLAUDE.md` for the operational guide.

---

## ADR-001: Dual-mode storage (JSON + Neon Postgres)

**Decision:** Storage is abstracted behind a public API (`get_entries`, `add_entry`,
`clear_today`, `delete_entry`, `start_sleep_session`, `end_sleep_session`,
`get_sleep_sessions_today`, `get_open_sleep_session`, `write_sleep_heartbeat`,
`read_sleep_status`). At startup, `USE_DB = bool(DATABASE_URL and PSYCOPG2_AVAILABLE)` selects
between two private implementation families: `_pg_*` and `_json_*`.

**Context:** This is a single-Pi household appliance. It must work out of the box with zero
infrastructure. Cloud backup is desirable for durability but not required on day one, and the
operator may not have Postgres credentials at install time.

**Alternatives considered:**
- SQLite-only: still requires a schema and migration, offers no cloud path, harder to inspect remotely.
- Postgres-only: fails hard with no `DATABASE_URL`; requires network access at all times.
- Pluggable backend (ABC): over-engineered for two backends.

**Consequences:**
- Any new event type or field must be added to both implementation families simultaneously.
- JSON IDs are positional list indices (`id = enumerate(entries)`), so `_json_delete_entry`
  shifts indices on deletion. The client always refetches before rendering delete buttons,
  making this safe in practice but fragile under concurrent deletions.
- Settings (`settings.json`) remain JSON-only in both storage modes. This is intentional:
  settings are per-machine (e.g. `camera_rtsp_url` is IP-specific) and do not belong in the
  shared Postgres database.
- Sleep sessions write atomically via `os.replace(tmp, SLEEP_FILE)` in the JSON path to prevent
  corruption from a mid-write crash.

---

## ADR-002: Two separate systemd services

**Decision:** The keypad/Flask backend (`nursery-tracker`, runs `app.py`) and the camera sleep
detector (`nursery-sleep-monitor`, runs `sleep_monitor.py`) are two distinct systemd services
with independent restart policies.

**Context:** Sleep detection requires OpenCV and a live RTSP connection. A failed import or
dropped camera stream must not take down the dashboard. Conversely, restarting the Flask server
to pick up a template change must not interrupt an in-progress sleep session.

**Alternatives considered:**
- Single process with threads: OpenCV's `VideoCapture` is not thread-safe; a blocking frame read
  would stall Flask response processing.
- Single process with multiprocessing: adds IPC complexity with no clear advantage over two
  services sharing the filesystem/database.

**Consequences:**
- The two services communicate only through the storage layer: `sleep_monitor.py` writes sessions
  and a heartbeat file (`sleep_state.json`); `app.py` reads them.
- If `nursery-sleep-monitor` is offline, `read_sleep_status()` returns `"unknown"` after the
  60-second heartbeat timeout, and the dashboard shows "Camera offline" — a safe degraded state.
- **Operational gotcha:** the dashboard HTML (including the "Crib is empty" button) is served by
  `nursery-tracker`. Flask caches the compiled Jinja template in memory, so template changes
  require restarting `nursery-tracker` specifically — restarting only the sleep monitor will not
  refresh the page.
- The `calibrate.flag` file is the only cross-service signal: `app.py` creates it on
  `POST /sleep/calibrate`; `sleep_monitor.py` detects and consumes it on the next frame loop.

---

## ADR-003: Keypad listener as a daemon thread inside the Flask process

**Decision:** `keypad_listener()` runs as a `daemon=True` thread started in `__main__` alongside
`app.run()`. It calls `find_all_sayodevices()`, spawns one `listen_one_interface()` thread per
detected SayoDevice `/dev/input/event*` interface, joins those threads, and rescans after 2
seconds when all threads exit (device disconnect).

**Context:** The SayoDevice keypad presents multiple HID interfaces. A single-interface listener
would silently miss key events from whichever interface the OS assigned to an unexpected path.
Exclusive grab (`dev.grab()`) prevents key events from bleeding into other applications.

**Alternatives considered:**
- Separate systemd service for keypad: unnecessary IPC for two lines of shared state.
- udev-triggered script: stateless, cannot maintain grab, would not detect all interfaces.
- USB HID raw mode: more portable but loses evdev's symbolic key names, making `KEYPAD_KEYS` harder to maintain.

**Consequences:**
- Flask and the keypad share `add_entry()`, protected by `log_lock` (JSON) or Postgres
  transactions. No additional synchronization needed.
- If `evdev` is unavailable (dev machine, macOS), `EVDEV_AVAILABLE = False` and the listener
  returns immediately. The dashboard runs fully via HTTP buttons.
- `keystate == key_down` filtering ensures exactly one event per physical press (ignores repeat/up).
- `key_name` may be a list when a keycode maps to multiple names; the listener takes `key_name[0]`
  before matching against `KEYPAD_KEYS`.

---

## ADR-004: Reference-frame differencing for presence (vs MOG2 vs optical flow vs ML)

**Decision:** Presence (baby-in-crib vs empty crib) is determined by
`active_fraction(current_frame, reference_frame)`, where `reference_frame` is a stored empty-crib
baseline (`reference_frame.npy`). Motion (awake vs asleep) is `active_fraction(current, previous)`.
Both use the same primitive: absdiff → per-pixel threshold (20 levels) → `MORPH_OPEN` speck
removal → fraction of changed pixels.

**Context — the algorithm passed through several iterations, each driven by a real failure:**

1. **Static background frame:** one saved empty-crib frame diffed against each incoming frame.
   Failed: if captured with the baby present, the baby was baked into the reference and never
   detected.
2. **Adaptive background blend:** a per-frame weighted blend. Drifted — gradually absorbed a
   present baby, causing false absence.
3. **MOG2 + Farneback optical flow:** MOG2 with state-dependent learning rate; Farneback mean
   magnitude for motion. Failed two ways: (a) the "sleeping-object problem" — MOG2 absorbed a very
   still baby into its model within ~20s → false AWAY; (b) Farneback mean flow had a noise floor
   above the practical threshold on the low-bitrate H.264/IR stream, so motion read as always-true
   and the machine never reached ASLEEP.
4. **Reference-frame differencing (current):** compare each frame to a stored empty-crib baseline.
   Immune to still-object absorption because the reference is only updated during triple-gated
   trusted-empty periods (see ADR-005). Optical flow was retired and replaced by frame-to-frame
   differencing using the same `active_fraction` primitive.

**ML was considered and rejected:** custom training needs labeled footage from this specific
camera (top-down IR, swaddled baby) and GPU inference unavailable on a Pi 4. A pretrained
person/face detector trained on upright adults cannot reliably distinguish a swaddled infant from
blankets in an IR top-down view, and even correct presence detection would not resolve the core
awake/asleep ambiguity. The fundamental constraint: **empty crib versus a perfectly still sleeping
baby is indistinguishable from a single static frame, regardless of vision technique.** The manual
"Crib is empty" button — which saves the current frame as the new reference — is the authoritative
resolution to this irreducible ambiguity.

**Consequences:**
- `reference_frame.npy` is machine-specific and not committed. A fresh install or camera
  repositioning requires pressing "Crib is empty" once with the crib confirmed empty.
- `active_fraction` is bounded [0, 1], interpretable, and cheap on a Pi 4 at 1 fps.
- `MORPH_OPEN` (erode then dilate) was chosen over `MORPH_CLOSE`: OPEN removes isolated noise
  specks; CLOSE would bridge nearby noise pixels into larger blobs, inflating the fraction.

---

## ADR-005: Triple-gated reference drift (`maybe_update_reference`)

**Decision:** The empty-crib reference drifts very slowly toward the current frame
(`REFERENCE_UPDATE_LR = 0.02` via `cv2.addWeighted`) only when all three conditions hold:
(1) state is `STATE_AWAY`; (2) `motion_frac < NOISE_FLOOR_FRACTION (0.005)`;
(3) `active_fraction(curr, reference) <= presence_threshold`.

**Context:** Gradual lighting changes (sunrise, IR day/night) would cause a fixed reference to
accumulate false-presence readings over hours. But updating the reference while the baby is present
is precisely the failure of the earlier adaptive-blend iteration.

**Consequences:**
- Gate 1 prevents any update while a baby might be present.
- Gate 2 prevents updates if anything is moving in the empty crib.
- Gate 3 prevents inversion entrenchment — if the current frame has already diverged from the
  reference, slow drift would anchor to the wrong value; this gate blocks that.
- IR day/night flip produces >80% pixel change, caught upstream by the lighting guard before this
  function is reached.

---

## ADR-006: Settings-driven runtime tuning with per-frame INFO logging

**Decision:** All detection thresholds (`sleep_presence_threshold`, `sleep_motion_fraction`,
`sleep_min_minutes`, `sleep_wake_seconds`, `sleep_max_session_hours`) are read from `settings.json`
at daemon startup. Every frame logs `state`, `presence_frac`, `motion_frac`, and both thresholds at
INFO level.

**Context:** The Pi is headless. Without per-frame visibility, tuning thresholds requires code
changes, redeploys, and guesswork. The values that matter are the actual fractions produced by this
specific camera on this specific scene.

**Consequences:**
- An operator can `sudo journalctl -u nursery-sleep-monitor -f` with the crib empty then occupied,
  observe the actual `presence` values, and set `sleep_presence_threshold` between the two ranges —
  no code change required.
- The daemon must be restarted to pick up settings changes (read once at `main()` entry, passed by
  value into `run_state_machine()`).
- Per-frame INFO logging at 1 fps is ~86,400 lines/day; configure journal size limits on small SD cards.

---

## ADR-007: Open-session sanity cap (`sleep_max_session_hours`)

**Decision:** If a sleep session has been open longer than `sleep_max_session_hours` (default 14),
`run_state_machine()` force-ends it at `now`, even if presence still reads present.
`today_sleep_stats()` in `app.py` additionally clamps a still-open session's displayed duration to
`max_open_minutes`.

**Context:** Several failure modes leave a session open indefinitely: a camera dropout that misses a
wake event, state-machine bugs, or a daemon restart on an empty crib resuming the previous session.
A still-open session is counted up to `now`, so the dashboard total grows without bound.

**Alternatives considered:** no cap (dashboard becomes useless); short cap of ~4h (would truncate a
genuine overnight); 14h (no healthy infant sleeps >14 continuous hours; catches any stuck session
within one calendar day).

**Consequences:**
- A force-ended session's duration is a sentinel indicating detection was lost, not real sleep.
- The cap is a backstop; it does not fix the root cause of overcounting (see backlog H-2 / TODO-1).

---

## ADR-008: Degradable optional integrations (evdev, Huckleberry, feedback)

**Decision:** Optional integrations fail-soft at import time via `try/except` with a boolean
`*_AVAILABLE` flag. The application starts normally with the feature silently disabled.

**Current state of each integration:**
- **evdev:** Linux-only. `EVDEV_AVAILABLE = False` on macOS dev machines; `keypad_listener()`
  returns immediately and the dashboard works via HTTP buttons.
- **huckleberry_sync:** Installed and working on the Pi. The import guard catches a broad
  `Exception` (an earlier version of the upstream library shipped Python 2 syntax that raised
  `SyntaxError` on import). The current open issue is **credential verification, not the
  integration**: auth reaches Firebase and returns `INVALID_PASSWORD`, which proves the path is
  reachable. Likely causes are whitespace in the stored value (now stripped in `_make_api`), a
  wrong email, or a social-login-only account with no email/password credential.
  `GET /huckleberry/test` surfaces the specific Firebase reason.
- **feedback (planned):** an audible beep per keypress via a new `feedback.py` + `aplay`. Not yet
  built; will follow the same fail-soft `try/except` import pattern.

**Consequences:**
- A fresh clone on macOS runs with `python app.py` and no optional dependencies.
- Every integration is independently disable-able, and a missing one never blocks core logging.
