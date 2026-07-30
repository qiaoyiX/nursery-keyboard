# Nanny Report — Product Review (2026-07-29)

**Status:** review conducted 2026-07-29 against the pipeline as it stood after the token/merge/
context work. Items 1–8 and 12 implemented 2026-07-30; the rest are deliberately deferred with
reasons recorded below.
**Audience:** anyone picking up `nanny_report.py` / `nanny_analyze.py` / `templates/nanny.html`.
Read `CLAUDE.md`'s "Nanny report" section first for what the thing *does*; this doc is about
whether what it does is the right thing.

---

## 1. The assessment

The engineering went into the **evidence pipeline** and not into the **decision the report is
supposed to drive**. Quota management, idempotency, the deliberate "asleep outranks with-baby"
and "only medium/high confidence flags" biases, and the config-degrades-never-deletes philosophy
are all sound. But the page handed the reader six independently-computed metric cards at equal
visual weight whether the day was clean or not, when its actual job is to answer one question
fast: *should I be concerned about today, and about what specifically.*

Three things were concretely wrong rather than merely improvable:

- **Coverage overstated what was reviewed.** `merge_pieces()` set `segment_minutes` to the full
  hour and recorded `failed_pieces` separately; `coverage_for()` read only the former. A segment
  where 2 of 4 pieces failed reported 60 analyzed minutes. Coverage is the number the reader
  uses to calibrate trust in *every other number*, so this undermined the whole design.
- **`notable_events` shipped with no evidence.** `extract_clip()` was called only for
  `phone_use`. `safety_concern` — the finding most likely to lead to a real conversation with a
  real person — was the one category presented as unverifiable prose.
- **Medium-confidence flags looked identical to high-confidence ones.** Only `low` got a badge,
  yet `medium` counts toward `unauthorized_minutes` under `FLAGGABLE_CONFIDENCE`. A red "not
  allowed" someone might act on was overstating how sure the model was.

## 2. What was implemented (2026-07-30)

| # | Change | Where |
|---|--------|-------|
| 1 | Failed pieces carry their own time range → `unanalyzed_intervals` → subtracted in coverage | `nanny_analyze.merge_pieces`, `nanny_report.coverage_for` |
| 2 | Evidence clips for notable events; `prune_superseded_clips` taught to keep them | `nanny_analyze.process_segment`, `nanny_report.prune_superseded_clips` |
| 3 | Confidence tier shown on every flagged event, not only `low` | `templates/nanny.html` |
| 4 | `day_verdict()` — a deterministic top-line computed in Python, above the LLM narrative | `nanny_report.day_verdict` |
| 5 | Notable events and coverage moved to the top; narrative labelled "AI-written" | `templates/nanny.html` |
| 7 | Chunk retention (14 d, privacy not space) + local report retention (365 d) | `nanny_report.prune_old_chunks` / `prune_old_reports` |
| 8 | Stale-context warning in a `warnings` list, kept distinct from `config_errors` | `nanny_report.pipeline_warnings` |
| 9 | 28-day trend strip, low-coverage days rendered as uncertain rather than clean | `app.nanny_trend`, `templates/nanny.html` |
| — | Disk/backlog health surface | `nanny_common.disk_status`, `nanny_report.storage_status` |
| — | Reports archived to Neon by upsert (never snapshot) | `backup_sync.sync_nanny_reports` |

Two judgement calls worth knowing about:

- **Degradation qualifies a finding; it never replaces one.** A flag found in 25% coverage is
  still `attention`, not `degraded` — but the coverage caveat rides along in `reasons`. Hiding a
  real finding behind "we didn't see enough" would be the worse failure.
- **Chunk retention is a privacy measure, not a disk one.** Measured: a report is ~1.9 KB, a
  year of them ~5 MB, chunks ~0.5 MB/day. Raw video is the only thing that ever threatens the
  card, and it is already bounded. The reason chunks die at 14 days is that the granular
  per-camera record of a person's day should not outlive the clip trail that could substantiate
  it.

## 3. Deferred, with reasons

**Same-day alerting for `safety_concern`.** The fastest a parent currently learns of one is the
18:45 report. A push channel (ntfy/email) was considered and declined for now; the verdict
banner at the top of `/nanny` is the compromise. Note this leaves the highest-stakes finding on
the highest-latency path — revisit if a real safety concern is ever missed.

**A banner on the main tracker dashboard.** Deliberately not done: commit `030d8d5` removed the
nanny link from that header on purpose, and reversing it silently is not this change's call.

**Weekly digest / disposition workflow.** No way exists to mark a flag "discussed", "false
positive" or "resolved". Six months in, handled flags will look identical to fresh ones — bad
for the caregiver (stale flags read as current) and bad for the reader (no way to separate
signal from noise on review). Worth building once there is enough history to know whether false
positives are actually common.

**Higher-fidelity re-check of flagged windows.** Re-analyzing just the flagged span at higher fps
before presenting it as confirmed would cut false positives, but it partially undoes the token
work and it is unclear it beats simply labelling uncertainty well. Revisit only if false
positives prove to be a live problem.

**The 30-second cross-camera merge window** (`MERGE_GAP_SECONDS`) may collapse two genuinely
distinct back-to-back phone check-ins into one event, understating *frequency* — which is
exactly the metric a "pattern of behaviour" read depends on. Needs real footage to tune, not a
blind change.

## 4. The largest open risk: no caregiver-facing transparency

**There is nothing anywhere in this system that tells the caregiver it exists.** It produces a
daily, evidence-backed record of a named employee's conduct — phone minutes attributed to her,
video clips cut around the moments it flags — and she is not told that is happening, what is
measured, how long it is kept, or what a flag leads to.

This is a policy decision, not an engineering one, and it is recorded here precisely because
nothing in the code will ever force it. Minimum viable version: a written disclosure at hiring
covering what is recorded, what is analyzed, what is retained and for how long. Local law varies
sharply on recording employees in a private home — video-only was chosen deliberately (audio of
an employee is wiretap territory), which shows the question was already half-asked.

A second-order point that follows: the system is tuned to under-flag, and the page now says so.
That matters for the *absence* of a flag as much as its presence — a clean report is weak
evidence of a clean day, not proof of one, and it should never be cited as though it were.
