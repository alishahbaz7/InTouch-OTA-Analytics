"""Web FOTA's Devices list: every device of a snapshot, filtered and sorted on any column.

The Intouch COTA Devices pattern (`cota_fleet`) on the platform's data, so the two modules' lists
look and behave alike (the user, 10-10-2026: one UI through the application). Every column with a
known set of values filters as in Excel — each value with its device count, any number ticked —
the IMEI box takes part of one number or a pasted list, and every header sorts. All of it is SQL
over the resolved snapshot, never Python over rows.

The page and its export share `rows()`: an export that returned a different set from the table
above it would send someone to act on the wrong devices.
"""
from __future__ import annotations

import re
import sqlite3

from . import metrics, normalize

# A value the platform did not send. One sentinel for every list filter; each column names the
# empty case in its own words.
UNKNOWN = "(unknown)"

TEXT_FILTERS = ("q",)                     # the IMEI
MULTI_FILTERS = ("model", "firmware", "changed", "target", "config", "hw", "status",
                 "queue_state", "seen")
SINGLE_FILTERS = ("group", "fallback")

# Sortable columns, whitelisted so a query param can never reach the ORDER BY as raw SQL.
SORTABLE = {
    "imei": "d.imei",
    "model": "d.device_model",
    "firmware": "d.firmware",
    "changed": "r.last_fw_change_at",
    "target": "d.update_firmware",
    "config": "d.configuration",
    "hw": "d.hw_ver",
    "status": "d.status",
    "task": "d.queue_state",
    "seen": "d.seen_at",
    "checked": "r.last_checked_at",
}
# Most recently seen first: the devices that just reported are the ones worth looking at.
DEFAULT_SORT, DEFAULT_DIR = "seen", "desc"

PAGE_SIZES = (25, 50, 100, 200)
DEFAULT_PAGE_SIZE = 100

NONE_LABELS = {"model": "(unknown)", "firmware": "(unknown)", "target": "No target",
               "config": "(unknown)", "hw": "(unknown)"}
COLUMNS = {"model": "d.device_model", "firmware": "d.firmware", "target": "d.update_firmware",
           "config": "d.configuration", "hw": "d.hw_ver"}
TASK_STATES = (("pending", "Task pending"), ("completed", "Task completed"),
               ("never_tasked", "No pending task"))
TASK_LABELS = dict(TASK_STATES)
# Disjoint, so several can be ticked together and each device sits in exactly one.
CHANGE_BUCKETS = (("hour", "Last hour"), ("day", "1–24 hours"), ("week", "1–7 days"),
                  ("month", "7–30 days"), ("older", "Over 30 days"), ("never", "Never changed"))
# Relative to the snapshot, like every other age on these pages: its time is the data's "now".
SEEN_BUCKETS = (("hour", "Within an hour"), ("day", "Within 24 h"), ("week", "1–7 days"),
                ("month", "7–30 days"), ("older", "Over 30 days"), ("never", "Never pinged"))
FALLBACKS = (("yes", "Fallback"), ("missed", "Missed target"))

_CHANGED = """CASE WHEN r.last_changed_at IS NULL THEN 'never'
    WHEN r.last_changed_at >= datetime('now', 'localtime', '-1 hours') THEN 'hour'
    WHEN r.last_changed_at >= datetime('now', 'localtime', '-24 hours') THEN 'day'
    WHEN r.last_changed_at >= datetime('now', 'localtime', '-168 hours') THEN 'week'
    WHEN r.last_changed_at >= datetime('now', 'localtime', '-720 hours') THEN 'month'
    ELSE 'older' END"""
_SEEN = """CASE WHEN d.seen_at IS NULL THEN 'never'
    WHEN d.seen_age_hours < 1 THEN 'hour' WHEN d.seen_age_hours < 24 THEN 'day'
    WHEN d.seen_age_hours < 168 THEN 'week' WHEN d.seen_age_hours < 720 THEN 'month'
    ELSE 'older' END"""
# The "last update" windows older links still carry (?changed=24h): cumulative, not buckets.
_LEGACY_CHANGED = {"1h": 1, "24h": 24, "7d": 168, "30d": 720}


