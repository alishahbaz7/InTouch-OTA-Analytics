"""The fact tables a fetch is rolled up into.

On 09-11-2026 a fetch failed with `NOT NULL constraint failed: fact_fleet_version.stale_7d_count`.
The platform had listed one new device that had never pinged, with its own IMEI as its model — so
it formed a model/firmware/hardware group of one with no last ping in it, and `SUM(age > 168)`
over nothing but NULLs is NULL, not 0. Every earlier group had held at least one device that had
pinged. Each fetch after it stored its data and then failed here, leaving the history without
its totals.
"""

from __future__ import annotations

from ota_analytics import ingest, rollup
from tests.conftest import device


def facts(conn, snapshot_id: int) -> dict:
    return {r["device_model"]: dict(r) for r in conn.execute(
        "SELECT * FROM fact_fleet_version WHERE snapshot_id = ?", (snapshot_id,))}


def test_a_group_where_no_device_has_pinged_counts_zero_not_null(conn, make_export):
    result = ingest.ingest_file(conn, make_export([
        device("111"),
        # The live case: never pinged, and the platform's model field holds its IMEI.
        device("866685088278487", model="866685088278487", firmware="-", hw_ver="-",
               status="-", seen_at="-", queue="-"),
    ]))
    rollup.rollup_snapshot(conn, result.snapshot_id)

    lonely = facts(conn, result.snapshot_id)["866685088278487"]
    assert (lonely["device_count"], lonely["never_seen_count"]) == (1, 1)
    assert (lonely["stale_7d_count"], lonely["stale_30d_count"]) == (0, 0)


def test_a_fleet_where_no_device_has_pinged_still_rolls_up(conn, make_export):
    """The same arithmetic in the fleet-wide row: a first export of devices waiting to be
    activated has no last ping anywhere."""
    result = ingest.ingest_file(conn, make_export([
        device(str(n), status="-", seen_at="-", queue="-") for n in range(3)]))
    rollup.rollup_snapshot(conn, result.snapshot_id)

    kpi = conn.execute("SELECT * FROM fact_snapshot_kpi WHERE snapshot_id = ?",
                       (result.snapshot_id,)).fetchone()
    assert (kpi["devices_total"], kpi["never_seen"], kpi["stale_7d"], kpi["stale_30d"]) == \
        (3, 3, 0, 0)


def test_snapshots_left_without_totals_are_rolled_up_by_the_next_run(conn, make_export):
    """A fetch that stored its data and then failed to roll up leaves a gap in the history.
    The next run fills it, rather than leaving it for a full rebuild nobody knows to ask for."""
    first = ingest.ingest_file(conn, make_export([device("1")],
                                                 name="Devices_1_15Aug26_1000.xlsx"))
    second = ingest.ingest_file(conn, make_export([device("1"), device("2")],
                                                  name="Devices_2_15Aug26_1100.xlsx"))
    rollup.rollup_snapshot(conn, first.snapshot_id)          # second never got its totals

    assert rollup.rollup_missing(conn) == [second.snapshot_id]
    assert conn.execute("SELECT devices_total FROM fact_snapshot_kpi WHERE snapshot_id = ?",
                        (second.snapshot_id,)).fetchone()[0] == 2
    assert rollup.rollup_missing(conn) == []                  # nothing left to do
