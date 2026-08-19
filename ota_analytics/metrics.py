"""Metric functions for the dashboard, reports and (later) the AI insight layer.

Every function returns plain dicts and lists so the same numbers feed all three without an
ORM or DataFrame in between — and so they are trivial to test.

The framing throughout follows how the platform is actually operated: tasks are assigned in
bulk to devices that may be offline, so a pending task is *parked*, not failed. The job of
these metrics is to separate parked-and-waiting from genuinely stuck.
"""

from __future__ import annotations

import sqlite3

from . import config

# Wait-time buckets for pending devices, in hours. Chosen around the operating reality: a device
# dark for a day is routine, one dark for three months is effectively retired.
WAIT_BUCKETS = [
    ("online now", None, config.ONLINE_THRESHOLD_HOURS),
    ("1-7 days", config.ONLINE_THRESHOLD_HOURS, 168),
    ("7-30 days", 168, 720),
    ("30-90 days", 720, 2160),
    ("90+ days", 2160, None),
]


def model_filter(models: list[str] | None) -> tuple[str, list]:
    """WHERE fragment restricting to a set of device models. Empty selection means all.

    Threaded through every overview metric so one control on the page filters the whole view
    rather than each panel disagreeing about what is being shown.
    """
    models = [m for m in (models or []) if m]
    if not models:
        return "", []
    return f" AND device_model IN ({','.join('?' * len(models))})", list(models)


def latest_snapshot_id(conn: sqlite3.Connection) -> int | None:
    row = conn.execute("SELECT id FROM snapshot WHERE row_count > 0 "
                       "ORDER BY snapshot_at DESC, id DESC LIMIT 1").fetchone()
    return row["id"] if row else None


def snapshots(conn: sqlite3.Connection) -> list[dict]:
    """Snapshots that actually hold data.

    Ingest inserts the snapshot row first and fills in the device rows afterwards, so for a few
    seconds during a fetch there is a newest snapshot with nothing in it. Serving that as
    "latest" gives every page zero devices and blows up the templates — so an empty snapshot is
    simply not offered until it is complete.
    """
    return [dict(r) for r in conn.execute(
        "SELECT id, snapshot_at, source_file, row_count FROM snapshot "
        "WHERE row_count > 0 ORDER BY snapshot_at DESC")]


def kpis(conn: sqlite3.Connection, snapshot_id: int,
         models: list[str] | None = None) -> dict:
    """Headline numbers, with the pending pile already split by reason.

    Computed from the snapshot rows rather than the pre-aggregated KPI table, because those
    aggregates carry no model dimension and the page needs to be filterable by model.
    """
    clause, params = model_filter(models)
    row = conn.execute(f"""
        SELECT COUNT(*) AS devices_total,
               SUM(status = 'Online')   AS devices_online,
               SUM(status = 'Offline')  AS devices_offline,
               SUM(status = 'Inactive') AS devices_inactive,
               SUM(queue_state = 'never_tasked') AS devices_never_tasked,
               SUM(queue_state = 'completed')    AS devices_completed,
               SUM(queue_state = 'pending')      AS devices_pending,
               COALESCE(SUM(CASE WHEN queue_state = 'pending' THEN queue ELSE 0 END), 0)
                   AS pending_tasks_total,
               SUM(queue_state = 'pending' AND status = 'Online') AS pending_reachable,
               COUNT(DISTINCT firmware)     AS distinct_firmware,
               COUNT(DISTINCT device_model) AS distinct_models,
               SUM(seen_age_hours > {config.STALE_7D_HOURS})  AS stale_7d,
               SUM(seen_age_hours > {config.STALE_30D_HOURS}) AS stale_30d,
               SUM(seen_at IS NULL) AS never_seen
        FROM device_state WHERE snapshot_id = ?{clause}
    """, [snapshot_id, *params]).fetchone()

    if not row or not row["devices_total"]:
        return {}

    data = {k: (row[k] or 0) for k in row.keys()}
    data["snapshot_id"] = snapshot_id
    data["snapshot_at"] = conn.execute(
        "SELECT snapshot_at FROM snapshot WHERE id = ?", (snapshot_id,)).fetchone()["snapshot_at"]
    data["pending_waiting"] = data["devices_pending"] - data["pending_reachable"]
    data["online_pct"] = data["devices_online"] / (data["devices_total"] or 1)
    from . import rollup      # imported here to keep the module import graph acyclic
    data["fragmentation"] = rollup.fragmentation(conn, snapshot_id, models)
    data["compliance"] = compliance_summary(conn, snapshot_id)
    return data


