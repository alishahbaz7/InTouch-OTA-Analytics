"""Which database this is, and exactly what data it holds.

Two people running this tool against the same platform do not have the same numbers unless
they have ingested the same snapshots — and nothing else in the UI answers that question. The
.db file cannot answer it either: two databases holding identical snapshots are never
byte-identical, because rowids, ingested_at, WAL state and vacuum history all differ. Hashing
the file would report a mismatch every single time and prove nothing.

What *is* comparable is the input set. Every derived table is rebuildable from the raw
snapshots (`rollup.rollup_all`, `registry.rebuild`), and a snapshot is identified by
`file_sha256` — already the idempotency key ingest uses. So:

    same set of file_sha256  =>  same numbers, by construction

`fleet_digest` hashes exactly that set. Put it in the footer and on every report, and the
first question in any disagreement — "are we even looking at the same data?" — is settled by
comparing eight characters instead of by argument.

The other two values here answer a different question, and it is worth keeping them apart:

    db_id / instance_label   *whose* numbers are these
    fleet_digest             do they match mine

A creation date belongs to the first group. It is provenance, not a reconciliation key: two
databases created months apart can hold identical snapshots, and two created in the same
minute can hold completely different ones.
"""

from __future__ import annotations

import hashlib
import os
import socket
import sqlite3
import uuid
from datetime import datetime

from . import config

# db_meta keys. Kept as constants because they are written from three modules.
DB_ID = "db_id"
CREATED_AT = "created_at"
INSTANCE_LABEL = "instance_label"
LAST_IMPORT_AT = "last_import_at"
LAST_IMPORT_FROM = "last_import_from"
LAST_EXPORT_AT = "last_export_at"

# What the deployment configured wins over what is stored, for the same reason
# OTA_PLATFORM_PASSWORD wins over keyring: a stale stored label shadowing the configured one
# would be undebuggable. A blank value counts as unset.
ENV_LABEL = "OTA_INSTANCE_LABEL"


def _now() -> str:
    return datetime.now().isoformat(sep=" ", timespec="seconds")


