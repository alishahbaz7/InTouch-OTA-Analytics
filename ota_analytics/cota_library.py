"""The command library: names the user gives to parameters and to whole commands.

A command reads as `<op> D7 <parameter> [D5 values…]` and cota.describe_command already names it
from built-in parameter names — `GET FTP_SETTINGS`, `CLR SOS`, `SET 6B82`. This adds the user's
own names on top, in two kinds, each kept across days like groups and the device map:

  * **A parameter name** (`6C0A` → "Timers") names every GET, SET and CLR of that parameter,
    everywhere: Configure, Jobs, exports. It overrides a built-in name.
  * **A saved command** (`DBD76B82D531` → "Ignition timer 1 s", tagged) names that exact command
    and can be picked in Configure and Jobs instead of typed.

describe_command cannot reach the database, so the names are held in memory (cota.LIBRARY) and
refreshed from it on every COTA page and after every change here. A reader that missed a refresh
shows the built-in name for a moment — never a wrong one.
"""

from __future__ import annotations

import csv
import io
import re

from . import cota, cota_run

_CODE = re.compile(r"^[0-9A-F]{4}$")
_HEX = re.compile(r"^[0-9A-F]+$")


class LibraryError(Exception):
    pass


_loaded_from: str | None = None          # the database file the names in memory came from


def refresh(conn) -> None:
    """Load the names into memory for describe_command. Two small tables; cheap."""
    global _loaded_from
    parameters = {r["code"]: r["name"] for r in conn.execute("SELECT code, name FROM cota_parameter")}
    # Two saved names for one command: the first saved names it everywhere — newest read first,
    # so the oldest is the one left standing. Not whichever row the database returned last.
    commands = {r["val1"]: r["name"] for r in conn.execute(
        "SELECT val1, name FROM cota_saved_command ORDER BY id DESC")}
    cota.LIBRARY = {"parameters": parameters, "commands": commands}
    _loaded_from = _database_of(conn)


def ensure_loaded(conn) -> None:
    """Load once per database. Every change made here refreshes on its own, so after the first
    load the names in memory are current; a different database (a test's) loads its own."""
    if _database_of(conn) != _loaded_from:
        refresh(conn)


def _database_of(conn) -> str:
    return conn.execute("PRAGMA database_list").fetchone()[2] or ":memory:"


# ── parameters ──────────────────────────────────────────────────────────────────────────────

def parameters(conn, *, q: str = "") -> list[dict]:
    """The user's names and the built-ins they have not renamed, by code — each with how many
    saved commands use it, so a rename says what it will affect."""
    mine = {r["code"]: dict(r) for r in conn.execute(
        "SELECT code, name, updated_at FROM cota_parameter")}
    used: dict[str, int] = {}
    for (value,) in conn.execute("SELECT val1 FROM cota_saved_command"):
        if len(value) >= 8 and value[2:4] == "D7":
            used[value[4:8]] = used.get(value[4:8], 0) + 1
    rows = []
    for code in sorted(set(mine) | set(cota.COMMAND_PARAMETERS)):
        own = mine.get(code)
        rows.append({"code": code, "name": own["name"] if own else cota.COMMAND_PARAMETERS[code],
                     "builtin": cota.COMMAND_PARAMETERS.get(code), "own": bool(own),
                     "updated_at": own["updated_at"] if own else "", "used_by": used.get(code, 0)})
    if q.strip():
        needle = q.strip().lower()
        rows = [r for r in rows if needle in r["code"].lower() or needle in r["name"].lower()]
    return rows


def save_parameter(conn, code: str, name: str) -> str:
    code = (code or "").strip().upper()
    name = (name or "").strip()
    if not _CODE.match(code):
        raise LibraryError("A parameter code is four hex digits — e.g. 6C0A, the part after D7.")
    if not name:
        raise LibraryError("Give the parameter a name.")
    with conn:
        conn.execute("""
            INSERT INTO cota_parameter (code, name, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(code) DO UPDATE SET name = excluded.name, updated_at = excluded.updated_at
        """, (code, name, cota._now()))
    refresh(conn)
    return code


def delete_parameter(conn, code: str) -> None:
    """Removes the user's name; a built-in name, if there is one, applies again."""
    with conn:
        conn.execute("DELETE FROM cota_parameter WHERE code = ?", ((code or "").strip().upper(),))
    refresh(conn)


# ── saved commands ──────────────────────────────────────────────────────────────────────────

def clean_tags(text: str) -> str:
    seen, tags = set(), []
    for tag in re.split(r"[,;]", text or ""):
        tag = " ".join(tag.split())
        if tag and tag.lower() not in seen:
            seen.add(tag.lower())
            tags.append(tag)
    return ", ".join(tags)


