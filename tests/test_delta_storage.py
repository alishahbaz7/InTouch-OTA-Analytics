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


# ─── the last ping lives on its own (v23) ────────────────────────────────────
#
# On the live database 86.8% of all device rows (1.34 M of 1.58 M, 23-10-2026) differed from the
# row before them only in seen_at: a full copy of a device, written to record one timestamp.

def pinged(imei: str, at: str, **kwargs) -> dict:
    record = api_device(imei, **kwargs)
    record["seenAt"] = at
    return record


def plain(rows: dict) -> dict:
    return {imei: dict(row) for imei, row in rows.items()}


def test_a_fetch_where_only_pings_moved_stores_no_device_rows(conn):
    fetch(conn, [api_device(str(i)) for i in range(50)], when="2026-08-15 10:00:00", tag="a")
    moved = [pinged(str(i), "15-08-26 10:40:00") if i < 10 else api_device(str(i))
             for i in range(50)]
    second = fetch(conn, moved, when="2026-08-15 10:45:00", tag="b")

    assert stored(conn, second.snapshot_id) == 0
    assert second.seen_rows == 10
    state = resolved(conn, second.snapshot_id)
    assert state["3"]["seen_at"] == "2026-08-15 10:40:00"
    assert state["30"]["seen_at"] == "2026-08-15 10:00:00"


def test_a_ping_resolves_per_snapshot_like_any_other_change(conn):
    first = fetch(conn, [pinged("1", "15-08-26 09:00:00")], when="2026-08-15 10:00:00", tag="a")
    second = fetch(conn, [pinged("1", "15-08-26 10:30:00")], when="2026-08-15 11:00:00", tag="b")
    third = fetch(conn, [pinged("1", "15-08-26 10:30:00", firmware="8.0.0")],
                  when="2026-08-15 12:00:00", tag="c")

    assert resolved(conn, first.snapshot_id)["1"]["seen_at"] == "2026-08-15 09:00:00"
    assert resolved(conn, second.snapshot_id)["1"]["seen_at"] == "2026-08-15 10:30:00"
    # A firmware move with the ping unchanged: a device row, and no new ping row.
    assert (third.changed_rows, third.seen_rows) == (1, 0)
    assert resolved(conn, third.snapshot_id)["1"]["seen_at"] == "2026-08-15 10:30:00"
    assert resolved(conn, third.snapshot_id)["1"]["seen_age_hours"] == pytest.approx(1.5)


def test_the_newest_snapshot_kept_resolved_follows_the_pings(conn):
    """device_current advances from both tables — including a device that comes back on a
    ping from before it left, which has no ping row of its own in the fetch it returns in."""
    fetch(conn, [pinged("1", "15-08-26 09:00:00"), pinged("2", "15-08-26 08:00:00")],
          when="2026-08-15 10:00:00", tag="a")
    fetch(conn, [pinged("1", "15-08-26 10:30:00")], when="2026-08-15 11:00:00", tag="b")
    last = fetch(conn, [pinged("1", "15-08-26 11:30:00"), pinged("2", "15-08-26 08:00:00")],
                 when="2026-08-15 12:00:00", tag="c")

    now = {r["imei"]: dict(r) for r in conn.execute(
        "SELECT * FROM device_now WHERE snapshot_id = ?", (last.snapshot_id,))}
    assert now == plain(resolved(conn, last.snapshot_id))
    assert now["2"]["seen_at"] == "2026-08-15 08:00:00"


def test_pruning_carries_last_pings_forward(conn):
    """A pruned snapshot's ping moves to the survivor, like its device rows: without it, the
    newest snapshot would report the ping from before the pruned fetch."""
    from ota_analytics import retention

    fetch(conn, [pinged("1", "01-06-26 09:00:00")], when="2026-06-01 10:00:00", tag="a")
    fetch(conn, [pinged("1", "01-06-26 10:05:00")], when="2026-06-01 10:10:00", tag="b")
    fetch(conn, [pinged("1", "01-06-26 10:05:00")], when="2026-06-01 10:20:00", tag="c")
    newest = fetch(conn, [pinged("1", "01-06-26 10:05:00")],
                   when="2026-08-15 10:00:00", tag="d")

    removed = retention.prune(conn, now=datetime(2026, 8, 15, 12, 0), vacuum=False).removed
    assert removed == 2                                   # b and c share a's day
    assert resolved(conn, newest.snapshot_id)["1"]["seen_at"] == "2026-06-01 10:05:00"


V22_INDEXES = {
    "ix_ds_imei": "device_snapshot(imei)",
    "ix_ds_queue": "device_snapshot(snapshot_id, queue_state)",
    "ix_ds_model_fw": "device_snapshot(snapshot_id, device_model, firmware)",
    "ix_ds_status": "device_snapshot(snapshot_id, status)",
}