def _getlist(raw, key: str) -> list[str]:
    if hasattr(raw, "getlist"):
        return [str(v) for v in raw.getlist(key)]
    value = raw.get(key) if raw else None
    if value is None:
        return []
    return [str(v) for v in value] if isinstance(value, (list, tuple)) else [str(value)]


def clean_filters(raw) -> dict:
    """{text filter: str, list filter: [values], single filter: str} — only the ones set."""
    out: dict = {}
    for key in TEXT_FILTERS:
        value = " ".join(v.strip() for v in _getlist(raw, key) if v.strip())
        if value:
            out[key] = value
    for key in MULTI_FILTERS:
        values = list(dict.fromkeys(v.strip() for v in _getlist(raw, key) if v.strip()))
        if values:
            out[key] = values
    for key in SINGLE_FILTERS:
        values = [v.strip() for v in _getlist(raw, key) if v.strip()]
        if values:
            out[key] = values[0]
    if out.get("fallback") not in dict(FALLBACKS):
        out.pop("fallback", None)
    return out


def order_by(sort: str, direction: str) -> str:
    column = SORTABLE.get(sort, SORTABLE[DEFAULT_SORT])
    descending = direction != "asc"
    # Missing values sort last either way — a device that has never reported should not head
    # the list just because its timestamp is NULL.
    return f"{column} IS NULL, {column} {'DESC' if descending else 'ASC'}, d.imei"


def _where(snapshot_id: int, filters: dict) -> tuple[str, list]:
    """The filters as SQL: AND across columns, OR across one column's values, every value bound."""
    clauses = ["d.snapshot_id = ?"]
    params: list = [snapshot_id]

    def among(column: str, values: list) -> None:
        real = [v for v in values if v != UNKNOWN]
        parts = ([f"{column} IN ({','.join('?' * len(real))})"] if real else []) \
            + ([f"{column} IS NULL"] if UNKNOWN in values else [])
        if parts:
            clauses.append("(" + " OR ".join(parts) + ")")
            params.extend(real)

    for key, value in filters.items():
        if key == "q":
            # Digits only: the platform is pasted from lists that carry commas, quotes and stray
            # spaces. One number matches any part of an IMEI — the last few digits are what
            # people read off a label; a pasted list matches exactly those IMEIs.
            tokens = [re.sub(r"\D", "", t) for t in re.split(r"[\s,;]+", value)]
            tokens = [t for t in tokens if t]
            if len(tokens) > 1:
                clauses.append(f"d.imei IN ({','.join('?' * len(tokens))})")
                params.extend(tokens)
            elif tokens:
                clauses.append("d.imei LIKE ?")
                params.append(f"%{tokens[0]}%")
        elif key in COLUMNS:
            among(COLUMNS[key], value)
        elif key == "status":
            among("d.status", value)
        elif key == "queue_state":
            among("d.queue_state", value)
        elif key == "seen":
            known = [v for v in value if v in dict(SEEN_BUCKETS)]
            if known:
                clauses.append(f"({_SEEN}) IN ({','.join('?' * len(known))})")
                params.extend(known)
        elif key == "changed":
            parts = []
            buckets = [v for v in value if v in dict(CHANGE_BUCKETS)]
            if buckets:
                parts.append(f"({_CHANGED}) IN ({','.join('?' * len(buckets))})")
                params.extend(buckets)
            for legacy in value:
                if legacy in _LEGACY_CHANGED:
                    parts.append("r.last_changed_at >= datetime('now', 'localtime', "
                                 f"'-{_LEGACY_CHANGED[legacy]} hours')")
            if parts:
                clauses.append("(" + " OR ".join(parts) + ")")
        elif key == "group":
            clauses.append("g.group_name = ?")
            params.append(value)
        elif key == "fallback":
            rule = metrics.fallback_rule("d")
            clauses.append(f"({rule})" if value == "yes"
                           else f"({rule}) AND {metrics.missed_target_rule('d')}")
    return " AND ".join(clauses), params


