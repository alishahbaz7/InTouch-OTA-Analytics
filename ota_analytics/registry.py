"""The device registry: one row per IMEI, and a log that grows only on real change.

Two timestamps per device, and the distinction between them is the point:

    last_checked_at  every fetch touches this, whether anything changed or not
    last_changed_at  only moves when a tracked value actually changed

So a device on 1.1.0 since 1 Aug that moved to 1.2.0 on 2 Aug and has been polled every
15 minutes since reads: firmware 1.2.0, changed 2 Aug, last checked 15 Aug 18:00. One row,
one change record — not 1,300 identical copies.
"""

from __future__ import annotations

import sqlite3

from . import config

# Fields whose change is a fact worth keeping forever.
#
# `status` and `seen_at` are deliberately excluded: they flip constantly (hundreds of devices
# per fetch as vehicles are switched off), which is connectivity noise rather than device
# change. Their current values live on the device row, and their aggregate history is already
# in fact_snapshot_kpi.
TRACKED = [
    "firmware", "configuration", "device_model", "hw_ver",
    "update_firmware", "base_firmware", "target_config", "base_config",
    "queue_state", "groups_raw", "iccid", "vin",
]

# Everything carried on the current-state row.
CARRIED = TRACKED + ["fw_sortkey", "status", "queue", "seen_at", "first_ping"]