def get(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    """Read one metadata value, tolerating a database that predates the table."""
    try:
        row = conn.execute("SELECT value FROM db_meta WHERE key = ?", (key,)).fetchone()
    except sqlite3.OperationalError:
        return default
    return row["value"] if row else default


def put(conn: sqlite3.Connection, key: str, value: str | None) -> None:
    conn.execute("INSERT INTO db_meta (key, value) VALUES (?, ?) "
                 "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))
    conn.commit()


def ensure(conn: sqlite3.Connection) -> None:
    """Stamp identity onto a database that does not have it yet.

    Called from `db.migrate`, so every connection path gets it. Deliberately does not touch a
    db_id that already exists — that value is what lets a bundle say where it came from, and
    rewriting it would break the link on every upgrade.
    """
    try:
        existing = {r["key"] for r in conn.execute("SELECT key FROM db_meta")}
    except sqlite3.OperationalError:
        return                                  # table not created yet; nothing to stamp

    if DB_ID not in existing:
        conn.execute("INSERT INTO db_meta (key, value) VALUES (?, ?)", (DB_ID, uuid.uuid4().hex))
    if CREATED_AT not in existing:
        # For a database that already has snapshots this is an upgrade, not a creation, so the
        # honest value is when its data starts — not today.
        first = conn.execute("SELECT MIN(ingested_at) AS t FROM snapshot").fetchone()
        conn.execute("INSERT INTO db_meta (key, value) VALUES (?, ?)",
                     (CREATED_AT, (first["t"] if first else None) or _now()))
    if INSTANCE_LABEL not in existing:
        conn.execute("INSERT INTO db_meta (key, value) VALUES (?, ?)",
                     (INSTANCE_LABEL, _default_label()))


def _default_label() -> str:
    try:
        return socket.gethostname() or "unnamed"
    except OSError:
        return "unnamed"


def instance_label(conn: sqlite3.Connection) -> str:
    return (os.environ.get(ENV_LABEL) or "").strip() or get(conn, INSTANCE_LABEL) \
        or _default_label()


def set_instance_label(conn: sqlite3.Connection, label: str) -> str:
    """Name this install, so a shared report says whose numbers it is."""
    cleaned = " ".join(str(label).split())[:60] or _default_label()
    put(conn, INSTANCE_LABEL, cleaned)
    return cleaned


def fleet_digest(conn: sqlite3.Connection) -> str:
    """SHA-256 over the sorted set of ingested file hashes.

    Sorted, so two installs that loaded the same exports in a different order still agree —
    the resulting state does not depend on ingest order, so the fingerprint must not either.

    Snapshots still being written are excluded (`row_count > 0`, the same rule
    `metrics.snapshots` uses), otherwise the digest would flicker for the seconds a fetch is
    in flight and two people comparing mid-fetch would see a false mismatch.
    """
    digest = hashlib.sha256()
    for row in conn.execute("SELECT file_sha256 FROM snapshot WHERE row_count > 0 "
                            "ORDER BY file_sha256"):
        digest.update(row["file_sha256"].encode())
        digest.update(b"\n")
    return digest.hexdigest()


def short(digest: str | None) -> str:
    return (digest or "")[:8] or "—"


def coverage(conn: sqlite3.Connection) -> dict:
    """What this database holds: how many snapshots, spanning what.

    Two clocks, reported separately on purpose. `snapshot_at` is when the platform's data was
    true; `ingested_at` is when this install pulled it. A single "last sync" figure conflates
    them, and it is the first that decides what the numbers say.
    """
    row = conn.execute("""
        SELECT COUNT(*)         AS snapshots,
               MIN(snapshot_at) AS first_snapshot_at,
               MAX(snapshot_at) AS last_snapshot_at,
               MAX(ingested_at) AS last_ingest_at,
               SUM(row_count)   AS device_rows
        FROM snapshot WHERE row_count > 0
    """).fetchone()
    return {k: row[k] for k in row.keys()}


def manifest(conn: sqlite3.Connection) -> dict:
    """Everything needed to decide whether two installs agree, in one dict."""
    from . import db

    digest = fleet_digest(conn)
    data = {
        "db_id": get(conn, DB_ID),
        "created_at": get(conn, CREATED_AT),
        "instance_label": instance_label(conn),
        "db_path": str(config.DB_PATH),
        "schema_version": db.SCHEMA_VERSION,
        "fleet_digest": digest,
        "digest_short": short(digest),
        "last_import_at": get(conn, LAST_IMPORT_AT),
        "last_import_from": get(conn, LAST_IMPORT_FROM),
        "last_export_at": get(conn, LAST_EXPORT_AT),
        **coverage(conn),
    }
    if not data["snapshots"]:
        data["digest_short"] = "empty"
    return data


def compare(mine: dict, theirs: dict) -> dict:
    """Read two manifests side by side and say, in words, how they differ.

    The digest answers yes/no; this answers "then what". Written here rather than in a template
    because the same wording is wanted by the CLI, the page and the import screen.
    """
    same = mine.get("fleet_digest") == theirs.get("fleet_digest")
    result = {"match": same, "reasons": []}
    if same:
        return result

    mine_n, theirs_n = mine.get("snapshots") or 0, theirs.get("snapshots") or 0
    if mine_n != theirs_n:
        result["reasons"].append(
            f"snapshot count differs: {mine_n:,} here, {theirs_n:,} there")
    for label, key in (("newest snapshot", "last_snapshot_at"),
                       ("oldest snapshot", "first_snapshot_at")):
        if mine.get(key) != theirs.get(key):
            result["reasons"].append(
                f"{label} differs: {mine.get(key) or 'none'} here, "
                f"{theirs.get(key) or 'none'} there")
    if not result["reasons"]:
        # Same count and same endpoints, different contents — something in the middle differs.
        result["reasons"].append(
            "same number of snapshots over the same period, but not the same ones — "
            "one side has a fetch the other is missing")
    return result
