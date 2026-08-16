"""Build derived fact tables from the raw snapshot tables.

Everything here is reproducible: a rollup can be dropped and rebuilt at any time. If a metric
is wrong, the fix is code plus a re-run, never a hand-patched fact row.
"""

from __future__ import annotations

import math
import sqlite3

from . import config

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
  SUM(d.status = 'Online'),
  SUM(d.status = 'Offline'),
  SUM(d.status = 'Inactive'),
  SUM(d.queue_state = 'never_tasked'),
  SUM(d.queue_state = 'completed'),
  SUM(d.queue_state = 'pending'),
  COALESCE(SUM(CASE WHEN d.queue_state = 'pending' THEN d.queue ELSE 0 END), 0),
  SUM(d.seen_age_hours > {config.STALE_7D_HOURS}),
  SUM(d.seen_age_hours > {config.STALE_30D_HOURS}),
  SUM(d.seen_at IS NULL)
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
  SUM(d.status = 'Online'),
  SUM(d.status = 'Offline'),
  SUM(d.status = 'Inactive'),
  SUM(d.queue_state = 'never_tasked'),
  SUM(d.queue_state = 'completed'),
  SUM(d.queue_state = 'pending'),
  COALESCE(SUM(CASE WHEN d.queue_state = 'pending' THEN d.queue ELSE 0 END), 0),
  COUNT(DISTINCT d.firmware),
  COUNT(DISTINCT d.device_model),
  0.0,
  SUM(d.seen_age_hours > {config.STALE_7D_HOURS}),
  SUM(d.seen_age_hours > {config.STALE_30D_HOURS}),
  SUM(d.seen_at IS NULL)
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

    rows = conn.execute(f"""
        SELECT device_model, firmware, COUNT(*) c
        FROM device_state
        WHERE snapshot_id = ? AND device_model IS NOT NULL AND firmware IS NOT NULL{clause}
        GROUP BY device_model, firmware
    """, (snapshot_id, *params)).fetchall()

    by_model: dict[str, list[int]] = {}
    for row in rows:
        by_model.setdefault(row["device_model"], []).append(row["c"])

    total_devices = sum(sum(counts) for counts in by_model.values())
    if not total_devices:
        return 0.0
    weighted = sum(_entropy(counts) * sum(counts) for counts in by_model.values())
    return round(weighted / total_devices, 4)


def rollup_snapshot(conn: sqlite3.Connection, snapshot_id: int) -> None:
    """(Re)build the fact tables for a single snapshot."""
    conn.execute("DELETE FROM fact_fleet_version WHERE snapshot_id = ?", (snapshot_id,))
    conn.execute("DELETE FROM fact_snapshot_kpi WHERE snapshot_id = ?", (snapshot_id,))
    conn.execute(FLEET_VERSION_SQL, (snapshot_id,))
    conn.execute(KPI_SQL, (snapshot_id,))
    conn.execute("UPDATE fact_snapshot_kpi SET fragmentation = ? WHERE snapshot_id = ?",
                 (fragmentation(conn, snapshot_id), snapshot_id))
    conn.commit()


def rollup_all(conn: sqlite3.Connection) -> int:
    """Rebuild facts for every snapshot. Returns the number of snapshots processed."""
    ids = [r["id"] for r in conn.execute("SELECT id FROM snapshot ORDER BY snapshot_at")]
    for snapshot_id in ids:
        rollup_snapshot(conn, snapshot_id)
    return len(ids)
