"""Command line entry point: python -m ota_analytics.cli <command>"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import bundle, config, db, identity, ingest, registry, rollup


def _print_ingest_result(result: ingest.IngestResult) -> None:
    if result.status == "already_ingested":
        print(f"  skip   {result.path.name}  (already ingested as snapshot {result.snapshot_id})")
        return

    print(f"  ok     {result.path.name}")
    print(f"         snapshot {result.snapshot_id} @ {result.snapshot_at} "
          f"(from {result.ts_source})")
    print(f"         {result.rows:,} devices, {result.groups:,} group memberships, "
          f"{result.duration_ms / 1000:.1f}s")
    if result.skipped_no_imei or result.duplicate_imei:
        print(f"         skipped: {result.skipped_no_imei} without IMEI, "
              f"{result.duplicate_imei} duplicate")
    if result.unknown_columns:
        print(f"         unmapped columns: {', '.join(result.unknown_columns)}")
    high = [f for f in result.findings if f[1] == "high"]
    for rule, severity, affected, _sample, detail in high:
        print(f"         [{severity}] {rule}: {affected:,} — {detail}")


def cmd_ingest(args: argparse.Namespace) -> int:
    conn = db.connect()
    result = ingest.ingest_file(conn, Path(args.path))
    print(f"Ingesting {args.path}")
    _print_ingest_result(result)
    if result.status == "ingested":
        rollup.rollup_snapshot(conn, result.snapshot_id)
        print("         rolled up")
    return 0


def cmd_ingest_dir(args: argparse.Namespace) -> int:
    conn = db.connect()
    directory = Path(args.path or config.EXPORT_DIR)
    print(f"Ingesting exports from {directory}")
    results = ingest.ingest_dir(conn, directory)
    if not results:
        print("  no .xlsx files found")
        return 0
    for result in results:
        _print_ingest_result(result)
    rollup.rollup_all(conn)
    print(f"Rolled up {len(results)} snapshot(s)")
    return 0


def cmd_rollup(args: argparse.Namespace) -> int:
    conn = db.connect()
    count = rollup.rollup_all(conn)
    print(f"Rebuilt facts for {count} snapshot(s)")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    conn = db.connect()
    snapshots = conn.execute("""
        SELECT s.id, s.snapshot_at, s.source_file, s.row_count, s.ts_source, k.devices_online,
               k.devices_offline, k.devices_inactive, k.devices_never_tasked, k.devices_completed,
               k.devices_pending, k.pending_tasks_total, k.distinct_firmware, k.fragmentation,
               k.stale_30d
        FROM snapshot s LEFT JOIN fact_snapshot_kpi k ON k.snapshot_id = s.id
        ORDER BY s.snapshot_at
    """).fetchall()

    if not snapshots:
        print(f"No snapshots ingested yet. Database: {config.DB_PATH}")
        print(f"Try: python -m ota_analytics.cli ingest-dir \"{config.EXPORT_DIR}\"")
        return 0

    print(f"Database: {config.DB_PATH}")
    print(f"Snapshots: {len(snapshots)}\n")
    for s in snapshots:
        total = s["row_count"] or 0
        online = s["devices_online"] or 0
        print(f"  [{s['id']}] {s['snapshot_at']}  {s['source_file']}")
        print(f"        devices {total:,} | online {online:,} "
              f"({online / total:.1%})" if total else "")
        print(f"        tasks: never {s['devices_never_tasked']:,} | "
              f"completed {s['devices_completed']:,} | "
              f"pending {s['devices_pending']:,} devices "
              f"({s['pending_tasks_total']:,} tasks)")
        print(f"        firmware versions {s['distinct_firmware']} | "
              f"fragmentation {s['fragmentation']:.3f} | "
              f"not seen 30d+ {s['stale_30d']:,}")
        # 'api' timestamps are the moment of the pull, so they are exact; only a filename that
        # could not be parsed leaves us guessing from the file date.
        if s["ts_source"] == "mtime":
            print("        WARNING: snapshot time guessed from file date, not the filename")

    latest = snapshots[-1]["id"]
    issues = conn.execute("""
        SELECT rule, severity, affected, detail FROM quality_issue
        WHERE snapshot_id = ?
        ORDER BY CASE severity WHEN 'high' THEN 0 WHEN 'medium' THEN 1
                               WHEN 'low' THEN 2 ELSE 3 END, affected DESC
    """, (latest,)).fetchall()
    if issues:
        print(f"\nData quality (snapshot {latest}):")
        for i in issues:
            print(f"  {i['severity']:>6}  {i['rule']:<28} {i['affected']:>7,}  {i['detail']}")
    # Movement comes from the change log, which records each move individually — no snapshot
    # pair to pick, so nothing hides between two endpoints.
    reg = registry.summary(conn)
    print(f"\nDevice registry: {reg['devices']:,} devices, "
          f"{reg['change_rows']:,} recorded changes, last checked {reg['last_checked']}")

    moves = registry.movement_summary(conn)
    if moves["moves"]:
        print(f"\nFirmware moves today: {moves['moves']:,} across {moves['devices']:,} devices")
        print(f"  raised              {moves['upgrades']:>6,}")
        print(f"  lowered - planned   {moves['planned_downgrades']:>6,}")
        print(f"  lowered - unplanned {moves['unplanned_downgrades']:>6,}")
        print(f"  fell back to base   {moves['fallbacks']:>6,}")
    else:
        print("\nNo firmware moved today.")
    return 0


def cmd_quality(args: argparse.Namespace) -> int:
    conn = db.connect()
    row = conn.execute("SELECT id FROM snapshot ORDER BY snapshot_at DESC LIMIT 1").fetchone()
    if not row:
        print("No snapshots ingested yet.")
        return 1
    issues = conn.execute("""
        SELECT rule, severity, affected, sample, detail FROM quality_issue
        WHERE snapshot_id = ? ORDER BY affected DESC
    """, (row["id"],)).fetchall()
    for i in issues:
        print(f"[{i['severity']}] {i['rule']}: {i['affected']:,}")
        print(f"    {i['detail']}")
        sample = json.loads(i["sample"] or "[]")
        if sample:
            print(f"    e.g. {', '.join(sample[:5])}")
    return 0


def cmd_errors(args: argparse.Namespace) -> int:
    from . import errors

    conn = db.connect()
    if args.clear:
        print(f"Cleared {errors.clear(conn)} recorded error(s).")
        return 0

    entries = errors.recent(conn, limit=args.limit)
    if not entries:
        print("No errors recorded.")
        return 0
    for e in entries:
        print(f"{e['occurred_at']}  [{e['source']}] {e['path'] or ''}")
        print(f"    {e['error_type']}: {e['message']}")
    print(f"\nFull tracebacks: {errors.LOG_PATH}")
    return 0


def cmd_registry(args: argparse.Namespace) -> int:
    """One row per IMEI: current state, last checked, last changed."""
    from . import registry

    conn = db.connect()
    if args.registry_command == "rebuild":
        totals = registry.rebuild(conn)
        print(f"Rebuilt from {totals['snapshots']} snapshot(s): "
              f"{totals['new_devices']:,} devices, {totals['changes']:,} recorded changes")
        return 0

    if args.registry_command == "device":
        if not args.imei:
            print("error: --imei is required", file=sys.stderr)
            return 2
        data = registry.device_history(conn, args.imei)
        if not data["device"]:
            print(f"No device with IMEI {args.imei}")
            return 1
        d = data["device"]
        print(f"{d['imei']}: FW {d['firmware'] or '—'}  "
              f"last change {d['last_fw_change_at'] or 'never'}  "
              f"last check {d['last_checked_at']}")
        print(f"  model {d['device_model']} · hw {d['hw_ver']} · config {d['configuration']}")
        print(f"  status {d['status']} · task {d['queue_state']} · target {d['update_firmware']}")
        print(f"  seen by {d['checks']} fetches, {d['changes']} recorded changes")
        if data["history"]:
            print("\n  change log:")
            for h in data["history"]:
                print(f"    {h['changed_at']}  {h['field']:<16} "
                      f"{h['old_value'] or '—'} → {h['new_value'] or '—'}")
        return 0

    summary = registry.summary(conn)
    print(f"Devices tracked      {summary['devices']:,}")
    print(f"  ever changed       {summary['ever_changed']:,}")
    print(f"  recorded changes   {summary['change_rows']:,}")
    print(f"  total checks       {summary['total_checks']:,}")
    print(f"  first seen         {summary['first_seen']}")
    print(f"  last checked       {summary['last_checked']}")
    if summary["total_checks"]:
        saved = summary["total_checks"] - summary["devices"]
        print(f"\n  rows avoided by not re-storing unchanged devices: {saved:,}")
    return 0


def cmd_prune(args: argparse.Namespace) -> int:
    """Apply the retention policy so the database does not grow without bound."""
    from . import retention

    conn = db.connect()
    print("Retention policy:")
    for line in retention.describe_policy():
        print(f"  {line}")

    result = retention.prune(conn, dry_run=args.dry_run, vacuum=not args.no_vacuum)
    print(f"\nSnapshots: {result.examined} examined, {result.kept} kept, "
          f"{result.removed} {'would be removed' if args.dry_run else 'removed'}")
    print(f"  kept because they record a change: {result.kept_for_change}")
    print(f"  device rows {'to remove' if args.dry_run else 'removed'}: "
          f"{result.device_rows_removed:,}")
    if not args.dry_run and result.bytes_freed:
        print(f"  space reclaimed: {result.bytes_freed / 1024 / 1024:.1f} MB "
              f"({result.bytes_before / 1024 / 1024:.1f} -> "
              f"{result.bytes_after / 1024 / 1024:.1f} MB)")
    if args.dry_run:
        print("\n(dry run — nothing was deleted)")
    return 0


def _print_identity(data: dict) -> None:
    print(f"Instance      {data['instance_label']}")
    print(f"Database      {data['db_path']}")
    print(f"DB id         {data['db_id']}")
    print(f"Created       {data['created_at']}")
    print(f"Schema        v{data['schema_version']}")
    print()
    print(f"Fleet digest  {data['digest_short']}   ({data['fleet_digest'] or '—'})")
    print(f"Snapshots     {data['snapshots'] or 0:,}")
    print(f"Coverage      {data['first_snapshot_at'] or '—'}  ->  "
          f"{data['last_snapshot_at'] or '—'}")
    # Two clocks, deliberately not merged into one "last sync": snapshot_at is when the
    # platform's data was true, ingested_at is when this install pulled it. The first is what
    # decides the numbers.
    print(f"Last fetched  {data['last_ingest_at'] or '—'}   (when this install pulled)")
    if data.get("last_import_at"):
        print(f"Last import   {data['last_import_at']} from {data['last_import_from']}")
    if data.get("last_export_at"):
        print(f"Last export   {data['last_export_at']}")


def cmd_db_info(args: argparse.Namespace) -> int:
    """Identity and coverage — the numbers two people compare when their dashboards disagree."""
    conn = db.connect()
    if args.label:
        name = identity.set_instance_label(conn, args.label)
        print(f"This install is now called {name!r}.\n")

    data = identity.manifest(conn)
    _print_identity(data)

    if args.compare:
        other = bundle.describe(Path(args.compare))
        theirs = {**(other.get("source") or {}),
                  "snapshots": other["snapshot_count"],
                  "first_snapshot_at": other["first_snapshot_at"],
                  "last_snapshot_at": other["last_snapshot_at"]}
        print(f"\nCompared with {args.compare}:")
        print(f"  their instance   {theirs.get('instance_label') or '—'}")
        print(f"  their digest     {identity.short(theirs.get('fleet_digest'))}")
        verdict = identity.compare(data, theirs)
        if verdict["match"]:
            print("  MATCH — both sides hold the same snapshots and will report the same "
                  "numbers.")
        else:
            print("  DIFFERENT:")
            for reason in verdict["reasons"]:
                print(f"    - {reason}")
    if not data["snapshots"]:
        print("\nNo snapshots yet. The digest becomes meaningful after the first ingest.")
    return 0


def cmd_db_export(args: argparse.Namespace) -> int:
    """Write this install's snapshot history to a bundle another install can merge."""
    conn = db.connect()
    out = Path(args.out) if args.out else Path(bundle.suggested_filename(conn))
    if out.is_dir():
        out = out / bundle.suggested_filename(conn)

    result = bundle.export_bundle(conn, out, since=args.since, until=args.until)
    print(f"Wrote {result.path}")
    print(f"  snapshots     {result.snapshots:,}")
    print(f"  coverage      {result.first_snapshot_at}  ->  {result.last_snapshot_at}")
    print(f"  rows          {result.baseline_rows:,} baseline + {result.device_rows:,} changes")
    print(f"  size          {result.bytes_written / 1024 / 1024:.1f} MB")
    print(f"  fleet digest  {identity.short(result.digest)}")
    print("\nThe other side merges it with:")
    print(f"  python -m ota_analytics.cli db-import \"{result.path.name}\"")
    return 0


