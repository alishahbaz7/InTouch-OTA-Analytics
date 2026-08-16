"""Sharing snapshot history between two installs.

The tests that matter here are the ones about *not* rewriting history. `device_snapshot` stores
one row per change, so merging is not appending rows — a foreign snapshot landing between two
local ones becomes the answer for every device that did not change locally in that gap. That
failure is silent: no error, no exception, just numbers nobody measured.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ota_analytics import bundle, db, identity, ingest, registry, retention, rollup

from tests.conftest import device


def firmware_at(conn, snapshot_at: str) -> dict:
    """Every device's firmware as of one snapshot, read the way the dashboard reads it."""
    row = conn.execute("SELECT id FROM snapshot WHERE snapshot_at = ?", (snapshot_at,)).fetchone()
    assert row is not None, f"no snapshot at {snapshot_at}"
    return {r["imei"]: r["firmware"] for r in conn.execute(
        "SELECT imei, firmware FROM device_state WHERE snapshot_id = ?", (row["id"],))}


def load(conn, paths):
    for path in paths:
        ingest.ingest_file(conn, path)
    rollup.rollup_all(conn)


@pytest.fixture
def exports(make_export):
    """Three fetches an hour apart, shaped to expose the merge hazard.

    Device 1 moves at every fetch, so it always has a row of its own. Device 2 is the
    interesting one: it reads 5.0.0 at 10:00 and again at 12:00, so delta storage writes no row
    for it at 12:00 — its value there is inherited from 10:00. In between, at 11:00, it briefly
    reads 5.5.0. Any merge that inserts the 11:00 fetch without making 12:00 self-sufficient
    first will have 12:00 answer 5.5.0 for a device whose own fetch said 5.0.0.
    """
    return {
        "10:00": make_export([device("1", firmware="1.0.0"), device("2", firmware="5.0.0")],
                             name="Devices_2_15Aug26_1000.xlsx"),
        "11:00": make_export([device("1", firmware="1.1.0"), device("2", firmware="5.5.0")],
                             name="Devices_2_15Aug26_1100.xlsx"),
        "12:00": make_export([device("1", firmware="1.2.0"), device("2", firmware="5.0.0")],
                             name="Devices_2_15Aug26_1200.xlsx"),
    }


# ─── round trip ─────────────────────────────────────────────────────────────

def test_export_import_reproduces_every_snapshot(tmp_path: Path, exports):
    """A bundle replayed into an empty database gives the same answers as the original."""
    source = db.connect(tmp_path / "source.db")
    load(source, exports.values())

    path = tmp_path / f"share{bundle.SUFFIX}"
    bundle.export_bundle(source, path)

    target = db.connect(tmp_path / "target.db")
    result = bundle.import_bundle(target, path)
    assert result.status == "imported"
    assert result.snapshots_new == 3

    for at in ("2026-08-15 10:00:00", "2026-08-15 11:00:00", "2026-08-15 12:00:00"):
        assert firmware_at(target, at) == firmware_at(source, at), at
    assert identity.fleet_digest(target) == identity.fleet_digest(source)
    source.close()
    target.close()


def test_reimport_is_a_no_op(tmp_path: Path, exports):
    source = db.connect(tmp_path / "source.db")
    load(source, exports.values())
    path = tmp_path / f"share{bundle.SUFFIX}"
    bundle.export_bundle(source, path)

    target = db.connect(tmp_path / "target.db")
    bundle.import_bundle(target, path)
    digest = identity.fleet_digest(target)

    again = bundle.import_bundle(target, path)
    assert again.status == "already_present"
    assert identity.fleet_digest(target) == digest
    assert target.execute("SELECT COUNT(*) FROM snapshot").fetchone()[0] == 3
    source.close()
    target.close()


def test_append_merge_fills_a_gap(tmp_path: Path, exports):
    """The case this feature exists for: a colleague synced later, hand over the difference."""
    mine = db.connect(tmp_path / "mine.db")
    load(mine, [exports["10:00"]])

    theirs = db.connect(tmp_path / "theirs.db")
    load(theirs, exports.values())

    path = tmp_path / f"gap{bundle.SUFFIX}"
    bundle.export_bundle(theirs, path, since="2026-08-15 11:00:00")

    result = bundle.import_bundle(mine, path)
    assert result.status == "imported"
    assert result.interleaved is False
    assert identity.fleet_digest(mine) == identity.fleet_digest(theirs)
    assert firmware_at(mine, "2026-08-15 12:00:00") == firmware_at(theirs, "2026-08-15 12:00:00")
    mine.close()
    theirs.close()


