"""SQLite connection handling and schema application."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from . import config, identity, normalize

SCHEMA_VERSION = 23
# Read through config.resource: in a packaged build the DDL is inside the bundle, not beside
# this module — `__file__` there points at a path that does not exist on disk.
SCHEMA_PATH = config.resource("ota_analytics", "schema.sql")

# A whole ingest runs as one transaction — 35k rows, measured at 2.8-10.6s — and it holds the
# write lock for that entire time. SQLite's default 5s wait therefore expires mid-ingest and
# surfaces as "database is locked" on a page load, which is the one moment several people are
# most likely to be looking. Waiting is the correct behaviour here: the writer always finishes.
BUSY_TIMEOUT_SECONDS = 30.0

# Page cache per connection, in KiB (a negative cache_size is KiB). SQLite's default is 2 MB, and
# every request opens its own connection, so a page started cold and anything larger than 2 MB —
# the registry is 11 MB, the change log 35 MB with its indexes — was read again by each of its
# queries. Measured on the live database (23-10-2026): Changes 5.15s -> 0.55s, Overview 0.94s ->
# 0.44s, Devices 1.44s -> 0.77s, Pending 1.34s -> 0.79s. Allocated only as pages are read, and
# returned when the connection closes at the end of the request. 64 MB bought little more.
CACHE_KIB = 32 * 1024

# Databases this process has already migrated. Applying the schema costs ~0.25s and, because it
# opens a write transaction, it also serializes against every other connection. Doing that once
# per request made every page load a writer; with several people on the dashboard while the
# scheduler ingests, they queue behind each other for no reason.
_MIGRATED: set[str] = set()

# Columns added after v1. `CREATE TABLE IF NOT EXISTS` silently skips existing tables, so new
# columns have to be ALTERed in explicitly or an upgraded install keeps the old shape.
ADDED_COLUMNS: dict[str, dict[str, str]] = {
    "device_snapshot": {
        "present": "INTEGER NOT NULL DEFAULT 1",
        "config_sortkey": "TEXT",
        "update_firmware": "TEXT",
        "base_firmware": "TEXT",
        "target_config": "TEXT",
        "base_config": "TEXT",
    },
    "device": {
        "prev_firmware": "TEXT",
        "prev_configuration": "TEXT",
    },
    # v12: when a command record stopped appearing in the cloud's answers for its period —
    # deleted in the portal, most likely. Kept, and shown as such, rather than shown as current.
    "cota_command": {
        "missing_since": "TEXT",
    },
    # v16: a job's own wait for an answer, and when each answer came — on which attempt.
    "cota_campaign": {
        "answer_wait_seconds": "REAL",
        # v17: the automatic stop counts devices, and what a person has already seen is not
        # counted again; an answer that arrived during a pause has no measurable time.
        "fail_acknowledged": "TEXT",
        "resumed_at": "REAL",
        # v18: how many devices a job keeps in progress at once (NULL: all of them).
        "in_progress": "INTEGER",
        # v20: a pause stops a job's clock — its limit is its max time, and a pause must not eat it.
        "paused_at": "REAL",
        "paused_seconds": "REAL NOT NULL DEFAULT 0",
        # v22: the wait after each command, JSON, one per command (NULL: none).
        "waits": "TEXT",
    },
    "cota_campaign_result": {
        "answer_seconds": "REAL",
        "answered_attempt": "INTEGER",
    },
    # v22: a wait after a command — on the saved command, per job step, per sequence step.
    "cota_saved_command": {
        "wait_after_seconds": "INTEGER NOT NULL DEFAULT 0",
    },
    "cota_run_step": {
        "wait_after_seconds": "INTEGER NOT NULL DEFAULT 0",
    },
    "device_transition": {
        "from_configuration": "TEXT",
        "to_configuration": "TEXT",
        "config_kind": "TEXT",
        "is_fallback": "INTEGER NOT NULL DEFAULT 0",
        "fallback_kind": "TEXT",
        "matched_target": "INTEGER NOT NULL DEFAULT 0",
    },
}


def connect(db_path: Path | None = None, *, apply_schema: bool = True) -> sqlite3.Connection:
    """Open the analytics database, creating and migrating it if needed."""
    path = Path(db_path) if db_path else config.DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(path, timeout=BUSY_TIMEOUT_SECONDS)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute(f"PRAGMA busy_timeout = {int(BUSY_TIMEOUT_SECONDS * 1000)}")
    conn.execute(f"PRAGMA cache_size = -{CACHE_KIB}")

    # Version ordering in SQL: lets diffing compare firmware and config versions directly,
    # and lets migrations backfill sort keys without a Python pass over every row.
    conn.create_function("version_sortkey", 1, normalize.fw_sortkey, deterministic=True)

    if apply_schema and _needs_migration(conn, path):
        migrate(conn)
        _MIGRATED.add(str(path.resolve()))
    return conn


def _needs_migration(conn: sqlite3.Connection, path: Path) -> bool:
    """Whether the schema still has to be applied to this database.

    The in-memory set answers this for free after the first connection. It is not trusted on its
    own, though: a test — or a person clearing out `data/` — can delete and recreate the file
    underneath a long-lived process, and a cached "already migrated" would then hand out a
    connection to an empty database. `user_version` is stored in the file itself, so it stays
    true across that; reading it costs one page and settles the question.
    """
    if str(path.resolve()) not in _MIGRATED:
        return True
    return conn.execute("PRAGMA user_version").fetchone()[0] < SCHEMA_VERSION


def migrate(conn: sqlite3.Connection) -> None:
    """Apply schema.sql, then record the version.

    The DDL is written with IF NOT EXISTS throughout, so applying it to an existing database
    is a no-op. Future versions add numbered migration steps below.
    """
    # Before anything writes: v23 rewrites the whole history, so it gets a copy of it first.
    splitting = _backup_before_split(conn)

    # Add columns to already-existing tables FIRST: schema.sql creates indexes over the new
    # columns, and CREATE TABLE IF NOT EXISTS leaves an old table untouched — so running the
    # script first would try to index a column that does not exist yet. On a fresh database
    # there are no tables to alter and the script below creates everything in final form.
    for table, columns in ADDED_COLUMNS.items():
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)).fetchone()
        if not exists:
            continue
        present = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        for name, decl in columns.items():
            if name not in present:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")

    # Before the script, which indexes the new shape of cota_device.
    _rekey_cota_device(conn)

    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))

    # Backfill the config sort key for snapshots ingested before the column existed.
    conn.execute("""
        UPDATE device_snapshot SET config_sortkey = version_sortkey(configuration)
        WHERE config_sortkey IS NULL AND configuration IS NOT NULL
    """)

    current = conn.execute("PRAGMA user_version").fetchone()[0]

    if current < 6:
        # v6: compact device_snapshot from one row per device per fetch down to one row per
        # change. Every reader now goes through device_state, which reconstructs a snapshot from
        # the most recent row at or before it, so the duplicates carry no information — on the
        # sample database this removed 87% of rows and took the file from 384 MB to 50 MB.
        #
        # The comparison is built from the live table definition rather than a hard-coded list,
        # so a column added later cannot be silently left out of it — which would drop rows that
        # differ only in that column. seen_age_hours is the one deliberate exclusion: it is
        # derived from the snapshot time, so it differs on every row of every fetch and would
        # make every row look like a change (measured: 6.7% compaction with it, 87.2% without).
        columns = [r["name"] for r in conn.execute("PRAGMA table_info(device_snapshot)")
                   if r["name"] not in ("snapshot_id", "imei", "seen_age_hours")]
        if columns:
            unchanged = " AND ".join(f"p.{c} IS device_snapshot.{c}" for c in columns)
            conn.execute(f"""
                DELETE FROM device_snapshot WHERE EXISTS (
                  SELECT 1 FROM device_snapshot p
                  WHERE p.imei = device_snapshot.imei
                    AND p.snapshot_id = (SELECT MAX(y.snapshot_id) FROM device_snapshot y
                                         WHERE y.imei = device_snapshot.imei
                                           AND y.snapshot_id < device_snapshot.snapshot_id)
                    AND {unchanged})
            """)

    if current < 5:
        # v5: clear transitions left behind by the version of diff.py that recorded a row per
        # device per pair, before the "only devices that actually moved" filter existed. They
        # survive because diff_all only rebuilds consecutive pairs, while these sit mostly in
        # the wide comparisons ensure_pair creates on demand — 249,490 of 249,631 rows in the
        # sample database, all saying nothing happened. Deleting them is safe because every
        # transition is derivable: ensure_pair recomputes any pair that is asked for again.
        #
        # The condition is exactly the negation of that filter, so a row is removed only when
        # every tracked value is unchanged. Status is not among them — a status-only flip is no
        # longer a transition either (see diff.py). Note config_kind must be NULL or
        # 'unchanged': 'unknown' means one side was NULL, which is a real difference, not a no-op.
        conn.execute("""
            DELETE FROM device_transition
            WHERE kind = 'unchanged'
              AND (config_kind IS NULL OR config_kind = 'unchanged')
              AND queue_state_from IS queue_state_to
        """)
    if current < 8:
        # v8 adds device_current: the newest snapshot, kept resolved. Building it costs seconds
        # once, here, instead of on every page load — which is what it had become, because each
        # request opens its own connection and the temp table it used could not outlive one.
        # On the 245-snapshot database that was 10.9s per page.
        from . import current as _current

        newest = conn.execute("SELECT id FROM snapshot WHERE row_count > 0 "
                              "ORDER BY snapshot_at DESC, id DESC LIMIT 1").fetchone()
        if newest is not None:
            _current.rebuild(conn, newest[0])

    if current < 9:
        # v9: give the query planner statistics. Without them SQLite guesses, and on a database
        # this shape it guessed catastrophically: the registry's prev_firmware update chose
        # ix_change_field over ix_change_imei, so for each of 35,848 devices it scanned all
        # 21,097 change rows with field='firmware' — 756 million row visits, measured at **367
        # seconds for one statement**, on every single fetch. With statistics it picks the imei
        # index and the same statement takes **0.21s**.
        #
        # ANALYZE itself is 1.2s on the live 574 MB database, and this is the one place it can
        # run against an install that has been accumulating snapshots for a month without ever
        # having had them.
        conn.execute("ANALYZE")

    if current < 19:
        # v19: a device-list upload on 08-10-2026 stored 69 devices with their IMEI as the device
        # id too (the id column defaulted onto the IMEI column). The cloud's own records pair each
        # device id with its IMEI, so they put the right id back. No cloud id is an IMEI, so such
        # a row is never "a person's entry" to keep.
        conn.execute("""
            UPDATE cota_device SET source = 'cloud record',
                   device_id = (SELECT m.device_id FROM cota_command m WHERE m.imei = cota_device.imei
                                ORDER BY m.last_seen_at DESC LIMIT 1)
            WHERE CAST(device_id AS TEXT) = imei
              AND EXISTS (SELECT 1 FROM cota_command m WHERE m.imei = cota_device.imei
                          AND NOT EXISTS (SELECT 1 FROM cota_device o WHERE o.device_id = m.device_id))
        """)

    if current < 23:
        _split_seen(conn)

    if current < SCHEMA_VERSION:
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    # v7 adds db_meta. Stamping it here means every connection path gets an identity, including
    # a database created by a test or by an older version being upgraded in place.
    identity.ensure(conn)
    conn.commit()

    if splitting:
        _shrink_after_split(conn, splitting)


# The four device_snapshot indexes v23 drops. Kept by name so an upgraded database loses them
# too: schema.sql no longer creates them, and IF NOT EXISTS never removes anything.
DROPPED_INDEXES = ("ix_ds_imei", "ix_ds_queue", "ix_ds_model_fw", "ix_ds_status")


def _has_seen_column(conn: sqlite3.Connection) -> bool:
    return any(r["name"] == "seen_at" for r in conn.execute("PRAGMA table_info(device_snapshot)"))


def _backup_before_split(conn: sqlite3.Connection) -> Path | None:
    """Copy a database v23 is about to rewrite, and say so. Returns the copy, or None when
    there is nothing to rewrite (a new database, or one already converted).

    The split deletes most rows of the only history this install holds — the platform keeps the
    present, never the past — so a defect in it would be unrecoverable. The copy is the undo.
    Taken through SQLite's backup API, so it is consistent even with a WAL beside it; ~3s for the
    828 MB live file.
    """
    if conn.execute("PRAGMA user_version").fetchone()[0] >= 23 or not _has_seen_column(conn):
        return None
    if not conn.execute("SELECT 1 FROM device_snapshot LIMIT 1").fetchone():
        return None
    path = Path(conn.execute("PRAGMA database_list").fetchone()["file"])
    copy = path.with_name(f"{path.stem}.before-v23{path.suffix}")
    print(f"\n  Upgrading the database, once ({path.stat().st_size / 1e6:,.0f} MB, "
          f"about two minutes on a large history)", flush=True)
    print(f"  Copy of it as it was: {copy}", flush=True)
    target = sqlite3.connect(copy)
    try:
        conn.backup(target)
    finally:
        target.close()
    return copy


def _split_seen(conn: sqlite3.Connection) -> None:
    """v23: last pings into device_seen, and device_snapshot down to the rows that say more.

    A device pings on nearly every fetch, so with seen_at in device_snapshot 1.34 M of its 1.58 M
    rows on the live database were a full copy of a device written to record one timestamp. Here
    every move of the ping becomes a ~45-byte row of its own, the copies that recorded nothing
    else are dropped, and so is the column. Lossless: every snapshot resolves exactly as before,
    which tests/test_delta_storage.py checks on a database converted this way.

    Runs inside migrate's transaction, so an interruption leaves the database as it was and the
    next start does the whole step again.
    """
    from . import current, retention

    for name in DROPPED_INDEXES:
        conn.execute(f"DROP INDEX IF EXISTS {name}")
    if not _has_seen_column(conn):
        return                                      # created at v23, or converted already
    # The steps are said only when there is history to convert: ~2 minutes on the live file,
    # which a silent console would make look like a program that failed to start.
    has_rows = conn.execute("SELECT 1 FROM device_snapshot LIMIT 1").fetchone() is not None
    say = print if has_rows else (lambda *args, **kwargs: None)

    # 1. Every move of the last ping, per device, in snapshot order. A tombstone is not a ping
    #    and is left out of the comparison: a device that left and came back on the same last
    #    ping has not pinged again. No earlier row compares as NULL, so a device that has never
    #    pinged writes nothing.
    say("  1/3 Moving last-ping times to a table of their own...", flush=True)
    conn.execute("""
        INSERT OR IGNORE INTO device_seen (imei, snapshot_id, seen_at)
        SELECT imei, snapshot_id, seen_at FROM (
          SELECT imei, snapshot_id, seen_at, LAG(seen_at) OVER w AS before
          FROM device_snapshot WHERE present = 1
          WINDOW w AS (PARTITION BY imei ORDER BY snapshot_id))
        WHERE seen_at IS NOT before
    """)
    # 2. The rows that differed from the one before only in the ping.
    say("  2/3 Removing the copies that recorded only a ping...", flush=True)
    removed = retention.compact_rows(conn, ignore=("seen_at",))
    # 3. The column itself, and seen_age_hours, unwritten since it was found to defeat compaction.
    conn.execute("ALTER TABLE device_snapshot DROP COLUMN seen_at")
    if any(r["name"] == "seen_age_hours"
           for r in conn.execute("PRAGMA table_info(device_snapshot)")):
        conn.execute("ALTER TABLE device_snapshot DROP COLUMN seen_age_hours")
    # The resolved newest snapshot was built from the column that is now gone.
    current.refresh_latest(conn)
    # Statistics for the new table and the reshaped one: a planner guessing here is how one
    # statement once took 367 seconds (v9).
    conn.execute("ANALYZE device_snapshot")
    conn.execute("ANALYZE device_seen")
    say(f"      {removed:,} copies replaced by "
        f"{conn.execute('SELECT COUNT(*) FROM device_seen').fetchone()[0]:,} ping entries",
        flush=True)


def _shrink_after_split(conn: sqlite3.Connection, copy: Path) -> None:
    """Give the space v23 freed back to the disk. Outside migrate's transaction, because VACUUM
    cannot run inside one. Failing is not fatal — the database is converted and correct, only
    still large — and `cli vacuum` does the same later."""
    print("  3/3 Compacting the file...", flush=True)
    try:
        conn.execute("VACUUM")
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except sqlite3.OperationalError as exc:
        print(f"  ! could not compact it now ({exc}); run the vacuum command later", flush=True)
        return
    path = Path(conn.execute("PRAGMA database_list").fetchone()["file"])
    print(f"  Done: {copy.stat().st_size / 1e6:,.0f} MB -> "
          f"{path.stat().st_size / 1e6:,.0f} MB.\n", flush=True)


def _rekey_cota_device(conn: sqlite3.Connection) -> None:
    """v21: cota_device was keyed by IMEI, so a device added by its cloud id alone could not be
    stored. Rebuild it keyed by device id, keeping every row; ids were unique on the live data
    (23,103 of 23,103), and should two rows share one, the newest wins. Group members missing
    from it (2 on the live data) are added, so a device in a group is always in the list."""
    columns = {r["name"]: r["pk"] for r in conn.execute("PRAGMA table_info(cota_device)")}
    if not columns or columns.get("imei") != 1:
        return                                  # a new database, or already rebuilt
    now = conn.execute("SELECT strftime('%Y-%m-%d %H:%M:%S', 'now', 'localtime')").fetchone()[0]
    conn.execute("""
        CREATE TABLE cota_device_v21 (
          device_id INTEGER PRIMARY KEY, imei TEXT, device_type INTEGER NOT NULL,
          tags TEXT NOT NULL DEFAULT '', source TEXT, added_at TEXT, updated_at TEXT NOT NULL,
          last_job_name TEXT, last_job_status TEXT, last_job_at TEXT)
    """)
    conn.execute("""
        INSERT INTO cota_device_v21 (device_id, imei, device_type, source, added_at, updated_at)
        SELECT device_id, imei, device_type, source, updated_at, MAX(updated_at)
        FROM cota_device GROUP BY device_id
    """)
    conn.execute("DROP TABLE cota_device")
    conn.execute("ALTER TABLE cota_device_v21 RENAME TO cota_device")
    # One IMEI, one device: keep it on the newest row (the schema then indexes it as unique).
    conn.execute("""
        UPDATE cota_device SET imei = NULL
        WHERE imei IS NOT NULL AND device_id NOT IN (
            SELECT device_id FROM (SELECT device_id, MAX(updated_at) FROM cota_device
                                   WHERE imei IS NOT NULL GROUP BY imei))
    """)
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'cota_group_member'").fetchone():
        conn.execute("""
            INSERT OR IGNORE INTO cota_device (device_id, device_type, source, added_at, updated_at)
            SELECT DISTINCT device_id, 124, 'group', ?, ? FROM cota_group_member
        """, (now, now))


def refresh_statistics(conn: sqlite3.Connection) -> None:
    """Keep the query planner's statistics current. Call after anything that writes in bulk.

    `PRAGMA optimize` re-analyzes only the tables whose statistics have drifted far enough to
    matter, so on an ordinary fetch it costs milliseconds and usually does nothing at all. It is
    the maintenance half of the ANALYZE in migration v9: statistics that are never refreshed go
    stale as the database grows, and a stale plan is how one statement came to take 367 seconds.

    Writes, so it belongs on a write path only — never in a request handler.
    """
    try:
        conn.execute("PRAGMA optimize")
    except sqlite3.Error:
        pass    # planner statistics are an optimization; failing to refresh them is not an error


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Run a block in one transaction, rolling back on any exception."""
    try:
        yield conn
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()
