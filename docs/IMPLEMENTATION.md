# Implementation Spec — InTouch OTA Analytics

The technical detail behind [PROJECT_PLAN.md](PROJECT_PLAN.md). Data facts referenced here come
from [DATA_PROFILE.md](DATA_PROFILE.md).

## Architecture

```
Platform export (.xlsx, dropped daily into Sample data/)
        │   filename carries the snapshot timestamp
        ▼
  ingest.py   streaming read (openpyxl read_only) → normalize → raw snapshot tables
        │     idempotent: SHA-256 of file; re-ingest is a no-op
        ▼
  SQLite  data/ota_analytics.db
        │     raw:  snapshot, device_snapshot, device_group
        │     derived: fact_* , device_transition, quality_issue, insight
        ▼
  rollup.py + diff.py    raw → facts (rebuildable at any time)
        ▼
  metrics.py             one function per view, returns plain dicts
        ├────────────► api.py (FastAPI) → Jinja templates + Chart.js  [dashboard]
        ├────────────► reports.py → XLSX / PDF                        [download]
        └────────────► insights.py → one Claude call → insight table  [narrative]

  scheduler.py: watch folder → ingest new files → rollup → (later) insights
```

Two invariants hold the design together:

1. **Raw tables are append-only and never rewritten.** They are the record of what the platform
   said at a point in time.
2. **Every derived table is reproducible** by re-running `rollup` over the raw tables. A bug in
   a metric is fixed with code plus a re-run, never with a manual UPDATE.

## SQLite schema

`ota_analytics/schema.sql`, applied by a migration runner in `db.py` that tracks
`schema_version` in a `meta` table.

