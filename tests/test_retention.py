"""Retention: keep the database bounded without destroying change history."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from ota_analytics import ingest, registry, retention
from tests.conftest import device

NOW = datetime(2026, 8, 15, 12, 0)


from tests.conftest import HEADERS


def ingest_at(conn, make_export, when: datetime, firmware="7.5.0.51A", imei="111"):
    """Ingest a snapshot dated `when` in which the device itself has not changed.

    Two constraints pull against each other here:

    * ingest is idempotent on file *content*, so byte-identical exports collapse into one
      snapshot and the fixture would silently test nothing;
    * device_snapshot now stores a row only when a device actually differs, and a snapshot that
      owns rows cannot be pruned without rewriting history.

    So the bytes vary in a column the mapper ignores. That is exactly the real case retention
    exists for: a fetch that found nothing new.
    """
    name = f"Devices_1_{when.strftime('%d%b%y_%H%M')}.xlsx"
    row = device(imei, firmware=firmware) + [when.isoformat()]
    return ingest.ingest_file(
        conn, make_export([row], name=name, headers=HEADERS + ["Fetch note"]))


def test_recent_snapshots_are_all_kept(conn, make_export):
    """Inside the first tier nothing is thinned — recent detail is what people look at."""
    for hours_ago in (1, 3, 6, 12, 24, 36):
        ingest_at(conn, make_export, NOW - timedelta(hours=hours_ago))

    keep, remove = retention.plan(conn, now=NOW)
    assert remove == []
    assert len(keep) == 6


def test_older_snapshots_are_thinned_to_one_per_hour(conn, make_export):
    """Several pulls inside one hour, five days old, collapse to one survivor."""
    base = NOW - timedelta(days=5)
    for minutes in (0, 10, 20, 30, 40, 50):
        ingest_at(conn, make_export, base + timedelta(minutes=minutes))
    ingest_at(conn, make_export, NOW - timedelta(hours=1))     # a recent one to be newest

    keep, remove = retention.plan(conn, now=NOW)
    assert remove, "old duplicates within the hour should be thinned"

    # The contract is the property, not a count: at most one old snapshot survives per hour.
    kept_times = [datetime.fromisoformat(r["snapshot_at"]) for r in conn.execute(
        f"SELECT snapshot_at FROM snapshot WHERE id IN ({','.join('?' * len(keep))})", keep)]
    old_buckets = [t.replace(minute=0, second=0) for t in kept_times
                   if (NOW - t).days >= 2]
    assert len(old_buckets) == len(set(old_buckets))


def test_a_snapshot_that_recorded_a_change_is_never_pruned(conn, make_export):
    """Thinning must not erase the evidence of an upgrade."""
    base = NOW - timedelta(days=5)
    ingest_at(conn, make_export, base + timedelta(minutes=0), firmware="7.5.0.27")
    ingest_at(conn, make_export, base + timedelta(minutes=10), firmware="7.5.0.27")
    changed = ingest_at(conn, make_export, base + timedelta(minutes=20), firmware="7.5.0.51A")
    ingest_at(conn, make_export, base + timedelta(minutes=30), firmware="7.5.0.51A")
    ingest_at(conn, make_export, NOW)
    keep, remove = retention.plan(conn, now=NOW)
    assert changed.snapshot_id in keep
    assert changed.snapshot_id not in remove


def test_newest_and_oldest_always_survive(conn, make_export):
    for days_ago in (400, 200, 100, 50, 5, 0):
        ingest_at(conn, make_export, NOW - timedelta(days=days_ago))

    keep, remove = retention.plan(conn, now=NOW)
    ids = [r["id"] for r in conn.execute("SELECT id FROM snapshot ORDER BY snapshot_at")]
    assert ids[0] in keep
    assert ids[-1] in keep


def test_pruning_never_destroys_a_device_reading(conn, make_export):
    """Thinning removes empty snapshots and must leave the device history intact.

    Under change-only storage a prunable snapshot owns no device rows by definition — that is
    what makes it prunable. The contract is therefore not "rows go away" but "the state a
    reader resolves is unchanged, and nothing is orphaned".
    """
    base = NOW - timedelta(days=30)
    for minutes in (0, 5, 10, 15):
        ingest_at(conn, make_export, base + timedelta(minutes=minutes))
    ingest_at(conn, make_export, NOW)

    before = conn.execute(
        "SELECT firmware, status FROM device_state WHERE imei = '111' "
        "ORDER BY snapshot_id DESC LIMIT 1").fetchone()

    result = retention.prune(conn, now=NOW, vacuum=False)
    assert result.removed > 0

    after = conn.execute(
        "SELECT firmware, status FROM device_state WHERE imei = '111' "
        "ORDER BY snapshot_id DESC LIMIT 1").fetchone()
    assert (after["firmware"], after["status"]) == (before["firmware"], before["status"])

    orphans = conn.execute("""
        SELECT COUNT(*) FROM device_snapshot d
        WHERE NOT EXISTS (SELECT 1 FROM snapshot s WHERE s.id = d.snapshot_id)
    """).fetchone()[0]
    assert orphans == 0


def test_dry_run_changes_nothing(conn, make_export):
    base = NOW - timedelta(days=30)
    for minutes in (0, 5, 10, 15):
        ingest_at(conn, make_export, base + timedelta(minutes=minutes))
    ingest_at(conn, make_export, NOW)

    before = conn.execute("SELECT COUNT(*) FROM snapshot").fetchone()[0]
    result = retention.prune(conn, dry_run=True, now=NOW, vacuum=False)
    after = conn.execute("SELECT COUNT(*) FROM snapshot").fetchone()[0]

    assert result.removed > 0        # it reports what it would do
    assert before == after           # and does none of it


def test_a_single_snapshot_is_left_alone(conn, make_export):
    ingest_at(conn, make_export, NOW)
    keep, remove = retention.plan(conn, now=NOW)
    assert remove == []
    assert len(keep) == 1


def test_only_devices_that_moved_are_recorded(conn, make_export):
    """Storage scales with changes, not with fetches × devices — the whole point."""
    ingest.ingest_file(conn, make_export(
        [device("moved", firmware="7.5.0.27"), device("still", firmware="7.5.0.51A"),
         device("also_still", firmware="7.5.0.51A")], name="Devices_3_15Aug26_1000.xlsx"))
    ingest.ingest_file(conn, make_export(
        [device("moved", firmware="7.5.0.51A"), device("still", firmware="7.5.0.51A"),
         device("also_still", firmware="7.5.0.51A")], name="Devices_3_16Aug26_1000.xlsx"))

    rows = conn.execute(
        "SELECT imei, field, old_value, new_value FROM device_change").fetchall()
    assert [r["imei"] for r in rows] == ["moved"]
    assert (rows[0]["old_value"], rows[0]["new_value"]) == ("7.5.0.27", "7.5.0.51A")

    # The devices that did not move are still tracked — just not re-recorded.
    assert conn.execute("SELECT COUNT(*) FROM device").fetchone()[0] == 3


def test_a_status_flip_alone_is_not_a_change(conn, make_export):
    """Status is a 24h recency bucket, so it flips whenever a device sleeps or wakes.

    Treating that as movement would record ordinary fleet behaviour hundreds of times a day.
    Online/offline counts are kept per snapshot, so nothing is lost by leaving it out.
    """
    ingest.ingest_file(conn, make_export(
        [device("a", status="Online", seen_at="15-08-26 09:00:00")],
        name="Devices_1_15Aug26_1000.xlsx"))
    ingest.ingest_file(conn, make_export(
        [device("a", status="Offline", seen_at="15-08-26 09:00:00")],
        name="Devices_1_16Aug26_1000.xlsx"))

    assert conn.execute("SELECT COUNT(*) FROM device_change").fetchone()[0] == 0
    assert conn.execute(
        "SELECT status FROM device WHERE imei = 'a'").fetchone()["status"] == "Offline"


def test_last_checked_advances_even_when_nothing_changed(conn, make_export):
    """The distinction that makes the registry worth having."""
    ingest.ingest_file(conn, make_export(
        [device("a", seen_at="15-08-26 09:00:00")], name="Devices_1_15Aug26_1000.xlsx"))
    ingest.ingest_file(conn, make_export(
        [device("a", seen_at="16-08-26 09:00:00")], name="Devices_1_16Aug26_1000.xlsx"))

    row = conn.execute("SELECT last_checked_at, last_changed_at, checks FROM device").fetchone()
    assert row["checks"] == 2
    assert row["last_checked_at"].startswith("2026-08-16")
    assert row["last_changed_at"] is None

