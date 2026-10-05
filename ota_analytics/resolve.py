"""Resolving one snapshot out of the change rows — the fast form of what `device_state` does.

`device_snapshot` stores changes, not fetches, so reading "the fleet as it stood at snapshot N"
means taking each device's most recent row at or before N. The `device_state` view expresses
that with a correlated subquery per row, and SQLite plans it as a full scan of the whole table:

    FROM snapshot s JOIN device_snapshot d
      ON d.snapshot_id = (SELECT MAX(x.snapshot_id) FROM device_snapshot x
                          WHERE x.imei = d.imei AND x.snapshot_id <= s.id)

Every row of `device_snapshot` is visited and re-tested, so one reference costs time
proportional to *all history*, not to fleet size — and history grows by another fetch every
hour. Measured on the live 272-snapshot database (1,081,179 rows, 35,848 devices): **4-18s per
reference**, against 0.01s for a plain count off the physical table.

The same answer comes out of a grouped join, which reads `ix_ds_imei_snap` as a covering index
and never touches a row it will not return:

    JOIN (SELECT imei, MAX(snapshot_id) ms FROM device_snapshot
          WHERE snapshot_id <= ?1 GROUP BY imei) m
      ON m.imei = d.imei AND m.ms = d.snapshot_id

Measured on the same database, resolving all 27 columns for all 35,848 devices: **17.5s → 1.3s**,
with byte-identical output on nine snapshots (the first, the last, and seven at random). Two
other formulations were tried and are slower than the view, not faster — a window function
(`ROW_NUMBER() OVER (PARTITION BY imei)`) at 17.4s, and restricting the view's join with
`d.snapshot_id <= s.id` at 12.5s.

**This cannot be a view.** The snapshot id has to reach the inner `GROUP BY` to bound it, and
SQLite has no LATERAL, so a view would have to resolve every snapshot before filtering to one.
`device_state` therefore stays exactly as it is: it is the readable statement of what resolution
*means*, it is what `tests/test_resolve.py` checks this module against, and it is the fallback
whenever a caller has no particular snapshot in mind. What changes is that nothing on a hot path
reads it any more.

Three ways in, cheapest first:

    source()      the table name to read a snapshot from — free when it is already resolved
    materialize() build a named temp table for it
    select()      the bare SELECT, for streaming straight into something else
"""

from __future__ import annotations

import sqlite3

# Every column `device_state` exposes except the two it computes rather than stores:
# `snapshot_id` (the id being resolved) and `seen_age_hours` (derived from the snapshot time,
# which is why storing it is forbidden — it would differ on every row of every fetch).
STORED_COLUMNS = (
    "imei", "status", "queue", "queue_state", "device_name", "created_by",
    "device_model_raw", "device_model", "firmware_raw", "firmware", "fw_family", "fw_sortkey",
    "configuration", "config_sortkey", "update_firmware", "base_firmware", "target_config",
    "base_config", "seen_at", "iccid", "hw_ver", "vin", "vin_raw", "groups_raw", "first_ping",
)

# Kept in step with device_state's own expression. Both read snapshot.snapshot_at, so a snapshot
# whose time is edited moves both together.
SEEN_AGE = ("CASE WHEN d.seen_at IS NULL THEN NULL "
            "ELSE (julianday((SELECT snapshot_at FROM snapshot WHERE id = ?1)) "
            "- julianday(d.seen_at)) * 24.0 END AS seen_age_hours")

VIEW = "device_state"


def select(columns=None, *, with_snapshot_id: bool = True, with_seen_age: bool = True) -> str:
    """The SELECT that resolves a snapshot. Takes the snapshot id once, as `?1`.

    `?1` rather than three separate placeholders because the id appears three times — in the
    projection, in the last-seen arithmetic and in the group-by bound — and three copies of one
    value is how they drift apart. Callers pass a one-element parameter tuple.
    """
    chosen = tuple(columns) if columns is not None else STORED_COLUMNS
    projected = [f"d.{c}" for c in chosen]
    if with_snapshot_id:
        projected.insert(0, "?1 AS snapshot_id")
    if with_seen_age:
        projected.append(SEEN_AGE)

    return f"""
SELECT {', '.join(projected)}
FROM device_snapshot d
JOIN (SELECT imei, MAX(snapshot_id) AS ms FROM device_snapshot
      WHERE snapshot_id <= ?1 GROUP BY imei) m
  ON m.imei = d.imei AND m.ms = d.snapshot_id
WHERE d.present = 1
"""


