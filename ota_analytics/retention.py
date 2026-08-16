"""Keeping the database a sensible size without losing history, and the physical-table
surgery that depends on the same invariant.

`device_snapshot` holds one row per CHANGE, so a stored row is the authoritative value for
that device in every later snapshot until the next change, and `device_state` resolves a
snapshot by taking each device's most recent row at or before it — *by snapshot id*. Three
consequences, and every operation in this module exists because of one of them:

    prune    dropping a snapshot must carry its rows forward, or history is rewritten
    densify  a snapshot that inherits state is not self-sufficient; merging needs it to be
    renumber id order must match time order, or inserting an older snapshot resolves wrong

`compact` is the inverse of `densify` and squeezes the duplicates back out afterwards.


A snapshot costs roughly one row per device, so a 15-minute cadence over 35,000 devices is
~3.4 M device rows a day. Almost all of it is identical to the row before it. The fleet does
not need that resolution once it is a week old — but it must never lose a snapshot where
something actually changed, or the change history is destroyed.

The policy is therefore: thin by age, but always keep snapshots that recorded a change.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta

# (age in days, keep one snapshot per N hours). Anything newer than the first tier is kept in
# full; anything older than the last tier follows its rule too.
DEFAULT_TIERS = [
    (2, 0),        # < 2 days   : keep everything
    (14, 1),       # 2-14 days  : one per hour
    (90, 24),      # 14-90 days : one per day
    (10_000, 168),  # older      : one per week
]


@dataclass
class PruneResult:
    examined: int = 0
    kept: int = 0
    removed: int = 0
    kept_for_change: int = 0
    device_rows_removed: int = 0
    bytes_before: int = 0
    bytes_after: int = 0

    @property
    def bytes_freed(self) -> int:
        return max(0, self.bytes_before - self.bytes_after)


def _bucket_hours(age_days: float, tiers) -> int:
    for max_age, hours in tiers:
        if age_days < max_age:
            return hours
    return tiers[-1][1]


def _bucket_key(taken: datetime, hours: int) -> tuple:
    """Group snapshots by local clock time, not by epoch seconds.

    Epoch-based buckets align to UTC, so in a half-hour-offset timezone such as IST (+5:30)
    an "hourly" bucket would break at :30 past and keep two snapshots per clock hour. Grouping
    on the local calendar makes "one per hour" mean what it says wherever this runs.
    """
    if hours < 24:
        return (taken.year, taken.month, taken.day, taken.hour // max(1, hours))
    if hours < 168:
        return (taken.year, taken.month, taken.day, 0)
    iso = taken.isocalendar()
    return (iso[0], iso[1], 0, 0)


def snapshots_with_changes(conn: sqlite3.Connection) -> set[int]:
    """Snapshots in which something actually changed — never prune these.

    Read from the change log, which is now the single record of movement. Losing one of these
    would erase the evidence of an upgrade, rollback or fallback, which is the whole reason the
    history exists.
    """
    rows = conn.execute(
        "SELECT DISTINCT snapshot_id AS id FROM device_change "
        "WHERE snapshot_id IS NOT NULL").fetchall()
    return {r["id"] for r in rows}


def _carry_state_forward(conn: sqlite3.Connection, remove: list[int]) -> None:
    """Move a doomed snapshot's device rows onto the next surviving snapshot.

    device_snapshot holds one row per CHANGE, so a stored row is the authoritative value for
    that device in every later snapshot until the next change. Snapshot rows cascade-delete
    their device rows, so simply dropping a snapshot would not thin history — it would rewrite
    it, silently reverting those devices to an older value for every reader.

    Thinning is meant to cost time resolution, not facts. So the change moves forward to the
    snapshot that survives it: the value is preserved, and all that is lost is the knowledge of
    exactly which of the two fetches it arrived in — which is what "one per hour" asks for.

    Newest wins on collision. Doomed snapshots are handled newest-first, so anything already
    sitting on the survivor is by definition more recent than the row arriving next.
    """
    if not remove:
        return

    doomed = [r["id"] for r in conn.execute(
        f"SELECT id FROM snapshot WHERE id IN ({','.join('?' * len(remove))}) "
        f"ORDER BY snapshot_at DESC, id DESC", remove)]

    for snapshot_id in doomed:
        survivor = conn.execute("""
            SELECT id FROM snapshot
            WHERE (snapshot_at, id) > (SELECT snapshot_at, id FROM snapshot WHERE id = ?)
              AND id NOT IN (%s)
            ORDER BY snapshot_at, id LIMIT 1
        """ % ','.join('?' * len(remove)), (snapshot_id, *remove)).fetchone()
        if survivor is None:
            continue                      # nothing later survives; the rows go with the snapshot

        conn.execute("""
            DELETE FROM device_snapshot
            WHERE snapshot_id = ?
              AND EXISTS (SELECT 1 FROM device_snapshot k
                          WHERE k.snapshot_id = ? AND k.imei = device_snapshot.imei)
        """, (snapshot_id, survivor["id"]))
        conn.execute("UPDATE device_snapshot SET snapshot_id = ? WHERE snapshot_id = ?",
                     (survivor["id"], snapshot_id))


def plan(conn: sqlite3.Connection, *, now: datetime | None = None, tiers=None) -> tuple[list, list]:
    """Decide which snapshots to keep and which to drop. Returns (keep, remove) id lists."""
    tiers = tiers or DEFAULT_TIERS
    now = now or datetime.now()

    rows = conn.execute(
        "SELECT id, snapshot_at FROM snapshot ORDER BY snapshot_at, id").fetchall()
    if len(rows) <= 1:
        return [r["id"] for r in rows], []

    protected = snapshots_with_changes(conn)
    newest_id = rows[-1]["id"]
    oldest_id = rows[0]["id"]

    keep, remove, seen_buckets = [], [], set()
    for row in rows:
        try:
            taken = datetime.fromisoformat(row["snapshot_at"])
        except (TypeError, ValueError):
            keep.append(row["id"])           # unparseable time: keep it rather than guess
            continue

        age_days = (now - taken).total_seconds() / 86400
        hours = _bucket_hours(age_days, tiers)

        if hours == 0:                      # inside the full-detail window
            keep.append(row["id"])
            continue

        bucket = _bucket_key(taken, hours)

        # A snapshot kept for another reason still occupies its bucket — otherwise the bucket
        # would keep a second copy on top of it and thinning would quietly under-deliver.
        if row["id"] in (newest_id, oldest_id) or row["id"] in protected:
            seen_buckets.add(bucket)
            keep.append(row["id"])
            continue

        if bucket in seen_buckets:
            remove.append(row["id"])
        else:
            seen_buckets.add(bucket)
            keep.append(row["id"])

    return keep, remove


def prune(conn: sqlite3.Connection, *, dry_run: bool = False, now: datetime | None = None,
          tiers=None, vacuum: bool = True) -> PruneResult:
    """Apply the retention policy. Child rows go with the snapshot via ON DELETE CASCADE."""
    from pathlib import Path

    from . import config

    keep, remove = plan(conn, now=now, tiers=tiers)
    protected = snapshots_with_changes(conn)

    result = PruneResult(
        examined=len(keep) + len(remove), kept=len(keep), removed=len(remove),
        kept_for_change=len([i for i in keep if i in protected]),
    )

    db_path = Path(config.DB_PATH)
    result.bytes_before = db_path.stat().st_size if db_path.exists() else 0
    result.bytes_after = result.bytes_before

    if not remove or dry_run:
        if remove:
            result.device_rows_removed = conn.execute(
                f"SELECT COUNT(*) FROM device_snapshot WHERE snapshot_id IN "
                f"({','.join('?' * len(remove))})", remove).fetchone()[0]
        return result

    result.device_rows_removed = conn.execute(
        f"SELECT COUNT(*) FROM device_snapshot WHERE snapshot_id IN "
        f"({','.join('?' * len(remove))})", remove).fetchone()[0]

    _carry_state_forward(conn, remove)

    conn.execute("PRAGMA foreign_keys = ON")
    for batch_start in range(0, len(remove), 400):
        batch = remove[batch_start:batch_start + 400]
        conn.execute(f"DELETE FROM snapshot WHERE id IN ({','.join('?' * len(batch))})", batch)
    conn.commit()

    if vacuum:
        conn.execute("VACUUM")          # actually return the space to the filesystem
        conn.commit()

    result.bytes_after = db_path.stat().st_size if db_path.exists() else 0
    return result


# ─── physical layout: densify, compact, renumber ────────────────────────────
#
# Used by bundle import. A bundle carries change rows, which only mean anything relative to the
# chain they were written against — so before another install's snapshots can be interleaved
# into this one, the snapshots either side of the insertion point have to stop depending on
# what came before them.

# Derived from the snapshot time, so it differs on every row of every fetch. Comparing it would
# make every row look like a change and defeat compaction entirely (measured during the v6
# migration: 6.7% compaction with it, 87.2% without).
DERIVED_COLUMN = "seen_age_hours"

# Renumbering moves every id out of the way before assigning final values, so a new id can never
# land on one still in use. Snapshot counts are in the thousands at most.
RENUMBER_OFFSET = 1_000_000_000

# Tables carrying a snapshot id that is cheaper to remap than to rebuild.
RENUMBER_TABLES = [
    ("device_snapshot", "snapshot_id"),
    ("device_group", "snapshot_id"),
    ("quality_issue", "snapshot_id"),
    ("insight", "snapshot_id"),
    ("device_change", "snapshot_id"),
]

# Pure derived tables: dropped rather than remapped, because rollup rebuilds them from the raw
# snapshots in less time than remapping would take.
RENUMBER_REBUILDABLE = ["fact_fleet_version", "fact_snapshot_kpi", "device_transition"]


def device_columns(conn: sqlite3.Connection) -> list[str]:
    """Every column of device_snapshot except the snapshot it belongs to.

    Read from the live table rather than hard-coded, so a column added later cannot be silently
    left out — which would drop or duplicate rows that differ only in that column.
    """
    return [r["name"] for r in conn.execute("PRAGMA table_info(device_snapshot)")
            if r["name"] != "snapshot_id"]


def densify(conn: sqlite3.Connection, snapshot_id: int) -> int:
    """Give one snapshot an explicit row for every device, so it inherits nothing.

    A snapshot normally stores only what changed and reads the rest from earlier rows. That is
    correct as long as nothing is ever inserted between the two — the moment another install's
    snapshot lands in the gap, the later snapshot starts resolving to values its own fetch never
    saw, and nothing errors. Materializing the inherited rows first makes it immune.

    Rows already at this snapshot win: INSERT OR IGNORE keeps what the fetch actually observed
    and fills in only what was being inherited. Tombstones are copied like any other row —
    "this device is gone" is inherited exactly the same way a firmware version is.
    """
    columns = device_columns(conn)
    cursor = conn.execute(f"""
        INSERT OR IGNORE INTO device_snapshot (snapshot_id, {', '.join(columns)})
        SELECT ?, {', '.join('d.' + c for c in columns)}
        FROM device_snapshot d
        WHERE d.snapshot_id = (SELECT MAX(x.snapshot_id) FROM device_snapshot x
                               WHERE x.imei = d.imei AND x.snapshot_id <= ?)
    """, (snapshot_id, snapshot_id))
    return cursor.rowcount


def compact(conn: sqlite3.Connection, since_snapshot_id: int | None = None) -> int:
    """Drop rows identical to the same device's previous row — the inverse of densify.

    Same statement the v6 migration used, which took the sample database from 384 MB to 50 MB.
    Scoping it to a snapshot id limits the *deletion*, not the lookup: the oldest row in the
    window is still compared against its real predecessor outside it.
    """
    columns = [c for c in device_columns(conn) if c not in ("imei", DERIVED_COLUMN)]
    if not columns:
        return 0

    unchanged = " AND ".join(f"p.{c} IS device_snapshot.{c}" for c in columns)
    scope, params = "", []
    if since_snapshot_id is not None:
        scope = "device_snapshot.snapshot_id >= ? AND "
        params = [since_snapshot_id]

    cursor = conn.execute(f"""
        DELETE FROM device_snapshot
        WHERE {scope}EXISTS (
          SELECT 1 FROM device_snapshot p
          WHERE p.imei = device_snapshot.imei
            AND p.snapshot_id = (SELECT MAX(y.snapshot_id) FROM device_snapshot y
                                 WHERE y.imei = device_snapshot.imei
                                   AND y.snapshot_id < device_snapshot.snapshot_id)
            AND {unchanged})
    """, params)
    return cursor.rowcount


def is_chronological(conn: sqlite3.Connection) -> bool:
    """Whether snapshot ids are in the same order as snapshot times.

    `device_state` resolves state by comparing snapshot *ids*, so this is not a tidiness
    property — it is a correctness precondition. Ingest maintains it for free by only ever
    appending; importing another install's history is the one thing that can break it.
    """
    ids = [r["id"] for r in conn.execute("SELECT id FROM snapshot ORDER BY snapshot_at, id")]
    return ids == sorted(ids)


def renumber_snapshots(conn: sqlite3.Connection) -> int:
    """Reassign snapshot ids 1..N in chronological order. Returns how many were renumbered.

    A no-op — and cheap to ask for — when the order is already right, which it is for every
    database that has only ever ingested. Derived tables are dropped rather than remapped;
    the caller rebuilds them.
    """
    if is_chronological(conn):
        return 0

    ordered = [r["id"] for r in conn.execute("SELECT id FROM snapshot ORDER BY snapshot_at, id")]
    highest = max(ordered)
    if highest >= RENUMBER_OFFSET:
        raise ValueError(
            f"snapshot id {highest} is too large to renumber safely "
            f"(offset {RENUMBER_OFFSET:,})")

    # PRAGMA foreign_keys is silently ignored inside a transaction, and the middle of a
    # renumber is exactly when every child row points at a parent that does not exist yet.
    conn.commit()
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        conn.execute("CREATE TEMP TABLE IF NOT EXISTS _idmap "
                     "(old_id INTEGER PRIMARY KEY, new_id INTEGER)")
        conn.execute("DELETE FROM _idmap")
        conn.executemany("INSERT INTO _idmap (old_id, new_id) VALUES (?, ?)",
                         [(old, new) for new, old in enumerate(ordered, start=1)])

        tables = [("snapshot", "id"), *RENUMBER_TABLES]
        for table, column in tables:
            conn.execute(f"UPDATE {table} SET {column} = {column} + {RENUMBER_OFFSET} "
                         f"WHERE {column} IS NOT NULL")
        for table, column in tables:
            conn.execute(f"""
                UPDATE {table} SET {column} =
                  (SELECT new_id FROM _idmap WHERE old_id = {table}.{column} - {RENUMBER_OFFSET})
                WHERE {column} IS NOT NULL
            """)

        for table in RENUMBER_REBUILDABLE:
            conn.execute(f"DELETE FROM {table}")
        conn.commit()
    finally:
        conn.execute("PRAGMA foreign_keys = ON")
    return len(ordered)


def describe_policy(tiers=None) -> list[str]:
    tiers = tiers or DEFAULT_TIERS
    lines, previous = [], 0
    for max_age, hours in tiers:
        window = f"{previous}-{max_age} days" if max_age < 10_000 else f"older than {previous} days"
        if hours == 0:
            lines.append(f"{window}: keep every snapshot")
        elif hours < 24:
            lines.append(f"{window}: keep one per {hours} hour(s)")
        else:
            lines.append(f"{window}: keep one per {hours // 24} day(s)")
        previous = max_age
    lines.append("always kept: newest, oldest, and any snapshot where something changed")
    return lines