def pending_by_reason(conn: sqlite3.Connection, snapshot_id: int,
                      models: list[str] | None = None) -> list[dict]:
    """Pending devices bucketed by how long they have been unreachable.

    The top bucket ('online now') is the actionable one: the device is powered and connected,
    so the task should have been delivered. Everything below it is waiting by design.
    """
    model_clause, model_params = model_filter(models)
    out = []
    for label, low, high in WAIT_BUCKETS:
        clauses = ["snapshot_id = ?", "queue_state = 'pending'"]
        params: list = [snapshot_id]
        if label == "online now":
            clauses.append("status = 'Online'")
        else:
            clauses.append("status <> 'Online'")
            if low is not None:
                clauses.append("seen_age_hours > ?")
                params.append(low)
            if high is not None:
                clauses.append("seen_age_hours <= ?")
                params.append(high)
        row = conn.execute(
            f"SELECT COUNT(*) n, COALESCE(SUM(queue), 0) tasks FROM device_state "
            f"WHERE {' AND '.join(clauses)}{model_clause}", [*params, *model_params]).fetchone()
        actionable = label == "online now"
        out.append({
            "bucket": label,
            # Read as a sentence: the device is pending, and this is how long it has been
            # unreachable. "Online" is the odd one out — it is reachable and still not updated.
            "label": "Pending — Online" if actionable else f"Pending — Offline since {label}",
            "devices": row["n"],
            "tasks": row["tasks"],
            "actionable": actionable,
            # Warm for a short outage, fading to grey the longer it has been dark: an hour is
            # worth chasing, ninety days is effectively retired.
            "tone": "pending-online" if actionable else f"grad-{len(out)}",
        })

    never = conn.execute(f"""
        SELECT COUNT(*) n, COALESCE(SUM(queue), 0) tasks FROM device_state
        WHERE snapshot_id = ? AND queue_state = 'pending' AND seen_at IS NULL{model_clause}
    """, [snapshot_id, *model_params]).fetchone()
    if never["n"]:
        out.append({"bucket": "never pinged", "label": "Pending — never pinged",
                    "devices": never["n"], "tasks": never["tasks"],
                    "actionable": False, "tone": "grad-5"})
    return out


# FALLBACK, as defined by the platform owner (2026-08-15):
#
#     the OTA task completed (QUEUE = 0) AND the device is running its BASE firmware.
#
# The task closed, yet the device sits on the build it shipped with — so it went back. The
# strength of this rule is that it needs no history: a device that fell back long before this
# system existed still matches, which watching for the move never could.
#
# One nuance the rule leaves in, deliberately surfaced rather than silently filtered: a device
# whose assigned target IS its base firmware has completed exactly what it was asked to do. It
# satisfies the letter of the rule without having gone backwards.
FALLBACK_RULE = """
    queue_state = 'completed'
    AND base_firmware IS NOT NULL AND firmware IS NOT NULL
    AND firmware = base_firmware
"""


def fallback_rule(alias: str = "") -> str:
    """The rule as a SQL fragment, optionally qualified for a joined query.

    One definition, one place. A second copy written inline somewhere would drift, and then two
    pages would disagree about which devices are tagged.
    """
    prefix = f"{alias}." if alias else ""
    return (f"{prefix}queue_state = 'completed' "
            f"AND {prefix}base_firmware IS NOT NULL AND {prefix}firmware IS NOT NULL "
            f"AND {prefix}firmware = {prefix}base_firmware")


def missed_target_rule(alias: str = "") -> str:
    """The unambiguous subset: it was supposed to be on something newer."""
    prefix = f"{alias}." if alias else ""
    return (f"{prefix}update_firmware IS NOT NULL "
            f"AND {prefix}update_firmware <> {prefix}firmware")

# The subset that is unambiguous: the task closed, the device is on base, and it was supposed
# to be somewhere newer.
MISSED_TARGET = " AND update_firmware IS NOT NULL AND update_firmware <> firmware"