def cmd_db_import(args: argparse.Namespace) -> int:
    """Merge another install's snapshots into this database."""
    conn = db.connect()
    path = Path(args.path)

    if args.inspect:
        data = bundle.describe(path)
        source = data.get("source") or {}
        print(f"Bundle        {path.name}")
        print(f"  from        {source.get('instance_label') or '—'} "
              f"(db {(source.get('db_id') or '')[:8]})")
        print(f"  exported    {data.get('exported_at')}")
        print(f"  snapshots   {data['snapshot_count']:,}")
        print(f"  coverage    {data['first_snapshot_at']}  ->  {data['last_snapshot_at']}")
        print(f"  digest      {identity.short(source.get('fleet_digest'))}")
        return 0

    result = bundle.import_bundle(conn, path, allow_interleave=args.allow_interleave,
                                  dry_run=args.dry_run)
    print(result.message)
    if result.status == "refused":
        return 1
    if result.status == "imported" and not args.dry_run:
        print(f"\n  digest {identity.short(result.digest_before)} -> "
              f"{identity.short(result.digest_after)}")
        print(f"  snapshots now {identity.coverage(conn)['snapshots']:,}")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn
    print(f"Dashboard: http://{args.host}:{args.port}")
    uvicorn.run("ota_analytics.api:app", host=args.host, port=args.port, reload=args.reload)
    return 0


