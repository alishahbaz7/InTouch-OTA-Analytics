"""SQLite connection handling and schema application."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from . import config, identity, normalize

SCHEMA_VERSION = 7
SCHEMA_PATH = Path(__file__).parent / "schema.sql"

# A whole ingest runs as one transaction — 35k rows, measured at 2.8-10.6s — and it holds the
# write lock for that entire time. SQLite's default 5s wait therefore expires mid-ingest and
# surfaces as "database is locked" on a page load, which is the one moment several people are
# most likely to be looking. Waiting is the correct behaviour here: the writer always finishes.
BUSY_TIMEOUT_SECONDS = 30.0

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
    if current < SCHEMA_VERSION:
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    # v7 adds db_meta. Stamping it here means every connection path gets an identity, including
    # a database created by a test or by an older version being upgraded in place.
    identity.ensure(conn)
    conn.commit()


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
