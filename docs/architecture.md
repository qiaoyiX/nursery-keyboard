# Architecture Decision Records

Decisions behind the nursery-keyboard baby tracker. Each record states the decision, why it
was made, what was rejected, and the consequences we live with.

See also: [`sleep-detection-research.md`](sleep-detection-research.md) for the current detection
algorithm (v5, "event-gated latched presence") — [`sleep-monitor-algorithm.md`](sleep-monitor-algorithm.md)
is the historical v4 spec, superseded 2026-07-02 — [`backlog.md`](backlog.md) for improvements and
to-dos, and `../CLAUDE.md` for the operational guide.

---

## ADR-001: Local-first JSON storage, Neon Postgres as snapshot-only backup

**Decision (updated 2026-07-04, `ff628d8`):** Storage is abstracted behind a public API
(`get_entries`, `add_entry`, `clear_today`, `delete_entry`, `update_entry`, `start_sleep_session`,
`end_sleep_session`, `get_sleep_sessions_today`, `get_sleep_sessions_range(days)`,
`get_open_sleep_session`, `write_sleep_heartbeat`, `read_sleep_status`, `load_settings`,
`save_settings`, `update_setting`). `USE_DB = bool(DATABASE_URL and PSYCOPG2_AVAILABLE)` still
selects between `_pg_*` and `_json_*` implementation families, but **the two are no longer a
symmetric live choice**: `nursery-tracker` and `nursery-sleep-monitor` must always run with
`DATABASE_URL` unset, so every live read/write goes through the `_json_*` path. The `_pg_*` path
is exercised only by a separate batch job, `backup_sync.py`, run every 6 hours by the
`nursery-backup.timer`/`.service` systemd units (installed by `install.sh`), which mirrors both
JSON files into Postgres in one transaction. `export_db.py` does the reverse (Neon → JSON) for
disaster recovery after an SD-card death. `migrate_log.py` (the original one-shot JSON → Postgres
importer) is superseded by `backup_sync.py`. Full runbook: `neon-backup-migration.md`.

**Context:** This is a single-Pi household appliance. It must work out of the box with zero
infrastructure. Cloud backup is desirable for durability but not required on day one, and the
operator may not have Postgres credentials at install time. The original design (2026-06) let
either mode serve live traffic; in production this meant the dashboard's 8-second poll interval
kept Neon's free-tier compute awake nearly 24/7, burning ~180 compute-hours/month against a
5-minute-autosuspend free tier — the fix was to stop reading Neon live at all and treat it purely
as an off-site backup target.

**Alternatives considered:**
- SQLite-only: still requires a schema and migration, offers no cloud path, harder to inspect remotely.
- Postgres-only (live): fails hard with no `DATABASE_URL`; requires network access at all times;
  this is also what caused the compute-cost problem above.
- Pluggable backend (ABC): over-engineered for two backends.

**Consequences:**
- Any new event type or field must be added to both implementation families, but only the
  `_json_*` side is on the live-traffic critical path — `_pg_*` correctness only matters at backup/
  restore time.
- **`DATABASE_URL` must never be set on `nursery-tracker` or `nursery-sleep-monitor`** — it lives
  only in `/etc/nursery-tracker/backup.env` (chmod 600), read solely by `backup_sync.py`.