def materialize(conn: sqlite3.Connection, name: str, snapshot_id: int, *,
                columns=None, indexes: tuple[str, ...] = ()) -> int:
    """Resolve a snapshot into a temp table and return the row count.

    The table is created in SQLite's temp schema, so building it takes no write lock on the
    database itself — a read path stays a read path even though this writes.

    `indexes` are column lists, e.g. `("imei", "device_model, firmware")`. They are named after
    the table so two materializations on one connection cannot collide.
    """
    conn.execute(f"DROP TABLE IF EXISTS {name}")
    conn.execute(f"CREATE TEMP TABLE {name} AS {select(columns)}", (snapshot_id,))
    for position, spec in enumerate(indexes):
        unique = "UNIQUE " if spec == "imei" else ""
        conn.execute(f"CREATE {unique}INDEX ix_{name.lstrip('_')}_{position} ON {name}({spec})")
    return conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]


# Index sets worth building on a materialized snapshot. Named rather than passed as literals so
# the several callers that want "the usual ones" cannot each pick a different usual.
METRIC_INDEXES = ("imei", "device_model, firmware", "status, queue_state")


def source(conn: sqlite3.Connection, snapshot_id: int | None, *,
           name: str = "_snap", indexes: tuple[str, ...] = METRIC_INDEXES) -> str:
    """The table name to read `snapshot_id` from, materializing it only if nothing holds it.

    Order of preference, and the whole point of the function:

    1. `device_now` — the newest snapshot, kept resolved in the database by `current.py`. Free:
       no build, no per-request work. This is what almost every read asks for.
    2. A temp table already built for exactly this snapshot on this connection. Free after the
       first ask, which matters because a page calls six to ten metrics.
    3. A fresh temp table. ~1.3s on the live database, paid once per connection.

    A caller asking for a snapshot nothing holds still gets the right answer, just more slowly,
    and `snapshot_id=None` falls back to the view. Both failure modes are safe ones: this can
    make a read slow, never wrong.
    """
    from . import current                # local: current imports nothing from here, keep it so

    if snapshot_id is None:
        return VIEW
    if current.held(conn) == snapshot_id:
        return current.VIEW

    # Which snapshot the temp table holds is read back out of the table rather than tracked
    # beside it: sqlite3.Connection does not accept attributes, and a dict keyed on the
    # connection would outlive it. The rows carry snapshot_id anyway.
    try:
        held = conn.execute(f"SELECT snapshot_id FROM {name} LIMIT 1").fetchone()
        if held is not None and held[0] == snapshot_id:
            return name
    except sqlite3.OperationalError:
        pass                             # not built on this connection yet

    materialize(conn, name, snapshot_id, indexes=indexes)
    return name


def at(conn: sqlite3.Connection, snapshot_id: int | None, sql: str, *,
       name: str = "_snap", indexes: tuple[str, ...] = METRIC_INDEXES) -> str:
    """Point a per-snapshot query at whatever currently resolves that snapshot.

    The table name is swapped on the finished SQL rather than interpolated into the literal, so
    the query text in the file is exactly what runs. That is deliberate: editing inside the
    literals corrupted them twice while this was first being written — once turning SQL's
    `'Online'` into `'Onlinef'`, once rewriting a helper's query into a reference to itself.

    Every query passed here must keep its own `WHERE snapshot_id = ?`. A caller asking for a
    different snapshot than the one held then gets *nothing* rather than the wrong rows.
    """
    resolved = source(conn, snapshot_id, name=name, indexes=indexes)
    return sql if resolved == VIEW else sql.replace(VIEW, resolved)
