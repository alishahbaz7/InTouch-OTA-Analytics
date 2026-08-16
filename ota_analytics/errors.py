"""Error log: nothing should fail silently or leave only a blank 'Internal Server Error'.

Failures are written to the database *and* to a plain text file. The database copy powers the
dashboard; the file survives a database that is itself the problem.
"""

from __future__ import annotations

import sqlite3
import traceback
from datetime import datetime

from . import config

LOG_PATH = config.DATA_DIR / "errors.log"
MAX_ROWS = 2000     # keep the table bounded; the file keeps the long tail


def record(source: str, exc: BaseException, path: str | None = None,
           conn: sqlite3.Connection | None = None) -> None:
    """Record a failure. Must never raise — logging an error cannot become an error."""
    occurred = datetime.now().isoformat(sep=" ", timespec="seconds")
    error_type = type(exc).__name__
    message = str(exc)[:500] or error_type
    detail = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))[-4000:]

    try:
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as handle:
            handle.write(f"\n[{occurred}] {source} {path or ''}\n{detail}\n")
    except Exception:
        pass

    try:
        own = conn is None
        if own:
            from . import db
            conn = db.connect()
        conn.execute(
            "INSERT INTO app_error (occurred_at, source, path, error_type, message, detail) "
            "VALUES (?,?,?,?,?,?)", (occurred, source, path, error_type, message, detail))
        conn.execute(
            "DELETE FROM app_error WHERE id NOT IN "
            "(SELECT id FROM app_error ORDER BY id DESC LIMIT ?)", (MAX_ROWS,))
        conn.commit()
    except Exception:
        pass        # the file copy above is the fallback


def recent(conn: sqlite3.Connection, limit: int = 100, hours: float | None = None) -> list[dict]:
    where, params = "1 = 1", []
    if hours:
        from datetime import timedelta
        since = (datetime.now() - timedelta(hours=hours)).isoformat(sep=" ", timespec="seconds")
        where, params = "occurred_at >= ?", [since]
    rows = conn.execute(f"""
        SELECT id, occurred_at, source, path, error_type, message, detail, seen
        FROM app_error WHERE {where} ORDER BY id DESC LIMIT ?
    """, [*params, limit]).fetchall()
    return [dict(r) for r in rows]


def summary(conn: sqlite3.Connection) -> dict:
    """What the header badge needs: how many, and how recent."""
    try:
        row = conn.execute("""
            SELECT COUNT(*) AS total,
                   SUM(occurred_at >= datetime('now', 'localtime', '-24 hours')) AS last_24h,
                   MAX(occurred_at) AS latest
            FROM app_error
        """).fetchone()
    except sqlite3.Error:
        return {"total": 0, "last_24h": 0, "latest": None}
    return {"total": row["total"] or 0, "last_24h": row["last_24h"] or 0,
            "latest": row["latest"]}


def clear(conn: sqlite3.Connection) -> int:
    count = conn.execute("SELECT COUNT(*) FROM app_error").fetchone()[0]
    conn.execute("DELETE FROM app_error")
    conn.commit()
    return count
