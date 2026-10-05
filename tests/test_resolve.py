"""The fast snapshot resolver must agree with `device_state`, exactly, always.

`resolve.py` exists only because the view is slow; it is worth nothing if it is also different.
So the view stays in the schema as the readable statement of what resolving a snapshot *means*,
and these tests hold the fast form against it — every column, every snapshot, including the
awkward ones: a device that leaves, a device that comes back, a device that never changes again
after its first fetch.

A mismatch here would not look like a failure in the dashboard. It would look like plausible
numbers that are wrong, which is why this compares rows rather than counts.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from ota_analytics import current, ingest, resolve
from tests.conftest import HEADERS, device

NOW = datetime(2026, 8, 15, 12, 0)


def ingest_rows(conn, make_export, when: datetime, rows: list[list]):
    """Ingest one fetch, guaranteeing it is a distinct snapshot.

    Ingest is idempotent on file *bytes*, so a fetch that found nothing new would collapse into
    the previous snapshot and the fixture would silently stop testing the case it exists for —
    a snapshot that stores no rows of its own. So every row carries the fetch time in a column
    the mapper ignores, exactly as `test_retention.py` does. The device data is untouched.
    """
    name = f"Devices_{len(rows)}_{when.strftime('%d%b%y_%H%M')}.xlsx"
    stamped = [row + [when.isoformat()] for row in rows]
    return ingest.ingest_file(
        conn, make_export(stamped, name=name, headers=HEADERS + ["Fetch note"]))


@pytest.fixture
def history(conn, make_export):
    """A few fetches with every shape of change that resolution has to get right."""
    base = NOW - timedelta(hours=6)

    # 1: three devices arrive.
    ingest_rows(conn, make_export, base, [
        device("111", firmware="1.0.0"),
        device("222", firmware="1.0.0", model="AX1_SCAN"),
        device("333", firmware="1.0.0", status="Inactive", seen_at="", queue="-"),
    ])
    # 2: one moves, one is untouched, one drops off the platform entirely (tombstone).
    ingest_rows(conn, make_export, base + timedelta(hours=1), [
        device("111", firmware="1.1.0"),
        device("222", firmware="1.0.0", model="AX1_SCAN"),
    ])
    # 3: nothing at all changes — the common case, and the one that stores no rows.
    ingest_rows(conn, make_export, base + timedelta(hours=2), [
        device("111", firmware="1.1.0"),
        device("222", firmware="1.0.0", model="AX1_SCAN"),
    ])
    # 4: the missing device comes back, and a new one appears.
    ingest_rows(conn, make_export, base + timedelta(hours=3), [
        device("111", firmware="1.1.0"),
        device("222", firmware="1.2.0", model="AX1_SCAN"),
        device("333", firmware="1.0.0"),
        device("444", firmware="2.0.0", groups="north,fleet-a"),
    ])
    # 5: a fallback — 111 drops back to a version it has run before.
    ingest_rows(conn, make_export, base + timedelta(hours=4), [
        device("111", firmware="1.0.0"),
        device("222", firmware="1.2.0", model="AX1_SCAN"),
        device("333", firmware="1.0.0"),
        device("444", firmware="2.0.0", groups="north,fleet-a"),
    ])
    return [r["id"] for r in conn.execute("SELECT id FROM snapshot ORDER BY id")]


def view_rows(conn, snapshot_id):
    return [tuple(r) for r in conn.execute(
        "SELECT snapshot_id, imei, status, queue, queue_state, device_name, created_by, "
        "device_model_raw, device_model, firmware_raw, firmware, fw_family, fw_sortkey, "
        "configuration, config_sortkey, update_firmware, base_firmware, target_config, "
        "base_config, seen_at, iccid, hw_ver, vin, vin_raw, groups_raw, first_ping, "
        "seen_age_hours FROM device_state WHERE snapshot_id = ? ORDER BY imei", (snapshot_id,))]


def resolved_rows(conn, snapshot_id):
    return [tuple(r) for r in conn.execute(
        resolve.select() + " ORDER BY d.imei", (snapshot_id,))]


def test_it_matches_the_view_on_every_snapshot(conn, history):
    """Every column, every snapshot. The view is the specification."""
    for snapshot_id in history:
        assert resolved_rows(conn, snapshot_id) == view_rows(conn, snapshot_id), (
            f"snapshot {snapshot_id} resolves differently through resolve.select() than "
            "through device_state")


def test_it_matches_the_view_after_a_device_leaves_and_returns(conn, history):
    """The tombstone cases specifically, since they are what a grouped join can get wrong.

    Device 333 is present at snapshot 1, absent at 2 and 3, back at 4. A resolver that takes
    each device's newest row without checking `present` would report it in all five.
    """
    present = {sid: {r[1] for r in resolved_rows(conn, sid)} for sid in history}
    assert "333" in present[history[0]]
    assert "333" not in present[history[1]], "a tombstoned device must not resolve"
    assert "333" not in present[history[2]], "and must stay gone while nothing says otherwise"
    assert "333" in present[history[3]], "and must come back when the platform lists it again"


def test_a_snapshot_that_stored_no_rows_still_resolves_the_whole_fleet(conn, history):
    """Snapshot 3 changed nothing, so it owns almost no rows of its own.

    This is the entire point of delta storage, and the thing a naive resolver breaks.
    """
    unchanged = history[2]
    stored = conn.execute("SELECT COUNT(*) FROM device_snapshot WHERE snapshot_id = ?",
                          (unchanged,)).fetchone()[0]
    assert stored < 2, "the fixture is meant to store nothing new here"
    assert len(resolved_rows(conn, unchanged)) == 2


def test_seen_age_is_measured_from_the_snapshot_it_is_asked_about(conn, history):
    """Not from now, and not from the snapshot the row was stored in.

    `seen_age_hours` is the one derived column, and storing it is forbidden precisely because it
    moves with the snapshot being resolved. A resolver that computed it against the wrong
    snapshot would age every carried-forward device incorrectly.
    """
    ages = {}
    for snapshot_id in history:
        for row in conn.execute(resolve.select(), (snapshot_id,)):
            if row["imei"] == "111":
                ages[snapshot_id] = row["seen_age_hours"]
    # The fixture's seen_at is fixed, so the age grows by an hour for each later snapshot.
    values = [ages[s] for s in history]
    assert all(b > a for a, b in zip(values, values[1:])), values


# ─── the ways a caller can ask for a snapshot ───────────────────────────────

def test_source_prefers_the_materialized_copy_when_it_holds_the_snapshot(conn, history):
    """The newest snapshot is kept resolved, and that is the free answer."""
    newest = history[-1]
    current.rebuild(conn, newest)
    assert resolve.source(conn, newest) == current.VIEW


def test_source_builds_a_temp_table_for_anything_else(conn, history):
    """An older snapshot is nobody's resolved copy, so it is materialized once and reused."""
    older = history[1]
    current.rebuild(conn, history[-1])
    assert resolve.source(conn, older) == "_snap"
    assert resolve.source(conn, older) == "_snap", "asking twice must not rebuild"

    held = conn.execute("SELECT DISTINCT snapshot_id FROM _snap").fetchall()
    assert [tuple(r) for r in held] == [(older,)]


