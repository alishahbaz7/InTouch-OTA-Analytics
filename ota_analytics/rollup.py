"""Build derived fact tables from the raw snapshot tables.

Everything here is reproducible: a rollup can be dropped and rebuilt at any time. If a metric
is wrong, the fix is code plus a re-run, never a hand-patched fact row.
"""

from __future__ import annotations

import math
import sqlite3

from . import config

# Every conditional count is COALESCEd: SUM over a group in which the condition is NULL for every
# row is NULL, not 0, and these columns are NOT NULL. A device that has never pinged has a NULL
# age, so a group made only of such devices broke the fetch on 09-11-2026 — one new device, never
# pinged, listed with its own IMEI as its model, alone in its group.
FLEET_VERSION_SQL = f"""
INSERT INTO fact_fleet_version (
  snapshot_id, snapshot_at, device_model, firmware, hw_ver,
  device_count, online_count, offline_count, inactive_count,
  never_tasked_count, completed_count, pending_count, pending_tasks,
  stale_7d_count, stale_30d_count, never_seen_count
)
SELECT
  d.snapshot_id,
  s.snapshot_at,
  COALESCE(d.device_model, '(unknown)'),
  COALESCE(d.firmware, '(unknown)'),
  COALESCE(d.hw_ver, '(unknown)'),
  COUNT(*),
  COALESCE(SUM(d.status = 'Online'), 0),
  COALESCE(SUM(d.status = 'Offline'), 0),
  COALESCE(SUM(d.status = 'Inactive'), 0),
  COALESCE(SUM(d.queue_state = 'never_tasked'), 0),
  COALESCE(SUM(d.queue_state = 'completed'), 0),
  COALESCE(SUM(d.queue_state = 'pending'), 0),
  COALESCE(SUM(CASE WHEN d.queue_state = 'pending' THEN d.queue ELSE 0 END), 0),
  COALESCE(SUM(d.seen_age_hours > {config.STALE_7D_HOURS}), 0),
  COALESCE(SUM(d.seen_age_hours > {config.STALE_30D_HOURS}), 0),
  COALESCE(SUM(d.seen_at IS NULL), 0)
FROM device_state d
JOIN snapshot s ON s.id = d.snapshot_id
WHERE d.snapshot_id = ?
GROUP BY d.snapshot_id, d.device_model, d.firmware, d.hw_ver
"""

KPI_SQL = f"""
INSERT INTO fact_snapshot_kpi (
  snapshot_id, snapshot_at, devices_total, devices_online, devices_offline, devices_inactive,
  devices_never_tasked, devices_completed, devices_pending, pending_tasks_total,
  distinct_firmware, distinct_models, fragmentation, stale_7d, stale_30d, never_seen
)
SELECT
  d.snapshot_id,
  s.snapshot_at,
  COUNT(*),
  COALESCE(SUM(d.status = 'Online'), 0),
  COALESCE(SUM(d.status = 'Offline'), 0),
  COALESCE(SUM(d.status = 'Inactive'), 0),
  COALESCE(SUM(d.queue_state = 'never_tasked'), 0),
  COALESCE(SUM(d.queue_state = 'completed'), 0),
  COALESCE(SUM(d.queue_state = 'pending'), 0),
  COALESCE(SUM(CASE WHEN d.queue_state = 'pending' THEN d.queue ELSE 0 END), 0),
  COUNT(DISTINCT d.firmware),
  COUNT(DISTINCT d.device_model),
  0.0,
  COALESCE(SUM(d.seen_age_hours > {config.STALE_7D_HOURS}), 0),
  COALESCE(SUM(d.seen_age_hours > {config.STALE_30D_HOURS}), 0),
  COALESCE(SUM(d.seen_at IS NULL), 0)
FROM device_state d
JOIN snapshot s ON s.id = d.snapshot_id
WHERE d.snapshot_id = ?
GROUP BY d.snapshot_id
"""


def _entropy(counts: list[int]) -> float:
    """Normalized Shannon entropy: 0 = one version everywhere, 1 = evenly spread."""
    total = sum(counts)
    if total <= 0 or len(counts) <= 1:
        return 0.0
    h = -sum((c / total) * math.log(c / total) for c in counts if c > 0)
    return round(h / math.log(len(counts)), 4)