def apply_snapshot(conn: sqlite3.Connection, snapshot_id: int) -> dict:
    """Fold one snapshot into the registry: log what changed, refresh current state.

    Set-based on purpose — one statement per tracked field over 35,000 devices, rather than a
    Python loop per device.
    """
    taken = conn.execute("SELECT snapshot_at FROM snapshot WHERE id = ?",
                         (snapshot_id,)).fetchone()
    if not taken:
        return {"changes": 0, "new_devices": 0, "checked": 0}
    snapshot_at = taken["snapshot_at"]

    # `device_state` is a view: every reference to it re-resolves each device's most recent row
    # at or before this snapshot, across the whole fleet. This function referred to it fifteen
    # times — once per tracked field, plus the upsert and two counts — so a single fetch paid for
    # fifteen full resolutions. Measured on the real database that was ~14s per snapshot, which
    # made replaying 37 snapshots take nearly nine minutes and put the same cost on every
    # ordinary fetch. Resolving it once into a temp table leaves fourteen scans of a plain
    # 35,000-row table instead.
    conn.execute("DROP TABLE IF EXISTS _state")
    conn.execute("CREATE TEMP TABLE _state AS SELECT * FROM device_state WHERE snapshot_id = ?",
                 (snapshot_id,))
    conn.execute("CREATE UNIQUE INDEX ix_state_imei ON _state(imei)")

    # 1. Record every tracked value that differs from what we currently hold. Devices we have
    #    never seen produce no rows here — their arrival is the 'new device' case below.
    changes = 0
    for field in TRACKED:
        cursor = conn.execute(f"""
            INSERT INTO device_change (imei, changed_at, field, old_value, new_value, snapshot_id)
            SELECT d.imei, ?, ?, r.{field}, d.{field}, ?
            FROM _state d
            JOIN device r ON r.imei = d.imei
            -- A change needs a known value on BOTH sides. NULL -> value is a first observation
            -- and value -> NULL means the source stopped reporting the field; neither is the
            -- device changing. Without this, switching from the spreadsheet export to the API
            -- logs ~35k false "changes" per missing column.
            WHERE r.{field} IS NOT NULL AND d.{field} IS NOT NULL
              AND r.{field} <> d.{field}
        """, (snapshot_at, field, snapshot_id))
        changes += cursor.rowcount

    # 2. Which devices changed at all, and which changed firmware specifically.
    conn.execute("""
        CREATE TEMP TABLE IF NOT EXISTS _changed (imei TEXT PRIMARY KEY, fw INTEGER)
    """)
    conn.execute("DELETE FROM _changed")
    conn.execute("""
        INSERT INTO _changed (imei, fw)
        SELECT imei, MAX(field = 'firmware')
        FROM device_change WHERE snapshot_id = ? GROUP BY imei
    """, (snapshot_id,))

    # Carry the "from" side of this move onto the device row, so the last change reads as
    # from → to directly.
    for field, column in (("firmware", "prev_firmware"),
                          ("configuration", "prev_configuration")):
        conn.execute(f"""
            UPDATE device SET {column} = (
                SELECT c.old_value FROM device_change c
                WHERE c.imei = device.imei AND c.snapshot_id = ? AND c.field = ?
                ORDER BY c.id DESC LIMIT 1)
            WHERE EXISTS (
                SELECT 1 FROM device_change c
                WHERE c.imei = device.imei AND c.snapshot_id = ? AND c.field = ?)
        """, (snapshot_id, field, snapshot_id, field))

    new_devices = conn.execute("""
        SELECT COUNT(*) FROM _state d
        WHERE NOT EXISTS (SELECT 1 FROM device r WHERE r.imei = d.imei)
    """).fetchone()[0]

    # 3. Upsert current state. last_checked_at always advances; last_changed_at only when this
    #    device actually moved, so "unchanged since" stays truthful across thousands of polls.
    assignments = ", ".join(f"{f} = excluded.{f}" for f in CARRIED)
    conn.execute(f"""
        INSERT INTO device (
            imei, {', '.join(CARRIED)},
            first_seen_at, last_checked_at, last_changed_at, last_fw_change_at, checks, changes)
        SELECT d.imei, {', '.join('d.' + f for f in CARRIED)},
               ?, ?, ?, ?, 1, 0
        -- `WHERE 1` is required, not decorative: with no WHERE clause on the SELECT, SQLite's
        -- parser cannot tell this ON CONFLICT from the ON of a join and refuses the statement.
        FROM _state d WHERE 1
        ON CONFLICT(imei) DO UPDATE SET
            {assignments},
            last_checked_at = excluded.last_checked_at,
            checks = device.checks + 1,
            changes = device.changes +
                      (SELECT COUNT(*) FROM _changed c WHERE c.imei = device.imei),
            last_changed_at = CASE
                WHEN EXISTS (SELECT 1 FROM _changed c WHERE c.imei = device.imei)
                THEN excluded.last_checked_at ELSE device.last_changed_at END,
            last_fw_change_at = CASE
                WHEN EXISTS (SELECT 1 FROM _changed c WHERE c.imei = device.imei AND c.fw = 1)
                THEN excluded.last_checked_at ELSE device.last_fw_change_at END
    """, (snapshot_at, snapshot_at, None, None))

    checked = conn.execute("SELECT COUNT(*) FROM _state").fetchone()[0]
    # Dropped rather than left behind: the scheduler holds one connection for the life of the
    # process, and this is a full copy of the fleet.
    conn.execute("DROP TABLE IF EXISTS _state")
    conn.commit()
    return {"changes": changes, "new_devices": new_devices, "checked": checked}


def rebuild(conn: sqlite3.Connection, on_step=None) -> dict:
    """Replay every snapshot in order to rebuild the registry from scratch.

    `on_step(done, total)` is called after each snapshot. This is the slowest phase of a merge —
    around 60% of it — so it is the one that most needs to be visible while it runs.
    """
    conn.execute("DELETE FROM device_change")
    conn.execute("DELETE FROM device")
    conn.commit()

    rows = conn.execute("SELECT id FROM snapshot ORDER BY snapshot_at, id").fetchall()
    if on_step:
        on_step(0, len(rows))

    totals = {"changes": 0, "new_devices": 0, "snapshots": 0}
    for row in rows:
        result = apply_snapshot(conn, row["id"])
        totals["changes"] += result["changes"]
        totals["new_devices"] += result["new_devices"]
        totals["snapshots"] += 1
        if on_step:
            on_step(totals["snapshots"], len(rows))
    return totals


