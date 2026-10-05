"""The newest snapshot, kept resolved.

`device_state` is a view, and resolving it grows with the number of snapshots stored: on the
real 245-snapshot database a single reference cost 4.5s and materializing it per request cost
10.9s — paid on every page load, because each request opens its own connection. Keeping the
newest snapshot resolved removes that from the request path entirely.

The risk that buys is a copy that disagrees with the source. These tests exist mostly to pin
that it cannot: whatever the materialization returns must equal what the view returns.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from ota_analytics import current, ingest, metrics

COMPARE = ("imei", "status", "queue", "queue_state", "device_model", "firmware",
           "configuration", "seen_at", "iccid", "hw_ver", "vin", "groups_raw")


def fetch(conn, records, *, when: str, tag: str):
    return ingest.ingest_records(conn, records, source_name=f"test:{tag}",
                                 snapshot_at=datetime.fromisoformat(when), fingerprint=tag)


def api_device(imei: str, *, firmware="7.5.0.51A", status="Online", queue=0,
               configuration="2.2.2") -> dict:
    return {"imei": imei, "status": status, "queue": queue, "deviceModel": "LOCAT140VB",
            "firmware": firmware, "configuration": configuration,
            "seenAt": "15-08-26 10:00:00", "iccid": "8991119018554142514",
            "hwVer": "1.2.0", "groups": "49A 7k"}


def rows(conn, table: str, snapshot_id: int) -> list[tuple]:
    return conn.execute(f"SELECT {', '.join(COMPARE)} FROM {table} "
                        f"WHERE snapshot_id = ? ORDER BY imei", (snapshot_id,)).fetchall()


def assert_agrees(conn, snapshot_id: int) -> None:
    """The materialization and the view must be indistinguishable. This is the whole contract."""
    assert rows(conn, "device_now", snapshot_id) == rows(conn, "device_state", snapshot_id)


# ─── it keeps up with ingest ────────────────────────────────────────────────

def test_the_first_fetch_is_resolved(conn):
    first = fetch(conn, [api_device(str(i)) for i in range(20)],
                  when="2026-08-15 10:00:00", tag="a")
    assert current.held(conn) == first.snapshot_id
    assert_agrees(conn, first.snapshot_id)


def test_it_advances_when_a_device_changes(conn):
    fetch(conn, [api_device(str(i)) for i in range(20)], when="2026-08-15 10:00:00", tag="a")

    moved = [api_device(str(i)) for i in range(20)]
    moved[7]["firmware"] = "8.0.0"
    second = fetch(conn, moved, when="2026-08-15 10:15:00", tag="b")

    assert current.held(conn) == second.snapshot_id
    assert_agrees(conn, second.snapshot_id)
    held = dict(conn.execute("SELECT imei, firmware FROM device_current WHERE imei = '7'"
                             ).fetchone())
    assert held["firmware"] == "8.0.0"


def test_it_advances_when_nothing_changes(conn):
    """An unchanged fetch stores no rows at all, and the materialization must still move on —
    otherwise every later read falls back to the slow path for good."""
    fleet = [api_device(str(i)) for i in range(20)]
    fetch(conn, fleet, when="2026-08-15 10:00:00", tag="a")
    second = fetch(conn, fleet, when="2026-08-15 10:15:00", tag="b")

    assert current.held(conn) == second.snapshot_id
    assert_agrees(conn, second.snapshot_id)


def test_a_departed_device_is_dropped(conn):
    fetch(conn, [api_device("1"), api_device("2")], when="2026-08-15 10:00:00", tag="a")
    second = fetch(conn, [api_device("1")], when="2026-08-15 10:15:00", tag="b")

    assert {r[0] for r in conn.execute("SELECT imei FROM device_current")} == {"1"}
    assert_agrees(conn, second.snapshot_id)


def test_a_device_that_comes_back_is_restored(conn):
    fetch(conn, [api_device("1"), api_device("2")], when="2026-08-15 10:00:00", tag="a")
    fetch(conn, [api_device("1")], when="2026-08-15 10:15:00", tag="b")
    third = fetch(conn, [api_device("1"), api_device("2")], when="2026-08-15 10:30:00", tag="c")

    assert {r[0] for r in conn.execute("SELECT imei FROM device_current")} == {"1", "2"}
    assert_agrees(conn, third.snapshot_id)


def test_crossing_several_snapshots_at_once_lands_the_newest_value(conn):
    """A rebuild or an import can leave it several snapshots behind."""
    fetch(conn, [api_device("1", firmware="7.0.0")], when="2026-08-15 10:00:00", tag="a")
    fetch(conn, [api_device("1", firmware="7.5.0")], when="2026-08-15 10:15:00", tag="b")
    last = fetch(conn, [api_device("1", firmware="8.0.0")], when="2026-08-15 10:30:00", tag="c")

    current.invalidate(conn)
    current.advance(conn, last.snapshot_id)

    assert conn.execute("SELECT firmware FROM device_current").fetchone()[0] == "8.0.0"
    assert_agrees(conn, last.snapshot_id)


# ─── it is never used for the wrong snapshot ────────────────────────────────

def test_reading_an_older_snapshot_does_not_use_it(conn):
    first = fetch(conn, [api_device("1", firmware="7.0.0")], when="2026-08-15 10:00:00", tag="a")
    second = fetch(conn, [api_device("1", firmware="8.0.0")], when="2026-08-15 10:15:00", tag="b")

    assert metrics.snapshot_source(conn, second.snapshot_id) == current.VIEW
    assert metrics.snapshot_source(conn, first.snapshot_id) != current.VIEW
    # ...and the older snapshot still reports what it saw at the time.
    assert rows(conn, "device_state", first.snapshot_id)[0]["firmware"] == "7.0.0"


def test_an_empty_materialization_is_slow_not_wrong(conn):
    """Falling back to the view has to stay correct — it is the safety net for every path that
    rewrites history."""
    snap = fetch(conn, [api_device(str(i)) for i in range(20)],
                 when="2026-08-15 10:00:00", tag="a")
    current.invalidate(conn)

    assert current.held(conn) is None
    assert metrics.snapshot_source(conn, snap.snapshot_id) != current.VIEW
    assert metrics.kpis(conn, snap.snapshot_id)["devices_total"] == 20


def test_the_kpis_agree_whichever_source_is_used(conn):
    snap = fetch(conn, [api_device(str(i)) for i in range(20)] +
                 [api_device("x", status="Offline", queue=3)],
                 when="2026-08-15 10:00:00", tag="a")

    with_materialization = metrics.kpis(conn, snap.snapshot_id)
    current.invalidate(conn)
    from_the_view = metrics.kpis(conn, snap.snapshot_id)

    assert with_materialization == from_the_view
