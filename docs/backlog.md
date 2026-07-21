# Improvements Backlog & To-Do List

Prioritized improvements and concrete next tasks. See [`architecture.md`](architecture.md) and
[`sleep-detection-research.md`](sleep-detection-research.md) for the current design context
referenced below ([`sleep-monitor-algorithm.md`](sleep-monitor-algorithm.md) is the historical,
superseded v4 spec).

---

## Improvements backlog

### High priority

**H-1: Verify Huckleberry credentials end-to-end.**
The library is installed and working on the Pi — auth reaches Firebase and returns
`INVALID_PASSWORD`, which proves the path is reachable (not an import/library failure). The open
issue is credential verification: whitespace in the stored value (now stripped in `_make_api`), a
wrong/typo'd email, or a social-login-only account with no email/password credential.
`GET /huckleberry/test` already surfaces the specific Firebase reason. See TODO-2.

**H-2: Diagnose and fix sleep-session overcounting.** ✅ **Resolved 2026-07-02 → 2026-07-16**
(v5 rewrite `8c68b08` + ~15 follow-on hardening commits through `b86b753`). The candidate causes
below drove the original v4→v5 rewrite; each subsequent real incident (2026-07-07 phantom session,
2026-07-09 missed pickup, 2026-07-14 missed put-down, 2026-07-15/16 gentle night pickups) was
root-caused against real footage and locked with a regression scenario in
`tests/test_arousal_probation.py` (A–K). Full writeup: `sleep-detection-research.md`. Kept below as
historical context for *why* v5 looks the way it does:
The 14h cap (ADR-007) is only a backstop; nap totals can be wrong before it fires. Three candidate
root causes, each needing a different fix: (a) stale reference reads an empty crib as present;
(b) presence flickers so the 2-frame hysteresis never latches and the session never closes;
(c) a state-machine bug in the ASLEEP→absent path. Requires reading per-frame logs across a real
baby-pickup event. See TODO-1.

**H-3: Add reference-frame metadata.** ✅ **Done** — `sleep_monitor.py` writes
`reference_frame_meta.json` alongside `reference_frame.npy` with shape + a `trusted`/`untrusted`
provenance flag (`sleep_monitor.py:133,260-287`). See TODO-3.
`reference_frame.npy` has no provenance. A stale reference from a prior camera position silently
produces wrong presence readings. A sidecar JSON with `saved_at` and `shape` enables staleness/
mismatch warnings.

**H-4: Validate reference-frame shape at load time.** ✅ **Done** — `load_reference_frame()`
discards a shape-mismatched reference and bootstraps fresh rather than feeding `active_fraction`
nonsense (`sleep_monitor.py:285-287`).
If the saved reference has different dimensions than the current capture, `active_fraction` breaks
or returns nonsense. A shape check on load with fallback to bootstrap makes this failure explicit.

### Medium priority

**M-1: Add automated tests for stat helpers and state-machine logic.** **Half done.** The
state-machine half is covered: `tests/test_arousal_probation.py` (scenarios A–K) exercises the
`SleepStateMachine` refactor with mocked frames. The stat-helper half is still fully open — `today_stats`,
`daily_stats`, `hourly_stats`, `next_feed_iso`, `today_sleep_stats`, and `weekly_pattern_stats` are
pure functions with zero test coverage. See TODO-5 (open) / TODO-6 (done).

**M-2: Implement keypress feedback (audible beep).**
Parents using the keypad at 3 AM cannot confirm a key registered. A short beep per press eliminates
double/missed presses. Needs `feedback.py`, a speaker, ALSA setup, new settings, and a
`/feedback/test` route. See TODO-4.

**M-3: Make sleep thresholds configurable from the dashboard UI.**
All thresholds currently require editing `settings.json` over SSH. Exposing
`sleep_presence_threshold` and `sleep_motion_fraction` in the dashboard would allow tuning without SSH.