# ─── reading it back ────────────────────────────────────────────────────────

def summary(conn: sqlite3.Connection) -> dict:
    row = conn.execute("""
        SELECT COUNT(*) AS devices,
               MIN(first_seen_at) AS first_seen,
               MAX(last_checked_at) AS last_checked,
               SUM(last_changed_at IS NOT NULL) AS ever_changed,
               SUM(checks) AS total_checks,
               SUM(changes) AS total_changes
        FROM device
    """).fetchone()
    data = dict(row) if row else {}
    data["change_rows"] = conn.execute("SELECT COUNT(*) FROM device_change").fetchone()[0]
    return data


def recent_changes(conn: sqlite3.Connection, limit: int = 200,
                   field: str | None = None, since: str | None = None) -> list[dict]:
    """The change log — every individual move, in order, immune to endpoint blind spots."""
    where, params = ["1 = 1"], []
    if field:
        where.append("c.field = ?")
        params.append(field)
    if since:
        where.append("c.changed_at >= ?")
        params.append(since)

    rows = conn.execute(f"""
        SELECT c.imei, c.changed_at, c.field, c.old_value, c.new_value,
               d.device_model, d.status, d.queue_state, d.update_firmware
        FROM device_change c
        LEFT JOIN device d ON d.imei = c.imei
        WHERE {' AND '.join(where)}
        ORDER BY c.changed_at DESC, c.id DESC
        LIMIT ?
    """, [*params, limit]).fetchall()
    return [dict(r) for r in rows]


def device_history(conn: sqlite3.Connection, imei: str) -> dict:
    """Everything known about one device: current state plus its full change log."""
    current = conn.execute("SELECT * FROM device WHERE imei = ?", (imei,)).fetchone()
    history = conn.execute("""
        SELECT changed_at, field, old_value, new_value FROM device_change
        WHERE imei = ? ORDER BY changed_at DESC, id DESC
    """, (imei,)).fetchall()
    return {"device": dict(current) if current else None,
            "history": [dict(r) for r in history]}


def round_trips(conn: sqlite3.Connection, since: str | None = None,
                until: str | None = None) -> list[dict]:
    """Devices that moved away from a version and came back to it within the period.

    Comparing two snapshots reports these as unchanged, because the endpoints match. The change
    log records each step, so the round trip is visible however the fetches happened to land.
    """
    window, params = _window_clause(since, until)
    rows = conn.execute(f"""
        SELECT c.imei, c.changed_at, c.old_value, c.new_value, d.device_model
        FROM device_change c
        JOIN device d ON d.imei = c.imei
        WHERE c.field = 'firmware' AND {window}
        ORDER BY c.imei, c.changed_at, c.id
    """, params).fetchall()

    by_device: dict[str, list] = {}
    for row in rows:
        by_device.setdefault(row["imei"], []).append(row)

    trips = []
    for imei, moves in by_device.items():
        if len(moves) < 2 or moves[0]["old_value"] != moves[-1]["new_value"]:
            continue
        trips.append({
            "imei": imei,
            "device_model": moves[0]["device_model"],
            "path": " → ".join([moves[0]["old_value"] or "?"]
                               + [m["new_value"] or "?" for m in moves]),
            "moves": len(moves),
            "last_seen": moves[-1]["changed_at"],
        })
    return trips


def fallback_segments(conn: sqlite3.Connection) -> dict[str, list[dict]]:
    """Fallbacks sliced every way that could point at a cause.

    With no recorded reason, finding one means looking for what the affected devices share.
    Each slice is a candidate explanation to check, never an answer.
    """
    base = """
        FROM device_change c
        JOIN device d ON d.imei = c.imei
        WHERE c.field = 'firmware' AND d.base_firmware IS NOT NULL
          AND c.new_value = d.base_firmware AND c.old_value <> d.base_firmware
    """

    def group_by(expression: str) -> list[dict]:
        rows = conn.execute(f"""
            SELECT {expression} AS label, COUNT(*) AS devices, COUNT(*) AS to_original
            {base} GROUP BY label ORDER BY devices DESC
        """).fetchall()
        return [dict(r) for r in rows]

    return {
        "by_model": group_by("COALESCE(d.device_model, '(unknown)')"),
        "by_path": group_by("c.old_value || '  ->  ' || c.new_value"),
        "by_hw": group_by("COALESCE(d.hw_ver, '(unknown)')"),
        "by_group": group_by("COALESCE(d.groups_raw, '(none)')"),
    }