def cmd_target(args: argparse.Namespace) -> int:
    """Declare the correct firmware per model — compliance is never inferred."""
    from . import metrics
    conn = db.connect()

    if args.target_command == "list":
        rows = conn.execute("SELECT * FROM firmware_target ORDER BY device_model").fetchall()
        if not rows:
            print("No targets declared. Compliance and coverage-gap views stay empty until "
                  "at least one is set.")
            models = conn.execute("""
                SELECT device_model, COUNT(*) n FROM device_state
                WHERE snapshot_id = (SELECT id FROM snapshot ORDER BY snapshot_at DESC LIMIT 1)
                  AND device_model IS NOT NULL
                GROUP BY device_model ORDER BY n DESC
            """).fetchall()
            if models:
                print("\nModels in the latest snapshot:")
                for m in models:
                    print(f"  {m['device_model']:<16}{m['n']:>8,} devices")
            return 0
        for r in rows:
            state = "EOL" if r["eol"] else r["target_firmware"]
            print(f"  {r['device_model']:<16}{state or '—':<16}{r['note'] or ''}")
        return 0

    if not args.model:
        print("error: --model is required", file=sys.stderr)
        return 2
    if not args.eol and not args.firmware:
        print("error: pass --firmware VERSION, or --eol for a model with no further releases",
              file=sys.stderr)
        return 2

    metrics.set_target(conn, args.model, args.firmware, eol=args.eol, note=args.note)
    if args.eol:
        print(f"{args.model}: marked end-of-life — its devices count as compliant as they are")
    else:
        print(f"{args.model}: target firmware set to {args.firmware}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ota_analytics", description="InTouch OTA analytics warehouse")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("ingest", help="ingest one export file")
    p.add_argument("path")
    p.set_defaults(func=cmd_ingest)

    p = sub.add_parser("ingest-dir", help="ingest every export in a folder")
    p.add_argument("path", nargs="?", default=None)
    p.set_defaults(func=cmd_ingest_dir)

    p = sub.add_parser("rollup", help="rebuild all derived facts")
    p.set_defaults(func=cmd_rollup)

    p = sub.add_parser("status", help="show snapshots, KPIs and quality issues")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("quality", help="show data-quality findings for the latest snapshot")
    p.set_defaults(func=cmd_quality)

    p = sub.add_parser("errors", help="show what has failed")
    p.add_argument("--limit", type=int, default=30)
    p.add_argument("--clear", action="store_true")
    p.set_defaults(func=cmd_errors)

    p = sub.add_parser("registry", help="per-device state: last checked and last changed")
    p.add_argument("registry_command", choices=["summary", "rebuild", "device"],
                   nargs="?", default="summary")
    p.add_argument("--imei")
    p.set_defaults(func=cmd_registry)

    p = sub.add_parser("prune", help="apply the retention policy and reclaim space")
    p.add_argument("--dry-run", action="store_true", help="report what would go, delete nothing")
    p.add_argument("--no-vacuum", action="store_true", help="skip reclaiming file space")
    p.set_defaults(func=cmd_prune)

    p = sub.add_parser("db-info", help="identity, coverage and fleet digest of this database")
    p.add_argument("--label", help="name this install, e.g. 'shahbaz-laptop'")
    p.add_argument("--compare", metavar="BUNDLE",
                   help="say how this database differs from a bundle")
    p.set_defaults(func=cmd_db_info)

    p = sub.add_parser("db-export", help="write snapshot history to a shareable bundle")
    p.add_argument("--out", help="output file or folder (default: an auto-named file here)")
    p.add_argument("--since", help="only snapshots at or after this time (YYYY-MM-DD [HH:MM])")
    p.add_argument("--until", help="only snapshots at or before this time")
    p.set_defaults(func=cmd_db_export)

    p = sub.add_parser("db-import", help="merge another install's bundle into this database")
    p.add_argument("path")
    p.add_argument("--inspect", action="store_true", help="describe it, import nothing")
    p.add_argument("--dry-run", action="store_true", help="report what would happen")
    p.add_argument("--allow-interleave", action="store_true",
                   help="accept fetches dated BEFORE your newest one; rewrites the surrounding "
                        "snapshots and rebuilds the change log (minutes, pauses collection)")
    p.set_defaults(func=cmd_db_import)

    p = sub.add_parser("serve", help="run the dashboard")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--reload", action="store_true")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("passwd", help="hash a dashboard password for the service environment")
    p.add_argument("--role", choices=["admin", "viewer"], default="admin")
    p.set_defaults(func=cmd_passwd)

    p = sub.add_parser("target", help="declare the correct firmware per model")
    p.add_argument("target_command", choices=["set", "list"])
    p.add_argument("--model")
    p.add_argument("--firmware")
    p.add_argument("--eol", action="store_true",
                   help="model has no further releases; its devices are compliant as they are")
    p.add_argument("--note")
    p.set_defaults(func=cmd_target)
    return parser