# ─── the interleave hazard ──────────────────────────────────────────────────

def test_interleave_is_refused_by_default(tmp_path: Path, exports):
    mine = db.connect(tmp_path / "mine.db")
    load(mine, [exports["10:00"], exports["12:00"]])

    theirs = db.connect(tmp_path / "theirs.db")
    load(theirs, [exports["11:00"]])
    path = tmp_path / f"middle{bundle.SUFFIX}"
    bundle.export_bundle(theirs, path)

    before = identity.fleet_digest(mine)
    result = bundle.import_bundle(mine, path)
    assert result.status == "refused"
    assert result.interleaved is True
    assert identity.fleet_digest(mine) == before
    assert mine.execute("SELECT COUNT(*) FROM snapshot").fetchone()[0] == 2
    mine.close()
    theirs.close()


def test_interleave_does_not_rewrite_the_snapshots_around_it(tmp_path: Path, exports):
    """The whole reason merging is not just an INSERT.

    Device 2 has no row of its own at 12:00, because it read the same value there as at 10:00.
    Slotting an 11:00 fetch in between makes that 11:00 row the most recent one at or before
    12:00, so a naive merge has device 2 reporting 5.5.0 at a snapshot whose own fetch said
    5.0.0 — wrong, and silent. Removing the densify step ahead of the insert makes this fail.
    """
    mine = db.connect(tmp_path / "mine.db")
    load(mine, [exports["10:00"], exports["12:00"]])
    before = {at: firmware_at(mine, at)
              for at in ("2026-08-15 10:00:00", "2026-08-15 12:00:00")}
    assert before["2026-08-15 12:00:00"]["2"] == "5.0.0"    # inherited, no row of its own

    theirs = db.connect(tmp_path / "theirs.db")
    load(theirs, [exports["11:00"]])
    path = tmp_path / f"middle{bundle.SUFFIX}"
    bundle.export_bundle(theirs, path)

    result = bundle.import_bundle(mine, path, allow_interleave=True)
    assert result.status == "imported"
    assert result.interleaved is True

    # The snapshots that were already here must answer exactly as they did before.
    for at, expected in before.items():
        assert firmware_at(mine, at) == expected, at
    # And the inserted one answers as its own install did.
    assert firmware_at(mine, "2026-08-15 11:00:00") == firmware_at(theirs, "2026-08-15 11:00:00")
    mine.close()
    theirs.close()


def test_interleave_leaves_ids_in_chronological_order(tmp_path: Path, exports):
    """device_state resolves by comparing snapshot ids, so id order is a correctness property."""
    mine = db.connect(tmp_path / "mine.db")
    load(mine, [exports["10:00"], exports["12:00"]])

    theirs = db.connect(tmp_path / "theirs.db")
    load(theirs, [exports["11:00"]])
    path = tmp_path / f"middle{bundle.SUFFIX}"
    bundle.export_bundle(theirs, path)
    bundle.import_bundle(mine, path, allow_interleave=True)

    rows = mine.execute("SELECT id, snapshot_at FROM snapshot ORDER BY snapshot_at").fetchall()
    assert [r["id"] for r in rows] == sorted(r["id"] for r in rows)
    assert retention.is_chronological(mine)
    mine.close()
    theirs.close()


def test_interleave_replays_the_change_log(tmp_path: Path, exports):
    """A fetch inserted into the middle changes what moved and when, so the log is rebuilt."""
    mine = db.connect(tmp_path / "mine.db")
    load(mine, [exports["10:00"], exports["12:00"]])
    moves = mine.execute("SELECT COUNT(*) FROM device_change WHERE field = 'firmware' "
                         "AND imei = '1'").fetchone()[0]
    assert moves == 1                          # 1.0.0 -> 1.2.0, the intermediate step unseen

    theirs = db.connect(tmp_path / "theirs.db")
    load(theirs, [exports["11:00"]])
    path = tmp_path / f"middle{bundle.SUFFIX}"
    bundle.export_bundle(theirs, path)
    bundle.import_bundle(mine, path, allow_interleave=True)

    steps = [(r["old_value"], r["new_value"]) for r in mine.execute(
        "SELECT old_value, new_value FROM device_change WHERE field = 'firmware' "
        "AND imei = '1' ORDER BY changed_at")]
    assert steps == [("1.0.0", "1.1.0"), ("1.1.0", "1.2.0")]
    mine.close()
    theirs.close()


