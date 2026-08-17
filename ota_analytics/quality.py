"""Data-quality rules, run on every ingest.

Findings are stored per snapshot rather than logged and forgotten, so the dashboard can show
data quality as a first-class panel — the export has enough quirks that hiding them would make
the numbers above look more authoritative than they are.
"""

from __future__ import annotations

import json
import sqlite3

from . import config

Finding = tuple[str, str, int, list[str], str]  # rule, severity, affected, sample, detail


def _scalar(conn: sqlite3.Connection, sql: str, params: tuple) -> int:
    return conn.execute(sql, params).fetchone()[0] or 0


def _sample(conn: sqlite3.Connection, sql: str, params: tuple, limit: int = 10) -> list[str]:
    rows = conn.execute(f"{sql} LIMIT {limit}", params).fetchall()
    return [str(r[0]) for r in rows]


def run_rules(
    conn: sqlite3.Connection,
    snapshot_id: int,
    *,
    skipped_no_imei: int = 0,
    duplicate_imei: int = 0,
    unknown_columns: list[str] | None = None,
    ts_source: str = "filename",
) -> list[Finding]:
    """Evaluate every rule against one ingested snapshot and persist the findings."""
    sid = (snapshot_id,)
    findings: list[Finding] = []

    if skipped_no_imei:
        findings.append((
            "missing_imei", "high", skipped_no_imei, [],
            "Rows with no IMEI cannot be keyed to a device and were skipped.",
        ))

    if duplicate_imei:
        findings.append((
            "duplicate_imei", "high", duplicate_imei, [],
            "The same IMEI appeared more than once; only the first occurrence was kept.",
        ))

    if ts_source != "filename":
        findings.append((
            "snapshot_time_guessed", "high", 1, [],
            "Snapshot timestamp came from file mtime, not the filename. Trend spacing may be wrong.",
        ))

    if unknown_columns:
        findings.append((
            "unknown_columns", "medium", len(unknown_columns), list(unknown_columns),
            "Export contains columns this build does not map. They were ignored.",
        ))

    # Control characters in an identifier. Found in the real export: 128 devices whose ICCID is
    # a valid number followed by a backspace and stray bytes, and one whose CONFIGURATION has the
    # same shape. Worth a rule of its own rather than a silent clean-up on the way out: an ICCID
    # is what someone uses to chase a SIM with the operator, and a mangled one sends them to the
    # wrong place. It also made .xlsx downloads fail outright, since a spreadsheet may not
    # contain these characters at all.
    #
    # GLOB rather than a regex: it is built into SQLite, needs no extension, and '*[...]*'
    # matches a character class. The class is every character a spreadsheet forbids, not just the
    # backspace actually seen, so a different stray byte next month is caught too.
    for column, label in (("iccid", "ICCID"), ("configuration", "CONFIGURATION"),
                          ("vin", "VIN"), ("device_name", "Device Name")):
        match = (f"{column} GLOB '*[' || char("
                 f"{','.join(str(c) for c in [*range(1, 9), 11, 12, *range(14, 32)])}"
                 f") || ']*'")
        n = _scalar(conn, f"""
            SELECT COUNT(*) FROM device_state
            WHERE snapshot_id = ? AND {column} IS NOT NULL AND {match}
        """, sid)
        if not n:
            continue
        findings.append((
            f"control_characters_in_{column}", "high", n,
            _sample(conn, f"""
                SELECT imei FROM device_state WHERE snapshot_id = ? AND {match}
            """, sid),
            f"{label} contains control characters, so the value is corrupt at source. The "
            f"stored value is kept exactly as received; downloads strip them so the file stays "
            f"readable.",
        ))

    # Model spelling variants: raw differs from canonical.
    n = _scalar(conn, """
        SELECT COUNT(*) FROM device_state
        WHERE snapshot_id = ? AND device_model_raw IS NOT NULL
          AND device_model_raw <> device_model
    """, sid)
    if n:
        findings.append((
            "model_spelling_variant", "medium", n,
            _sample(conn, """
                SELECT DISTINCT device_model_raw || ' -> ' || device_model FROM device_state
                WHERE snapshot_id = ? AND device_model_raw <> device_model
            """, sid),
            "Device model spelled inconsistently in the source; normalized for analysis.",
        ))

    # Firmware V-prefix inconsistency.
    n = _scalar(conn, """
        SELECT COUNT(*) FROM device_state
        WHERE snapshot_id = ? AND firmware_raw IS NOT NULL AND firmware_raw <> firmware
    """, sid)
    if n:
        findings.append((
            "firmware_prefix_variant", "medium", n,
            _sample(conn, """
                SELECT DISTINCT firmware_raw || ' -> ' || firmware FROM device_state
                WHERE snapshot_id = ? AND firmware_raw <> firmware
            """, sid),
            "Firmware version carried a 'V' prefix on some devices only; stripped for analysis.",
        ))

    # Placeholder VIN.
    n = _scalar(conn, """
        SELECT COUNT(*) FROM device_state
        WHERE snapshot_id = ? AND vin IS NULL AND vin_raw IS NOT NULL
    """, sid)
    if n:
        findings.append((
            "placeholder_vin", "medium", n, [],
            "VIN held a shared placeholder value rather than a real vehicle identifier.",
        ))

    # Firmware that cannot be ordered — blocks upgrade/downgrade classification.
    n = _scalar(conn, """
        SELECT COUNT(*) FROM device_state
        WHERE snapshot_id = ? AND firmware IS NOT NULL AND fw_sortkey IS NULL
    """, sid)
    if n:
        findings.append((
            "unsortable_firmware", "medium", n,
            _sample(conn, "SELECT DISTINCT firmware FROM device_state "
                          "WHERE snapshot_id = ? AND firmware IS NOT NULL AND fw_sortkey IS NULL", sid),
            "Firmware string could not be parsed into a comparable version.",
        ))

    # STATUS is a 24h recency bucket over SEEN AT, so the two must agree. Disagreement means
    # the export's status and timestamp columns were computed at different moments, which would
    # quietly skew every reachability number.
    n = _scalar(conn, f"""
        SELECT COUNT(*) FROM device_state
        WHERE snapshot_id = ? AND seen_age_hours IS NOT NULL AND status IS NOT NULL AND (
              (status = 'Online'  AND seen_age_hours >  {config.ONLINE_THRESHOLD_HOURS})
           OR (status = 'Offline' AND seen_age_hours <= {config.ONLINE_THRESHOLD_HOURS})
           OR (status = 'Inactive'))
    """, sid)
    if n:
        findings.append((
            "status_seen_at_mismatch", "medium", n,
            _sample(conn, f"""
                SELECT imei || ' ' || status || ' age=' || ROUND(seen_age_hours, 1) || 'h'
                FROM device_state
                WHERE snapshot_id = ? AND seen_age_hours IS NOT NULL AND status IS NOT NULL AND (
                      (status = 'Online'  AND seen_age_hours >  {config.ONLINE_THRESHOLD_HOURS})
                   OR (status = 'Offline' AND seen_age_hours <= {config.ONLINE_THRESHOLD_HOURS})
                   OR (status = 'Inactive'))
            """, sid),
            f"STATUS disagrees with SEEN AT age against the {config.ONLINE_THRESHOLD_HOURS}h "
            "online threshold (or an Inactive device has a last-seen time despite never pinging).",
        ))

    # Missing values per column. '-' in the source is already NULL by this point.
    missing_rules = [
        ("device_model", "high", "Device model missing — excluded from per-model analysis."),
        ("firmware", "high", "Firmware missing — the core analytical dimension."),
        ("seen_at", "info", "No last-seen timestamp; counted as never-seen, not as stale."),
        ("configuration", "low", "Configuration version missing."),
        ("iccid", "low", "SIM identifier missing."),
        ("first_ping", "low", "Commissioning date missing; device age unavailable."),
        ("hw_ver", "low", "Hardware revision missing; excluded from hardware segmentation."),
        ("groups_raw", "info", "Device belongs to no group/cohort."),
    ]
    for column, severity, detail in missing_rules:
        n = _scalar(conn, f"SELECT COUNT(*) FROM device_state "
                          f"WHERE snapshot_id = ? AND {column} IS NULL", sid)
        if n:
            findings.append((f"missing_{column}", severity, n, [], detail))

    # Single-valued columns carry no analytical signal — worth flagging so nobody builds a
    # segmentation on them.
    for column in ("created_by",):
        row = conn.execute(f"""
            SELECT {column}, COUNT(*) c FROM device_state
            WHERE snapshot_id = ? AND {column} IS NOT NULL
            GROUP BY {column} ORDER BY c DESC LIMIT 1
        """, sid).fetchone()
        total = _scalar(conn, "SELECT COUNT(*) FROM device_state WHERE snapshot_id = ?", sid)
        if row and total and row["c"] / total > 0.95:
            findings.append((
                f"low_cardinality_{column}", "info", row["c"], [str(row[0])],
                f"{row[0]!r} accounts for {row['c'] / total:.1%} of devices; not useful for segmentation.",
            ))

    conn.executemany(
        "INSERT OR REPLACE INTO quality_issue "
        "(snapshot_id, rule, severity, affected, sample, detail) VALUES (?,?,?,?,?,?)",
        [(snapshot_id, rule, sev, cnt, json.dumps(sample), detail)
         for rule, sev, cnt, sample, detail in findings],
    )
    return findings