def stalled_devices(conn: sqlite3.Connection, min_snapshots: int | None = None) -> list[dict]:
    """Devices carrying a pending task across several snapshots with no firmware change.

    Derived from the snapshots themselves: "still pending and nothing changed" is a non-event,
    and storing a row per device per pair to represent it cost more than the rest of the
    database combined.

    An inference, not a platform-reported state — its reliability follows snapshot cadence.
    """
    n = min_snapshots or config.STALL_SNAPSHOTS
    rows = conn.execute("""
        SELECT d.imei,
               MAX(d.device_model)  AS device_model,
               COUNT(*)             AS pending_streak,
               MAX(d.firmware)      AS firmware,
               MAX(d.queue)         AS pending_tasks,
               MAX(s.snapshot_at)   AS last_seen_pending
        FROM device_state d
        JOIN snapshot s ON s.id = d.snapshot_id
        WHERE d.queue_state = 'pending'
        GROUP BY d.imei
        HAVING COUNT(*) >= ? AND COUNT(DISTINCT d.firmware) = 1
        ORDER BY pending_streak DESC, pending_tasks DESC
    """, (n,)).fetchall()
    return [dict(r) for r in rows]


def fallbacks(conn: sqlite3.Connection, limit: int = 300) -> list[dict]:
    """Devices that returned to their BASE firmware — the platform owner's definition.

    Read straight from the change log, so every occurrence is caught regardless of how many
    fetches happened around it. A device that fell back and was pushed forward again still
    shows the fallback, which comparing two snapshots would miss.
    """
    rows = conn.execute("""
        SELECT c.imei, c.changed_at, c.old_value AS from_firmware, c.new_value AS to_firmware,
               d.device_model, d.base_firmware, d.update_firmware, d.firmware AS current_firmware,
               d.hw_ver, d.status, d.queue_state, d.groups_raw, d.last_checked_at
        FROM device_change c
        JOIN device d ON d.imei = c.imei
        WHERE c.field = 'firmware'
          AND d.base_firmware IS NOT NULL
          AND c.new_value = d.base_firmware
          AND c.old_value <> d.base_firmware
        ORDER BY c.changed_at DESC
        LIMIT ?
    """, (limit,)).fetchall()
    return [dict(r) for r in rows]


def at_base_firmware(conn: sqlite3.Connection) -> dict:
    """Devices sitting on their base build while targeted at something newer.

    Two populations are mixed here and only history separates them: devices that fell back,
    and devices that never successfully moved off the factory build. The count is reported with
    that caveat rather than presented as fallbacks.
    """
    row = conn.execute("""
        SELECT COUNT(*) AS at_base,
               SUM(status = 'Online') AS reachable
        FROM device
        WHERE base_firmware IS NOT NULL AND firmware = base_firmware
          AND update_firmware IS NOT NULL AND update_firmware <> firmware
    """).fetchone()
    confirmed = conn.execute("""
        SELECT COUNT(DISTINCT c.imei) FROM device_change c
        JOIN device d ON d.imei = c.imei
        WHERE c.field = 'firmware' AND d.base_firmware IS NOT NULL
          AND c.new_value = d.base_firmware AND c.old_value <> d.base_firmware
    """).fetchone()[0]
    return {"at_base": row["at_base"] or 0, "reachable": row["reachable"] or 0,
            "confirmed_fallbacks": confirmed,
            "never_moved_or_unobserved": max(0, (row["at_base"] or 0) - confirmed)}


