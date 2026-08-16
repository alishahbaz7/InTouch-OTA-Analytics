# Project Plan — InTouch OTA Analytics

Status legend: ✅ done · 🔵 in progress · ⬜ not started · 🔒 blocked

## Goal

Turn the InTouch OTA platform's data from operationally sufficient into analytically useful:
a dashboard that shows fleet and rollout health, reports that can be downloaded and shared, and
a background job that keeps both current without anyone running anything by hand.

## Guiding constraints

- **Local first.** Everything must run on a Windows laptop with Python and nothing else.
  Cloud hosting is a later deployment step, not a rewrite.
- **Ship value before completeness.** A dashboard answering five real questions beats a
  half-built platform answering none.
- **Honest metrics only.** If the data can't support a metric, we don't fake it — we log it as
  blocked on Phase 6 and say so in the UI.

## Phases

### Phase 0 — Data audit ✅ (complete, 2026-08-15)

Profiled the 35,477-row platform export. Established that it is a **fleet snapshot, not an
event stream**, which reshapes the whole architecture: history must be accumulated by ingesting
successive snapshots. Full findings in [DATA_PROFILE.md](DATA_PROFILE.md).

Deliverables: ✅ column profile · ✅ distributions · ✅ data-quality rule list · ✅ metric
feasibility matrix · ✅ open questions for the platform owner.

### Phase 1 — Snapshot warehouse ✅ (complete, 2026-08-15)

The foundation. Ingest exports into SQLite, append-only and idempotent.

- ✅ SQLite schema + migration runner (`schema.sql`, `db.py`)
- ✅ Streaming XLSX ingest, 22 MB files without loading the workbook (`ingest.py`)
- ✅ Snapshot timestamp parsed from filename, SHA-256 dedupe
- ✅ Normalization: `-` → NULL, model canonicalization, firmware family + sort key, `DD-MM-YY` dates
- ✅ `QUEUE` decoded into `queue_state` (never_tasked / completed / pending)
- ✅ `STATUS` cross-checked against `SEEN AT` age
- ✅ `Groups` exploded into a child table
- ✅ Data-quality rules run per ingest, results stored (`quality.py`)
- ✅ Rollups to fact tables (`rollup.py`) and snapshot diffing (`diff.py`)
- ✅ CLI: `ingest`, `ingest-dir`, `rollup`, `status`, `quality`
- ✅ 63 tests against a synthetic fixture carrying every known data quirk

**Result:** the 22.8 MB / 35,477-row sample ingests in **4.2 s** (target was 60 s), producing
35,475 devices and 103,897 group memberships. Re-ingesting is a no-op. 15 quality findings
recorded automatically.

### Phase 2 — Metrics + dashboard ✅ (complete, 2026-08-15)

Everything answerable from a single snapshot.

- ✅ Rollup to fact tables (`rollup.py`)
- ✅ Metric functions returning plain dicts (`metrics.py`)
- ✅ FastAPI app + Jinja templates, server-rendered charts, no JS library (`api.py`, `web/`)
- ✅ **Overview** — KPI tiles, pending-by-reason, task state per model, staleness distribution
- ✅ **Pending** — the flagship view: reachable-but-stuck devices separated from devices
  waiting on power-on, with a per-device actionable list
- ✅ **Firmware** — version share per model, compliance against declared targets, coverage gaps
- ✅ **Reachability** — online% per version paired with how long offline devices have been dark
- ✅ **Groups** — cohort composition, version spread, stuck counts
- ✅ **Devices** — filterable, paginated table (model · firmware · status · task state · group)
- ✅ **Quality** — visible panel including the permanent limits of the data source
- ✅ `firmware_target` + `cli target set` so compliance is declared, never inferred
- ✅ JSON API mirroring every view
- ✅ 72 tests

**Result:** the dashboard's headline number is no longer "7,859 pending" but **155 devices
online and stuck** — the population that was previously invisible.

### Phase 3 — Trends from multiple snapshots ⬜

Unlocked by the second export. This is where the project stops being a spreadsheet replacement.

- ✅ Snapshot diffing → per-device transitions (`diff.py`): upgrade · downgrade · unchanged ·
  new · disappeared