**M-4: JSON delete-by-index fragility.** **Reframed as an accepted tradeoff, not an open TODO.**
`_json_delete_entry` still uses list position as `id`; concurrent deletions could in theory remove
the wrong entry. `CLAUDE.md` now documents why this is safe in practice: the client always refetches
a fresh list before rendering delete buttons, so there's no stale-index window in the actual UI
flow. Revisit only if a second client (e.g. a second phone) starts mutating concurrently.

**M-5: Document the network boundary and assess route security.**
All API routes are unauthenticated — acceptable on a home LAN, but the assumption is undocumented.
If the Pi is ever port-forwarded, all write routes need protection.

**M-6: Allow settings reload without a daemon restart.** **Partially mitigated.**
`main()`'s reconnect loop now re-reads `load_settings()` on every RTSP reconnect, not only at
process start, so a dropped-stream reconnect already picks up new thresholds without a manual
restart. What's still open: a threshold change with *no* intervening reconnect still requires
restarting `nursery-sleep-monitor` (interrupting any open session) — the original `SIGHUP`
live-reload idea below is still valid for that case. See TODO-7.

**M-7: Review Huckleberry event mapping completeness.** The gap predicted here has since happened:
`Probiotic` (added `593aa38`, 2026-07-10) has **no branch at all** in `huckleberry_sync.py`'s
`if`/`elif` chain (`huckleberry_sync.py:59-66` only handles Wet/Dirty/Feed/Play) — a Probiotic
keypress silently never syncs to Huckleberry, no error, no log. This needs its own fix, not just a
"revisit later": add an `elif event_type == "Probiotic":` branch mapped to whatever Huckleberry
activity type fits best (or explicitly document it as intentionally unsynced if there's no good
Huckleberry equivalent).
`Play` maps to `log_activity(mode="tummyTime")` — a reasonable approximation that conflates "play"
with one type.

### Low priority

**L-1: Add journal rotation guidance to `install.sh`.**
Per-frame INFO logging is ~86,400 lines/day from the sleep monitor. The installer doesn't configure
`SystemMaxUse`/journald rotation; small SD cards will fill. See TODO-9.

**L-2: `migrate_log.py` dedup uses exact type+timestamp match.**
Two same-type events in the same second would drop the second. Count-based dedup would be more correct.

**L-3: Dashboard history shows the last 50 entries across all days, not just today.**
`entries[-50:]` can include yesterday's entries on a slow day. Filter to today before slicing.

**L-4: Verify Chart.js 4.5.1 SRI hash.**
The pinned SHA-384 hash silently blocks the bundle if the CDN build drifts. Periodically verify, or
self-host in `static/`. See TODO-10.

**L-5: Add a `/healthz` endpoint.**
A simple `{"ok": true}` route enables lightweight monitoring without parsing the dashboard HTML.

**L-6: Tune `sleep_wake_minutes` against real data.**
`sleep-detection-research.md` flags the default (3 minutes of sustained motion to end a nap) as "a
sleep-science default, not yet tuned to a real baby's data." Works fine so far, but worth
revisiting once enough real wake events have been logged to check it against this baby specifically.

---

## To-do list

### Immediate (blocking correct behavior)

**TODO-1: Diagnose sleep overcounting — read per-frame logs across one baby pickup.** ✅ **Resolved**
— see H-2 above. Original steps kept as a record of the diagnostic method, which is still the right
playbook for any *future* detection incident:
1. `sudo journalctl -u nursery-sleep-monitor -f` during a real pickup.
2. Find the frames around the moment the baby was removed.
3. Identify the candidate:
   - **(a) stale reference:** `presence` stays above threshold on an empty crib → press "Crib is
     empty," retest; if fixed, the reference was wrong.
   - **(b) flicker / hysteresis never latches:** `presence` crosses the threshold up/down each
     frame; both `baby_present` and `baby_absent` stay False so the session never closes. Fix:
     lower `sleep_presence_threshold` to stabilize, or drop the absence hysteresis to 1 frame in ASLEEP.
   - **(c) state-machine bug:** `baby_absent` is True in the logs but the session doesn't end →
     trace the `if baby_absent` branch.
4. Implement the targeted fix; verify with another pickup.

