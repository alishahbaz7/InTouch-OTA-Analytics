"""The Intouch COTA Devices list — the user's design (10-10-2026), for handing the utility over.

Every device by its cloud id, with its IMEI, its Web FOTA model, firmware and last ping, its
group, what its last job came to, and its tags — filtered and sorted on any column, server-side,
because the list is 23,000 devices and growing. And the devices themselves managed here: added
by id (one, a pasted list, or a file), tagged, put in groups and moved between them, removed.

**A device may be in several groups — a warning, never a restriction** (the user, 10-10-2026,
replacing the one-group rule of the same morning). Putting devices in a group says which are
already in another one; they join as well unless left out, and "take them out of their other
groups" makes it a move.

**What a device's last job came to** is read live from today's jobs and, once the day's record is
cleared, from the copy `cota_campaign.keep_last_jobs` leaves on the device's row.
"""

from __future__ import annotations

import csv
import io
import re
from datetime import datetime, timedelta

from . import cota, cota_campaign, cota_library

PAGE_SIZES = (25, 50, 100, 200)
DEFAULT_PAGE_SIZE = 50

# column key -> the fleet column it sorts on. Every header sorts (the user's spec).
SORTS = {"id": "device_id", "imei": "imei", "model": "model", "group": "group_name",
         "job": "job_name", "status": "job_status", "fw": "firmware", "seen": "seen_at",
         "tag": "tags", "added": "added_at"}
FILTERS = ("id", "imei", "model", "group", "job", "status", "fw", "seen", "tag")
TEXT_FILTERS = ("id", "imei")             # typed: part of a number, or a pasted list of them
# Picked from a list, any number at once — Excel's filter: all, some, inverted, one (10-10-2026).
MULTI_FILTERS = ("model", "group", "job", "status", "fw", "seen", "tag")
NONE = "(none)"                     # the filter value for "this column is empty"

SEEN_BUCKETS = (("hour", "Within an hour"), ("day", "Within 24 h"), ("week", "1–7 days"),
                ("older", "Over 7 days"), ("never", "Never pinged"), ("none", "Not in Web FOTA"))

TEMPLATE_CSV = "id,trackingCode,tags\n14906,865510083360422,\"Delhi, pilot\"\n786,,Delhi\n"
_ID_HEADERS = {"id", "deviceid", "device"}
_TRACKING_HEADERS = {"trackingcode", "trackercode", "tracking", "imei", "deviceuniqueno"}
_TAG_HEADERS = {"tag", "tags", "label", "labels"}


class FleetError(Exception):
    pass


# ── the list ───────────────────────────────────────────────────────────────────────────────

def _fleet_cte(now: datetime) -> tuple[str, dict]:
    """`fleet`: one row per listed device with every column the page shows, in SQL — so any
    column filters and sorts without a pass in Python over 23,000 rows."""
    live = cota_campaign.LAST_JOB_SQL.format(where="1")
    stamp = lambda delta: (now - delta).strftime(cota.TIME_FORMAT)
    sql = f"""
        WITH live AS ({live}),
        fleet AS (
            SELECT d.device_id, d.imei, d.tags, d.device_type, d.added_at, d.source,
                   f.device_model AS model, f.firmware, f.seen_at, f.status AS fota_status,
                   (SELECT group_concat(g.name, ', ') FROM cota_group_member m
                      JOIN cota_group g ON g.id = m.group_id WHERE m.device_id = d.device_id) AS group_name,
                   (SELECT COUNT(*) FROM cota_group_member m WHERE m.device_id = d.device_id) AS group_count,
                   COALESCE(l.job_name, d.last_job_name) AS job_name,
                   COALESCE(l.job_status, d.last_job_status) AS job_status,
                   COALESCE(l.job_at, d.last_job_at) AS job_at,
                   CASE WHEN f.imei IS NULL THEN 'none' WHEN f.seen_at IS NULL THEN 'never'
                        WHEN f.seen_at >= :hour THEN 'hour' WHEN f.seen_at >= :day THEN 'day'
                        WHEN f.seen_at >= :week THEN 'week' ELSE 'older' END AS seen_bucket
            FROM cota_device d
            LEFT JOIN device f ON f.imei = d.imei
            LEFT JOIN live l ON l.device_id = d.device_id
        )
    """
    return sql, {"hour": stamp(timedelta(hours=1)), "day": stamp(timedelta(hours=24)),
                 "week": stamp(timedelta(days=7))}