def fragmentation(conn: sqlite3.Connection, snapshot_id: int,
                  models: list[str] | None = None) -> float:
    """Fleet fragmentation: per-model firmware entropy, weighted by device count.

    Computed per model because the models use unrelated versioning schemes — pooling them
    would report fragmentation that is really just model diversity.
    """
    clause, params = "", []
    if models:
        clause = f" AND device_model IN ({','.join('?' * len(models))})"
        params = list(models)

    # Routed through metrics.at like every other per-snapshot read. This one is on the request
    # path — kpis() calls it, so every page carrying the headline numbers pays for it — and
    # reading device_state directly cost a full fleet resolution each time: 4.5s on the
    # 245-snapshot database, which was most of the ten seconds kpis() took.
    from . import metrics        # imported here to keep the module import graph acyclic

    rows = conn.execute(metrics.at(conn, snapshot_id, f"""
        SELECT device_model, firmware, COUNT(*) c
        FROM device_state
        WHERE snapshot_id = ? AND device_model IS NOT NULL AND firmware IS NOT NULL{clause}
        GROUP BY device_model, firmware
    """), (snapshot_id, *params)).fetchall()

    by_model: dict[str, list[int]] = {}
    for row in rows:
        by_model.setdefault(row["device_model"], []).append(row["c"])

    total_devices = sum(sum(counts) for counts in by_model.values())
    if not total_devices:
        return 0.0
    weighted = sum(_entropy(counts) * sum(counts) for counts in by_model.values())
    return round(weighted / total_devices, 4)


def rollup_snapshot(conn: sqlite3.Connection, snapshot_id: int) -> None:
    """(Re)build the fact tables for a single snapshot.

    Both statements are routed through `resolve.at`, like every other per-snapshot read. Left
    naming the view they cost a full resolution of all history each — 13s apiece on the live
    database, paid on every fetch and again for every snapshot of a rebuild.
    """
    from . import resolve       # local, to keep the module import graph acyclic

    # All or nothing: a failure part-way rolls back rather than leaving its deletes and inserts
    # in an open transaction, which holds the write lock for as long as the connection lives.
    try:
        conn.execute("DELETE FROM fact_fleet_version WHERE snapshot_id = ?", (snapshot_id,))
        conn.execute("DELETE FROM fact_snapshot_kpi WHERE snapshot_id = ?", (snapshot_id,))
        conn.execute(resolve.at(conn, snapshot_id, FLEET_VERSION_SQL), (snapshot_id,))
        conn.execute(resolve.at(conn, snapshot_id, KPI_SQL), (snapshot_id,))
        conn.execute("UPDATE fact_snapshot_kpi SET fragmentation = ? WHERE snapshot_id = ?",
                     (fragmentation(conn, snapshot_id), snapshot_id))
    except BaseException:
        conn.rollback()
        raise
    conn.commit()


def rollup_missing(conn: sqlite3.Connection) -> list[int]:
    """Roll up every snapshot that has data but no totals. Returns the ids it rolled up.

    A fetch stores its snapshot before rolling it up, so a rollup that fails leaves the data in
    place and a gap in the history's totals — after 09-11-2026, one per hourly fetch until the fix
    was installed. Called after each fetch and at startup, so a gap closes by itself; when there
    is none it costs one indexed query.
    """
    missing = [r["id"] for r in conn.execute("""
        SELECT s.id FROM snapshot s
        WHERE s.row_count > 0
          AND NOT EXISTS (SELECT 1 FROM fact_snapshot_kpi k WHERE k.snapshot_id = s.id)
        ORDER BY s.snapshot_at, s.id
    """)]
    for snapshot_id in missing:
        rollup_snapshot(conn, snapshot_id)
    return missing


def rollup_all(conn: sqlite3.Connection, on_step=None) -> int:
    """Rebuild facts for every snapshot. Returns the number of snapshots processed.

    `on_step(done, total)` is called after each snapshot, so a caller can report progress.
    """
    ids = [r["id"] for r in conn.execute("SELECT id FROM snapshot ORDER BY snapshot_at")]
    if on_step:
        on_step(0, len(ids))
    for position, snapshot_id in enumerate(ids, start=1):
        rollup_snapshot(conn, snapshot_id)
        if on_step:
            on_step(position, len(ids))
    return len(ids)