def commands(conn, *, q: str = "", tag: str = "") -> list[dict]:
    rows = []
    for r in conn.execute("SELECT * FROM cota_saved_command ORDER BY name COLLATE NOCASE"):
        row = dict(r)
        row["tag_list"] = [t for t in (x.strip() for x in row["tags"].split(",")) if t]
        row["kind"] = cota_run.classify(row["val1"])[0]
        row["reads_as"] = _built_in_reading(row["val1"])
        rows.append(row)
    if tag:
        rows = [r for r in rows if tag.lower() in (t.lower() for t in r["tag_list"])]
    if q.strip():
        needle = q.strip().lower()
        rows = [r for r in rows if needle in r["name"].lower() or needle in r["val1"].lower()
                or needle in r["note"].lower() or needle in r["reads_as"].lower()
                or any(needle in t.lower() for t in r["tag_list"])]
    # Grouped by what they do — how a command is looked for — then by name.
    rows.sort(key=lambda r: (_KIND_ORDER.get(r["kind"], 9), r["name"].lower()))
    return rows


_KIND_ORDER = {"get": 0, "set": 1, "clear": 2, "unknown": 3}


def same_command(conn, val1: str, command_id: int | None = None) -> list[str]:
    """Other saved commands with exactly this value — said when saving, not refused: two names
    for one command can be on purpose."""
    value = "".join((val1 or "").split()).upper()
    return [r[0] for r in conn.execute(
        "SELECT name FROM cota_saved_command WHERE val1 = ? AND id IS NOT ? ORDER BY name",
        (value, command_id))]


def all_tags(conn) -> list[str]:
    tags: dict[str, str] = {}
    for (text,) in conn.execute("SELECT tags FROM cota_saved_command"):
        for t in (x.strip() for x in text.split(",")):
            if t:
                tags.setdefault(t.lower(), t)
    return sorted(tags.values(), key=str.lower)