- `backup_sync.py` refuses to run (and logs, doesn't overwrite) if local files are missing/
  unparseable or local counts collapsed below half of the remote's — a broken Pi must never
  clobber a good backup.
- JSON IDs are positional list indices (`id = enumerate(entries)`), so `_json_delete_entry`
  shifts indices on deletion. The client always refetches before rendering delete buttons,
  making this safe in practice but fragile under concurrent deletions.
- `get_sleep_sessions_today()` / `get_sleep_sessions_range(days)` return sessions *overlapping*
  the window, not just ones whose start falls inside it — otherwise an overnight sleep put down
  the evening before and picked up the next morning would vanish from the day view (`cbdf1e5`,
  2026-07-16).
- Settings (`settings.json`) remain JSON-only in both storage modes, and are never backed up to
  Postgres. This is intentional: settings are per-machine (e.g. `camera_rtsp_url` is IP-specific)
  and do not belong in a shared database. `update_setting(key, value)` writes only the single
  changed key rather than the full merged dict — see ADR-006's consequences for why that matters.
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
4. **Reference-frame differencing (v4, superseded by v5 below):** compare each frame to a stored
   empty-crib baseline. Immune to still-object absorption because the reference is only updated
   during triple-gated trusted-empty periods (see ADR-005). Optical flow was retired and replaced
   by frame-to-frame differencing using the same `active_fraction` primitive. Frozen spec:
   `sleep-monitor-algorithm.md` (historical).
5. **Event-gated latched presence (v5, current, 2026-07-02 onward):** per-frame reference
   comparison no longer flips presence directly. Presence is a *latched* state that changes only
   at explicit events: a parent-scale disturbance (motion above `sleep_disturbance_fraction`)
   opens an episode; once it settles, the frame is compared to the reference — a match sets AWAY
   (and refreshes the reference), a mismatch sets AWAKE on a micro-motion *probation* window. A
   `reference_frame_meta.json` sidecar tracks whether the current reference is `trusted` (set by a
   settle-empty verdict or the calibrate button) or merely inferred (probation expiry, liveness
   timeout) — only trusted references may back the fast "silent-departure close" path. Two more
   exits were added after the 2026-07-15/16 incidents: the **silent-departure close** (latched
   occupied + trusted-empty reference + zero micro-motion for 5 min ⇒ a pickup the camera missed)
   and the **liveness backstop** (ASLEEP with zero micro-motion for `sleep_liveness_minutes`,
   reference-free, catching cases no reference comparison could). Full rationale, every incident
   that drove each rule, and the regression suite: `sleep-detection-research.md`.

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
(1) state is `STATE_AWAY`; (2) `motion_frac < cfg["micromotion_fraction"]` (settings-driven,
default `0.002`, `storage.py`'s `sleep_micromotion_fraction`); (3) `active_fraction(curr, reference)
<= presence_threshold`.

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
- This slow drift is the *trusted* refresh path (see ADR-004's v5 entry): a settle evaluation that
  confirms the crib empty, or the "Crib is empty" button, are the only other writers of a trusted
  reference. Probation expiry and the liveness backstop can also refresh the reference (self-healing
  a stale or baby-poisoned one) but are marked untrusted in `reference_frame_meta.json`, which gates
  them out of the fast silent-departure-close path.

---

## ADR-006: Settings-driven runtime tuning with per-frame INFO logging

**Decision:** All detection thresholds (`sleep_presence_threshold`, `sleep_motion_fraction`,
`sleep_disturbance_fraction`, `sleep_settle_seconds`, `sleep_micromotion_fraction`,
`sleep_probation_minutes`, `sleep_min_minutes`, `sleep_wake_seconds`, `sleep_wake_minutes`,
`sleep_max_session_hours`, `sleep_liveness_minutes`) are read from `settings.json`, keyed off
`DEFAULT_SETTINGS` when absent. Every frame logs `state`, `presence`, `motion`, `micro`, and `dist`
plus the active thresholds at INFO level.

**Context:** The Pi is headless. Without per-frame visibility, tuning thresholds requires code
changes, redeploys, and guesswork. The values that matter are the actual fractions produced by this
specific camera on this specific scene.

**Consequences:**
- An operator can `sudo journalctl -u nursery-sleep-monitor -f` with the crib empty then occupied,
  observe the actual `presence` values, and set `sleep_presence_threshold` between the two ranges —
  no code change required.
- `main()`'s reconnect loop re-reads `load_settings()` on every RTSP reconnect, not just once at
  process start — a dropped-stream reconnect (or a full `systemctl restart`) both pick up new
  settings; only a value change with no intervening reconnect requires a manual restart (see
  backlog M-6 for the still-open true live-reload item).
- `update_setting(key, value)` (`storage.py`) writes only the single changed key, never the full
  merged settings dict — this was fixed after the 2026-07-14 missed-put-down incident, where
  `POST /settings` used to bake every `DEFAULT_SETTINGS` value of that day into `settings.json`,
  permanently shadowing later tuning of the code defaults it never touched. A `sleep_*` key present
  in `settings.json` shadows the code default forever; delete the key to return to it.
- On startup, the daemon logs a WARNING for every `sleep_*` key in `settings.json` that diverges
  from `DEFAULT_SETTINGS`, so a stale override left over from an old tuning pass is visible without
  having to diff the file by hand.
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
- The cap is a backstop, not a fix for the root cause of overcounting — the root cause itself was
  addressed by the v5 rewrite and its 2026-07-09/07-14/07-15-16 hardening passes (see backlog
  H-2 / TODO-1, now resolved-with-caveat). The cap still matters as a defense against detection
  loss the algorithm can't reason about (e.g. a camera stuck mid-stream).

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

---

## ADR-009: Weekly Pattern card computed server-side, rendered as DOM/CSS

**Decision:** The dashboard's "Weekly Pattern" card (last 7 days as vertical 24h columns, sleep
sessions as blocks, Feed events as dots) is built from `weekly_pattern_stats(entries, sessions,
days=7)` in `app.py`, shipped as `/data`'s `"week"` key, and rendered in `templates/index.html` as
plain positioned `<div>`s — not a Chart.js chart.

**Context:** Chart.js has no "day-by-time-of-day interval" chart type suited to this layout. A
midnight-spanning sleep session also needs splitting into two per-day segments for the client to
position by minute-of-day (a segment ending at midnight is minute 1440, which the client's
`minuteOfDay()` helper can't express) — simpler to do once, server-side, than duplicate the logic
in JS.

**Consequences:**
- `weekly_pattern_stats()` splits sessions into per-day segments for positioning, but always ships
  the *whole* session's `start_iso`/`end_iso`/`duration_minutes` too, so the client's tap-toast can
  describe the real session even when it's only showing one half of it.
- Backed by `storage.get_sleep_sessions_range(days)` (mirrors `get_sleep_sessions_today`'s overlap
  rule — see ADR-001), not a new query family.
- Because it's DOM, not canvas, it inherits the `:root` CSS custom properties used for event-type
  colors — dark mode and the is-open fade come for free, with no separate Chart.js theming path.

---

## ADR-010: Mutable history entries and an overloaded keypad key

**Decision:** Two independent extensions widened the event model after the original architecture
was written:
- History entries are editable, not just deletable: `PATCH /log/entry` + `storage.update_entry()`
  let a parent correct a mistyped type or backdate a timestamp via an edit modal, rather than
  deleting and re-logging.
- The 4-key pad has no free key for a 5th event type, so `Probiotic` is layered onto the existing
  Play key (`KEY_DOWN`) instead of getting its own: a first press arms a 3-second timer; a second
  press within the window cancels it and logs Probiotic immediately; otherwise the timer fires and
  logs Play, timestamped up to 3s late. Both paths funnel through one shared `log_keypad_event()`
  (debounce → `add_entry` → Huckleberry push) so the timer callback and direct keys behave
  identically.

**Context:** Both are usability fixes driven by real logging mistakes — a fat-fingered key press
with no correction path, and running out of physical keys for a new event type parents wanted to
track.

**Consequences:**
- `update_entry()` had to be added to both `_pg_*` and `_json_*` storage families (ADR-001's
  "any new field touches both" consequence applies to behavior changes too, not just new fields).
- Single-press Play is now timestamped up to 3s late by design — negligible for this use case, but
  worth knowing if timestamp precision ever matters elsewhere.
- Debounce for the overloaded key runs at *fire* time, after the double-press window has resolved
  which event it actually is — a naive debounce-on-keydown would have debounced Play against
  Probiotic incorrectly.
- New event types added to the keypad vocabulary must also be added to `huckleberry_sync.py`'s
  `event_type` branch — this was missed for Probiotic (see backlog M-7).