**TODO-2: Verify Huckleberry credentials.**
1. Run the credential diagnostic (reads `settings.json`, reports email + password length/whitespace
   without printing it, then POSTs directly to Firebase). 
2. Interpret: `AUTH OK` after `.strip()` → whitespace was the cause (already handled);
   `INVALID_PASSWORD`/`INVALID_LOGIN_CREDENTIALS` → wrong password or social-login-only account
   (set an email+password in the Huckleberry app); `EMAIL_NOT_FOUND` → wrong email in settings.
3. Confirm via `GET /huckleberry/test` → `{"ok": true, "child_count": N}`.

**TODO-3: Add reference-frame metadata + load-time validation.** ✅ **Resolved** — see H-3/H-4 above.
Save `reference_frame_meta.json` next to `reference_frame.npy` with `{"saved_at": "<ISO>",
"shape": [h, w]}`. On load, check the shape matches the first captured frame; if mismatched, warn
and fall back to bootstrap. Optionally warn if `saved_at` is older than N days.

### Near-term (reliability and usability)

**TODO-4: Implement keypress feedback (audible beep).**
1. `feedback.py`: synthesize short per-event WAV tones (stdlib `wave`), play via
   `subprocess.run(["aplay", "-q", *device_args, wav])` in a daemon thread; fail-soft.
2. Add `sound_feedback_enabled` (bool, default **`True`**) and `sound_device` (string, default `""`
   = ALSA default) to `DEFAULT_SETTINGS`.
3. In `listen_one_interface()`, after a successful `add_entry()`, call `play_feedback(label)` gated
   on `FEEDBACK_AVAILABLE` and the setting — same `try/except` import pattern as other integrations.
4. Add `GET /feedback/test` for on-device speaker testing.
5. Document speaker + ALSA setup (USB speaker recommended; `usermod -aG audio`, `alsamixer`,
   `speaker-test`).

**TODO-5: Unit tests for stat helpers.**
`test_stats.py` covering `today_stats` (empty / other days / all four types), `next_feed_iso` (no
Feed entries), `today_sleep_stats` (open session hitting `max_open_minutes`), `hourly_stats`
(midnight boundary).

**TODO-6: Unit tests for the state machine.** ✅ **Done** — `tests/test_arousal_probation.py`
(scenarios A–K) drives the real `SleepStateMachine` with mocked frames, covering this and every
incident-shaped regression since. Stat-helper tests (TODO-5) remain open.
`test_sleep_monitor.py`: extract per-frame transition logic into a pure function (or drive with
mock frames). Cover AWAY→AWAKE→ASLEEP→AWAKE, cap force-end, absent-from-ASLEEP, flicker hysteresis,
and start/wake backdating.

**TODO-7: Add `SIGHUP` settings reload to the sleep daemon.**
Install a `SIGHUP` handler that sets a flag; on the next frame loop, reload `load_settings()` in
place without ending the session. Document `sudo systemctl kill --signal=SIGHUP nursery-sleep-monitor`.

### Housekeeping

**TODO-8: Spot-check `CLAUDE.md` for stale references.** ✅ **Done** — `CLAUDE.md` is current for v5.
The stale references turned out to be in `architecture.md`, `backlog.md` (this file), and
`sleep-detection-research.md` instead — all still pointing at `sleep-monitor-algorithm.md` as
current and/or H-2/TODO-1 as open — fixed in this same doc pass (2026-07-21).
The sleep section was already updated to the `active_fraction` reference-frame approach with current
setting names/defaults. Re-scan for any lingering MOG2/optical-flow or `sleep_motion_threshold`
mentions and correct as needed.

**TODO-9: Add journal rotation to the sleep monitor service.**
In `install.sh`, set `SyslogIdentifier=nursery-sleep-monitor` and note `SystemMaxUse=100M` in
`/etc/systemd/journald.conf` (or `LogRateLimit*` on the unit) to prevent SD exhaustion.

**TODO-10: Verify or self-host Chart.js 4.5.1.**
Confirm the pinned SHA-384 SRI hash still matches the CDN build; if drifted, update it or copy
`chart.umd.min.js` into `static/` and point the `<script src>` at the local path.
