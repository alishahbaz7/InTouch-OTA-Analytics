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
    commands = {r["val1"]: r["name"] for r in conn.execute("SELECT val1, name FROM cota_saved_command")}
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

def parameters(conn) -> list[dict]:
    """The user's names and the built-ins they have not renamed, by code."""
    mine = {r["code"]: dict(r) for r in conn.execute(
        "SELECT code, name, updated_at FROM cota_parameter")}
    rows = []
    for code in sorted(set(mine) | set(cota.COMMAND_PARAMETERS)):
        own = mine.get(code)
        rows.append({"code": code, "name": own["name"] if own else cota.COMMAND_PARAMETERS[code],
                     "builtin": cota.COMMAND_PARAMETERS.get(code), "own": bool(own),
                     "updated_at": own["updated_at"] if own else ""})
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
                or needle in r["note"].lower() or any(needle in t.lower() for t in r["tag_list"])]
    return rows


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
