"""The newest snapshot, kept resolved so reading it costs nothing.

`device_state` is a view. Every reference re-resolves each device's most recent row at or before
the snapshot, across the whole fleet, and that work grows with the number of snapshots stored.
Measured on the 245-snapshot database: **4.5s per reference**, and materializing it into a temp
table per request cost **10.9s** — paid again on every page load, because each request opens its
own connection and temp tables do not outlive one.

Almost every read wants the newest snapshot, and it only changes when a fetch lands. So it is
resolved when that happens and kept in `device_current`. Advancing is incremental: a fetch of a
fixed fleet changes ~150 devices, so it costs ~150 upserts rather than re-resolving 35,848.

This is a materialization, not a cache: `device_current_meta` records which snapshot the rows
resolve, and `metrics.snapshot_source` uses them only when that matches what the caller asked
for. Anything else falls back to the view — slower, and always right.
"""

from __future__ import annotations

import sqlite3

# Every device_state column except snapshot_id (which lives in the meta row) and seen_age_hours
# (derived from the snapshot time, so storing it would go stale the moment the snapshot moved).
COLUMNS = (
    "imei", "status", "queue", "queue_state", "device_name", "created_by",
    "device_model_raw", "device_model", "firmware_raw", "firmware", "fw_family", "fw_sortkey",
    "configuration", "config_sortkey", "update_firmware", "base_firmware", "target_config",
    "base_config", "seen_at", "iccid", "hw_ver", "vin", "vin_raw", "groups_raw", "first_ping",
)

VIEW = "device_now"


def held(conn: sqlite3.Connection) -> int | None:
    """Which snapshot the materialized rows resolve, or None if there are none."""
    try:
        row = conn.execute("SELECT snapshot_id FROM device_current_meta WHERE only_row = 1"
                           ).fetchone()
    except sqlite3.OperationalError:
        return None                      # older database, before this table existed
    return row[0] if row else None


def _mark(conn: sqlite3.Connection, snapshot_id: int) -> None:
    conn.execute("INSERT INTO device_current_meta (only_row, snapshot_id) VALUES (1, ?) "
                 "ON CONFLICT(only_row) DO UPDATE SET snapshot_id = excluded.snapshot_id",
                 (snapshot_id,))


def rebuild(conn: sqlite3.Connection, snapshot_id: int) -> int:
    """Resolve a snapshot from scratch. Seconds, so only when advancing cannot be used."""
    columns = ", ".join(COLUMNS)
    conn.execute("DELETE FROM device_current")
    conn.execute(f"INSERT INTO device_current ({columns}) "
                 f"SELECT {columns} FROM device_state WHERE snapshot_id = ?", (snapshot_id,))
    _mark(conn, snapshot_id)
    return conn.execute("SELECT COUNT(*) FROM device_current").fetchone()[0]


def advance(conn: sqlite3.Connection, snapshot_id: int) -> int:
    """Move the materialization forward to `snapshot_id`. Returns rows touched.

    Only the change rows belonging to the snapshots being crossed are applied, so this is
    proportional to what moved rather than to fleet size. Crossing several at once is supported
    because a rebuild or an import can leave the materialization several snapshots behind.
    """
    previous = held(conn)
    if previous is None or previous > snapshot_id:
        # Nothing to move forward from, or the timeline was rewritten under us (import can
        # renumber). Resolving from scratch is the only answer that is certainly right.
        return rebuild(conn, snapshot_id)
    if previous == snapshot_id:
        return 0

    columns = ", ".join(COLUMNS)
    updates = ", ".join(f"{c} = excluded.{c}" for c in COLUMNS if c != "imei")

    # Devices the crossed fetches reported. Ordered by snapshot so the newest row for a device
    # is applied last and wins; ON CONFLICT then leaves exactly its values in place.
    conn.execute(f"""
        INSERT INTO device_current ({columns})
        SELECT {columns} FROM device_snapshot
        WHERE snapshot_id > ? AND snapshot_id <= ? AND present = 1
        ORDER BY snapshot_id
        ON CONFLICT(imei) DO UPDATE SET {updates}
    """, (previous, snapshot_id))
    touched = conn.total_changes

    # Devices that left. A tombstone can be followed by the device coming back, so this runs
    # after the upsert and only removes those whose newest crossed row is the tombstone.
    conn.execute("""
        DELETE FROM device_current WHERE imei IN (
          SELECT t.imei FROM device_snapshot t
          WHERE t.snapshot_id > ? AND t.snapshot_id <= ? AND t.present = 0
            AND t.snapshot_id = (SELECT MAX(x.snapshot_id) FROM device_snapshot x
                                 WHERE x.imei = t.imei AND x.snapshot_id <= ?)
        )
    """, (previous, snapshot_id, snapshot_id))

    _mark(conn, snapshot_id)
    return touched


def invalidate(conn: sqlite3.Connection) -> None:
    """Forget what is materialized, so readers fall back to the view until it is rebuilt.

    For operations that rewrite history — import renumbering snapshots, retention carrying rows
    forward. Being empty is never wrong, only slow.
    """
    try:
        conn.execute("DELETE FROM device_current_meta")
        conn.execute("DELETE FROM device_current")
    except sqlite3.OperationalError:
        pass


def refresh_latest(conn: sqlite3.Connection) -> int:
    """Re-resolve against whatever the newest snapshot now is.

    For the operations that rewrite history rather than append to it — retention carrying rows
    forward, an import renumbering snapshots. Advancing cannot be trusted across those, because
    the rows it would apply are no longer the ones that follow what is held.
    """
    row = conn.execute("SELECT id FROM snapshot WHERE row_count > 0 "
                       "ORDER BY snapshot_at DESC, id DESC LIMIT 1").fetchone()
    if row is None:
        invalidate(conn)
        return 0
    return rebuild(conn, row[0])