```sql
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- ─── raw ────────────────────────────────────────────────────────────────────
CREATE TABLE snapshot (
  id            INTEGER PRIMARY KEY,
  source_file   TEXT    NOT NULL,
  file_sha256   TEXT    NOT NULL UNIQUE,   -- idempotency key
  snapshot_at   TEXT    NOT NULL,          -- ISO8601, parsed from filename
  ts_source     TEXT    NOT NULL,          -- 'filename' | 'mtime' (mtime = warn)
  row_count     INTEGER NOT NULL,
  ingested_at   TEXT    NOT NULL,
  duration_ms   INTEGER
);
CREATE INDEX ix_snapshot_at ON snapshot(snapshot_at);

CREATE TABLE device_snapshot (
  snapshot_id       INTEGER NOT NULL REFERENCES snapshot(id),
  imei              TEXT    NOT NULL,
  status            TEXT,              -- Online | Offline | Inactive
  queue             INTEGER,
  device_name       TEXT,
  created_by        TEXT,
  device_model_raw  TEXT,
  device_model      TEXT,              -- canonical
  firmware_raw      TEXT,
  firmware          TEXT,              -- canonical (V-prefix stripped)
  fw_family         TEXT,              -- '7.5.x' | '2.0.x' | '5.1.x' | …
  fw_sortkey        TEXT,              -- zero-padded, for correct ordering
  configuration     TEXT,
  seen_at           TEXT,              -- ISO8601 or NULL
  seen_age_hours    REAL,              -- snapshot_at - seen_at, precomputed
  iccid             TEXT,
  hw_ver            TEXT,
  vin               TEXT,              -- NULL when placeholder DL1CAB1234
  vin_raw           TEXT,
  groups_raw        TEXT,
  first_ping        TEXT,              -- ISO8601 or NULL
  PRIMARY KEY (snapshot_id, imei)
) WITHOUT ROWID;
CREATE INDEX ix_ds_imei     ON device_snapshot(imei);
CREATE INDEX ix_ds_model_fw ON device_snapshot(snapshot_id, device_model, firmware);
CREATE INDEX ix_ds_status   ON device_snapshot(snapshot_id, status);

CREATE TABLE device_group (
  snapshot_id INTEGER NOT NULL REFERENCES snapshot(id),
  imei        TEXT    NOT NULL,
  group_name  TEXT    NOT NULL,
  PRIMARY KEY (snapshot_id, imei, group_name)
) WITHOUT ROWID;
CREATE INDEX ix_dg_group ON device_group(snapshot_id, group_name);

-- ─── derived ────────────────────────────────────────────────────────────────
CREATE TABLE fact_fleet_version (
  snapshot_id   INTEGER NOT NULL REFERENCES snapshot(id),
  snapshot_at   TEXT    NOT NULL,
  device_model  TEXT    NOT NULL,
  firmware      TEXT    NOT NULL,
  hw_ver        TEXT    NOT NULL,
  device_count  INTEGER NOT NULL,
  online_count  INTEGER NOT NULL,
  offline_count INTEGER NOT NULL,
  inactive_count INTEGER NOT NULL,
  queued_count  INTEGER NOT NULL,
  stale_7d_count  INTEGER NOT NULL,
  stale_30d_count INTEGER NOT NULL,
  PRIMARY KEY (snapshot_id, device_model, firmware, hw_ver)
) WITHOUT ROWID;

CREATE TABLE fact_snapshot_kpi (
  snapshot_id      INTEGER PRIMARY KEY REFERENCES snapshot(id),
  snapshot_at      TEXT    NOT NULL,
  devices_total    INTEGER NOT NULL,
  devices_online   INTEGER NOT NULL,
  devices_offline  INTEGER NOT NULL,
  devices_inactive INTEGER NOT NULL,
  devices_queued   INTEGER NOT NULL,
  distinct_firmware INTEGER NOT NULL,
  fragmentation    REAL    NOT NULL,   -- see definition below
  stale_7d         INTEGER NOT NULL,
  stale_30d        INTEGER NOT NULL,
  never_seen       INTEGER NOT NULL
);

CREATE TABLE device_transition (
  imei            TEXT    NOT NULL,
  from_snapshot   INTEGER NOT NULL REFERENCES snapshot(id),
  to_snapshot     INTEGER NOT NULL REFERENCES snapshot(id),
  from_firmware   TEXT,
  to_firmware     TEXT,
  device_model    TEXT,
  kind            TEXT    NOT NULL,  -- upgrade|downgrade|unchanged|new|disappeared
  status_from     TEXT,
  status_to       TEXT,
  queue_from      INTEGER,
  queue_to        INTEGER,
  PRIMARY KEY (imei, from_snapshot, to_snapshot)
) WITHOUT ROWID;
CREATE INDEX ix_dt_kind ON device_transition(to_snapshot, kind);

CREATE TABLE quality_issue (
  snapshot_id INTEGER NOT NULL REFERENCES snapshot(id),
  rule        TEXT    NOT NULL,
  severity    TEXT    NOT NULL,   -- info | low | medium | high
  affected    INTEGER NOT NULL,
  sample      TEXT,               -- JSON array, ≤10 examples
  detail      TEXT,
  PRIMARY KEY (snapshot_id, rule)
) WITHOUT ROWID;

CREATE TABLE insight (
  id           INTEGER PRIMARY KEY,
  snapshot_id  INTEGER NOT NULL REFERENCES snapshot(id),
  generated_at TEXT    NOT NULL,
  severity     TEXT    NOT NULL,   -- info | warning | critical
  headline     TEXT    NOT NULL,
  detail       TEXT    NOT NULL,
  evidence     TEXT    NOT NULL,   -- JSON: the numbers behind the claim
  dismissed_at TEXT
);

CREATE TABLE report_job (
  id           INTEGER PRIMARY KEY,
  requested_at TEXT NOT NULL,
  format       TEXT NOT NULL,
  filters      TEXT,               -- JSON
  status       TEXT NOT NULL,      -- queued | running | done | failed
  output_path  TEXT,
  error        TEXT
);
```

## Normalization rules (`normalize.py`)

Each is a small pure function with a unit test. Precedence matters — null handling runs first.

