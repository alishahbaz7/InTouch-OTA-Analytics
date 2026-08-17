"""Exchanging snapshot history between two installs.

The point of this is a shared reference point: if you and a colleague both work offline and
sync at different times, your dashboards disagree, and no amount of comparing screenshots
resolves it. What resolves it is one of you handing the other the fetches they are missing.

**A bundle is replayed, not swapped in.** Shipping the .db file would be a *replace* — whoever
imports loses every snapshot the sender did not have — and it breaks the moment the two sides
are on different schema versions. Re-ingesting change rows is a genuine *merge*: ingest is
append-only and keyed on file_sha256, so the two snapshot sets union, duplicates are no-ops,
and after a rebuild both installs compute the same numbers. That works because of the
architecture already in place, not in spite of it.

What makes it non-trivial is that `device_snapshot` stores one row per CHANGE, so a row only
means anything relative to the chain it was written against. Two consequences shape everything
below:

**The bundle must carry a baseline.** Its first snapshot is exported as a full resolved state —
every device the sender knew, tombstones included — and the rest as deltas against it. Without
that, the deltas would be interpreted against the *receiver's* preceding snapshot, which is a
different chain, and devices would silently take values no fetch ever observed.

**Inserting into the middle of a timeline rewrites it.** `device_state` resolves a snapshot by
taking each device's most recent row at or before it, so a foreign snapshot landing between two
local ones becomes the answer for every device that did not change locally in that gap — a
number nobody ever measured, reported without error. So the default is an append-only merge,
which refuses that case outright. `allow_interleave` handles it properly instead:
`retention.densify` makes the snapshots either side of every insertion point self-sufficient
first, `retention.renumber_snapshots` restores id-order == time-order afterwards, and
`retention.compact` squeezes the materialized duplicates back out.
"""

from __future__ import annotations

import io
import json
import sqlite3
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import identity, normalize, registry, retention, rollup

SUFFIX = ".otabundle"

# Bumped only for a change that an older reader could not handle. The reader refuses a format
# it does not know rather than guessing, because a half-understood merge is worse than none.
FORMAT_VERSION = 1

MANIFEST = "manifest.json"
SNAPSHOTS = "snapshots.jsonl"
DEVICES = "devices.jsonl"

ZIP_MAGIC = b"PK\x03\x04"

# Snapshot columns carried across. `id` is deliberately absent: ids are local to a database and
# reusing the sender's would collide or, worse, land out of chronological order. `file_sha256`
# is the cross-database key — it is already how ingest recognises a file it has seen.
SNAPSHOT_COLUMNS = ["file_sha256", "snapshot_at", "source_file", "ts_source",
                    "row_count", "skipped_rows", "ingested_at", "duration_ms"]


class BundleError(Exception):
    """Raised when a bundle cannot be read or safely merged."""


@dataclass
class ExportResult:
    path: Path | None
    snapshots: int
    device_rows: int
    baseline_rows: int
    bytes_written: int
    first_snapshot_at: str | None
    last_snapshot_at: str | None
    digest: str


@dataclass
class ImportResult:
    status: str                  # imported | already_present | refused | empty
    message: str
    manifest: dict = field(default_factory=dict)
    snapshots_in_bundle: int = 0
    snapshots_new: int = 0
    device_rows: int = 0
    interleaved: bool = False
    renumbered: int = 0
    compacted: int = 0
    digest_before: str = ""
    digest_after: str = ""


# ─── export ─────────────────────────────────────────────────────────────────

def _selected_snapshots(conn: sqlite3.Connection, since: str | None,
                        until: str | None) -> list[dict]:
    where, params = ["row_count > 0"], []
    if since:
        where.append("snapshot_at >= ?")
        params.append(since)
    if until:
        where.append("snapshot_at <= ?")
        params.append(until)
    rows = conn.execute(
        f"SELECT id, {', '.join(SNAPSHOT_COLUMNS)} FROM snapshot "
        f"WHERE {' AND '.join(where)} ORDER BY snapshot_at, id", params).fetchall()
    return [dict(r) for r in rows]