def test_appending_folds_the_registry_without_replaying_it(tmp_path: Path, exports):
    """An append-only merge skips the full replay, so it must produce the same log without it.

    The replay is ~86% of an import's cost on the real database — 190s of 220s for 26
    snapshots — and appending newer fetches cannot change what already happened. That is only
    a safe shortcut if the result is indistinguishable, so it is checked rather than assumed.
    """
    mine = db.connect(tmp_path / "mine.db")
    load(mine, [exports["10:00"]])
    theirs = db.connect(tmp_path / "theirs.db")
    load(theirs, exports.values())

    path = tmp_path / f"gap{bundle.SUFFIX}"
    bundle.export_bundle(theirs, path, since="2026-08-15 11:00:00")
    bundle.import_bundle(mine, path)

    query = ("SELECT imei, changed_at, field, old_value, new_value FROM device_change "
             "ORDER BY changed_at, imei, field")
    folded = [tuple(r) for r in mine.execute(query)]
    assert folded, "the merge recorded no changes at all"

    registry.rebuild(mine)
    assert [tuple(r) for r in mine.execute(query)] == folded
    mine.close()
    theirs.close()


def test_two_installs_converge_on_the_same_digest(tmp_path: Path, exports):
    """The point of the whole exercise: swap bundles, end up provably in sync."""
    mine = db.connect(tmp_path / "mine.db")
    load(mine, [exports["10:00"], exports["11:00"]])
    theirs = db.connect(tmp_path / "theirs.db")
    load(theirs, [exports["11:00"], exports["12:00"]])
    assert identity.fleet_digest(mine) != identity.fleet_digest(theirs)

    mine_bundle = tmp_path / f"mine{bundle.SUFFIX}"
    theirs_bundle = tmp_path / f"theirs{bundle.SUFFIX}"
    bundle.export_bundle(mine, mine_bundle)
    bundle.export_bundle(theirs, theirs_bundle)

    bundle.import_bundle(mine, theirs_bundle)                        # append: 12:00
    bundle.import_bundle(theirs, mine_bundle, allow_interleave=True)  # interleave: 10:00

    assert identity.fleet_digest(mine) == identity.fleet_digest(theirs)
    for at in ("2026-08-15 10:00:00", "2026-08-15 11:00:00", "2026-08-15 12:00:00"):
        assert firmware_at(mine, at) == firmware_at(theirs, at), at
    mine.close()
    theirs.close()


# ─── reading and refusing ───────────────────────────────────────────────────

def test_describe_reads_without_importing(tmp_path: Path, exports):
    source = db.connect(tmp_path / "source.db")
    load(source, exports.values())
    identity.set_instance_label(source, "shahbaz-laptop")
    path = tmp_path / f"share{bundle.SUFFIX}"
    bundle.export_bundle(source, path)

    data = bundle.describe(path)
    assert data["snapshot_count"] == 3
    assert data["source"]["instance_label"] == "shahbaz-laptop"
    assert data["first_snapshot_at"].startswith("2026-08-15 10:00")
    source.close()


def test_a_non_bundle_is_refused(tmp_path: Path, conn, exports):
    """The usual failure is a downloaded error page, not a damaged archive."""
    junk = tmp_path / "login.html"
    junk.write_text("<html>Please sign in</html>", encoding="utf-8")
    with pytest.raises(bundle.BundleError, match="not a bundle"):
        bundle.import_bundle(conn, junk)

    # A real zip that is not a bundle is refused just as clearly.
    with pytest.raises(bundle.BundleError, match="not a bundle"):
        bundle.import_bundle(conn, exports["10:00"])


def test_an_unknown_format_is_refused_rather_than_guessed(tmp_path: Path, conn, exports):
    import json
    import zipfile

    source = db.connect(tmp_path / "source.db")
    load(source, [exports["10:00"]])
    path = tmp_path / f"share{bundle.SUFFIX}"
    bundle.export_bundle(source, path)

    # Rewrite the manifest as a format from the future.
    with zipfile.ZipFile(path) as archive:
        parts = {name: archive.read(name) for name in archive.namelist()}
    manifest = json.loads(parts[bundle.MANIFEST])
    manifest["format"] = bundle.FORMAT_VERSION + 1
    parts[bundle.MANIFEST] = json.dumps(manifest).encode()
    with zipfile.ZipFile(path, "w") as archive:
        for name, payload in parts.items():
            archive.writestr(name, payload)

    with pytest.raises(bundle.BundleError, match="format"):
        bundle.import_bundle(conn, path)
    source.close()