def save_command(conn, *, name: str, val1: str, tags: str = "", note: str = "",
                 command_id: int | None = None) -> int:
    name = (name or "").strip()
    value = "".join((val1 or "").split()).upper()
    if not name:
        raise LibraryError("Give the command a name.")
    kind, problem = cota_run.classify(value)
    if problem:
        raise LibraryError(f"Not saved — {problem}.")
    if not _HEX.match(value):
        raise LibraryError("Not saved — the command must be hexadecimal.")
    now = cota._now()
    clash = conn.execute("SELECT id FROM cota_saved_command WHERE name = ? AND id IS NOT ?",
                         (name, command_id)).fetchone()
    if clash:
        raise LibraryError(f"A saved command is already called “{name}”.")
    with conn:
        if command_id:
            conn.execute("""
                UPDATE cota_saved_command SET name = ?, val1 = ?, tags = ?, note = ?, updated_at = ?
                WHERE id = ?
            """, (name, value, clean_tags(tags), (note or "").strip(), now, command_id))
            saved = command_id
        else:
            saved = conn.execute("""
                INSERT INTO cota_saved_command (name, val1, tags, note, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (name, value, clean_tags(tags), (note or "").strip(), now, now)).lastrowid
    refresh(conn)
    return saved


def delete_command(conn, command_id: int) -> None:
    with conn:
        conn.execute("DELETE FROM cota_saved_command WHERE id = ?", (command_id,))
    refresh(conn)


def _built_in_reading(val1: str) -> str:
    """What a command reads as from its parts alone — shown beside the name it was saved under."""
    value = val1.upper()
    if len(value) < 8 or value[2:4] != "D7" or value[:2] not in cota.COMMAND_OPERATIONS:
        return ""
    return f"{cota.COMMAND_OPERATIONS[value[:2]]} {cota.parameter_name(value[4:8])}"


def describe_lines(text: str) -> list[dict]:
    """Each line of a command box with its name and any problem — what the page shows as it is
    typed. Same parser as a job and a sequence, so the preview cannot disagree with them."""
    out = []
    for step in cota_run.parse_lines(text):
        out.append({"line": step["line"], "val1": step["val1"], "kind": step["kind"],
                    "problem": step["problem"] or "",
                    "name": "" if step["problem"] else (cota.describe_command(step["val1"]) or "")})
    return out


# ── sharing: one CSV for both kinds ───────────────────────────────────────────────────────────
# A file a colleague can open in Excel, and load into their own install. Import only adds and
# updates — it never deletes — so loading someone's file cannot wipe your own names.

CSV_COLUMNS = ["kind", "name", "command", "tags", "note"]
TEMPLATE_CSV = ("kind,name,command,tags,note\n"
                "parameter,TIMERS,6C0A,,\n"
                "command,Read FTP settings,DAD76F4B,read,What the device sends its files to\n"
                "command,Clear SOS,DDD76D66,alarm,\n")
_KINDS = {"parameter": "parameter", "param": "parameter", "command": "command", "cmd": "command"}


def export_rows(conn) -> list[dict]:
    """Your parameter names (built-ins only when you renamed them) and every saved command."""
    rows = [{"kind": "parameter", "name": r["name"], "command": r["code"], "tags": "", "note": ""}
            for r in conn.execute("SELECT code, name FROM cota_parameter ORDER BY code")]
    rows += [{"kind": "command", "name": c["name"], "command": c["val1"], "tags": c["tags"],
              "note": c["note"]} for c in commands(conn)]
    return rows


def export_csv(conn) -> str:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=CSV_COLUMNS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(export_rows(conn))
    return buffer.getvalue()


def plan_import(conn, content: bytes) -> dict:
    """Read a shared file and say what it would change, row by row — nothing is written. Each
    row: new, update (what differs), same, or a problem with its line number."""
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        return {"rows": [], "error": "Not a text file — export a CSV from Commands, or start "
                                     "from the template."}
    reader = csv.DictReader(io.StringIO(text))
    header = [h.strip().lower() for h in (reader.fieldnames or [])]
    missing = [c for c in ("kind", "name", "command") if c not in header]
    if missing:
        return {"rows": [], "error": "The first line must name the columns kind, name and command "
                                     f"(missing: {', '.join(missing)}) — see the template."}
    params = {r["code"]: r["name"] for r in conn.execute("SELECT code, name FROM cota_parameter")}
    saved = {r["name"]: dict(r) for r in conn.execute("SELECT * FROM cota_saved_command")}
    rows = []
    for line, raw in enumerate(reader, start=2):
        row = {(k or "").strip().lower(): (v or "").strip() for k, v in raw.items()}
        if not any(row.get(c) for c in CSV_COLUMNS):
            continue                                    # a blank line
        kind = _KINDS.get(row.get("kind", "").lower())
        name, value = row.get("name", ""), "".join(row.get("command", "").split()).upper()
        entry = {"line": line, "kind": kind or row.get("kind", ""), "name": name, "command": value,
                 "tags": clean_tags(row.get("tags", "")), "note": row.get("note", ""),
                 "action": "error", "detail": ""}
        if not kind:
            entry["detail"] = "kind must be parameter or command"
        elif not name:
            entry["detail"] = "no name"
        elif kind == "parameter":
            if not _CODE.match(value):
                entry["detail"] = "a parameter code is four hex digits, e.g. 6C0A"
            elif value not in params:
                entry["action"] = "new"
            elif params[value] == name:
                entry["action"] = "same"
            else:
                entry.update(action="update", detail=f"was “{params[value]}”")
        else:
            _, problem = cota_run.classify(value)
            if problem or not _HEX.match(value):
                entry["detail"] = problem or "the command must be hexadecimal"
            elif name not in saved:
                entry["action"] = "new"
            else:
                old = saved[name]
                changes = [what for what, a, b in (("command", old["val1"], value),
                                                   ("tags", old["tags"], entry["tags"]),
                                                   ("note", old["note"], entry["note"])) if a != b]
                entry.update(action="update" if changes else "same",
                             detail=("changes " + ", ".join(changes)) if changes else "")
        rows.append(entry)
    # "updated", not "update": a key named like a dict method is read as the method in Jinja.
    counts = {"new": 0, "updated": 0, "same": 0, "error": 0}
    for r in rows:
        counts["updated" if r["action"] == "update" else r["action"]] += 1
    return {"rows": rows, "counts": counts, "error": "" if rows else "The file has no rows."}


def apply_import(conn, content: bytes) -> dict:
    """Apply what plan_import said, rows with a problem left out. Read again from the same
    bytes, so what is applied is exactly what was previewed."""
    planned = plan_import(conn, content)
    saved_ids = {r["name"]: r["id"] for r in conn.execute("SELECT id, name FROM cota_saved_command")}
    for r in planned["rows"]:
        if r["action"] not in ("new", "update"):
            continue
        if r["kind"] == "parameter":
            save_parameter(conn, r["command"], r["name"])
        else:
            save_command(conn, name=r["name"], val1=r["command"], tags=r["tags"], note=r["note"],
                         command_id=saved_ids.get(r["name"]))
    refresh(conn)
    return planned.get("counts", {})
