# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Raspberry Pi baby-tracking app. A 4-key USB keypad (SayoDevice) is plugged into the Pi; pressing a key logs a diaper-change or feeding event to storage and the dashboard updates instantly. The dashboard is a mobile-first Flask web app served on port 8080.

## Running locally (dev machine, no Pi)

```bash
python3 -m venv venv && source venv/bin/activate
pip install flask psycopg2-binary   # evdev is Linux-only, skip it
python app.py
# → http://localhost:8080
```

`evdev` import failure is handled gracefully — the keypad listener is disabled but the web dashboard runs normally.

## On the Pi (production)

```bash
# Deploy a change
git pull && sudo systemctl restart nursery-tracker

# Watch live logs (key presses, errors)
sudo journalctl -u nursery-tracker -f

# Service management
sudo systemctl status | restart | stop nursery-tracker
```

The service is installed by `install.sh` and runs `python app.py` from a venv at the repo root.

## Architecture

Three files form the core:

**`storage.py`** — Shared storage layer imported by both `app.py` and `sleep_monitor.py`.
- Dual-mode: `USE_DB = bool(DATABASE_URL and PSYCOPG2_AVAILABLE)`. `_pg_*` functions use Neon Postgres; `_json_*` functions use local files.
- Public API: `get_entries()`, `add_entry()`, `clear_today()`, `delete_entry()`, `load_settings()`, `save_settings()`.
- Sleep API: `start_sleep_session()`, `end_sleep_session()`, `get_sleep_sessions_today()`, `get_sleep_sessions_range(days)`, `get_open_sleep_session()`, `write_sleep_heartbeat()`, `read_sleep_status()`. `get_sleep_sessions_today`/`_range` both return sessions *overlapping* the window (not just ones starting in it), so an overnight sleep put down yesterday and picked up this morning still shows up.

**`app.py`** — Flask backend + keypad listener in one process.
- **Keypad thread**: `keypad_listener()` scans for all SayoDevice `/dev/input/event*` interfaces, spawns a `listen_one_interface()` thread per interface, grabs them exclusively, and calls `add_entry()` on key-down events. Runs as a daemon thread alongside Flask.
- **Stat helpers** (`today_stats`, `daily_stats`, `hourly_stats`, `next_feed_iso`, `today_sleep_stats`, `weekly_pattern_stats`) accept in-memory lists and are storage-agnostic. `weekly_pattern_stats(entries, sessions, days=7)` splits midnight-spanning sleep sessions into per-day segments server-side (a segment ending at midnight is minute 1440, which the client's `minuteOfDay()` can't express); `start_iso`/`end_iso`/`duration_minutes` always describe the whole session for the tap toast, shipped as `/data`'s `"week"` key.
- **Settings**: always `settings.json` (local file, not backed up to Postgres).

**`sleep_monitor.py`** — Standalone sleep detection daemon (second systemd service).
- Reads RTSP stream from TAPO C110 at ~1fps using OpenCV; all analysis inside the crib ROI (`sleep_crib_roi`).
- **v5 "event-gated latched presence"** — read `docs/sleep-detection-research.md` before changing anything here; it records why four previous algorithms failed. Presence is a latched state that changes only via: (1) settle evaluation after a parent-scale disturbance (with a micro-motion probation window when the reference says "occupied"), (2) micro-motion override while AWAY, (3) the "📷 Crib is empty" button, (4) the max-session cap. Per-frame reference comparison never flips state directly.
- One robust primitive for both signals: `active_fraction(a, b)` = `absdiff → per-pixel threshold → MORPH_OPEN denoise → fraction of changed pixels`. Bounded [0,1], robust to H.264/IR-grain noise (which defeats mean optical flow). `MORPH_OPEN` (not CLOSE) is essential — it removes isolated noise specks rather than bridging them into false blobs.
- The empty-crib reference (`reference_frame.npy` + shape-validated metadata sidecar) auto-refreshes every time a settle evaluation confirms the crib empty, plus slow triple-gated drift for lighting. A stale or baby-poisoned reference self-heals via probation.
- Every frame logs `state / presence / motion / micro / dist` + thresholds at INFO — tune the `sleep_*` settings from the real numbers (`journalctl -u nursery-sleep-monitor -f`), no code change needed. To tune offline: `record_camera.sh` (on the Pi) captures footage, `replay_sleep.py` runs the identical pipeline over it anywhere.
- Writes sleep sessions via `storage.py`; writes heartbeat file so Flask can detect if daemon is offline.