def test_source_falls_back_to_the_view_when_no_snapshot_is_named(conn, history):
    """Slower, never wrong — the safe direction, and what makes invalidating cheap."""
    assert resolve.source(conn, None) == resolve.VIEW


def test_at_rewrites_the_table_name_and_leaves_the_query_alone(conn, history):
    """The name is swapped on the finished SQL; the text in the file is what runs."""
    newest = history[-1]
    current.rebuild(conn, newest)
    sql = "SELECT COUNT(*) FROM device_state WHERE snapshot_id = ? AND status = 'Online'"
    rewritten = resolve.at(conn, newest, sql)
    assert "device_state" not in rewritten
    assert "status = 'Online'" in rewritten, "the rewrite must not touch anything but the name"
    assert conn.execute(rewritten, (newest,)).fetchone()[0] == \
        conn.execute(sql, (newest,)).fetchone()[0]


def test_a_materialized_snapshot_answers_only_for_its_own_snapshot(conn, history):
    """Asking the held copy for a different snapshot returns nothing, never the wrong rows.

    This is what makes the whole scheme safe to bolt onto existing queries: every one of them
    keeps its own `WHERE snapshot_id = ?`, so the failure mode of a stale or mismatched
    materialization is an empty answer, which is visible, rather than a plausible wrong one.
    """
    newest, older = history[-1], history[1]
    current.rebuild(conn, newest)
    table = resolve.source(conn, newest)
    rows = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE snapshot_id = ?",
                        (older,)).fetchone()[0]
    assert rows == 0


def test_materialize_builds_the_indexes_it_is_asked_for(conn, history):
    resolve.materialize(conn, "_probe", history[-1], indexes=("imei", "device_model, firmware"))
    indexes = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_temp_master WHERE type = 'index' AND tbl_name = '_probe'")}
    assert indexes == {"ix_probe_0", "ix_probe_1"}
