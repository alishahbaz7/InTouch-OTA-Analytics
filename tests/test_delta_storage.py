"""device_snapshot stores changes, not fetches.

A fetch of a fixed fleet is almost entirely identical to the one before it — on the real
database, 16 of 17 fetches differed in roughly 150 of 35,475 devices. Storing a full copy each
time cost ~23 MB to record nothing, which at a 15-minute cadence is ~2.2 GB a day. These tests
pin the two halves of the fix: a fetch writes only what moved, and device_state reconstructs
any snapshot exactly as if the full copy were still there.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from ota_analytics import ingest
from tests.conftest import device


def stored(conn, snapshot_id: int) -> int:
    """Rows physically written for a snapshot — the storage cost of that fetch."""
    return conn.execute("SELECT COUNT(*) FROM device_snapshot WHERE snapshot_id = ?",
                        (snapshot_id,)).fetchone()[0]


def resolved(conn, snapshot_id: int) -> dict:
    """The fleet as that snapshot saw it, reconstructed from the change rows."""
    return {r["imei"]: r for r in conn.execute(
        "SELECT * FROM device_state WHERE snapshot_id = ?", (snapshot_id,))}


def fetch(conn, records: list[dict], *, when: str, tag: str):
    """Ingest an API-shaped fetch. `tag` stands in for the content hash so that two fetches
    carrying identical data still land as two snapshots — which is the case under test."""
    return ingest.ingest_records(
        conn, records, source_name=f"test:{tag}",
        snapshot_at=datetime.fromisoformat(when), fingerprint=tag)


def api_device(imei: str, *, firmware: str = "7.5.0.51A", status: str = "Online",
               queue: object = 0, configuration: str = "2.2.2") -> dict:
    return {"imei": imei, "status": status, "queue": queue, "deviceModel": "LOCAT140VB",
            "firmware": firmware, "configuration": configuration,
            "seenAt": "15-08-26 10:00:00", "iccid": "8991119018554142514",
            "hwVer": "1.2.0", "groups": "49A 7k"}


def test_an_unchanged_fetch_stores_nothing(conn):
    """The whole point: polling a fleet that did not move must not cost a row per device."""
    fleet = [api_device(str(i)) for i in range(50)]
    first = fetch(conn, fleet, when="2026-08-15 10:00:00", tag="a")
    second = fetch(conn, fleet, when="2026-08-15 10:15:00", tag="b")

    assert stored(conn, first.snapshot_id) == 50      # the base state has to be written once
    assert stored(conn, second.snapshot_id) == 0      # nothing moved, so nothing is stored
    assert second.rows == 50                          # the fetch still reported 50 devices
    assert second.changed_rows == 0


def test_an_unchanged_fetch_still_resolves_the_whole_fleet(conn):
    """Storing nothing must not mean showing nothing — the snapshot is still a full view."""
    fleet = [api_device(str(i)) for i in range(50)]
    fetch(conn, fleet, when="2026-08-15 10:00:00", tag="a")
    second = fetch(conn, fleet, when="2026-08-15 10:15:00", tag="b")

    state = resolved(conn, second.snapshot_id)
    assert len(state) == 50
    assert state["7"]["firmware"] == "7.5.0.51A"


def test_only_the_device_that_moved_is_stored(conn):
    fleet = [api_device(str(i)) for i in range(50)]
    first = fetch(conn, fleet, when="2026-08-15 10:00:00", tag="a")

    moved = [api_device(str(i)) for i in range(50)]
    moved[7]["firmware"] = "7.5.0.60"
    second = fetch(conn, moved, when="2026-08-15 10:15:00", tag="b")

    assert stored(conn, second.snapshot_id) == 1
    assert resolved(conn, second.snapshot_id)["7"]["firmware"] == "7.5.0.60"
    # ...and the earlier snapshot still reports what it saw at the time.
    assert resolved(conn, first.snapshot_id)["7"]["firmware"] == "7.5.0.51A"


def test_history_survives_many_unchanged_fetches_in_between(conn):
    """The value must resolve correctly across a long gap with no stored rows."""
    fleet = [api_device("1")]
    first = fetch(conn, fleet, when="2026-08-15 10:00:00", tag="a")
    for n in range(6):
        fetch(conn, fleet, when=f"2026-08-15 1{n}:30:00", tag=f"idle{n}")

    upgraded = [api_device("1", firmware="8.0.0")]
    last = fetch(conn, upgraded, when="2026-08-15 20:00:00", tag="z")

    assert resolved(conn, first.snapshot_id)["1"]["firmware"] == "7.5.0.51A"
    assert resolved(conn, last.snapshot_id)["1"]["firmware"] == "8.0.0"


def test_a_device_that_disappears_is_tombstoned_not_forgotten(conn):
    """Without an explicit marker, "gone" and "unchanged" are the same absence of a row."""
    first = fetch(conn, [api_device("1"), api_device("2")],
                  when="2026-08-15 10:00:00", tag="a")
    second = fetch(conn, [api_device("1")], when="2026-08-15 10:15:00", tag="b")

    assert set(resolved(conn, first.snapshot_id)) == {"1", "2"}
    assert set(resolved(conn, second.snapshot_id)) == {"1"}


def test_a_device_that_comes_back_is_live_again(conn):
    fetch(conn, [api_device("1"), api_device("2")], when="2026-08-15 10:00:00", tag="a")
    fetch(conn, [api_device("1")], when="2026-08-15 10:15:00", tag="b")
    third = fetch(conn, [api_device("1"), api_device("2")],
                  when="2026-08-15 10:30:00", tag="c")

    assert set(resolved(conn, third.snapshot_id)) == {"1", "2"}


def test_seen_age_hours_is_derived_not_stored(conn):
    """Storing it was what defeated compaction: it is snapshot_at minus seen_at, so it differed
    on every row of every fetch. Two fetches of an unmoved fleet must still report it moving."""
    fleet = [api_device("1")]
    first = fetch(conn, fleet, when="2026-08-15 12:00:00", tag="a")
    second = fetch(conn, fleet, when="2026-08-15 18:00:00", tag="b")

    assert stored(conn, second.snapshot_id) == 0
    early = resolved(conn, first.snapshot_id)["1"]["seen_age_hours"]
    later = resolved(conn, second.snapshot_id)["1"]["seen_age_hours"]
    assert later == pytest.approx(early + 6, abs=0.01)


def test_a_field_the_source_never_sends_is_kept_not_wiped(conn, make_export):
    """The platform API carries no group information at all.

    Treating that absence as NULL wiped the groups of 29,384 devices on the first API fetch and
    every one after it — and group is one of the few dimensions available for explaining why a
    set of devices reverted. Absent means unknown, so the value on record stands.
    """
    ingest.ingest_file(conn, make_export(
        [device("111", groups="49A 7k")], name="Devices_1_15Aug26_1000.xlsx"))

    from_api = {"imei": "111", "deviceModel": "LOCAT140VB", "firmware": "7.5.0.51A",
                "seenAt": "15-08-26 11:00:00"}          # note: no group field of any kind
    assert "groups" not in from_api
    api = fetch(conn, [from_api], when="2026-08-15 11:05:00", tag="api1")

    assert resolved(conn, api.snapshot_id)["111"]["groups_raw"] == "49A 7k"


def test_a_field_the_source_does_send_still_updates(conn, make_export):
    """The carry-forward must not become a rule that values can never be cleared."""
    ingest.ingest_file(conn, make_export(
        [device("111", groups="49A 7k")], name="Devices_1_15Aug26_1000.xlsx"))
    second = ingest.ingest_file(conn, make_export(
        [device("111", groups="-")], name="Devices_1_16Aug26_1000.xlsx"))

    # '-' is the export's null marker, and this source did send the column — so it clears.
    assert resolved(conn, second.snapshot_id)["111"]["groups_raw"] is None


def test_switching_source_does_not_rewrite_the_whole_fleet(conn, make_export):
    """Fields the new source lacks must not make every device look changed at the boundary."""
    fleet = [device(str(i), groups="49A 7k") for i in range(30)]
    ingest.ingest_file(conn, make_export(fleet, name="Devices_30_15Aug26_1000.xlsx"))

    api = fetch(conn, [{"imei": str(i), "deviceModel": "LOCAT140VB",
                        "firmware": "7.5.0.51A", "seenAt": "15-08-26 10:00:00"}
                       for i in range(30)], when="2026-08-15 10:15:00", tag="api1")

    assert stored(conn, api.snapshot_id) == 0
    assert resolved(conn, api.snapshot_id)["7"]["groups_raw"] == "49A 7k"


def test_pruning_a_snapshot_carries_its_changes_forward(conn):
    """Thinning costs time resolution, never facts: a dropped snapshot's change moves to the
    snapshot that survives it, rather than reverting those devices to an older value."""
    from ota_analytics import retention

    fetch(conn, [api_device("1", firmware="7.0.0")], when="2026-06-01 10:00:00", tag="a")
    fetch(conn, [api_device("1", firmware="7.5.0")], when="2026-06-01 10:10:00", tag="b")
    fetch(conn, [api_device("1", firmware="8.0.0")], when="2026-06-01 10:20:00", tag="c")
    newest = fetch(conn, [api_device("1", firmware="8.0.0")],
                   when="2026-08-15 10:00:00", tag="d")

    retention.prune(conn, now=datetime(2026, 8, 15, 12, 0), vacuum=False)

    # Whatever survived, the newest snapshot must still know the device reached 8.0.0.
    assert resolved(conn, newest.snapshot_id)["1"]["firmware"] == "8.0.0"