def _getlist(raw, key: str) -> list[str]:
    """Every value of `key`: from request query params (repeated keys), a parse_qs dict of lists,
    or a plain dict of strings."""
    if hasattr(raw, "getlist"):
        return list(raw.getlist(key))
    value = raw.get(key)
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value]
    return [str(value)] if value not in (None, "") else []


def clean_filters(raw) -> dict:
    """{text filter: str, list filter: [values]} — only the filters actually set."""
    out: dict = {}
    for key in TEXT_FILTERS:
        value = " ".join(v.strip() for v in _getlist(raw, key) if v.strip())
        if value:
            out[key] = value
    for key in MULTI_FILTERS:
        values = list(dict.fromkeys(v.strip() for v in _getlist(raw, key) if v.strip()))
        if values:
            out[key] = values
    return out


def _where(filters: dict, params: dict) -> str:
    """The filters as SQL over `fleet`: AND across columns, OR across one column's values. Each
    value is a bound parameter, never spliced in."""
    clauses = []

    def bind(key: str, values: list) -> str:
        names = []
        for i, value in enumerate(values):
            params[f"{key}{i}"] = value
            names.append(f":{key}{i}")
        return ", ".join(names)

    def either(parts: list[str]) -> None:
        if parts:
            clauses.append("(" + " OR ".join(parts) + ")")

    def picked(column: str, key: str, values: list) -> None:
        real = [v for v in values if v != NONE]
        either(([f"{column} IN ({bind(key, real)})"] if real else [])
               + ([f"{column} IS NULL"] if NONE in values else []))

    for key, value in filters.items():
        if key in ("id", "imei"):
            tokens = [t for t in re.split(r"[\s,;]+", value) if t]
            digits = [re.sub(r"\D", "", t) for t in tokens]
            digits = [d for d in digits if d]
            column = "CAST(device_id AS TEXT)" if key == "id" else "imei"
            if len(digits) > 1:                       # a pasted list: exactly these
                clauses.append(f"{column} IN ({bind(key, digits)})")
            elif digits:                              # part of one number
                params[key] = digits[0]
                clauses.append(f"{column} LIKE '%' || :{key} || '%'")
        elif key == "model":
            picked("model", key, value)
        elif key == "fw":
            picked("firmware", key, value)
        elif key == "job":
            picked("job_name", key, value)
        elif key == "status":
            picked("job_status", key, value)
        elif key == "seen":
            either([f"seen_bucket IN ({bind(key, value)})"])
        elif key == "group":
            ids = [int(v) for v in value if v.isdigit()]
            either((["group_count = 0"] if "none" in value else [])
                   + (["group_count > 1"] if "several" in value else [])
                   + ([f"EXISTS (SELECT 1 FROM cota_group_member m WHERE m.device_id = fleet.device_id "
                       f"AND m.group_id IN ({bind(key, ids)}))"] if ids else []))
        elif key == "tag":
            real = [v.lower() for v in value if v != NONE]
            either((["tags = ''"] if NONE in value else [])
                   + [f"(',' || REPLACE(LOWER(tags), ', ', ',') || ',') LIKE '%,' || :{key}{i} || ',%'"
                      for i in range(len(real))])
            for i, tag in enumerate(real):
                params[f"{key}{i}"] = tag
    return ("WHERE " + " AND ".join(clauses)) if clauses else ""