- ✅ **Configuration version change tracking**, separate from firmware
- ✅ **Fallback detection** — reverting to a previously-run build, split into `original` vs
  `previous`, and separated from being sent an older build
- ✅ **Fallback segregation** by model, version path, hardware revision and group, to surface
  candidate causes
- ✅ Changes page with direction arrows (green up / red down) for firmware and config
- ⬜ Firmware adoption curves over time, per model
- ⬜ Upgrade velocity — devices/day migrating to the target version, projected completion date
- ⬜ Stall detection — queue > 0 across N consecutive snapshots with no firmware change
- ⬜ Fleet churn — devices entering and leaving the export
- ⬜ Reachability trend — is online% recovering after a rollout or not?
- ⬜ Scheduler: watch the export folder, auto-ingest and roll up (`scheduler.py`)

**Done when:** three snapshots produce a correct adoption curve and a stalled-device list.

### Phase 4 — Reports ⬜

- ⬜ XLSX export: summary sheet + one sheet per metric + raw filtered device list
- ⬜ Download from the dashboard, honoring the active filters
- ⬜ Scheduled report generation (daily/weekly) written to a folder
- ⬜ PDF via print stylesheet; a real PDF library only if that proves insufficient

**Done when:** a shareable report can be produced from the UI in one click and from the CLI in
one command.

### Phase 5 — AI insight layer ⬜

Deliberately small and last, because it is only as good as the aggregates beneath it.

- ⬜ Aggregate + baseline builder (yesterday vs 7-day vs 28-day deltas)
- ⬜ One Claude call per run with structured output → `insights` table
- ⬜ Every insight carries an `evidence` JSON field citing the numbers behind it
- ⬜ "What changed" panel on the dashboard; executive summary at the top of reports
- ⬜ Severity filtering + dismiss

**Rules:** feed computed deltas, never raw rows. Arithmetic in SQL, judgment in the model.
No free-text SQL generation.

### Phase 6 — Platform DB integration ❌ (closed, 2026-08-15)

**Cancelled.** The platform owner confirmed the platform holds the same data as the export —
there is no event log, campaign table, or failure-code store to integrate with. Campaign
success rates, failure taxonomy, retry analysis, durations, and bandwidth are permanently out
of reach from existing data.

Two consequences:

1. The snapshot warehouse is the architecture, not a bridge to something better. Snapshot
   cadence is the *only* lever on analytical resolution — which promotes "get a daily export
   running" from a nice-to-have to the project's single most important dependency.
2. The dashboard should state which metrics are unavailable and that they require device/platform
   instrumentation, so the gap is understood rather than mistaken for missing work here.

**Future option (separate project):** if failure-level analytics ever become a priority, the ask
is instrumentation — per-device OTA state transitions with timestamps and structured failure
codes. Worth scoping only once this system shows what questions people actually ask.

### Phase 7 — Cloud hosting ⬜

- ⬜ Containerize (needs Docker on the target host, not this laptop)
- ⬜ SQLite → Postgres if concurrent users require it; the SQL is written to make this cheap
- ⬜ Auth in front of the dashboard
- ⬜ Real cron/systemd for the scheduler
- ⬜ Alerting: failure-spike and stall notifications by email/Slack — likely higher value than
  any chart, since it reaches people who aren't looking

## Sequencing

Phases 1 → 2 are the critical path and should be built back to back; the dashboard is what makes
the warehouse worth having. Phase 3 needs a second export. Phases 4 and 5 are independent of each
other and can be done in either order. Phase 6 is closed.

With no event data available anywhere, **snapshot cadence is the only lever on how much this
system can ever tell you.** A daily export is worth more to the finished product than any
feature on this list.

## Immediate next actions

1. **Start collecting exports daily, today.** Every day without one is a day of trend history
   that cannot be reconstructed later. Even a manual download dropped into a folder is enough —
   the ingest is idempotent, so duplicates and out-of-order files are harmless.
2. Build Phase 1 (schema, ingest, normalize, quality, CLI).
3. Ask the platform owner questions 1–4 at the bottom of [DATA_PROFILE.md](DATA_PROFILE.md).
   These are about the *meaning* of `QUEUE`, `STATUS`, and `Groups` — with no event log to fall
   back on, correct interpretation of the columns we do have is now critical. Question 5 (DB
   access) is closed.