# Rolling windows and calendar windows side by side: "last 6 hours" and "yesterday" answer
# different questions, and both get asked.
WINDOWS = [
    ("1h", "Last 1 hour"),
    ("6h", "Last 6 hours"),
    ("today", "Today"),
    ("yesterday", "Yesterday"),
    ("week", "This week"),
    ("month", "This month"),
    ("all", "All time"),
]


def window_range(window: str, now=None) -> tuple[str | None, str | None]:
    """(since, until) for a named window. Either end may be None for open-ended.

    'Yesterday' is the reason this returns a range rather than a start: it is the one window
    with a hard upper bound, and treating it as "since yesterday" would silently include today.
    """
    from datetime import datetime, timedelta

    now = now or datetime.now()
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)

    def iso(moment):
        return moment.isoformat(sep=" ", timespec="seconds")

    if window == "1h":
        return iso(now - timedelta(hours=1)), None
    if window == "6h":
        return iso(now - timedelta(hours=6)), None
    if window == "today":
        return iso(midnight), None
    if window == "yesterday":
        return iso(midnight - timedelta(days=1)), iso(midnight)
    if window == "week":
        return iso(midnight - timedelta(days=midnight.weekday())), None   # from Monday
    if window == "month":
        return iso(midnight.replace(day=1)), None
    return None, None      # all time


def window_label(window: str) -> str:
    return dict(WINDOWS).get(window, "Today")


def window_since(window: str) -> str | None:
    """Backwards-compatible helper: just the start of a window."""
    return window_range(window)[0]


def _window_clause(since: str | None, until: str | None, column: str = "c.changed_at"):
    """WHERE fragment + params for a (since, until) range, either end optional."""
    clauses, params = [], []
    if since:
        clauses.append(f"{column} >= ?")
        params.append(since)
    if until:
        clauses.append(f"{column} < ?")
        params.append(until)
    return (" AND ".join(clauses) if clauses else "1 = 1"), params


def firmware_moves(conn: sqlite3.Connection, since: str | None = None,
                   until: str | None = None, limit: int = 500, offset: int = 0) -> list[dict]:
    """Every firmware move in a period, classified — no snapshot pair to choose.

    Direction uses the same padded version key the rest of the system does, so 7.5.0.9 →
    7.5.0.51A reads as an increase. `matched_target` compares against the device's *current*
    target, which is the best available signal but can lag if the target was changed after the
    move.
    """
    window, params = _window_clause(since, until)
    rows = conn.execute(f"""
        SELECT c.imei, c.changed_at, c.old_value AS from_firmware, c.new_value AS to_firmware,
               d.device_model, d.base_firmware, d.update_firmware, d.hw_ver, d.status,
               d.queue_state, d.groups_raw,
               CASE WHEN version_sortkey(c.new_value) > version_sortkey(c.old_value)
                    THEN 'upgrade' ELSE 'downgrade' END AS direction,
               CASE WHEN d.base_firmware IS NOT NULL AND c.new_value = d.base_firmware
                    THEN 1 ELSE 0 END AS is_fallback,
               CASE WHEN d.update_firmware = c.new_value THEN 1 ELSE 0 END AS matched_target
        FROM device_change c
        JOIN device d ON d.imei = c.imei
        WHERE c.field = 'firmware' AND {window}
        -- id breaks the tie: several moves share a changed_at, since a snapshot stamps every
        -- change it observes with the same time. Without it the same row can appear on two
        -- pages and another on none, which is the classic way paging loses records.
        ORDER BY c.changed_at DESC, c.id DESC
        LIMIT ? OFFSET ?
    """, [*params, limit, offset]).fetchall()
    return [dict(r) for r in rows]


