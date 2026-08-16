-- InTouch OTA Analytics schema.
-- Raw tables (snapshot, device_snapshot, device_group) are append-only and never rewritten.
-- Every other table is derived and can be rebuilt from them by `cli rollup`.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- ─── identity ───────────────────────────────────────────────────────────────
-- Who this database is and what it holds. Two people running this tool disagree about the
-- numbers whenever they have ingested different snapshots, and nothing else on the screen
-- answers that. See identity.py for why the comparable value is the set of ingested files
-- rather than anything about the .db file itself.

CREATE TABLE IF NOT EXISTS db_meta (
  key   TEXT PRIMARY KEY,
  value TEXT
) WITHOUT ROWID;

-- ─── raw ────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS snapshot (
  id           INTEGER PRIMARY KEY,
  source_file  TEXT    NOT NULL,
  file_sha256  TEXT    NOT NULL UNIQUE,  -- idempotency key: same file never lands twice
  snapshot_at  TEXT    NOT NULL,         -- ISO8601, parsed from the filename
  ts_source    TEXT    NOT NULL,         -- 'filename' | 'mtime' (mtime means we guessed)
  row_count    INTEGER NOT NULL,
  skipped_rows INTEGER NOT NULL DEFAULT 0,
  ingested_at  TEXT    NOT NULL,
  duration_ms  INTEGER
);
CREATE INDEX IF NOT EXISTS ix_snapshot_at ON snapshot(snapshot_at);