| Rule | Behavior |
|---|---|
| `null_marker(v)` | `'-'`, `''`, `None`, whitespace-only → `None`. Applied to every text column |
| `canon_model(v)` | `AX1_SCAN` / `AX1_sCAN` / `sCAN_AX1` → `AX1_SCAN`; else strip + uppercase-preserve. Table-driven so new variants are one line |
| `canon_firmware(v)` | Strip leading `V`/`v` (`V7.2.2` → `7.2.2`), trim whitespace |
| `fw_family(model, fw)` | First two numeric components + `.x` (`7.5.0.51A` → `7.5.x`). Family is scoped to model — never compare families across models |
| `fw_sortkey(fw)` | Numeric parts zero-padded to 5 chars, letter suffix appended: `7.5.0.51A` → `00007.00005.00000.00051A`. Enables correct ORDER BY and upgrade/downgrade comparison |
| `parse_dt(v)` | `%d-%m-%y %H:%M:%S` **only** — explicit day-first. Returns ISO8601 or `None`. Never fall back to a guessing parser |
| `canon_vin(v)` | `DL1CAB1234` → `None` (placeholder on 69% of rows); keep original in `vin_raw` |
| `split_groups(v)` | Split on `,`, strip each, drop empties and `-`. Preserve order for display |

**Upgrade vs downgrade** is decided by comparing `fw_sortkey` within the same
`(device_model, fw_family)`. Across families the comparison is meaningless → classify as
`unchanged` and log a quality issue rather than inventing a direction.

## Ingest algorithm (`ingest.py`)

1. Hash the file (SHA-256, streamed). If present in `snapshot.file_sha256` → skip, return
   `already_ingested`.
2. Parse `snapshot_at` from the filename `Devices_<rows>_<DDMonYY>_<HHMM>.xlsx`. On failure use
   file mtime, set `ts_source='mtime'`, and emit a warning — trend charts depend on this being right.
3. Open with `openpyxl.load_workbook(path, read_only=True, data_only=True)`, iterate
   `ws.iter_rows(values_only=True)`. Never materialize all rows.
4. Map the header row to expected columns **by name, not position** — the platform may reorder
   or add columns. Unknown columns are logged, not fatal; missing required columns are fatal.
5. Normalize each row, batch inserts of 5,000 in one transaction.
6. Explode `Groups` into `device_group`.
7. Rows with a null/blank IMEI go to `quality_issue` and are skipped (they cannot be keyed).
8. Run quality rules; write `quality_issue` rows.
9. Roll up this snapshot; if a previous snapshot exists, diff against it into `device_transition`.

Target: the 22.8 MB / 35,477-row sample ingests in well under a minute. If it doesn't, the
bottleneck is per-row commits — batch harder.

## Metric definitions

Precise definitions so the dashboard, reports, and AI layer never disagree.

| Metric | Definition |
|---|---|
| `devices_total` | Rows in the snapshot with a non-null IMEI |
| `online_pct` | `devices_online / devices_total` |
| `stale_7d` / `stale_30d` | `seen_age_hours > 168` / `> 720`. Devices with no `SEEN AT` count as `never_seen`, **not** as stale |
| `never_seen` | `seen_at IS NULL` (644 in the sample = the Inactive devices) |
| `fragmentation` | Normalized Shannon entropy of the firmware distribution **within a model**, 0 = single version, 1 = evenly spread. Fleet-level value is the device-count-weighted mean across models |
| `queued_devices` | `queue > 0` |
| `target_version` | Per model, the version with the highest device count in the latest snapshot. Overridable in config once campaigns are known |
| `adoption_pct` | Devices on `target_version` / devices of that model |
| `upgrade_velocity` | Count of `device_transition.kind='upgrade'` to the target version between two snapshots, divided by the days between them |
| `projected_completion` | Remaining devices / trailing 3-snapshot mean velocity. Shown only with ≥3 snapshots and suppressed if velocity ≤ 0 |
| `stalled` | `queue > 0` in N consecutive snapshots (default N=3) with `firmware` unchanged |
| `regression_count` | `device_transition.kind='downgrade'` — a rollback signal, surfaced prominently |
| `churn_in` / `churn_out` | Transitions of kind `new` / `disappeared` |