def fallback_summary(conn: sqlite3.Connection, snapshot_id: int,
                     models: list[str] | None = None) -> dict:
    """How many devices completed their task yet sit on base firmware."""
    clause, params = model_filter(models)
    row = conn.execute(f"""
        SELECT COUNT(*) AS total,
               SUM(update_firmware IS NOT NULL AND update_firmware <> firmware) AS missed_target,
               SUM(update_firmware IS NULL OR update_firmware = firmware) AS target_was_base,
               SUM(status = 'Online') AS online
        FROM device_state
        WHERE snapshot_id = ? AND {fallback_rule()}{clause}
    """, [snapshot_id, *params]).fetchone()
    return {k: (row[k] or 0) for k in row.keys()}


def fallback_devices(conn: sqlite3.Connection, snapshot_id: int, *,
                     missed_target_only: bool = False, models: list[str] | None = None,
                     limit: int = 1000) -> list[dict]:
    """The devices themselves, newest contact first."""
    clause, params = model_filter(models)
    extra = f" AND {missed_target_rule()}" if missed_target_only else ""
    rows = conn.execute(f"""
        SELECT imei, device_model, firmware, base_firmware, update_firmware, configuration,
               hw_ver, status, queue_state, queue, seen_at, seen_age_hours, groups_raw,
               vin, iccid,
               CASE WHEN update_firmware IS NOT NULL AND update_firmware <> firmware
                    THEN 'missed target' ELSE 'target is base' END AS verdict
        FROM device_state
        WHERE snapshot_id = ? AND {fallback_rule()}{extra}{clause}
        ORDER BY seen_age_hours IS NULL, seen_age_hours, device_model
        LIMIT ?
    """, [snapshot_id, *params, limit]).fetchall()
    return [dict(r) for r in rows]


def fallback_breakdown(conn: sqlite3.Connection, snapshot_id: int,
                       models: list[str] | None = None) -> list[dict]:
    """Grouped by the version path, which is where a pattern would show."""
    clause, params = model_filter(models)
    rows = conn.execute(f"""
        SELECT device_model, firmware AS base_firmware, update_firmware,
               COUNT(*) AS devices, SUM(status = 'Online') AS online
        FROM device_state
        WHERE snapshot_id = ? AND {fallback_rule()}{clause}
        GROUP BY device_model, firmware, update_firmware
        ORDER BY devices DESC
    """, [snapshot_id, *params]).fetchall()
    return [dict(r) for r in rows]


def stuck_devices(conn: sqlite3.Connection, snapshot_id: int, limit: int = 200) -> list[dict]:
    """Devices that are online but still carrying a pending task.

    Powered, connected, and not updating — the population the platform cannot currently show.
    """
    rows = conn.execute("""
        SELECT imei, device_model, firmware, hw_ver, queue AS pending_tasks,
               seen_at, seen_age_hours, groups_raw
        FROM device_state
        WHERE snapshot_id = ? AND queue_state = 'pending' AND status = 'Online'
        ORDER BY queue DESC, device_model, firmware
        LIMIT ?
    """, (snapshot_id, limit)).fetchall()
    return [dict(r) for r in rows]


def task_state_by(conn: sqlite3.Connection, snapshot_id: int, dimension: str,
                  models: list[str] | None = None) -> list[dict]:
    """Task state broken down by model, firmware or hardware revision."""
    column = {"model": "device_model", "firmware": "firmware", "hw_ver": "hw_ver"}[dimension]
    model_clause, model_params = model_filter(models)
    rows = conn.execute(f"""
        SELECT COALESCE({column}, '(unknown)') AS label,
               COUNT(*) AS devices,
               SUM(queue_state = 'never_tasked') AS never_tasked,
               SUM(queue_state = 'completed')    AS completed,
               SUM(queue_state = 'pending')      AS pending,
               SUM(queue_state = 'pending' AND status = 'Online') AS pending_reachable,
               SUM(status = 'Online')            AS online,
               SUM(seen_age_hours > {config.STALE_30D_HOURS}) AS stale_30d
        FROM device_state
        WHERE snapshot_id = ?{model_clause}
        GROUP BY label
        ORDER BY devices DESC
    """, [snapshot_id, *model_params]).fetchall()
    return [dict(r) for r in rows]