def movement_summary(conn: sqlite3.Connection, since: str | None = None,
                     until: str | None = None) -> dict:
    """Headline counts for a period, straight from the change log."""
    window, params = _window_clause(since, until)

    row = conn.execute(f"""
        SELECT
          COUNT(*) AS moves,
          COUNT(DISTINCT c.imei) AS devices,
          SUM(version_sortkey(c.new_value) >  version_sortkey(c.old_value)) AS upgrades,
          SUM(version_sortkey(c.new_value) <= version_sortkey(c.old_value)) AS downgrades,
          SUM(d.base_firmware IS NOT NULL AND c.new_value = d.base_firmware) AS fallbacks,
          SUM(version_sortkey(c.new_value) <= version_sortkey(c.old_value)
              AND d.update_firmware = c.new_value) AS planned_downgrades,
          SUM(version_sortkey(c.new_value) <= version_sortkey(c.old_value)
              AND (d.update_firmware IS NULL OR d.update_firmware <> c.new_value))
              AS unplanned_downgrades
        FROM device_change c
        JOIN device d ON d.imei = c.imei
        WHERE c.field = 'firmware' AND {window}
    """, params).fetchone()
    data = {k: (row[k] or 0) for k in row.keys()}

    data["devices_moved_twice"] = conn.execute(f"""
        SELECT COUNT(*) FROM (
          SELECT c.imei FROM device_change c
          WHERE c.field = 'firmware' AND {window}
          GROUP BY c.imei HAVING COUNT(*) > 1)
    """, params).fetchone()[0]

    plain_window, plain_params = _window_clause(since, until, "changed_at")
    other = conn.execute(f"""
        SELECT field, COUNT(*) n, COUNT(DISTINCT imei) devices FROM device_change
        WHERE field <> 'firmware' AND {plain_window}
        GROUP BY field ORDER BY n DESC
    """, plain_params).fetchall()
    data["other_fields"] = [dict(r) for r in other]

    data["config_changes"] = sum(f["n"] for f in data["other_fields"]
                                 if f["field"] == "configuration")
    return data


def changes_in_window(conn: sqlite3.Connection, hours: float,
                      field: str | None = None) -> dict:
    """How many devices changed in the last N hours — the question the registry makes cheap.

    One indexed range scan over the change log, regardless of how many fetches happened in
    between. No snapshot pair to choose, so round trips cannot hide.
    """
    from datetime import datetime, timedelta

    since = (datetime.now() - timedelta(hours=hours)).isoformat(sep=" ", timespec="seconds")
    where, params = ["changed_at >= ?"], [since]
    if field:
        where.append("field = ?")
        params.append(field)
    clause = " AND ".join(where)

    row = conn.execute(f"""
        SELECT COUNT(*) AS changes, COUNT(DISTINCT imei) AS devices
        FROM device_change WHERE {clause}
    """, params).fetchone()

    by_field = conn.execute(f"""
        SELECT field, COUNT(*) AS n, COUNT(DISTINCT imei) AS devices
        FROM device_change WHERE {clause}
        GROUP BY field ORDER BY n DESC
    """, params).fetchall()

    return {
        "hours": hours, "since": since,
        "changes": row["changes"], "devices": row["devices"],
        "by_field": [dict(r) for r in by_field],
    }


def change_windows(conn: sqlite3.Connection) -> list[dict]:
    """Standard windows for the dashboard: hour, day, week, month."""
    return [
        {"label": label, **changes_in_window(conn, hours)}
        for label, hours in (("Last hour", 1), ("Last 24 hours", 24),
                             ("Last 7 days", 168), ("Last 30 days", 720))
    ]


def stale_devices(conn: sqlite3.Connection, limit: int = 100) -> list[dict]:
    """Devices checked many times without ever changing — the settled part of the fleet."""
    rows = conn.execute("""
        SELECT imei, device_model, firmware, checks, last_changed_at, last_checked_at
        FROM device WHERE last_changed_at IS NULL
        ORDER BY checks DESC LIMIT ?
    """, (limit,)).fetchall()
    return [dict(r) for r in rows]