**Reachability-by-firmware view** (the audit's key finding) pairs online% per version with the
`SEEN AT` age distribution for that version's offline devices. If those devices' last contact
predates the rollout, they are parked hardware; if it falls inside the rollout window, the
update is the suspect. The UI must show both series together — the number alone is ambiguous.

## API routes (`api.py`)

Pages render server-side; `/api/*` returns JSON for the charts and for any future consumer.

```
GET  /                      → Fleet Overview
GET  /firmware              → Firmware distribution
GET  /reachability          → Online% × firmware + staleness overlay
GET  /queue                 → Pending-task view
GET  /groups                → Group/cohort composition
GET  /trends                → Adoption, velocity, transitions   (≥2 snapshots)
GET  /quality               → Data-quality panel
GET  /devices               → Filterable device table, paginated
GET  /api/kpis              ?snapshot=latest
GET  /api/firmware-mix      ?model=&snapshot=
GET  /api/reachability      ?model=
GET  /api/adoption          ?model=&from=&to=
GET  /api/transitions       ?kind=&limit=
GET  /api/quality
GET  /api/insights          ?since=
POST /api/reports           {format, filters} → report_job id
GET  /api/reports/{id}      → status / download link
```

All list endpoints accept the same filter params: `model`, `firmware`, `hw_ver`, `status`,
`group`, `snapshot`. One shared filter-parsing helper builds the WHERE clause; do not hand-roll
SQL per route.

## Dashboard

Jinja templates, one per page, sharing a base layout with the filter bar and snapshot selector.
Chart.js vendored in `web/static/` — no CDN, since the target may be an internal box without
internet. Charts read from `/api/*`.

Design rules: KPI tiles across the top, one primary chart per page, supporting table beneath.
Every chart states the snapshot it reflects and how many devices it covers. Metrics blocked on
Phase 6 appear as an explicit "requires platform DB access" note rather than being silently
absent — otherwise the first question is always "where is the failure rate?"

## AI insight layer (`insights.py`, Phase 5)

1. `build_context()` — assembles a few KB of JSON: latest KPIs, per-model firmware mix, top
   reachability outliers, transition counts, stall count, quality summary, plus 7- and 28-day
   baselines and computed deltas.
2. One `anthropic` Messages call with a tool/structured-output schema mirroring the `insight`
   table (`severity`, `headline`, `detail`, `evidence`).
3. Persist rows; the dashboard renders them and reports quote them as the executive summary.

Non-negotiable: **deltas in, judgment out.** The model receives
`online_pct: 0.23, baseline_28d: 0.71, delta_pct: -67` — never a table of device rows. No
model-generated SQL. Every insight must carry `evidence`, so any claim can be checked against
the numbers that produced it.

Cost is a rounding error: one call per day over a few KB.

## Testing

- Fixture: a ~200-row XLSX generated by a script, covering every quirk — `-` markers, the three
  model spellings, `V`-prefixed firmware, placeholder VIN, null IMEI, empty groups.
  **Never test against the 22 MB file.**
- `normalize.py`: table-driven unit tests per rule.
- Ingest: idempotency (same file twice → one snapshot), header reordering, missing column.
- Diff: hand-built two-snapshot fixture asserting each transition kind, especially downgrade
  across a letter suffix (`7.5.0.51A` → `7.5.0.49A`).
- Metrics: known-answer tests on the fixture.

## Dependencies

```
openpyxl>=3.1      # xlsx read + write (installed)
fastapi>=0.115     # (installed 0.136.3)
uvicorn>=0.30      # (installed 0.49.0)
jinja2>=3.1        # templates
python-multipart   # form posts
anthropic>=0.40    # Phase 5 only
pytest>=8          # dev
```

No pandas, no numpy, no ORM. Aggregation is SQL; that keeps the logic inspectable and the
install trivial on a locked-down machine.