def test_dry_run_changes_nothing(tmp_path: Path, exports):
    source = db.connect(tmp_path / "source.db")
    load(source, exports.values())
    path = tmp_path / f"share{bundle.SUFFIX}"
    bundle.export_bundle(source, path)

    target = db.connect(tmp_path / "target.db")
    result = bundle.import_bundle(target, path, dry_run=True)
    assert result.snapshots_new == 3
    assert target.execute("SELECT COUNT(*) FROM snapshot").fetchone()[0] == 0
    source.close()
    target.close()


def test_export_since_limits_the_range(tmp_path: Path, exports):
    source = db.connect(tmp_path / "source.db")
    load(source, exports.values())
    path = tmp_path / f"tail{bundle.SUFFIX}"
    result = bundle.export_bundle(source, path, since="2026-08-15 11:00:00")
    assert result.snapshots == 2
    assert bundle.describe(path)["snapshot_count"] == 2
    source.close()


def test_export_with_no_snapshots_says_so(conn):
    with pytest.raises(bundle.BundleError, match="no snapshots"):
        bundle.export_bundle(conn, io_target := __import__("io").BytesIO())
    assert io_target.tell() == 0


# ─── groups survive the trip ────────────────────────────────────────────────

def test_group_membership_is_rebuilt_on_import(tmp_path: Path, make_export):
    """device_group is derived from groups_raw rather than shipped, so it must come back."""
    export = make_export([device("1", groups="fleet-a"), device("2", groups="fleet-b")],
                         name="Devices_2_15Aug26_1000.xlsx")
    source = db.connect(tmp_path / "source.db")
    load(source, [export])
    path = tmp_path / f"share{bundle.SUFFIX}"
    bundle.export_bundle(source, path)

    target = db.connect(tmp_path / "target.db")
    bundle.import_bundle(target, path)

    def groups(conn):
        return sorted((r["imei"], r["group_name"]) for r in
                      conn.execute("SELECT imei, group_name FROM device_group"))

    assert groups(target) == groups(source)
    source.close()
    target.close()


# ─── the physical operations merging relies on ──────────────────────────────

def test_densify_then_compact_is_a_round_trip(tmp_path: Path, make_export):
    """Materializing inherited rows and squeezing them back out must both be answer-preserving.

    Needs a device that sits still: with every device changing at every fetch the table is
    already dense and densify would have nothing to do.
    """
    stable = [make_export([device("1", firmware=fw), device("2", firmware="5.0.0")],
                          name=f"Devices_2_15Aug26_{hour}00.xlsx")
              for hour, fw in (("10", "1.0.0"), ("11", "1.1.0"), ("12", "1.2.0"))]
    conn = db.connect(tmp_path / "d.db")
    load(conn, stable)
    before = {at: firmware_at(conn, at) for at in
              ("2026-08-15 10:00:00", "2026-08-15 11:00:00", "2026-08-15 12:00:00")}
    rows_before = conn.execute("SELECT COUNT(*) FROM device_snapshot").fetchone()[0]

    for snapshot_id in [r["id"] for r in conn.execute("SELECT id FROM snapshot")]:
        retention.densify(conn, snapshot_id)
    assert conn.execute("SELECT COUNT(*) FROM device_snapshot").fetchone()[0] > rows_before
    for at, expected in before.items():
        assert firmware_at(conn, at) == expected, f"densify changed {at}"

    retention.compact(conn)
    assert conn.execute("SELECT COUNT(*) FROM device_snapshot").fetchone()[0] == rows_before
    for at, expected in before.items():
        assert firmware_at(conn, at) == expected, f"compact changed {at}"
    conn.close()


def test_renumber_is_a_no_op_on_an_ingest_only_database(tmp_path: Path, exports):
    conn = db.connect(tmp_path / "d.db")
    load(conn, exports.values())
    assert retention.is_chronological(conn)
    assert retention.renumber_snapshots(conn) == 0
    conn.close()