def cmd_passwd(args) -> int:
    """Turn a password into the hash the service reads from its environment.

    Prompted rather than taken as an argument: a command line ends up in shell history and in
    `ps` output for every user on the box, which is the wrong place for the password that guards
    35,000 IMEIs.
    """
    import getpass

    from . import auth

    first = getpass.getpass(f"New {args.role} password: ")
    if not first:
        print("error: empty password", file=sys.stderr)
        return 2
    if first != getpass.getpass("Repeat: "):
        print("error: the two entries did not match", file=sys.stderr)
        return 2

    variable = auth.ENV_ADMIN_HASH if args.role == "admin" else auth.ENV_VIEWER_HASH
    print(f"\n{variable}={auth.hash_password(first)}\n")
    print("Put that line in the service EnvironmentFile (root-owned, chmod 600).")
    print("It is a hash, not the password — but it is still a verifier: do not commit it.")
    return 0


def command_names() -> set[str]:
    """Every subcommand this CLI accepts.

    Derived from the parser rather than listed again, so a command added above is reachable
    from the packaged executable without anyone remembering to update a second copy.
    """
    return {name for action in build_parser()._actions
            if isinstance(action, argparse._SubParsersAction)
            for name in action.choices}


def main(argv: list[str] | None = None) -> int:
    config.ensure_dirs()
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (ingest.IngestError, bundle.BundleError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())