def _row_json(sha: str, row: sqlite3.Row, columns: list[str]) -> bytes:
    """One device row as a line of JSON.

    Nulls are dropped rather than written: most devices have several empty columns, and a
    self-describing dict costs nothing to read back while staying tolerant of a sender whose
    schema has a column this install does not.
    """
    payload = {"sha": sha}
    for column in columns:
        value = row[column]
        if value is not None:
            payload[column] = value
    return (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")


def export_bundle(conn: sqlite3.Connection, out, *, since: str | None = None,
                  until: str | None = None) -> ExportResult:
    """Write the snapshot history to a bundle. `out` may be a path or a binary file object."""
    selected = _selected_snapshots(conn, since, until)
    if not selected:
        raise BundleError("There are no snapshots to export"
                          + (" in that period." if since or until else " yet."))

    columns = retention.device_columns(conn)
    baseline = selected[0]
    rest_ids = [s["id"] for s in selected[1:]]
    device_rows = baseline_rows = 0

    manifest_data = {
        "format": FORMAT_VERSION,
        "exported_at": datetime.now().isoformat(sep=" ", timespec="seconds"),
        "source": identity.manifest(conn),
        "baseline_sha": baseline["file_sha256"],
        "device_columns": columns,
        "snapshots": [{"sha": s["file_sha256"], "snapshot_at": s["snapshot_at"],
                       "source_file": s["source_file"], "row_count": s["row_count"]}
                      for s in selected],
    }

    target = out if hasattr(out, "write") else Path(out)
    if isinstance(target, Path):
        target.parent.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        with archive.open(SNAPSHOTS, "w") as stream:
            for snapshot in selected:
                payload = {k: snapshot[k] for k in SNAPSHOT_COLUMNS}
                stream.write((json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8"))

        with archive.open(DEVICES, "w") as stream:
            # The baseline is the sender's full resolved state, not its change rows — every
            # device it knew, at the value it held, tombstones included. This is what makes the
            # bundle a self-contained chain instead of a fragment only the sender can read.
            cursor = conn.execute(f"""
                SELECT {', '.join(columns)} FROM device_snapshot d
                WHERE d.snapshot_id = (SELECT MAX(x.snapshot_id) FROM device_snapshot x
                                       WHERE x.imei = d.imei AND x.snapshot_id <= ?)
            """, (baseline["id"],))
            for row in cursor:
                stream.write(_row_json(baseline["file_sha256"], row, columns))
                baseline_rows += 1

            # Everything after it is deltas, exactly as stored — which is why a bundle covering
            # weeks of fetches is barely larger than one covering a single day.
            if rest_ids:
                sha_by_id = {s["id"]: s["file_sha256"] for s in selected[1:]}
                cursor = conn.execute(f"""
                    SELECT snapshot_id, {', '.join(columns)} FROM device_snapshot
                    WHERE snapshot_id IN ({','.join('?' * len(rest_ids))})
                    ORDER BY snapshot_id
                """, rest_ids)
                for row in cursor:
                    stream.write(_row_json(sha_by_id[row["snapshot_id"]], row, columns))
                    device_rows += 1

        manifest_data["baseline_rows"] = baseline_rows
        manifest_data["delta_rows"] = device_rows
        archive.writestr(MANIFEST, json.dumps(manifest_data, indent=2))

    identity.put(conn, identity.LAST_EXPORT_AT,
                 datetime.now().isoformat(sep=" ", timespec="seconds"))

    size = target.stat().st_size if isinstance(target, Path) else target.tell()
    return ExportResult(
        path=target if isinstance(target, Path) else None,
        snapshots=len(selected), device_rows=device_rows, baseline_rows=baseline_rows,
        bytes_written=size,
        first_snapshot_at=selected[0]["snapshot_at"],
        last_snapshot_at=selected[-1]["snapshot_at"],
        digest=manifest_data["source"]["fleet_digest"],
    )


def suggested_filename(conn: sqlite3.Connection) -> str:
    """Name the file after who made it and what it covers, not just when."""
    data = identity.manifest(conn)
    label = "".join(c if c.isalnum() or c in "-_" else "-"
                    for c in (data["instance_label"] or "ota"))[:24]
    return f"ota_{label}_{data['digest_short']}_{datetime.now():%d%b%y_%H%M}{SUFFIX}"


# ─── reading ────────────────────────────────────────────────────────────────

def _open(source) -> zipfile.ZipFile:
    """Open a bundle, refusing anything that is not one.

    The check mirrors the .xlsx rule in sources.py and for the same reason: the normal failure
    mode of a download is a 200 OK containing an error page, which would otherwise be reported
    as a corrupt bundle rather than as what it is.
    """
    if isinstance(source, (bytes, bytearray)):
        if not bytes(source[:4]) == ZIP_MAGIC:
            raise BundleError("That file is not a bundle (it is not a zip archive).")
        source = io.BytesIO(source)
    elif not hasattr(source, "read"):
        path = Path(source)
        if not path.exists():
            raise BundleError(f"No such bundle: {path}")
        with open(path, "rb") as handle:
            if handle.read(4) != ZIP_MAGIC:
                raise BundleError(f"{path.name} is not a bundle (it is not a zip archive).")
        source = path

    try:
        archive = zipfile.ZipFile(source)
    except zipfile.BadZipFile as exc:
        raise BundleError(f"The bundle is damaged and could not be opened ({exc}).") from exc

    missing = [name for name in (MANIFEST, SNAPSHOTS, DEVICES)
               if name not in archive.namelist()]
    if missing:
        archive.close()
        raise BundleError("That zip file is not a bundle — it is missing "
                          + ", ".join(missing) + ".")
    return archive


def read_manifest(source) -> dict:
    """The bundle's manifest, without reading the device rows. Cheap enough for a preview."""
    with _open(source) as archive:
        try:
            data = json.loads(archive.read(MANIFEST).decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise BundleError(f"The bundle manifest is unreadable ({exc}).") from exc

    version = data.get("format")
    if version != FORMAT_VERSION:
        raise BundleError(
            f"This bundle is format {version}, and this version reads format "
            f"{FORMAT_VERSION}. Upgrade the older side rather than importing it — a "
            f"half-understood merge is worse than none.")
    return data


def describe(source) -> dict:
    """Manifest plus the derived facts a person actually wants to see before importing."""
    data = read_manifest(source)
    snapshots = data.get("snapshots", [])
    times = sorted(s["snapshot_at"] for s in snapshots if s.get("snapshot_at"))
    return {
        **data,
        "snapshot_count": len(snapshots),
        "first_snapshot_at": times[0] if times else None,
        "last_snapshot_at": times[-1] if times else None,
    }


# ─── import ─────────────────────────────────────────────────────────────────

def _stage(conn: sqlite3.Connection, archive: zipfile.ZipFile, columns: list[str],
           order: dict[str, int]) -> int:
    """Load the bundle's device rows into a staging table keyed by position in its chain.

    Staged rather than written straight through, because when snapshots have to be interleaved
    the bundle's own chain must be resolved *on its own terms* first. Resolving it against the
    merged table would hand foreign snapshots values from local fetches they never saw — the
    exact corruption this module exists to prevent.
    """
    # Dropped rather than reused: a TEMP table outlives one import on a long-lived connection,
    # and `IF NOT EXISTS` would silently keep a shape built for a different bundle's columns.
    conn.execute("DROP TABLE IF EXISTS stage_bundle")
    conn.execute(f"""
        CREATE TEMP TABLE stage_bundle (
          seq INTEGER NOT NULL,
          {', '.join(f'{c} TEXT' for c in columns)},
          PRIMARY KEY (seq, imei)
        ) WITHOUT ROWID
    """)
    # Resolving the chain looks a device up across sequences, which the (seq, imei) primary key
    # cannot serve — it would scan the whole staging table once per snapshot. On the real
    # database that alone took a 37-snapshot merge past ten minutes without finishing. This
    # mirrors ix_ds_imei_snap on the physical table, which exists for the same lookup.
    conn.execute("CREATE INDEX IF NOT EXISTS ix_stage_bundle_imei ON stage_bundle(imei, seq)")

    insert = (f"INSERT OR REPLACE INTO stage_bundle (seq, {', '.join(columns)}) "
              f"VALUES ({','.join('?' * (len(columns) + 1))})")
    batch, staged = [], 0

    with archive.open(DEVICES) as stream:
        for line in stream:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError as exc:
                raise BundleError(f"The bundle's device rows are corrupt ({exc}).") from exc
            seq = order.get(row.get("sha"))
            if seq is None:
                continue                      # a snapshot this install already has
            # `present` defaults to 1 so a bundle written before tombstones existed still reads.
            batch.append((seq, *(row.get(c, 1 if c == "present" else None) for c in columns)))
            if len(batch) >= 5_000:
                conn.executemany(insert, batch)
                staged += len(batch)
                batch.clear()

    if batch:
        conn.executemany(insert, batch)
        staged += len(batch)
    return staged


def plan_densify(local: list[tuple[str, int]],
                 bundle: list[tuple[str, int]]) -> tuple[set[int], set[int]]:
    """Decide which snapshots stop being self-sufficient once the two timelines merge.

    A change-row snapshot only means anything relative to the snapshot that came before it when
    it was written. Merging changes that predecessor for some snapshots and leaves it alone for
    others — a snapshot whose neighbour is unchanged still resolves correctly and needs nothing
    done to it.

    Densifying everything is also correct, and was what this did first. It is quadratic: on the
    real database a 37-snapshot merge became 1.3 M staged rows and had not finished in ten
    minutes. In the common shape — one side simply holds older history than the other — the two
    runs of snapshots barely interleave at all and this returns almost nothing.

    `local` is (snapshot_at, id) in local order; `bundle` is (snapshot_at, seq) in bundle order.
    Ties put the local snapshot first, matching the id order renumbering will produce.
    """
    merged = sorted([(when, 0, key) for when, key in local]
                    + [(when, 1, key) for when, key in bundle])

    # The bundle's first snapshot is exported as a full resolved state, so it inherits nothing
    # and can never need materializing — however the merge reorders things around it. This is
    # the whole reason a bundle carries a baseline.
    baseline = bundle[0][1] if bundle else None

    previous: dict[int, int | None] = {0: None, 1: None}
    merged_previous: tuple | None = None
    needs: dict[int, set[int]] = {0: set(), 1: set()}

    for entry in merged:
        _, side, key = entry
        own = previous[side]
        # Same neighbour as when the row was written => nothing to materialize.
        if merged_previous is None:
            unchanged = own is None
        else:
            unchanged = merged_previous[1] == side and merged_previous[2] == own
        if not unchanged and not (side == 1 and key == baseline):
            needs[side].add(key)
        previous[side] = key
        merged_previous = entry

    return needs[0], needs[1]


def _densify_stage(conn: sqlite3.Connection, columns: list[str], sequences: set[int]) -> int:
    """Write out the inherited rows of the staged snapshots that need them.

    Resolved within the bundle's own chain, on its own terms. Resolving against the merged table
    instead would hand foreign snapshots values from local fetches they never saw, which is the
    exact corruption this module exists to prevent.
    """
    added = 0
    for seq in sorted(sequences):
        cursor = conn.execute(f"""
            INSERT OR IGNORE INTO stage_bundle (seq, {', '.join(columns)})
            SELECT ?, {', '.join('s.' + c for c in columns)}
            FROM stage_bundle s
            WHERE s.seq = (SELECT MAX(x.seq) FROM stage_bundle x
                           WHERE x.imei = s.imei AND x.seq <= ?)
        """, (seq, seq))
        added += cursor.rowcount
    return added


def _rebuild_groups(conn: sqlite3.Connection, snapshot_ids: list[int]) -> int:
    """Re-derive group membership for the imported snapshots.

    device_group is not carried in the bundle: it is fully derivable from `groups_raw`, which
    is delta-stored and therefore a fraction of the size. Resolved through `device_state` rather
    than the raw rows, because a device that did not change carries no row of its own.
    """
    written = 0
    for snapshot_id in snapshot_ids:
        rows = conn.execute(
            "SELECT imei, groups_raw FROM device_state "
            "WHERE snapshot_id = ? AND groups_raw IS NOT NULL", (snapshot_id,)).fetchall()
        batch = [(snapshot_id, row["imei"], name)
                 for row in rows for name in normalize.split_groups(row["groups_raw"])]
        if batch:
            conn.executemany("INSERT OR IGNORE INTO device_group "
                             "(snapshot_id, imei, group_name) VALUES (?,?,?)", batch)
            written += len(batch)
    return written


def _finish_merge(conn: sqlite3.Connection, bundle_start: str | None,
                  shas: list[str] | None = None, *, full_rebuild: bool = True) -> dict:
    """Put the database back in order after foreign rows have landed, and rebuild everything.

    Split out so an import interrupted partway can be completed on the next attempt rather
    than leaving a database whose numbers are quietly wrong.

    `full_rebuild` is the difference between the two merge shapes. Appending newer snapshots
    changes nothing about the history already recorded, so they are folded in one at a time —
    the same call ingest makes, at the same cost per snapshot. Interleaving does change it: a
    fetch inserted into the middle alters what moved and when, so the change log has to be
    replayed from the beginning. Measured on the real database, the replay is ~86% of an
    import's total time, so paying it only when it is needed is most of what makes handing
    someone the fetches they are missing quick.
    """
    # `device_state` resolves state by comparing snapshot ids, not timestamps, so an id that
    # sits out of chronological order does not look untidy — it returns the wrong row.
    renumbered = retention.renumber_snapshots(conn)

    # Squeeze out the rows densify materialized, plus any row the bundle repeated because its
    # chain and ours already agreed.
    first_id = None
    if bundle_start:
        first_id = conn.execute("SELECT MIN(id) AS id FROM snapshot WHERE snapshot_at >= ?",
                                (bundle_start,)).fetchone()["id"]
    compacted = retention.compact(conn, first_id)
    conn.commit()

    ids: list[int] = []
    if shas:
        placeholders = ",".join("?" * len(shas))
        ids = [r["id"] for r in conn.execute(
            f"SELECT id FROM snapshot WHERE file_sha256 IN ({placeholders}) "
            f"ORDER BY snapshot_at, id", shas)]
        _rebuild_groups(conn, ids)

    # Renumbering drops the fact tables rather than remapping them, so it forces the full path
    # whatever the caller asked for.
    if full_rebuild or renumbered or not ids:
        registry.rebuild(conn)
        rollup.rollup_all(conn)
    else:
        for snapshot_id in ids:               # oldest first, so the change log reads in order
            registry.apply_snapshot(conn, snapshot_id)
            rollup.rollup_snapshot(conn, snapshot_id)
    conn.commit()
    return {"renumbered": renumbered, "compacted": compacted}


def import_bundle(conn: sqlite3.Connection, source, *, allow_interleave: bool = False,
                  dry_run: bool = False) -> ImportResult:
    """Merge another install's snapshots into this database.

    Idempotent for the same reason ingest is: a snapshot already held, matched by file_sha256,
    is skipped rather than duplicated. Importing the same bundle twice changes nothing.
    """
    data = describe(source)
    digest_before = identity.fleet_digest(conn)
    incoming = data.get("snapshots", [])
    if not incoming:
        return ImportResult(status="empty", message="That bundle contains no snapshots.",
                            manifest=data, digest_before=digest_before,
                            digest_after=digest_before)

    held = {r["file_sha256"] for r in conn.execute("SELECT file_sha256 FROM snapshot")}
    new = [s for s in incoming if s["sha"] not in held]

    source_label = (data.get("source") or {}).get("instance_label") or "another install"
    if not new:
        # An import is not atomic end to end: renumbering has to toggle PRAGMA foreign_keys,
        # which SQLite ignores inside a transaction, so it commits on its own. If a previous
        # run died after the rows landed but before the rebuild, every sha is already held and
        # this branch would otherwise report success over a half-merged database. Finishing the
        # job here makes an interrupted import self-healing on the next attempt.
        if not dry_run and not retention.is_chronological(conn):
            repaired = _finish_merge(conn, None)
            return ImportResult(
                status="imported", manifest=data, snapshots_in_bundle=len(incoming),
                renumbered=repaired["renumbered"], compacted=repaired["compacted"],
                digest_before=digest_before, digest_after=identity.fleet_digest(conn),
                message="Every snapshot in this bundle was already loaded, but the database "
                        "was left mid-merge by an earlier run. Finished it: snapshots "
                        f"reordered and {repaired['compacted']:,} redundant rows removed.")
        return ImportResult(
            status="already_present", manifest=data, snapshots_in_bundle=len(incoming),
            digest_before=digest_before, digest_after=digest_before,
            message=f"All {len(incoming)} snapshot(s) in this bundle are already loaded. "
                    f"Nothing changed — this database and {source_label} already agree.")

    bundle_start = min(s["snapshot_at"] for s in new)
    local_latest = conn.execute(
        "SELECT MAX(snapshot_at) AS t FROM snapshot").fetchone()["t"]
    interleaved = bool(local_latest and bundle_start <= local_latest)

    if interleaved and not allow_interleave:
        return ImportResult(
            status="refused", manifest=data, snapshots_in_bundle=len(incoming),
            snapshots_new=len(new), interleaved=True,
            digest_before=digest_before, digest_after=digest_before,
            message=(
                f"This bundle starts at {bundle_start}, which is inside history this database "
                f"already holds (newest local snapshot {local_latest}). Slotting a fetch into "
                f"the middle of a timeline changes what every unchanged device appears to have "
                f"been doing, so it is not done silently. Re-run allowing interleave to have "
                f"the surrounding snapshots rewritten safely first — it rebuilds the whole "
                f"database and takes longer."))

    if dry_run:
        return ImportResult(
            status="refused" if interleaved else "imported", manifest=data,
            snapshots_in_bundle=len(incoming), snapshots_new=len(new),
            interleaved=interleaved, digest_before=digest_before, digest_after=digest_before,
            message=f"Dry run: {len(new)} of {len(incoming)} snapshot(s) would be imported"
                    + (" (interleaved into existing history)." if interleaved else "."))

    local_columns = set(retention.device_columns(conn))
    bundle_columns = data.get("device_columns") or []
    columns = [c for c in retention.device_columns(conn)
               if c in set(bundle_columns) or c == "imei"]
    unknown = [c for c in bundle_columns if c not in local_columns]
    if "imei" not in columns:
        raise BundleError("The bundle's device rows carry no IMEI column.")

    order = {s["sha"]: seq for seq, s in enumerate(
        sorted(new, key=lambda s: s["snapshot_at"]), start=1)}

    # Steps 1-4 are one transaction: until they commit together, a failure leaves no trace.
    # The rebuild that follows cannot join them — renumbering has to toggle PRAGMA
    # foreign_keys, which SQLite ignores inside a transaction — so it is written to be
    # resumable instead, and re-running the same import finishes it.
    local_densify: set[int] = set()
    bundle_densify: set[int] = set()
    if interleaved:
        local_chain = [(r["snapshot_at"], r["id"]) for r in conn.execute(
            "SELECT id, snapshot_at FROM snapshot ORDER BY snapshot_at, id")]
        when_by_sha = {s["sha"]: s["snapshot_at"] for s in new}
        bundle_chain = [(when_by_sha[sha], seq)
                        for sha, seq in sorted(order.items(), key=lambda kv: kv[1])]
        local_densify, bundle_densify = plan_densify(local_chain, bundle_chain)

    try:
        # 1. Make the affected local snapshots self-sufficient, using the table as it stands —
        #    before a single foreign row lands in it.
        for snapshot_id in sorted(local_densify):
            retention.densify(conn, snapshot_id)

        # 2. Stage the bundle and resolve it on its own terms.
        with _open(source) as archive:
            staged = _stage(conn, archive, columns, order)
        if bundle_densify:
            staged += _densify_stage(conn, columns, bundle_densify)

        # 3. Insert the snapshot rows, oldest first, and map bundle position -> local id.
        meta_by_sha = {}
        with _open(source) as archive:
            with archive.open(SNAPSHOTS) as stream:
                for line in stream:
                    line = line.strip()
                    if line:
                        row = json.loads(line)
                        meta_by_sha[row["file_sha256"]] = row

        id_by_seq: dict[int, int] = {}
        imported_shas: list[str] = []
        for sha, seq in sorted(order.items(), key=lambda kv: kv[1]):
            meta = meta_by_sha.get(sha)
            if meta is None:
                raise BundleError(f"The bundle lists snapshot {sha[:8]} but carries no "
                                  f"metadata for it.")
            cursor = conn.execute(
                f"INSERT INTO snapshot ({', '.join(SNAPSHOT_COLUMNS)}) "
                f"VALUES ({','.join('?' * len(SNAPSHOT_COLUMNS))})",
                [meta.get(c) for c in SNAPSHOT_COLUMNS])
            id_by_seq[seq] = cursor.lastrowid
            imported_shas.append(sha)

        # 4. Move the staged rows across under their new ids.
        written = 0
        for seq, snapshot_id in id_by_seq.items():
            cursor = conn.execute(f"""
                INSERT OR IGNORE INTO device_snapshot (snapshot_id, {', '.join(columns)})
                SELECT ?, {', '.join(columns)} FROM stage_bundle WHERE seq = ?
            """, (snapshot_id, seq))
            written += cursor.rowcount
        conn.execute("DROP TABLE IF EXISTS stage_bundle")
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    # 5. Reorder, compact and rebuild. Resumable: see _finish_merge.
    finished = _finish_merge(conn, bundle_start, imported_shas, full_rebuild=interleaved)
    renumbered, compacted = finished["renumbered"], finished["compacted"]

    now = datetime.now().isoformat(sep=" ", timespec="seconds")
    identity.put(conn, identity.LAST_IMPORT_AT, now)
    identity.put(conn, identity.LAST_IMPORT_FROM, source_label)

    digest_after = identity.fleet_digest(conn)
    message = (f"Imported {len(new)} snapshot(s) from {source_label}"
               f"{' (interleaved into existing history)' if interleaved else ''}. "
               f"{written:,} device rows written, {compacted:,} redundant rows removed. "
               f"Fleet digest is now {identity.short(digest_after)}.")
    if unknown:
        message += (" Fields this version does not store were ignored: "
                    + ", ".join(unknown[:6]) + ".")

    return ImportResult(
        status="imported", message=message, manifest=data,
        snapshots_in_bundle=len(incoming), snapshots_new=len(new), device_rows=written,
        interleaved=interleaved, renumbered=renumbered, compacted=compacted,
        digest_before=digest_before, digest_after=digest_after)