def list_devices(conn, filters: dict, *, sort: str = "id", direction: str = "asc", page: int = 1,
                 size: int = DEFAULT_PAGE_SIZE, now: datetime | None = None) -> dict:
    """One page of the list as the filters and sort leave it, with the total that match."""
    now = now or datetime.now()
    size = size if size in PAGE_SIZES else DEFAULT_PAGE_SIZE
    cte, params = _fleet_cte(now)
    where = _where(filters, params)
    total = conn.execute(f"{cte} SELECT COUNT(*) FROM fleet {where}", params).fetchone()[0]
    pages = max(1, -(-total // size))
    page = min(max(1, page), pages)
    column = SORTS.get(sort, "device_id")
    order = "DESC" if direction == "desc" else "ASC"
    params.update(limit=size, offset=(page - 1) * size)
    rows = [dict(r) for r in conn.execute(
        f"{cte} SELECT * FROM fleet {where} "
        f"ORDER BY {column} IS NULL, {column} {order}, device_id LIMIT :limit OFFSET :offset", params)]
    for r in rows:
        r["tag_list"] = [t for t in (x.strip() for x in (r["tags"] or "").split(",")) if t]
    return {"rows": rows, "total": total, "page": page, "pages": pages, "size": size,
            "first": (page - 1) * size}


def matching_ids(conn, filters: dict, now: datetime | None = None) -> list[int]:
    """Every device the filters match, in id order — for an action on "all matching"."""
    cte, params = _fleet_cte(now or datetime.now())
    where = _where(filters, params)
    return [r[0] for r in conn.execute(f"{cte} SELECT device_id FROM fleet {where} ORDER BY device_id",
                                       params)]


def export_rows(conn, filters: dict, *, sort: str = "id", direction: str = "asc",
                now: datetime | None = None) -> list[dict]:
    """Every matching device, as the page shows it, for a download."""
    cte, params = _fleet_cte(now or datetime.now())
    where = _where(filters, params)
    column = SORTS.get(sort, "device_id")
    order = "DESC" if direction == "desc" else "ASC"
    return [dict(r) for r in conn.execute(
        f"{cte} SELECT * FROM fleet {where} ORDER BY {column} IS NULL, {column} {order}, device_id",
        params)]


EXPORT_COLUMNS = [("device_id", "Device ID"), ("imei", "IMEI"), ("model", "Device type"),
                  ("group_name", "Group"), ("job_name", "Last job"), ("job_status", "Last job status"),
                  ("job_at", "Last job started"), ("firmware", "FW version"),
                  ("seen_at", "FOTA last seen"), ("tags", "Tags")]


def options(conn, now: datetime | None = None) -> dict:
    """What each column's filter can be set to, from the whole list — not the filtered part, or
    choosing one filter would empty the others' choices."""
    from collections import Counter

    cte, params = _fleet_cte(now or datetime.now())
    counts = {k: Counter() for k in ("model", "fw", "job", "status", "seen", "tag")}
    tag_names: dict[str, str] = {}
    ungrouped = 0
    for r in conn.execute(f"{cte} SELECT model, firmware, job_name, job_status, seen_bucket, tags, "
                          "group_count FROM fleet", params):
        for key, value in (("model", r[0]), ("fw", r[1]), ("job", r[2]), ("status", r[3])):
            counts[key][value or NONE] += 1
        counts["seen"][r[4]] += 1
        listed = [t.strip() for t in (r[5] or "").split(",") if t.strip()]
        for t in listed:
            tag_names.setdefault(t.lower(), t)
            counts["tag"][t.lower()] += 1
        if not listed:
            counts["tag"][NONE] += 1
        ungrouped += r[6] == 0
    order = {s: i for i, s in enumerate(cota_campaign.JOB_STATUSES)}
    groups = cota_campaign.groups(conn)

    def column(key: str, empty_label: str, sort=str.lower) -> list[tuple]:
        """(value, label, devices) — every value in the whole list, the empty case first."""
        values = sorted((v for v in counts[key] if v != NONE), key=sort)
        rows = [(NONE, empty_label, counts[key][NONE])] if counts[key][NONE] else []
        return rows + [(v, v, counts[key][v]) for v in values]

    tags_sorted = sorted(tag_names, key=lambda t: tag_names[t].lower())
    # Each column's filter: [(value, label, devices)] — what the checkbox list offers.
    choices = {
        "model": column("model", "Not in Web FOTA"),
        # Each group by its own name only — never a merged "A + B" or "In several groups" (the
        # user, 10-10-2026). A device in two groups matches either name.
        "group": [("none", "No group", ungrouped)]
                 + [(str(g["id"]), g["name"], g["devices"]) for g in groups],
        "job": column("job", "No job yet"),
        "status": column("status", "No job yet", sort=lambda s: order.get(s, 99)),
        "fw": column("fw", "Unknown"),
        "seen": [(k, label, counts["seen"][k]) for k, label in SEEN_BUCKETS],
        "tag": ([(NONE, "No tag", counts["tag"][NONE])] if counts["tag"][NONE] else [])
               + [(tag_names[t], tag_names[t], counts["tag"][t]) for t in tags_sorted],
    }
    return {"choices": choices, "groups": groups, "tags": [tag_names[t] for t in tags_sorted],
            "all": conn.execute("SELECT COUNT(*) FROM cota_device").fetchone()[0]}


def overlapping(conn) -> int:
    """How many devices are in more than one group — put there before the one-group rule."""
    return conn.execute("SELECT COUNT(*) FROM (SELECT device_id FROM cota_group_member "
                        "GROUP BY device_id HAVING COUNT(*) > 1)").fetchone()[0]


# ── adding devices ─────────────────────────────────────────────────────────────────────────

def _merge_tags(current: str, extra: str) -> str:
    return cota_library.clean_tags(f"{current or ''},{extra or ''}")


def add_devices(conn, text: str, tags: str = "") -> dict:
    """Devices by id — one or a pasted list — each with the tags given. A device already listed
    keeps everything it has and gains the tags."""
    ids, problems, duplicates = cota_campaign.parse_device_ids(text)
    if problems:
        raise FleetError("Not added — " + "; ".join(problems[:5]) + (" …" if len(problems) > 5 else ""))
    if not ids:
        raise FleetError("Enter at least one device ID.")
    return _put(conn, [(d, None, tags) for d in ids], source="added", duplicates=duplicates)


def _put(conn, rows: list[tuple[int, str | None, str]], *, source: str, duplicates: int = 0) -> dict:
    added = existing = 0
    with conn:
        for device_id, imei, tags in rows:
            if cota.put_device(conn, device_id, imei=imei or None, source=source):
                added += 1
            else:
                existing += 1
            if tags and tags.strip():
                current = conn.execute("SELECT tags FROM cota_device WHERE device_id = ?",
                                       (device_id,)).fetchone()[0]
                conn.execute("UPDATE cota_device SET tags = ? WHERE device_id = ?",
                             (_merge_tags(current, tags), device_id))
    return {"added": added, "existing": existing, "duplicates": duplicates}


def _flat(header: str) -> str:
    return re.sub(r"[\s_\-]", "", header.strip().lower())


def read_device_file(content: bytes) -> tuple[list[tuple[int, str | None, str]], list[str]]:
    """(device id, IMEI or None, tags) per row of an uploaded CSV, and its problems.

    The cloud's own device list (`id, trackingCode`) with an optional `tags` column. A header row
    is optional: without one the columns are id, IMEI, tags in that order."""
    text = content.decode("utf-8-sig", errors="replace")
    table = [r for r in csv.reader(io.StringIO(text)) if any(c.strip() for c in r)]
    if not table:
        return [], ["The file is empty."]
    head = [_flat(h) for h in table[0]]
    has_header = not table[0][0].strip().isdigit()
    if has_header:
        id_col = next((i for i, h in enumerate(head) if h in _ID_HEADERS), None)
        imei_col = next((i for i, h in enumerate(head) if h in _TRACKING_HEADERS), None)
        tag_col = next((i for i, h in enumerate(head) if h in _TAG_HEADERS), None)
        if id_col is None:
            return [], ["The file has no id column — start from the template."]
        body, first = table[1:], 2
    else:
        id_col, imei_col, tag_col = 0, 1 if len(table[0]) > 1 else None, 2 if len(table[0]) > 2 else None
        body, first = table, 1
    rows, problems, seen = [], [], set()
    for number, r in enumerate(body, start=first):
        cell = lambda col: r[col].strip() if col is not None and len(r) > col else ""
        device, imei, tags = cell(id_col), cell(imei_col), cell(tag_col)
        if not device.isdigit() or cota_campaign.looks_like_imei(device):
            problems.append(f"line {number}: '{device[:20]}' is not a device ID")
            continue
        if imei and (not imei.isdigit() or imei == device):
            problems.append(f"line {number}: '{imei[:20]}' is not an IMEI")
            imei = ""
        if int(device) in seen:
            continue
        seen.add(int(device))
        rows.append((int(device), imei or None, tags))
    return rows, problems


def import_devices(conn, content: bytes) -> dict:
    rows, problems = read_device_file(content)
    if not rows:
        raise FleetError("Not added — " + ("; ".join(problems[:5]) if problems else "no device IDs in the file."))
    result = _put(conn, rows, source="device list")
    result["problems"] = problems
    return result


# ── tags ───────────────────────────────────────────────────────────────────────────────────

def _chunks(ids: list[int], size: int = 500):
    for i in range(0, len(ids), size):
        yield ids[i:i + size]


def _tags_of(conn, ids: list[int]) -> dict[int, str]:
    out = {}
    for part in _chunks(ids):
        out.update({r[0]: r[1] for r in conn.execute(
            f"SELECT device_id, tags FROM cota_device WHERE device_id IN ({','.join('?' * len(part))})", part)})
    return out


def add_tags(conn, ids: list[int], text: str) -> int:
    if not cota_library.clean_tags(text):
        raise FleetError("Enter a tag.")
    with conn:
        conn.executemany("UPDATE cota_device SET tags = ? WHERE device_id = ?",
                         [(_merge_tags(t, text), d) for d, t in _tags_of(conn, ids).items()])
    return len(ids)


def remove_tags(conn, ids: list[int], text: str) -> int:
    gone = {t.lower() for t in cota_library.clean_tags(text).split(", ") if t}
    if not gone:
        raise FleetError("Enter a tag.")
    with conn:
        conn.executemany("UPDATE cota_device SET tags = ? WHERE device_id = ?", [
            (", ".join(t for t in cota_library.clean_tags(tags).split(", ") if t and t.lower() not in gone), d)
            for d, tags in _tags_of(conn, ids).items()])
    return len(ids)


def tag_devices(conn, tag: str) -> list[int]:
    """Every device carrying a tag, in id order — a group can be made of them."""
    return [r[0] for r in conn.execute(
        "SELECT device_id FROM cota_device WHERE (',' || REPLACE(LOWER(tags), ', ', ',') || ',') "
        "LIKE '%,' || ? || ',%' ORDER BY device_id", (tag.strip().lower(),))]


def all_tags(conn) -> list[str]:
    tags: dict[str, str] = {}
    for (text,) in conn.execute("SELECT tags FROM cota_device WHERE tags <> ''"):
        for t in (x.strip() for x in text.split(",")):
            if t:
                tags.setdefault(t.lower(), t)
    return sorted(tags.values(), key=str.lower)


# ── groups: overlap allowed, said out loud ───────────────────────────────────────────────────

def _memberships(conn, ids: list[int]) -> dict[int, list[tuple[int, str]]]:
    """Each device's groups as (id, name) — none, one or several."""
    out: dict[int, list[tuple[int, str]]] = {}
    for part in _chunks(ids):
        for r in conn.execute(
                f"SELECT m.device_id, g.id, g.name FROM cota_group_member m JOIN cota_group g "
                f"ON g.id = m.group_id WHERE m.device_id IN ({','.join('?' * len(part))})", part):
            out.setdefault(r[0], []).append((r[1], r[2]))
    return out


def check_group(conn, ids: list[int], group_id: int | None = None) -> dict:
    """What putting these devices in a group would do: which are new to the list, which are in
    no group, which are already in it, and which are in another group — the warning the dialog
    shows, with the list to leave some out (the user, 10-10-2026)."""
    listed = set()
    for part in _chunks(ids):
        listed.update(r[0] for r in conn.execute(
            f"SELECT device_id FROM cota_device WHERE device_id IN ({','.join('?' * len(part))})", part))
    member = _memberships(conn, ids)
    imeis = {}
    for part in _chunks(ids):
        imeis.update({r[0]: r[1] for r in conn.execute(
            f"SELECT device_id, imei FROM cota_device WHERE device_id IN ({','.join('?' * len(part))})", part)})
    elsewhere: dict[int, dict] = {}
    here = free = 0
    for d in ids:
        groups = member.get(d, [])
        others = [g for g in groups if g[0] != group_id]
        if group_id is not None and any(g[0] == group_id for g in groups) and not others:
            here += 1
        elif not others:
            free += 1
        else:
            for gid, name in others:
                elsewhere.setdefault(gid, {"group_id": gid, "name": name, "devices": []})
            first = others[0]
            elsewhere[first[0]]["devices"].append(
                {"device_id": d, "imei": imeis.get(d) or "", "groups": [n for _, n in others]})
    groups = sorted(elsewhere.values(), key=lambda g: (-len(g["devices"]), g["name"].lower()))
    for g in groups:
        g["count"] = len(g["devices"])
    return {"total": len(ids), "new": sum(1 for d in ids if d not in listed), "free": free,
            "here": here, "elsewhere": groups, "elsewhere_total": sum(g["count"] for g in groups)}


def _join(conn, group_id: int, ids: list[int], transfer: bool = False) -> int:
    """Put devices in a group — also out of every other one when `transfer`. The caller holds
    the transaction."""
    now = cota._now()
    conn.executemany("INSERT OR IGNORE INTO cota_device (device_id, device_type, source, added_at, "
                     "updated_at) VALUES (?, ?, 'group', ?, ?)",
                     [(d, cota.DEFAULT_DEVICE_TYPE, now, now) for d in ids])
    if transfer:
        conn.executemany("DELETE FROM cota_group_member WHERE device_id = ? AND group_id <> ?",
                         [(d, group_id) for d in ids])
    seq = conn.execute("SELECT COALESCE(MAX(seq), -1) + 1 FROM cota_group_member WHERE group_id = ?",
                       (group_id,)).fetchone()[0]
    before = conn.total_changes
    conn.executemany("INSERT OR IGNORE INTO cota_group_member (group_id, device_id, seq) VALUES (?, ?, ?)",
                     [(group_id, d, seq + i) for i, d in enumerate(ids)])
    return conn.total_changes - before


def create_group(conn, name: str, ids: list[int], skip: set[int] | None = None,
                 transfer: bool = False) -> dict:
    """A new group of `ids`, less any a person left out (`skip`). Devices already in another group
    join as well and keep it — unless `transfer`, which takes them out of it. Refuses a name already
    used: adding to an existing group is Add to group."""
    name = (name or "").strip()
    if not name:
        raise FleetError("Give the group a name.")
    if conn.execute("SELECT 1 FROM cota_group WHERE name = ? COLLATE NOCASE", (name,)).fetchone():
        raise FleetError(f"A group is already called “{name}”. To add devices to it, select them "
                         "and use Add to group.")
    if not ids:
        raise FleetError("The group has no devices.")
    joining = [d for d in ids if d not in (skip or set())]
    if not joining:
        raise FleetError("Every device was left out.")
    elsewhere = sum(1 for groups in _memberships(conn, joining).values() if groups)
    with conn:
        group_id = conn.execute("INSERT INTO cota_group (name, created_at) VALUES (?, ?)",
                                (name, cota._now())).lastrowid
        _join(conn, group_id, joining, transfer)
    return {"group_id": group_id, "name": name, "joined": len(joining),
            "left": len(ids) - len(joining), "elsewhere": elsewhere, "transfer": transfer}


def add_to_group(conn, group_id: int, ids: list[int], transfer: bool = False) -> dict:
    """Devices into an existing group; with `transfer`, out of their other groups too."""
    name = conn.execute("SELECT name FROM cota_group WHERE id = ?", (group_id,)).fetchone()
    if not name:
        raise FleetError("That group no longer exists.")
    elsewhere = sum(1 for d, groups in _memberships(conn, ids).items()
                    if any(g[0] != group_id for g in groups))
    with conn:
        _join(conn, group_id, ids, transfer)
    return {"name": name[0], "devices": len(ids), "elsewhere": elsewhere, "transfer": transfer}


def move_to_group(conn, group_id: int, ids: list[int]) -> dict:
    """Add to group, leaving every other group — a transfer."""
    return add_to_group(conn, group_id, ids, transfer=True)


def remove_from_groups(conn, ids: list[int], group_id: int | None = None) -> int:
    """Take devices out of one group — the one in view — or, with none given, out of all."""
    with conn:
        before = conn.total_changes
        if group_id is None:
            conn.executemany("DELETE FROM cota_group_member WHERE device_id = ?", [(d,) for d in ids])
        else:
            conn.executemany("DELETE FROM cota_group_member WHERE device_id = ? AND group_id = ?",
                             [(d, group_id) for d in ids])
        return conn.total_changes - before


def rename_group(conn, group_id: int, name: str) -> str:
    name = (name or "").strip()
    if not name:
        raise FleetError("Give the group a name.")
    taken = conn.execute("SELECT 1 FROM cota_group WHERE name = ? COLLATE NOCASE AND id <> ?",
                         (name, group_id)).fetchone()
    if taken:
        raise FleetError(f"A group is already called “{name}”.")
    with conn:
        conn.execute("UPDATE cota_group SET name = ? WHERE id = ?", (name, group_id))
    return name


def delete_devices(conn, ids: list[int]) -> int:
    """Take devices off the list and out of their groups. Today's job record keeps them."""
    with conn:
        conn.executemany("DELETE FROM cota_group_member WHERE device_id = ?", [(d,) for d in ids])
        before = conn.total_changes
        conn.executemany("DELETE FROM cota_device WHERE device_id = ?", [(d,) for d in ids])
        return conn.total_changes - before