-- One row per device per CHANGE, not per fetch. A fetch of a fixed fleet is almost entirely
-- identical to the one before it — measured on the real database, 16 of 17 fetches differed in
-- ~150 of 35,475 devices — so storing a full copy each time cost ~23 MB to record nothing.
-- A device's state at any snapshot is its most recent row at or before that snapshot, which is
-- what the device_state view resolves. Ingest stays append-only: nothing here is ever updated.
--
-- seen_age_hours is left in place for older rows but is no longer written. It is derived
-- (snapshot_at - seen_at), so it differed on every row of every fetch and on its own defeated
-- the entire scheme: compacting with it included removed 6.7% of rows, and without it, 87.2%.
-- device_state computes it instead, from values that only change when the device actually pings.
CREATE TABLE IF NOT EXISTS device_snapshot (
  snapshot_id      INTEGER NOT NULL REFERENCES snapshot(id) ON DELETE CASCADE,
  imei             TEXT    NOT NULL,
  -- 0 marks a tombstone: the device was in the fleet and this fetch no longer lists it. Without
  -- it, "gone" and "unchanged" are the same absence of a row, and the view would resurrect
  -- devices that had left.
  present          INTEGER NOT NULL DEFAULT 1,
  status           TEXT,
  queue            INTEGER,   -- NULL = never tasked ('-' in source); 0 = none pending; n = n pending
  queue_state      TEXT,      -- never_tasked | completed | pending
  device_name      TEXT,
  created_by       TEXT,
  device_model_raw TEXT,
  device_model     TEXT,
  firmware_raw     TEXT,
  firmware         TEXT,
  fw_family        TEXT,
  fw_sortkey       TEXT,
  configuration    TEXT,
  config_sortkey   TEXT,      -- same padding scheme as fw_sortkey, so config can be ordered too
  -- API-only fields. The platform API carries the rollout intent that the spreadsheet drops:
  -- what this device is meant to move to, and what it originally shipped with.
  update_firmware  TEXT,      -- target build for this device (updateFirmVer)
  base_firmware    TEXT,      -- build the device shipped with (baseFirm) — the fallback floor
  target_config    TEXT,      -- newConfigVersion
  base_config      TEXT,      -- baseConfig
  seen_at          TEXT,
  seen_age_hours   REAL,
  iccid            TEXT,
  hw_ver           TEXT,
  vin              TEXT,      -- NULL when the source held the DL1CAB1234 placeholder
  vin_raw          TEXT,
  groups_raw       TEXT,
  first_ping       TEXT,
  PRIMARY KEY (snapshot_id, imei)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS ix_ds_imei     ON device_snapshot(imei);
CREATE INDEX IF NOT EXISTS ix_ds_queue    ON device_snapshot(snapshot_id, queue_state);
CREATE INDEX IF NOT EXISTS ix_ds_model_fw ON device_snapshot(snapshot_id, device_model, firmware);
CREATE INDEX IF NOT EXISTS ix_ds_status   ON device_snapshot(snapshot_id, status);
-- Drives the per-device "latest row at or before this snapshot" lookup device_state performs.
CREATE INDEX IF NOT EXISTS ix_ds_imei_snap ON device_snapshot(imei, snapshot_id);

-- Fleet state as of any snapshot, reconstructed from the change rows: for each device, its most
-- recent row at or before that snapshot. Readers query this exactly as they used to query
-- device_snapshot — it carries the same columns plus its own snapshot_id — so `WHERE
-- snapshot_id = ?` keeps working and no query had to be rewritten.
--
-- Measured on the real database once compacted: 0.030s for a full snapshot, 0.088s for a
-- KPI-shaped aggregate. On the uncompacted full-copy table the same view took 0.27s, so this
-- gets faster as the delta table stays small, not slower as snapshots accumulate.
DROP VIEW IF EXISTS device_state;
CREATE VIEW device_state AS
SELECT s.id AS snapshot_id,
       d.imei, d.status, d.queue, d.queue_state, d.device_name, d.created_by,
       d.device_model_raw, d.device_model, d.firmware_raw, d.firmware, d.fw_family, d.fw_sortkey,
       d.configuration, d.config_sortkey, d.update_firmware, d.base_firmware, d.target_config,
       d.base_config, d.seen_at, d.iccid, d.hw_ver, d.vin, d.vin_raw, d.groups_raw, d.first_ping,
       -- Derived rather than stored, which is what lets a row survive across fetches unchanged.
       CASE WHEN d.seen_at IS NULL THEN NULL
            ELSE (julianday(s.snapshot_at) - julianday(d.seen_at)) * 24.0 END AS seen_age_hours
FROM snapshot s
JOIN device_snapshot d
  ON d.snapshot_id = (SELECT MAX(x.snapshot_id) FROM device_snapshot x
                      WHERE x.imei = d.imei AND x.snapshot_id <= s.id)
WHERE d.present = 1;

CREATE TABLE IF NOT EXISTS device_group (
  snapshot_id INTEGER NOT NULL REFERENCES snapshot(id) ON DELETE CASCADE,
  imei        TEXT    NOT NULL,
  group_name  TEXT    NOT NULL,
  PRIMARY KEY (snapshot_id, imei, group_name)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS ix_dg_group ON device_group(snapshot_id, group_name);

-- ─── device registry: one row per IMEI, plus a log of real changes ──────────
-- Storing a full copy of every device in every snapshot costs ~35k rows per fetch and is
-- almost entirely identical each time. Instead the current state lives here once per device,
-- and history is recorded only when a tracked value actually changes. Storage then scales with
-- the number of CHANGES rather than fetches × devices.

CREATE TABLE IF NOT EXISTS device (
  imei             TEXT PRIMARY KEY,
  device_model     TEXT,
  firmware         TEXT,
  fw_sortkey       TEXT,
  configuration    TEXT,
  hw_ver           TEXT,
  vin              TEXT,
  iccid            TEXT,
  groups_raw       TEXT,
  status           TEXT,
  queue            INTEGER,
  queue_state      TEXT,
  update_firmware  TEXT,
  base_firmware    TEXT,
  target_config    TEXT,
  base_config      TEXT,
  seen_at          TEXT,          -- last ping reported by the platform
  first_ping       TEXT,
  -- The "from" side of the most recent change, kept on the row so the last move reads as
  -- from → to without touching the change log.
  prev_firmware    TEXT,
  prev_configuration TEXT,
  first_seen_at    TEXT NOT NULL, -- first fetch that ever included this device
  last_checked_at  TEXT NOT NULL, -- every fetch refreshes this, changed or not
  last_changed_at  TEXT,          -- only moves when a tracked value actually changed
  last_fw_change_at TEXT,         -- specifically the last firmware move
  checks           INTEGER NOT NULL DEFAULT 0,
  changes          INTEGER NOT NULL DEFAULT 0
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS ix_device_fw      ON device(firmware);
CREATE INDEX IF NOT EXISTS ix_device_model   ON device(device_model);
CREATE INDEX IF NOT EXISTS ix_device_changed ON device(last_changed_at);
CREATE INDEX IF NOT EXISTS ix_device_queue   ON device(queue_state, status);

CREATE TABLE IF NOT EXISTS device_change (
  id          INTEGER PRIMARY KEY,
  imei        TEXT    NOT NULL,
  changed_at  TEXT    NOT NULL,   -- the snapshot time the change was observed at
  field       TEXT    NOT NULL,   -- firmware | configuration | queue_state | model | …
  old_value   TEXT,
  new_value   TEXT,
  snapshot_id INTEGER
);
CREATE INDEX IF NOT EXISTS ix_change_imei  ON device_change(imei, changed_at);
CREATE INDEX IF NOT EXISTS ix_change_time  ON device_change(changed_at, field);
CREATE INDEX IF NOT EXISTS ix_change_field ON device_change(field, changed_at);

-- ─── derived ────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS fact_fleet_version (
  snapshot_id     INTEGER NOT NULL REFERENCES snapshot(id) ON DELETE CASCADE,
  snapshot_at     TEXT    NOT NULL,
  device_model    TEXT    NOT NULL,
  firmware        TEXT    NOT NULL,
  hw_ver          TEXT    NOT NULL,
  device_count    INTEGER NOT NULL,
  online_count    INTEGER NOT NULL,
  offline_count   INTEGER NOT NULL,
  inactive_count  INTEGER NOT NULL,
  never_tasked_count INTEGER NOT NULL,  -- QUEUE '-': never included in an OTA task
  completed_count    INTEGER NOT NULL,  -- QUEUE 0: tasked, nothing pending
  pending_count      INTEGER NOT NULL,  -- QUEUE >= 1: tasks outstanding
  pending_tasks      INTEGER NOT NULL,  -- SUM(queue): total outstanding tasks
  stale_7d_count  INTEGER NOT NULL,
  stale_30d_count INTEGER NOT NULL,
  never_seen_count INTEGER NOT NULL,
  PRIMARY KEY (snapshot_id, device_model, firmware, hw_ver)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS fact_snapshot_kpi (
  snapshot_id       INTEGER PRIMARY KEY REFERENCES snapshot(id) ON DELETE CASCADE,
  snapshot_at       TEXT    NOT NULL,
  devices_total     INTEGER NOT NULL,
  devices_online    INTEGER NOT NULL,
  devices_offline   INTEGER NOT NULL,
  devices_inactive  INTEGER NOT NULL,
  devices_never_tasked INTEGER NOT NULL,
  devices_completed    INTEGER NOT NULL,
  devices_pending      INTEGER NOT NULL,
  pending_tasks_total  INTEGER NOT NULL,
  distinct_firmware INTEGER NOT NULL,
  distinct_models   INTEGER NOT NULL,
  fragmentation     REAL    NOT NULL,
  stale_7d          INTEGER NOT NULL,
  stale_30d         INTEGER NOT NULL,
  never_seen        INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS device_transition (
  imei          TEXT    NOT NULL,
  from_snapshot INTEGER NOT NULL REFERENCES snapshot(id) ON DELETE CASCADE,
  to_snapshot   INTEGER NOT NULL REFERENCES snapshot(id) ON DELETE CASCADE,
  device_model  TEXT,
  from_firmware TEXT,
  to_firmware   TEXT,
  kind          TEXT    NOT NULL,  -- upgrade|downgrade|unchanged|new|disappeared|unknown
  status_from   TEXT,
  status_to     TEXT,
  queue_from    INTEGER,
  queue_to      INTEGER,
  queue_state_from TEXT,
  queue_state_to   TEXT,
  -- Inferred OTA task outcome. pending -> completed WITH a firmware change is the closest
  -- thing to a confirmed successful update available without an event log; the same
  -- transition WITHOUT a firmware change is the closest thing to a failure signal.
  task_event    TEXT,  -- assigned|completed_fw_changed|completed_no_fw_change|still_pending|none
  from_configuration TEXT,
  to_configuration   TEXT,
  config_kind   TEXT,  -- upgrade|downgrade|unchanged|unknown
  -- A fallback is a downgrade to a version this device has run before, which is materially
  -- different from a downgrade to a version it has never seen: it means the device reverted
  -- rather than being mis-targeted. 'original' = back to the earliest version we ever observed.
  is_fallback   INTEGER NOT NULL DEFAULT 0,
  fallback_kind TEXT,  -- original|previous|NULL
  -- Whether the device landed on the version it was actually told to install. A downgrade that
  -- matches its assigned target is an operator-driven rollback; one that does not is the device
  -- reverting on its own, which is the case worth investigating.
  matched_target INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (imei, from_snapshot, to_snapshot)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS ix_dt_kind ON device_transition(to_snapshot, kind);
CREATE INDEX IF NOT EXISTS ix_dt_fallback ON device_transition(is_fallback, to_snapshot);

CREATE TABLE IF NOT EXISTS quality_issue (
  snapshot_id INTEGER NOT NULL REFERENCES snapshot(id) ON DELETE CASCADE,
  rule        TEXT    NOT NULL,
  severity    TEXT    NOT NULL,  -- info | low | medium | high
  affected    INTEGER NOT NULL,
  sample      TEXT,              -- JSON array of up to 10 examples
  detail      TEXT,
  PRIMARY KEY (snapshot_id, rule)
) WITHOUT ROWID;

-- Declared target firmware per model. Compliance is measured against this, never against the
-- most common version: some models are end-of-life and correct as they are, and inferring the
-- target would mark an in-progress rollout's laggards as compliant.
CREATE TABLE IF NOT EXISTS firmware_target (
  device_model    TEXT PRIMARY KEY,
  target_firmware TEXT,                    -- NULL together with eol=1 means "any version is fine"
  eol             INTEGER NOT NULL DEFAULT 0,
  note            TEXT,
  updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS insight (
  id           INTEGER PRIMARY KEY,
  snapshot_id  INTEGER NOT NULL REFERENCES snapshot(id) ON DELETE CASCADE,
  generated_at TEXT    NOT NULL,
  severity     TEXT    NOT NULL,
  headline     TEXT    NOT NULL,
  detail       TEXT    NOT NULL,
  evidence     TEXT    NOT NULL,  -- JSON: the numbers behind the claim
  dismissed_at TEXT
);

-- Anything that went wrong, kept so a failure leaves a trace instead of a blank page.
CREATE TABLE IF NOT EXISTS app_error (
  id          INTEGER PRIMARY KEY,
  occurred_at TEXT NOT NULL,
  source      TEXT NOT NULL,       -- web | agent | ingest | cli
  path        TEXT,                -- request path, or the job that failed
  error_type  TEXT NOT NULL,
  message     TEXT NOT NULL,
  detail      TEXT,                -- traceback
  seen        INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_app_error_time ON app_error(occurred_at DESC);

CREATE TABLE IF NOT EXISTS report_job (
  id           INTEGER PRIMARY KEY,
  requested_at TEXT NOT NULL,
  format       TEXT NOT NULL,
  filters      TEXT,
  status       TEXT NOT NULL,
  output_path  TEXT,
  error        TEXT
);