def to_v22(conn) -> None:
    """Lay a database out as 2.1.0 stored it: the last ping on every device row, a full copy of
    the device wherever only its ping moved, and the four indexes v23 drops."""
    from ota_analytics import retention

    columns = retention.device_columns(conn)
    conn.execute("ALTER TABLE device_snapshot ADD COLUMN seen_at TEXT")
    conn.execute("ALTER TABLE device_snapshot ADD COLUMN seen_age_hours REAL")
    conn.execute(f"""
        INSERT INTO device_snapshot (snapshot_id, {', '.join(columns)})
        SELECT k.snapshot_id, {', '.join('d.' + c for c in columns)}
        FROM device_seen k
        JOIN device_snapshot d
          ON d.imei = k.imei
         AND d.snapshot_id = (SELECT MAX(x.snapshot_id) FROM device_snapshot x
                              WHERE x.imei = k.imei AND x.snapshot_id <= k.snapshot_id)
        WHERE NOT EXISTS (SELECT 1 FROM device_snapshot e
                          WHERE e.snapshot_id = k.snapshot_id AND e.imei = k.imei)
    """)
    conn.execute("""
        UPDATE device_snapshot SET seen_at = (
            SELECT p.seen_at FROM device_seen p
            WHERE p.imei = device_snapshot.imei AND p.snapshot_id <= device_snapshot.snapshot_id
            ORDER BY p.snapshot_id DESC LIMIT 1)
        WHERE present = 1
    """)
    conn.execute("DELETE FROM device_seen")
    for name, target in V22_INDEXES.items():
        conn.execute(f"CREATE INDEX {name} ON {target}")
    conn.execute("PRAGMA user_version = 22")
    conn.commit()


def test_v23_converts_a_v22_database_without_changing_any_snapshot(tmp_path):
    """The upgrade the live install runs once. Every snapshot must resolve exactly as before,
    with the rows that recorded only a ping gone and the four indexes with them."""
    from ota_analytics import db

    path = tmp_path / "upgrade.db"
    conn = db.connect(path)
    fetch(conn, [pinged("1", "15-08-26 09:00:00"), pinged("2", "15-08-26 08:00:00"),
                 api_device("3")], when="2026-08-15 10:00:00", tag="a")
    fetch(conn, [pinged("1", "15-08-26 10:30:00"), pinged("2", "15-08-26 08:00:00")],
          when="2026-08-15 11:00:00", tag="b")                 # 3 leaves
    fetch(conn, [pinged("1", "15-08-26 11:30:00", firmware="8.0.0"),
                 pinged("2", "15-08-26 11:45:00"), api_device("3")],
          when="2026-08-15 12:00:00", tag="c")                 # 3 comes back
    fetch(conn, [pinged("1", "15-08-26 12:30:00", firmware="8.0.0"),
                 pinged("2", "15-08-26 11:45:00"), api_device("3")],
          when="2026-08-15 13:00:00", tag="d")
    ids = [r[0] for r in conn.execute("SELECT id FROM snapshot ORDER BY id")]
    expected = {sid: plain(resolved(conn, sid)) for sid in ids}
    split_rows = conn.execute("SELECT COUNT(*) FROM device_snapshot").fetchone()[0]

    to_v22(conn)
    v22_rows = conn.execute("SELECT COUNT(*) FROM device_snapshot").fetchone()[0]
    assert v22_rows > split_rows                     # the copies that only carried a ping
    assert {sid: plain(resolved(conn, sid)) for sid in ids} != expected   # view now sees no pings
    conn.close()

    conn = db.connect(path)                          # migrates: user_version 22 < 23
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    columns = {r["name"] for r in conn.execute("PRAGMA table_info(device_snapshot)")}
    assert not {"seen_at", "seen_age_hours"} & columns
    indexes = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'device_snapshot'")}
    assert not set(V22_INDEXES) & indexes
    assert conn.execute("SELECT COUNT(*) FROM device_snapshot").fetchone()[0] == split_rows
    assert {sid: plain(resolved(conn, sid)) for sid in ids} == expected
    now = {r["imei"]: dict(r) for r in conn.execute(
        "SELECT * FROM device_now WHERE snapshot_id = ?", (ids[-1],))}
    assert now == expected[ids[-1]]

    # The copy taken before the rewrite holds the 2.1.0 layout, untouched.
    backup = db.sqlite3.connect(path.with_name("upgrade.before-v23.db"))
    assert backup.execute("SELECT COUNT(*) FROM device_snapshot").fetchone()[0] == v22_rows
    assert backup.execute("PRAGMA user_version").fetchone()[0] == 22
    backup.close()
    conn.close()


def test_a_new_database_needs_no_copy_and_has_no_dropped_index(tmp_path):
    from ota_analytics import db

    conn = db.connect(tmp_path / "fresh.db")
    indexes = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert not set(V22_INDEXES) & indexes
    assert {"ix_ds_imei_snap", "ix_seen_snap"} <= indexes
    assert not list(tmp_path.glob("*.before-v23.db"))
    conn.close()