def firmware_mix(conn: sqlite3.Connection, snapshot_id: int,
                 models: list[str] | str | None = None) -> list[dict]:
    """Firmware distribution, optionally restricted to a set of models.

    An empty or missing selection means every model — so the default view is the whole fleet
    and narrowing is opt-in.
    """
    if isinstance(models, str):
        models = [models]
    params: list = [snapshot_id]
    where = "snapshot_id = ?"
    if models:
        where += f" AND device_model IN ({','.join('?' * len(models))})"
        params.extend(models)
    rows = conn.execute(f"""
        SELECT COALESCE(firmware, '(unknown)') AS firmware,
               COALESCE(device_model, '(unknown)') AS device_model,
               COUNT(*) AS devices,
               SUM(status = 'Online') AS online,
               SUM(status = 'Offline') AS offline,
               -- Reported separately rather than left as the remainder: STATUS has three
               -- values, so online + offline does NOT reach the row total. Without this column
               -- the (unknown) firmware row reads 0% online and 4.3% offline and looks broken,
               -- when in fact 643 of its 672 devices have simply never pinged at all.
               SUM(status = 'Inactive') AS inactive,
               SUM(queue_state = 'pending') AS pending,
               MAX(fw_sortkey) AS sortkey
        FROM device_state WHERE {where}
        GROUP BY device_model, firmware
        ORDER BY devices DESC
    """, params).fetchall()
    return [dict(r) for r in rows]


def reachability_by_firmware(conn: sqlite3.Connection, snapshot_id: int,
                             min_devices: int = 20) -> list[dict]:
    """Online rate per firmware, paired with how long the offline ones have been dark.

    The pairing is the point: a low online rate with very old last-contact means retired
    hardware, while a low rate with recent contact means devices are dropping off now.
    """
    rows = conn.execute("""
        SELECT COALESCE(firmware, '(unknown)') AS firmware,
               COALESCE(device_model, '(unknown)') AS device_model,
               COUNT(*) AS devices,
               SUM(status = 'Online') AS online,
               SUM(queue_state = 'pending') AS pending,
               ROUND(AVG(CASE WHEN status <> 'Online' THEN seen_age_hours END) / 24.0, 1)
                   AS avg_dark_days,
               ROUND(MAX(seen_age_hours) / 24.0, 1) AS max_dark_days
        FROM device_state
        WHERE snapshot_id = ?
        GROUP BY device_model, firmware
        HAVING devices >= ?
        ORDER BY (CAST(online AS REAL) / devices) ASC, devices DESC
    """, (snapshot_id, min_devices)).fetchall()
    return [dict(r) for r in rows]


def staleness_buckets(conn: sqlite3.Connection, snapshot_id: int,
                      models: list[str] | None = None) -> list[dict]:
    """Whole-fleet last-seen distribution — Offline spans 25 hours to two years."""
    model_clause, model_params = model_filter(models)
    out = []
    for label, low, high in WAIT_BUCKETS:
        clauses = ["snapshot_id = ?"]
        params: list = [snapshot_id]
        if label == "online now":
            clauses.append("status = 'Online'")
        else:
            clauses.append("status <> 'Online'")
            if low is not None:
                clauses.append("seen_age_hours > ?")
                params.append(low)
            if high is not None:
                clauses.append("seen_age_hours <= ?")
                params.append(high)
        n = conn.execute(
            f"SELECT COUNT(*) FROM device_state WHERE {' AND '.join(clauses)}{model_clause}",
            [*params, *model_params]).fetchone()[0]
        out.append({"bucket": label, "devices": n})
    never = conn.execute(
        f"SELECT COUNT(*) FROM device_state WHERE snapshot_id = ? AND seen_at IS NULL"
        f"{model_clause}", [snapshot_id, *model_params]).fetchone()[0]
    out.append({"bucket": "never pinged", "devices": never})
    return out