**`templates/index.html`** — Single-page dashboard. Pure HTML/CSS/JS, no build step.
- Polls `GET /data` every 8 seconds; `refresh()` updates counts, history, next-feed card, all three Chart.js charts, and the weekly pattern grid in one pass.
- Section order (PRD hierarchy pass, 2026-07-06): next-feed card → log buttons → history → "😴 Today's Sleep" card (live state line + 24h timeline + per-nap list, one merged card, now including overnight sessions that span midnight) → count cards → charts (doughnut, hourly, daily, then Weekly Pattern) → maintenance row ("Clear today" + "📷 Crib is empty" calibrate, deliberately at the bottom away from the one-handed logging zone).
- Sleep UI: `updateSleepCard` fills the state/summary lines, `updateSleepTimeline` + `updateNapList` the track and rows; all consume `data.sleep`; calibrate POSTs `/sleep/calibrate`.
- Weekly Pattern card (`.week-grid`/`#weekGrid`, `updateWeeklyPattern()`): last 7 days as vertical 24h columns, sleep sessions as blocks and Feed events as dots — pure DOM/CSS, not Chart.js (no day-by-time-of-day interval type, and DOM blocks inherit the `:root` custom props so dark mode and the is-open fade come free). Consumes `data.week` from `weekly_pattern_stats`; feed-dot clusters offset 30%/70% when centers are <8px apart; today's column is highlighted.
- History rows have an edit (✎) and delete (✕) button. Edit opens a modal (`#editOverlay`) to change an entry's type + time, sent via `PATCH /log/entry`.
- Chart.js 4.5.1 loaded from CDN with SHA-384 SRI.
- Event type colors and sleep color are defined as CSS custom properties in `:root` and must match `COLORS` in the JS `<script>` block.

## Key mapping

`KEYPAD_KEYS` in `app.py` maps evdev key names to event types:

| evdev key | Event type |
|-----------|-----------|
| KEY_SPACE  | Wet |
| KEY_PAGEUP | Dirty |
| KEY_DOWN   | Play (single press) / Probiotic (double press within `PLAY_DOUBLE_PRESS_SECONDS` = 3s) |
| KEY_UP     | Feed |

The Play key is overloaded (`handle_play_press()` in `app.py`): a first press arms a 3s timer; a second press within the window cancels it and logs Probiotic; otherwise the timer fires and logs Play — so single-press Play is timestamped up to 3s late. Both paths go through `log_keypad_event()` (debounce → `add_entry` → Huckleberry push).

To add or rename an event type, update `KEYPAD_KEYS`, the validation list in `log_event()`, the counts dicts in all three stat helpers, the `TYPES` array and `COLORS` object in `index.html`, and the CSS color rules (`.type-*`, `.btn-*`, `.count-card.*`).

## Debounce

`is_debounced()` in `app.py` drops a repeat press of the same type within a per-type window, before `add_entry()`/`push_event()`. It guards both the keypad path (`log_keypad_event`) and the `/log` route (which returns `{"ok": true, "discarded": true}`). Windows are in `settings.json` `debounce_minutes` (minutes; `0` = off), default `{"Feed": 5, "Wet": 1, "Dirty": 1, "Play": 5, "Probiotic": 720}`. For the overloaded Play key, debounce runs at fire time — after the double-press window resolves which event it is.

## Storage: local-first, Neon as snapshot backup

**Architecture (since 2026-07-04):** the services read and write ONLY the local JSON files; Neon
Postgres is an optional off-site backup that a systemd timer snapshots to every 6 hours. Do NOT set
`DATABASE_URL` on `nursery-tracker` or `nursery-sleep-monitor` — live reads at the dashboard's 8s
poll keep Neon's compute awake 24/7 and burn ~180 free-tier compute-hours/month (the free tier's
5-min autosuspend cannot be tuned; billing is effectively "wake episodes × 5+ min"). Batched
snapshot sync costs ~2–3 h/month.

**Local JSON (primary)**
- Events in `log.json`, sleep sessions in `sleep_sessions.json`, daemon heartbeat in `sleep_state.json`. All writes are atomic (tmp + `os.replace`, fsync for events).
- `_json_delete_entry` uses list index as `id`; indices shift after any deletion, but the client always gets a fresh list before showing delete buttons, so this is safe.
- The `_pg_*` paths in `storage.py` remain for the backup/export scripts (which run with `DATABASE_URL` set); `db()` commits on clean exit, rolls back on exception, always closes.

**Neon backup (optional)**
- `backup_sync.py` — full-snapshot mirror (one transaction: delete + insert both tables). Idempotent; refuses to run if local files are missing/unparseable or local counts collapsed below half of the remote's (a broken Pi must never overwrite a good backup). Writes `last_sync.json`.
- Scheduled by `nursery-backup.timer` (installed by `install.sh`, every 6 h, `Persistent=true`); the `DATABASE_URL` lives ONLY in `/etc/nursery-tracker/backup.env` (chmod 600). Manual run: `sudo systemctl start nursery-backup.service`; logs: `journalctl -u nursery-backup`.
- `export_db.py` — the reverse direction (Neon → JSON): one-time switchover tool and the disaster-recovery restore after an SD-card death (`--force` to overwrite non-empty local files). Run with both services stopped.
- `migrate_log.py` (JSON → Postgres one-shot import) is superseded by `backup_sync.py`.
- Required schema:
  ```sql
  CREATE TABLE events (id BIGSERIAL PRIMARY KEY, type VARCHAR(10) NOT NULL, time TIMESTAMP NOT NULL);
  CREATE INDEX ON events (time);
  CREATE TABLE sleep_sessions (
      id BIGSERIAL PRIMARY KEY, start_time TIMESTAMP NOT NULL,
      end_time TIMESTAMP, duration_minutes NUMERIC(8,2));
  CREATE INDEX ON sleep_sessions (start_time);
  ```