def _from(filters: dict) -> str:
    join = (" JOIN device_group g ON g.snapshot_id = d.snapshot_id AND g.imei = d.imei"
            if filters.get("group") else "")
    return f"device_state d{join} LEFT JOIN device r ON r.imei = d.imei"


def rows(conn: sqlite3.Connection, snapshot_id: int, filters: dict, *, sort: str = DEFAULT_SORT,
         direction: str = DEFAULT_DIR, limit: int | None = None, offset: int = 0):
    """(rows, total) — the device list behind both the page and its export."""
    where, params = _where(snapshot_id, filters)
    # Both statements go through metrics.at, like every other per-snapshot read. Left pointing
    # at the view they cost a full fleet resolution each — which is why /devices once took 41s
    # on the 245-snapshot database while pages that used the resolved copy took under a second.
    total = conn.execute(metrics.at(
        conn, snapshot_id, f"SELECT COUNT(*) FROM {_from(filters)} WHERE {where}"),
        params).fetchone()[0]
    tag_sql = f"CASE WHEN {metrics.fallback_rule('d')} THEN 1 ELSE 0 END AS is_fallback"
    sql = metrics.at(conn, snapshot_id, f"""
        SELECT d.imei, d.device_model, d.firmware, d.hw_ver, d.status, d.queue_state, d.queue,
               d.seen_at, d.seen_age_hours, d.configuration, d.groups_raw, d.vin, d.iccid,
               d.update_firmware, d.base_firmware,
               {tag_sql},
               r.prev_firmware, r.last_changed_at, r.last_fw_change_at, r.last_checked_at,
               r.changes AS change_count
        FROM {_from(filters)} WHERE {where}
        ORDER BY {order_by(sort, direction)}
    """)
    if limit is not None:
        sql += " LIMIT ? OFFSET ?"
        params = [*params, limit, offset]
    return [dict(r) for r in conn.execute(sql, params)], total


def options(conn: sqlite3.Connection, snapshot_id: int) -> dict:
    """Every list filter's values, each with its device count over the whole snapshot — only
    values some device has (the user: "0 means it does not exist").

    {"choices": {key: [(value, label, count)]}, "all": devices in the snapshot}
    """
    def grouped(expr: str, join: str = "") -> dict:
        sql = metrics.at(conn, snapshot_id, f"""
            SELECT {expr} AS v, COUNT(*) AS n FROM device_state d {join}
            WHERE d.snapshot_id = ? GROUP BY v""")
        return {r["v"]: r["n"] for r in conn.execute(sql, (snapshot_id,))}

    choices: dict = {}
    for key, column in COLUMNS.items():
        counts = grouped(column)
        named = sorted(((v, n) for v, n in counts.items() if v is not None),
                       key=lambda vn: (-vn[1], str(vn[0])))
        choices[key] = [(str(v), str(v), n) for v, n in named]
        if counts.get(None):
            choices[key].append((UNKNOWN, NONE_LABELS[key], counts[None]))

    status = grouped("d.status")
    order = list(normalize.STATUS_LABELS) + sorted(str(s) for s in status if s
                                                   and s not in normalize.STATUS_LABELS)
    choices["status"] = [(s, normalize.STATUS_LABELS.get(s, s), status[s])
                         for s in order if status.get(s)]
    task = grouped("d.queue_state")
    choices["queue_state"] = [(k, label, task[k]) for k, label in TASK_STATES if task.get(k)]
    seen = grouped(_SEEN)
    choices["seen"] = [(k, label, seen[k]) for k, label in SEEN_BUCKETS if seen.get(k)]
    changed = grouped(_CHANGED, "LEFT JOIN device r ON r.imei = d.imei")
    choices["changed"] = [(k, label, changed[k]) for k, label in CHANGE_BUCKETS if changed.get(k)]
    return {"choices": choices, "all": sum(status.values())}


def describe(filters: dict) -> str:
    """A short slug for a downloaded file's name, so it says what it holds."""
    parts = []
    for key, value in filters.items():
        values = value if isinstance(value, list) else [value]
        parts.append("-".join(re.sub(r"[^\w.]+", "-", str(v)).strip("-") for v in values))
    return "_".join(p for p in parts if p)[:60]