def hourly_activity(conn: sqlite3.Connection, snapshot_id: int, hours: int = 24,
                    models: list[str] | None = None) -> list[dict]:
    """Devices whose last contact falls in each of the last N clock hours.

    An important limitation to carry into the UI: the export holds one last-ping per device,
    not a ping history. A device that pinged at 09:00 and again at 14:00 appears only in the
    14:00 bucket. So this is a *last-contact* curve, not a request-rate curve — the most recent
    bar is true traffic for that hour, and earlier bars show when devices dropped off.

    True hourly load would need hourly exports; each snapshot's most recent bucket is exact,
    so taking exports every hour would accumulate a real traffic series over time.
    """
    from datetime import datetime, timedelta

    row = conn.execute("SELECT snapshot_at FROM snapshot WHERE id = ?", (snapshot_id,)).fetchone()
    if not row:
        return []
    snapshot_at = datetime.fromisoformat(row["snapshot_at"])

    model_clause, model_params = model_filter(models)
    counts = {r["hour_key"]: r["n"] for r in conn.execute(f"""
        SELECT strftime('%Y-%m-%d %H', seen_at) AS hour_key, COUNT(*) AS n
        FROM device_state
        WHERE snapshot_id = ? AND seen_at IS NOT NULL
          AND seen_age_hours IS NOT NULL AND seen_age_hours < ?{model_clause}
        GROUP BY hour_key
    """, [snapshot_id, hours + 1, *model_params])}

    end = snapshot_at.replace(minute=0, second=0, microsecond=0)
    out = []
    for offset in range(hours - 1, -1, -1):
        slot = end - timedelta(hours=offset)
        current = offset == 0
        out.append({
            "hour": slot.strftime("%H:00"),
            "date": slot.strftime("%d %b"),
            "devices": counts.get(slot.strftime("%Y-%m-%d %H"), 0),
            "is_current": current,
            # The newest bucket is cut short by the export time, so its bar is not comparable
            # with the full hours beside it — flagged so the UI can say so rather than look
            # like traffic fell off a cliff.
            "partial": current and snapshot_at.minute > 0,
            "minutes": snapshot_at.minute if current else 60,
        })
    return out


def status_breakdown(conn: sqlite3.Connection, snapshot_id: int,
                     models: list[str] | None = None) -> list[dict]:
    """Online / Offline / Inactive, in a fixed order so colours stay stable."""
    clause, params = model_filter(models)
    counts = {r["status"]: r["n"] for r in conn.execute(f"""
        SELECT COALESCE(status, 'Unknown') AS status, COUNT(*) AS n
        FROM device_state WHERE snapshot_id = ?{clause} GROUP BY status
    """, [snapshot_id, *params])}
    return [
        {"label": "Online", "value": counts.get("Online", 0), "tone": "seg-ok"},
        {"label": "Offline", "value": counts.get("Offline", 0), "tone": "seg-warn"},
        {"label": "Never pinged", "value": counts.get("Inactive", 0), "tone": "seg-dim"},
    ]


def task_breakdown(conn: sqlite3.Connection, snapshot_id: int,
                   models: list[str] | None = None) -> list[dict]:
    """Task state, with pending split by whether the device is actually reachable."""
    clause, params = model_filter(models)
    counts = {r["queue_state"]: r["n"] for r in conn.execute(f"""
        SELECT queue_state, COUNT(*) AS n FROM device_state
        WHERE snapshot_id = ?{clause} GROUP BY queue_state
    """, [snapshot_id, *params])}
    reachable = conn.execute(f"""
        SELECT COUNT(*) FROM device_state
        WHERE snapshot_id = ? AND queue_state = 'pending' AND status = 'Online'{clause}
    """, [snapshot_id, *params]).fetchone()[0]
    pending = counts.get("pending", 0)
    return [
        {"label": "Updated", "value": counts.get("completed", 0), "tone": "seg-ok"},
        {"label": "Pending — stuck while online", "value": reachable, "tone": "seg-bad"},
        {"label": "Pending — waiting for power-on", "value": pending - reachable,
         "tone": "seg-warn"},
        {"label": "Never tasked", "value": counts.get("never_tasked", 0), "tone": "seg-dim"},
    ]


def model_breakdown(conn: sqlite3.Connection, snapshot_id: int, top: int = 5) -> list[dict]:
    """Device models, with the long tail folded into one slice so the donut stays readable."""
    rows = conn.execute("""
        SELECT COALESCE(device_model, '(unknown)') AS label, COUNT(*) AS value
        FROM device_state WHERE snapshot_id = ?
        GROUP BY label ORDER BY value DESC
    """, (snapshot_id,)).fetchall()

    out = [{"label": r["label"], "value": r["value"], "tone": f"seg-{i}"}
           for i, r in enumerate(rows[:top])]
    tail_models = len(rows) - top
    tail = sum(r["value"] for r in rows[top:])
    if tail:
        label = f"Other ({tail_models} model{'s' if tail_models != 1 else ''})"
        out.append({"label": label, "value": tail, "tone": "seg-dim"})
    return out