## Sleep monitoring

`sleep_monitor.py` is a standalone daemon that detects baby sleep via camera motion analysis.

**Service management:**
```bash
sudo systemctl status | start | stop | restart nursery-sleep-monitor
sudo journalctl -u nursery-sleep-monitor -f
```

**Configuration (in `settings.json`):**
| Key | Default | Description |
|-----|---------|-------------|
| `camera_rtsp_url` | `""` | `rtsp://user:pass@IP:554/stream2` — TAPO camera account credentials |
| `sleep_crib_roi` | `[0,0,1,1]` | Crib region `[x0, y0, x1, y1]` as 0–1 fractions of the frame; everything outside is ignored |
| `sleep_presence_threshold` | `0.02` | ROI fraction differing from the empty-crib reference for a settle evaluation to say "occupied" |
| `sleep_motion_fraction` | `0.01` | ROI fraction changed vs the previous frame above which baby is "moving" (awake) |
| `sleep_micromotion_fraction` | `0.002` | ROI fraction counting as living-thing micro-motion; must sit above the camera noise floor |
| `sleep_disturbance_fraction` | `0.30` | ROI fraction = parent-scale disturbance; presence is only re-evaluated after one settles. Measured: awake-baby squirming 0.10–0.17, pickups 0.57–1.0 |
| `sleep_settle_seconds` | `10` | Quiet seconds that end a disturbance episode and trigger the settle evaluation |
| `sleep_probation_minutes` | `10` | After an ambiguous settle (reference says occupied), micro-motion must appear within this window or the crib is ruled empty |
| `sleep_min_minutes` | `10` | Stillness minutes before marking asleep. Brief sleep stirs (1–7 s clusters) do NOT reset the timer |
| `sleep_wake_seconds` | `20` | Short window for "life evidence" (probation confirm / AWAY→AWAKE override). NOT used to wake a nap |
| `sleep_wake_minutes` | `3` | Sustained-motion minutes required to END a sleep session (actigraphy-style epoch scoring). Brief in-sleep arousals — startles, active-sleep squirms — stay scored as sleep; a disturbance while asleep ends the nap if the crib goes empty (a pickup), and a settle that reads "occupied" resumes the nap on probation (micro-motion must re-confirm, else it's scored as a missed pickup and the session closes) |
| `sleep_max_session_hours` | `14` | Force-end backstop for a stuck-open session |
| `sleep_liveness_minutes` | `20` | Reference-free empty-crib backstop: ASLEEP with zero micro-motion for this long → crib ruled empty, session closed backdated to the last life sign. An occupied crib is never truly still (longest measured fully-still stretch 7.5 min); bedding ghosts can't fool this because no reference is involved |

**How it works (v5 — full rationale in `docs/sleep-detection-research.md`):** Presence is latched; it changes only at events. A parent-scale disturbance (motion ≥ `sleep_disturbance_fraction` on 2 frames within a 4s window) opens an episode; when it settles, the frame is compared to the empty-crib reference: match → AWAY + reference refreshed from the settled frame; differ → AWAKE on probation (micro-motion must confirm within `sleep_probation_minutes`, else AWAY + reference refreshed — this self-heals stale/poisoned references). The settle verdict is deferred (capped at 180s) while presence reads person-scale (≥0.5): the parent is still over the crib and the verdict would be about them, not the crib. While AWAY, repeated micro-motion flips to AWAKE without needing any reference. AWAKE → ASLEEP uses a stillness timer with backdated start. **Waking from ASLEEP is deliberately hard** (infants move constantly in active/REM sleep without waking): a disturbance while asleep is a *candidate* arousal that ends the nap only if the crib then reads empty (a pickup) — if still occupied the nap resumes **on probation** with the burst rescored as sleep (the reference lies after every pickup, so unconfirmed resumes must self-heal: zero micro-motion → session closed backdated to the disturbance, reference refreshed; a sustained wide-margin *match* to the empty reference closes it in ~60s without waiting out the deadline); a self-wake without a pickup ends the nap only on motion sustained across `sleep_wake_minutes` of consecutive epochs (actigraphy-style). Micro-motion evidence is a band, not a floor: parent-scale frames and their flanking seconds never count as life evidence (2026-07-09 missed pickup: a parent's return falsely confirmed an empty crib), probation evidence must additionally stay within `EVIDENCE_PRESENCE_DELTA` (0.15) of the settle verdict's own presence (2026-07-15: a parent partially entering the ROI at presence 0.28 over a 0.04 anchor cleared probation as "sustained motion"), and taint suppression after a parent-scale frame runs 45s. Gentle night pickups/put-backs (2026-07-15/16: pickup peaked 0.13, put-back's ≥0.30 frames sat 14–17s apart) are caught by three additions: a single frame ≥0.5 opens a disturbance, the two-frame rule spans 20s, and two occupied-state exits need no disturbance at all — the **silent-departure close** (presence ≤ threshold vs a *trusted-empty* reference + zero micro-motion for 5 min; trust lives in the reference metadata and is granted only by a settle-empty verdict or the calibrate button) and the **liveness backstop** (`sleep_liveness_minutes` of zero micro-motion, reference-free). Regression tests: `venv/bin/python tests/test_arousal_probation.py` (scenarios A–K, including the 2026-07-09 and 2026-07-15/16 incident shapes); real-footage baseline: `20260709_165351`. Scene-change guard: a frame diff > 80% defers judgment one frame — a decode glitch is ignored, a persisted change (IR flip **or a pickup hidden in a frame drop**) opens a disturbance episode. Heartbeat written every frame; Flask shows "Camera offline" if stale > 60s.

**Tuning:** Measured thresholds from real footage live in `docs/sleep-detection-research.md` §6a — start there. A `sleep_*` key present in `settings.json` shadows the code default forever — the monitor logs a WARNING at startup for each overridden threshold (2026-07-14 missed put-down: stale baked-in 0.05/0.10 thresholds, see §6a). Delete the key from `settings.json` to return to the tuned default; `POST /settings` writes only the changed key (`storage.update_setting`), never the merged dict. For new footage: `bash record_camera.sh 120` on the Pi (needs `sudo apt install ffmpeg`; writes 10-min segments to `recordings/`), then anywhere with opencv: `python3 replay_sleep.py <segs>` prints motion/presence percentiles, and `python3 replay_sleep.py <segs> --simulate` drives the real `SleepStateMachine` over the footage and prints the state timeline + sessions to compare against ground truth. **The crib ROI must exclude the TAPO OSD timestamp** (top-left) — its per-second digit change reads as constant motion. To measure the ROI (new camera or reposition): `python3 pick_roi.py "rtsp://user:pass@IP:554/stream2"` from any LAN machine — it health-checks the stream (resolution, decode rate, fps), then opens a browser page (`roi_picker.html`, works with headless opencv) where dragging a rectangle over the crib yields the `"sleep_crib_roi"` line to paste into `settings.json`. The monitor re-reads settings on reconnect (or restart the service).

## Nanny report

Independent pipeline (does not touch the crib monitor): records the `NANNY_CAM_*` TAPO streams during the care window (default Mon–Fri 10:00–18:00), analyzes them with Gemini, and publishes a daily report at `/nanny`.

- **`nanny_record.py`** — always-on daemon (`nursery-nanny-record.service`); one supervised `ffmpeg -c copy -an` per camera, hourly wall-clock-named segments in `nanny/raw/`. Video only (`-an`) deliberately — audio of an employee is wiretap territory. Refuses to record under 2 GB free disk. systemd reads the env file only at start: after editing `nanny.env`, `sudo systemctl restart nursery-nanny-record`.
- **`nanny_analyze.py`** — half-hourly oneshot (`nursery-nanny-analyze.timer`, :05 and :35 of 10:00–19:00, `RandomizedDelaySec=180`, `Persistent=true`): 1 fps downsample → Gemini Files API upload → structured-JSON analysis (activities with `baby_state` / phone_use with baby-context / notable_events / summary, `media_resolution=LOW` ≈ 240k tokens per camera-hour) → evidence clips for medium/high-confidence phone events cut from the raw segment → raw deleted. The prompt carries the **camera topology** (`scene_description()`): every camera, its room, which one this video is, and that they all record the same hours — so one camera's frame is read as one vantage point on a multi-room house, not the whole world (a caregiver alone in frame is not a baby left alone; that's what `baby_not_in_frame` is for). Chunk JSON existence in `nanny/chunks/<date>/` is the idempotency marker; failures leave raw for the next run's retry (exit 0 — a red unit would mask config errors). flock in `nanny_common.AnalyzeLock` serializes with the report's straggler sweep.
- **Writing footage off** (2026-07-28; all three paths leave a chunk with `parse_error` + `error` + `error_detail` and `segment_minutes: 0`, so the report's `failures` list names the lost hour instead of showing an unexplained gap): `preflight()` rejects raw that ffprobe can't read, or shorter than `MIN_SEGMENT_SECONDS` (60 s), *before the first upload* — a power cut mid-write, a camera dropping, or the window-end sliver that used to be retried for 24 h and then deleted. `give_up_on_failed_raw()` deletes only after `NANNY_MAX_SEGMENT_ATTEMPTS` (6) genuine failures, or past `RAW_MAX_AGE_HOURS` (48) *having been tried at least once*; a per-segment ledger at `<raw>.fail.json` holds `attempts`/`last_error` (a 429 is charged to the quota, not to the segment, so a bad quota day can't spend a segment's budget). **Age alone never deletes** — after days of Pi downtime every pending segment is old and none of it has failed at anything, which is exactly what the old `purge_stale_raw` threw away unanalyzed. The one exception is `purge_raw_under_disk_pressure()`: below the `MIN_FREE_BYTES` floor `nanny_common` now shares with the recorder, oldest raw goes first even untried, because a full card also stops the recorder.
- **Sample rate is the cost** (2026-07-29): Gemini bills 66 tokens per *sampled frame* at `media_resolution=LOW`, so fps — not resolution, not model — is the dominant lever. `NANNY_SAMPLE_FPS` (default **0.25**, one frame per 4 s) takes a camera-hour from ~237k to ~59k input tokens, i.e. a 3-camera day from ~5.7M to ~1.4M. The value must reach **both** ffmpeg (`downsample()`'s `-vf fps=`) and the API (`types.Part(..., video_metadata=types.VideoMetadata(fps=...))` in `analyze_video()`): downsampling only the upload saves nothing, because Gemini then samples at its own default 1 fps and interpolates the missing frames back. `video_metadata` goes on the **Part**, never inside `FileData`/`Blob` — the wrong nesting returns a 500 (googleapis/python-genai#854). A 400 naming `video_metadata`/`fps` retries once without it (same shape as the `extras` fallback), degrading to the old cost rather than losing the segment. The `fps` filter drops frames without touching duration, so every wall-clock offset keeps working; that is why fps sampling is used rather than an ffmpeg `setpts` speed-up, which would rescale every returned timestamp. Below ~0.1 fps short phone pickups start falling between frames.
- **Knowing who is who** (2026-07-29): `load_context()` reads `/etc/nursery-tracker/nanny_context.md` (`NANNY_CONTEXT_FILE`, chmod 600, seeded by `install.sh`, comment lines stripped, truncated at 4000 chars) and `household_block()` puts it at the top of every prompt — the real names of a child and an employee stay out of git, and out of `nanny.env` which cannot hold a paragraph. Without it the model describes "a person" and cannot tell the paid caregiver from a parent. It never raises: a missing context file costs background, not the day. `earlier_block()` adds the last two summaries this camera produced today (`previous_summaries()` reads them back out of the chunk JSONs — chunks are the pipeline's only memory, since every Gemini call is stateless), so a half-hour is no longer read as if the day began at its first frame. `phone_use` gained a `person` field (`caregiver` / `other_adult` / `unclear`); the report scores only the caregiver, and **`unclear` is scored like `caregiver`** — an unattributed event must never be quietly excused, only a positive `other_adult` is.
- **On the free tier the binding limit is REQUESTS, not tokens** (2026-07-31, from an AI Studio dashboard showing ~150 429s + ~150 500s in two days): the console's "Requests per model" showed ~25/day while "Total API Requests" showed 500–1000 — because one piece costs 1 upload + N processing polls + 1 delete + 1 generate, and the `Pacer` only spaces the *generate* call. Three fixes, in descending order of effect: (1) **use a Flash-Lite model**, not Flash — free-tier Flash is 10 RPM / **250 RPD** against Flash-Lite's 15 RPM / **1000 RPD**, and this task is description, not reasoning; (2) **`NANNY_PIECE_MINUTES=60`**, which halves uploads, polls, deletes *and* generate calls at once (at 0.25 fps a whole hour is only ~59k tokens, comfortably under the 250k TPM shared by all free models); (3) polls now start at 10 s and double to 30 s (`POLL_INITIAL_S`/`POLL_GROWTH`/`POLL_MAX_S`) — a 0.25 fps piece is a small file that processes in seconds, so the old 3 s/1.5× schedule was buying nothing for ~8 requests per piece. Together: ~144 requests/day instead of ~530.
- **Two error paths that manufactured their own errors** (2026-07-31): a model that rejects `video_metadata` does not always answer 400 — some answer **500**, which the 400 fallback never caught, so five retries were spent re-sending the same rejected request (25 generations × 5 ≈ the 150 `InternalServerError`s on the dashboard). `analyze_with_retries` now drops custom fps after **two** server errors and takes the 4× token cost rather than losing the segment. Separately, a 429 naming a **per-day** quota (`daily_quota_exhausted()` / `QuotaExhausted`) now ends the run immediately: a daily quota does not recover in 30 s, so retrying it five times per segment across a whole backlog is how one exhausted quota becomes hundreds of console errors.
- **Staying inside the quota** (2026-07-28, after 500s + near-429s on a 3-camera day): the *paid*-tier binding limit is **input tokens per minute**, not requests — one camera-hour is ~240k tokens, so three cameras uploaded back-to-back blow a 250k–1M TPM quota on *three* requests. Four mechanisms, all in `nanny_analyze.py`: (1) `NANNY_PIECE_MINUTES` (30) splits the hour into per-call videos — smaller requests, smaller output JSON, and a truncated answer costs 30 min of coverage instead of the hour; pieces are merged back into one chunk per segment by `merge_pieces()`, and each piece is checkpointed to `<chunk>.partial` so a retry never re-buys a piece that already returned. (2) `Pacer` spaces calls by what they cost (`60s × tokens/NANNY_TPM_BUDGET`, default budget 200k, min gap 10s, jitter) and a 429 defers every *following* call by the server's own `retryDelay` (`retry_delay_seconds()` reads `RetryInfo`); a segment that dies on a 429 ends the run early rather than marching the same 429 through every camera. (3) `NANNY_MAX_SEGMENTS_PER_RUN` (4) + the half-hourly timer drain a backlog across runs; the report's sweep calls `analyze_pending(limit=None)` because the day's report must not ship with hours missing. (4) Files-API polling backs off 3s→20s (that poll, not `generate_content`, is what made the console show ~340 requests for 19 generations). Retries are code-based (`status_code()`/`RETRYABLE_CODES` = 408/409/429/5xx; 400/403/404 are config errors and raise at once, never burning quota). Empty/truncated responses (`resp.text` is None when thinking ate the output budget) raise `TruncatedResponse` and retry, then get written off as `parse_error` — deterministic failures must not be re-paid every run. `GEMINI_THINKING_LEVEL`/`GEMINI_MAX_OUTPUT_TOKENS` cap thinking on thinking models; a 400 naming those knobs retries once without them.
- **`nanny_report.py`** — daily oneshot (18:45, `Persistent=true`): re-runs `analyze_pending()`, then merges every chunk-date lacking a report (not "today" — Persistent catch-up after downtime merges yesterday correctly), **plus today itself when it is a care day whose window has closed** (`care_day_awaiting_report()`), so a day on which nothing was analyzed still reaches the dashboard as an explicit `no_analysis` report instead of vanishing from the date picker. Config is loaded by `load_config()`, which gives every setting its own fallback and collects `config_errors` into the report: a typo in one env line must degrade the report, never delete it (2026-07-28: `sys.exit("ABORT: bad configuration")` killed the daily report — and the straggler sweep with it — for days). Phone minutes are the interval **union** across cameras (double coverage counts once), then classified by `classify_phone_use()` against the house rule (below). Writes `nanny/reports/<date>.json`; prunes clips past `NANNY_CLIP_RETENTION_DAYS` (14).
- **Day metrics** (2026-07-29), all clipped to the **care window** (a nanny report is about the hours the nanny worked; overnight sleep would otherwise dominate) and all unioned across cameras before totalling, so an hour two cameras both watched counts once. `asleep_intervals()` is the single definition of "asleep" — crib-monitor naps ∪ every camera's `baby_state: asleep` — shared by the phone policy and the sleep metrics, because two definitions left to drift would let one report call the same minute both phone-allowed and awake. Report keys: `sleep` (`total_sleep_minutes`, `nap_count`, `longest_nap_minutes`, `longest_awake_stretch_minutes`, per-nap list, plus the crib-monitor-vs-camera cross-check `crib_only_minutes` / `camera_only_minutes` / `agreement_minutes` — camera-only is normally a bedroom nap the crib monitor cannot see, crib-only means one of the two sources is wrong); `care` (`minutes_by_category` / `event_counts` over `ACTIVITY_CATEGORIES`, `feeding_count`, `held_minutes`, `active_care_minutes`); `attendance` (`unattended_minutes`, `longest_unattended_stretch_minutes`, `unclear_minutes`, `caregiver_present_minutes`, `observed_minutes` / `uncovered_minutes`). Attendance inherits the phone policy's two biases exactly — presence evidence outranks absence evidence (a caregiver-present activity on *any* camera clears the span), and only confidence-gated evidence can flag — plus one the phone policy does not need: minutes nobody analyzed are subtracted out, so a footage gap can never read as time the baby was alone. A sleeping baby alone in a crib is not a finding.
- **CLI for maintenance** (2026-07-29): with no arguments `nanny_report.py` is exactly the production run the unit invokes, so systemd behaviour is unchanged. `--dry-run --date YYYY-MM-DD` rebuilds a day to stdout and touches nothing — no straggler sweep, no writes, no `update_status()`, no `cleanup()`. It implies `--no-sweep` **and** `--no-narrative`: the sweep is the obvious spender, but `day_narrative()` is a real Gemini call on *every* build, so a dozen tuning runs would quietly bill a dozen times (`--narrative` opts back in). Also `--force` (rebuild a day that already has a report), `--out DIR` (write to a scratch dir; never claims a production run in `nanny_status.json`), and `--list` (chunk dates, chunk counts, which have reports). On the Pi these need `sudo` only to read the chmod-600 env file.
- **One event per occurrence, not per camera** (2026-07-29): `merge_phone_events()` runs in `build_report()` *before* `classify_phone_use()`, so every downstream stat sees one event per real pickup. Merging is **per room** — a caregiver cannot be in two rooms at once, so simultaneous events in different rooms are separate observations, while two cameras sharing a room are two angles on one scene. A merged event takes the union span, the highest confidence, the most specific context (`CONTEXT_RANK`: a camera that saw the baby in their arms knows more than one with no baby in frame), and **one** clip from the most confident/longest view; the other cameras go in `also_seen_by` and the page credits them in text instead of showing a second video of the same moment. Minutes were always right (the interval union), but `event_count` was counting camera-observations. `prune_superseded_clips()` in `cleanup()` then deletes clips no report references — only for days that **have** a report, since clips are cut before the report exists and an unguarded sweep would delete the day's evidence before anything referenced it.
- **Phone-use policy** (`classify_phone_use()` in `nanny_report.py`): the phone is **allowed while the baby is asleep** and **not allowed while the caregiver is with an awake baby**. "Asleep" = crib-monitor nap windows ∪ spans any camera scored `baby_state: asleep` (the bedroom has no crib monitor), and it is a whole-house fact. "With the baby" = the model's own `while_holding_baby` / `baby_nearby_awake` / `baby_unattended` context, **or** an awake-baby span from *any camera in the same room* — that room fusion is the whole reason `NANNY_CAM_ROOMS` exists. Two deliberate biases, because these minutes describe a real person's conduct: asleep evidence outranks with-baby evidence (cameras disagreeing clears rather than accuses), and only medium/high-confidence phone detections can produce flagged minutes — low-confidence ones land in `unauthorized_unconfirmed_minutes` instead. Everything not proven either way is `unclear_minutes`, never a flag. Report keys: `unauthorized_minutes` / `unauthorized_event_count` / `unauthorized_intervals`, `while_baby_asleep_minutes`, `during_naps_minutes` (crib monitor only), `unclear_minutes`; each event gets `room`, `authorization` (`unauthorized` / `allowed_baby_asleep` / `unconfirmed` / `unclear`) and `unauthorized_minutes`.
- **Config/secrets** live ONLY in `/etc/nursery-tracker/nanny.env` (chmod 600; seeded by `install.sh`): `GEMINI_API_KEY`, `GEMINI_MODEL` (default `gemini-2.5-flash-lite`), `NANNY_CAM_N=name=rtsp://…/stream2` (one var per camera; use the sub-stream), `NANNY_CAM_ROOMS=cam:room,cam:room` (unlisted cameras become their own room, which disables room fusion for them), `NANNY_WINDOW`, `NANNY_DAYS`, `NANNY_CLIP_RETENTION_DAYS`, plus the quota knobs `NANNY_SAMPLE_FPS` / `NANNY_PIECE_MINUTES` / `NANNY_TPM_BUDGET` / `NANNY_MAX_SEGMENTS_PER_RUN` / `GEMINI_THINKING_LEVEL` / `GEMINI_MAX_OUTPUT_TOKENS`, and `NANNY_CONTEXT_FILE` (the household context, default `/etc/nursery-tracker/nanny_context.md`). Cost ≈ $0.15/day for 3 cameras on flash-lite-class pricing at the default 0.25 fps (it was ~$0.60 at 1 fps).
- **Free tier** (2026-07-29): the tier is a property of the **Google Cloud project** behind the key — free means no active Cloud Billing account linked — and rate limits apply per project, not per key. To move: AI Studio → *Create API key* → **in a new project**, leave that project unbilled, put the key in `nanny.env`, restart `nursery-nanny-analyze`; this leaves the old project's history and other services alone and is reversible by swapping the key back. The alternative is unlinking billing from the existing project (Cloud Console → Billing → Account management → Disable billing). **The tradeoff is not a quota one**: Google's API terms say unpaid-tier content is used "to provide, improve, and develop Google products and services" and that human reviewers may read API input and output, while the paid tier says the opposite explicitly — and the input here is video of a named employee and an infant. Practical limits on flash-lite free are roughly 15 RPM / 250k TPM / 1,000 RPD, so at 1 fps a single camera-hour (~237k tokens) saturated an entire minute of TPM; at the 0.25 fps default a day is ~1.4M tokens over ~48 calls and fits comfortably. `NANNY_TPM_BUDGET=200000` already sits under the 250k cap. With 0.25 fps a whole hour is only ~59k tokens, so `NANNY_PIECE_MINUTES=60` becomes affordable and halves the request count — a lever, not the default, because a longer video raises the truncated-JSON risk. Set `NANNY_TPM_BUDGET` to your tier's real input-TPM (AI Studio → the project's limits) — the default 200k is deliberately conservative, and raising it is what makes the pipeline finish faster.
- **The verdict comes first** (2026-07-30, from the product review in `docs/nanny-report-review.md` — read it before reshaping this page): `day_verdict()` computes `concern` (a `safety_concern`) > `attention` (flagged minutes) > `degraded` (no analysis / config errors / lost hours / worst-camera coverage under `MIN_TRUSTED_COVERAGE` 75%) > `clear`, **in Python from the classified numbers, never by the model** — `narrative` is a second LLM pass that is under no obligation to agree with what `classify_phone_use()` concluded, and is now labelled "AI-written" so it cannot stand in for the verdict. Degradation *qualifies* a finding, never replaces it: a flag in 25% coverage is still `attention`, with the caveat carried in `reasons`. Notable events and coverage moved to the top of the page (they calibrate everything below them), notable events now get evidence clips of their own, and every flagged event shows its confidence tier — a `medium` detection counts toward `unauthorized_minutes` and used to render identically to a `high` one behind the same red badge.
- **Coverage tells the truth** (2026-07-30): a failed piece records `piece_start_iso`/`piece_minutes`, `merge_pieces()` turns those into `unanalyzed_intervals` + `analyzed_minutes`, and `coverage_for()` subtracts them so the lost half hour becomes a real gap. Before this, `segment_minutes` was the full hour regardless and a segment with 2 of 4 pieces failed still reported 60 minutes analyzed — the number the reader uses to calibrate every other number on the page. Chunks written before the change have no `unanalyzed_intervals` and read exactly as they used to.
- **Retention is about privacy, not disk** (2026-07-30): measured, a report is ~1.9 KB (a year ≈ 5 MB) and chunks ~0.5 MB/day; raw video is the only thing that ever threatens the card and it is already bounded by `MIN_FREE_BYTES` + `purge_raw_under_disk_pressure()`. `prune_old_chunks()` drops chunks at `NANNY_CHUNK_RETENTION_DAYS` (default = clip retention, 14) so the granular per-camera record of a person's day does not outlive the clips that could substantiate it; `prune_old_reports()` keeps a year locally (`NANNY_REPORT_RETENTION_DAYS`, far past the 28-day trend window so the dashboard never needs Neon). Both skip dates without a report, same reason `prune_superseded_clips()` does: pruning an unmerged day is silent data loss. `pipeline_warnings()` flags a household context file older than 90 days in a `warnings` list kept **separate from `config_errors`** — stale is not broken. `storage_status()` surfaces free GB, unanalyzed backlog and disk-pressure deletions, so the analyzer falling behind is visible before footage is gone.
- **Reports are archived to Neon by UPSERT, never snapshot** (2026-07-30): `backup_sync.py`'s existing `DELETE`-then-insert is correct for events/sleep only because local JSON is the *complete* truth. Local reports are a bounded window over an archive that only Neon keeps, so `sync_nanny_reports()` does `INSERT ... ON CONFLICT (report_date) DO UPDATE` and **`guard_shrinkage()` is deliberately not applied to it** — fewer local rows than remote is the designed steady state, and the guard would abort the entire backup once local fell below half of remote. Rides the same 6-hourly timer, so `DATABASE_URL` stays only in `backup.env`. `export_db.py` restores them. Chunks are deliberately not mirrored. Extra table:
  ```sql
  CREATE TABLE nanny_reports (
      report_date DATE PRIMARY KEY, generated_at TIMESTAMP NOT NULL,
      report JSONB NOT NULL, synced_at TIMESTAMP NOT NULL DEFAULT now());
  ```
- **Dashboard**: `GET /nanny` (page), `GET /nanny/data?date=` (report + date list + `status` + a 28-day `trend`), `GET /nanny/clips/<date>/<file>` (validated). `templates/nanny.html` is self-contained like index.html. The trend strip is pure DOM (`nanny_trend()` in `app.py` reads only local report files — never Neon, whose compute-hours are what the local-first design exists to avoid); days under 75% coverage render hatched, because a day nobody watched must not read as a clean day.
- Tests: `venv/bin/python tests/test_nanny_report.py` (report/merge logic) and `venv/bin/python tests/test_nanny_analyze.py` (retry classification, `retryDelay` parsing, pacer math, piece merge + checkpoint). Logs: `journalctl -u nursery-nanny-record|nursery-nanny-analyze|nursery-nanny-report`.

## API routes

| Method | Path | Description |
|--------|------|-------------|
| GET | `/` | Dashboard HTML (server-rendered initial counts) |
| GET | `/data` | JSON: counts, recent 50 entries, hourly/daily stats, next feed, today's sleep (`sleep`), 7-day feed+sleep pattern (`week`) |
| POST | `/log` | Add event `{"type": "Wet\|Dirty\|Play\|Feed"}` |
| DELETE | `/log/today` | Remove all of today's entries |
| DELETE | `/log/entry` | Remove one entry `{"id": <int>}` |
| PATCH | `/log/entry` | Edit one entry `{"id": <int>, "type": "Wet\|Dirty\|Play\|Feed", "time": <ISO>}` |
| GET | `/settings` | Get `feed_interval_minutes` |
| POST | `/settings` | Set `feed_interval_minutes` (multiple of 15, 15–720) |
| POST | `/sleep/calibrate` | Save current frame as empty-crib reference baseline (crib must be empty) |
| GET | `/devices` | Debug: list input devices (only with `NURSERY_DEBUG=1`) |

## Debugging the keypad

```bash
# Confirm the keypad is visible to the app
bash find_device.sh

# See raw key names as reported by evdev
sudo journalctl -u nursery-tracker -f
# Look for: [/dev/input/eventN] Key event raw: KEY_SPACE

# List all input devices (requires NURSERY_DEBUG=1 in service env)
curl http://raspberrypi.local:8080/devices
```

If key presses appear in the log but don't match any `KEYPAD_KEYS` entry, the device is sending different key names than expected — update the mapping.