# ─── compliance ─────────────────────────────────────────────────────────────

def targets(conn: sqlite3.Connection) -> dict[str, dict]:
    return {r["device_model"]: dict(r) for r in conn.execute("SELECT * FROM firmware_target")}


def set_target(conn: sqlite3.Connection, model: str, firmware: str | None,
               *, eol: bool = False, note: str | None = None) -> None:
    from datetime import datetime
    conn.execute("""
        INSERT INTO firmware_target (device_model, target_firmware, eol, note, updated_at)
        VALUES (?,?,?,?,?)
        ON CONFLICT(device_model) DO UPDATE SET
          target_firmware = excluded.target_firmware, eol = excluded.eol,
          note = excluded.note, updated_at = excluded.updated_at
    """, (model, firmware, 1 if eol else 0, note,
          datetime.now().isoformat(sep=" ", timespec="seconds")))
    conn.commit()


def compliance_summary(conn: sqlite3.Connection, snapshot_id: int) -> dict:
    """Devices on their declared target firmware.

    Models with no declared target are reported separately rather than counted either way —
    guessing would either invent a rollout failure or hide a real one.
    """
    declared = targets(conn)
    rows = task_state_by(conn, snapshot_id, "model")
    on_target = off_target = eol_ok = undeclared = 0

    for row in rows:
        model = row["label"]
        target = declared.get(model)
        if target is None:
            undeclared += row["devices"]
            continue
        if target["eol"] and not target["target_firmware"]:
            eol_ok += row["devices"]
            continue
        matched = conn.execute("""
            SELECT COUNT(*) FROM device_state
            WHERE snapshot_id = ? AND device_model = ? AND firmware = ?
        """, (snapshot_id, model, target["target_firmware"])).fetchone()[0]
        on_target += matched
        off_target += row["devices"] - matched

    return {"on_target": on_target, "off_target": off_target,
            "eol_ok": eol_ok, "undeclared": undeclared,
            "declared_models": len(declared)}


def coverage_gaps(conn: sqlite3.Connection, snapshot_id: int) -> list[dict]:
    """Devices that are off-target, reachable, and have no task assigned.

    A real gap, unlike a never-tasked device that is already on its correct version. Empty
    until targets are declared — by design.
    """
    declared = targets(conn)
    out = []
    for model, target in declared.items():
        if target["eol"] and not target["target_firmware"]:
            continue
        rows = conn.execute("""
            SELECT COALESCE(firmware, '(unknown)') AS firmware, COUNT(*) AS devices,
                   SUM(status = 'Online') AS online
            FROM device_state
            WHERE snapshot_id = ? AND device_model = ? AND firmware IS NOT ?
              AND queue_state = 'never_tasked'
            GROUP BY firmware ORDER BY devices DESC
        """, (snapshot_id, model, target["target_firmware"])).fetchall()
        for row in rows:
            out.append({"device_model": model, "target": target["target_firmware"],
                        **dict(row)})
    return sorted(out, key=lambda r: -r["online"])


def quality_issues(conn: sqlite3.Connection, snapshot_id: int) -> list[dict]:
    return [dict(r) for r in conn.execute("""
        SELECT rule, severity, affected, sample, detail FROM quality_issue
        WHERE snapshot_id = ?
        ORDER BY CASE severity WHEN 'high' THEN 0 WHEN 'medium' THEN 1
                               WHEN 'low' THEN 2 ELSE 3 END, affected DESC
    """, (snapshot_id,))]


def groups(conn: sqlite3.Connection, snapshot_id: int, limit: int = 50) -> list[dict]:
    """Cohort composition — the closest thing to a campaign dimension in this data."""
    rows = conn.execute("""
        SELECT g.group_name,
               COUNT(*) AS devices,
               SUM(d.status = 'Online') AS online,
               SUM(d.queue_state = 'pending') AS pending,
               SUM(d.queue_state = 'pending' AND d.status = 'Online') AS pending_reachable,
               COUNT(DISTINCT d.firmware) AS firmware_versions
        FROM device_group g
        JOIN device_state d ON d.snapshot_id = g.snapshot_id AND d.imei = g.imei
        WHERE g.snapshot_id = ?
        GROUP BY g.group_name
        ORDER BY devices DESC
        LIMIT ?
    """, (snapshot_id, limit)).fetchall()
    return [dict(r) for r in rows]

