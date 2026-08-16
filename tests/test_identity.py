"""The fleet digest and what it is allowed to depend on."""

from __future__ import annotations

from pathlib import Path

import pytest

from ota_analytics import db, identity, ingest

from tests.conftest import device


@pytest.fixture
def two_exports(make_export):
    first = make_export([device("1", firmware="1.0.0"), device("2", firmware="1.0.0")],
                        name="Devices_2_15Aug26_1000.xlsx")
    second = make_export([device("1", firmware="1.1.0"), device("2", firmware="1.0.0")],
                         name="Devices_2_15Aug26_1100.xlsx")
    return first, second


def test_identity_is_stamped_on_creation(conn):
    data = identity.manifest(conn)
    assert data["db_id"] and len(data["db_id"]) == 32
    assert data["created_at"]
    assert data["instance_label"]


def test_db_id_survives_reconnecting(tmp_path: Path):
    path = tmp_path / "keep.db"
    first = db.connect(path)
    original = identity.get(first, identity.DB_ID)
    first.close()

    db._MIGRATED.clear()                      # force the migration path to run again
    second = db.connect(path)
    assert identity.get(second, identity.DB_ID) == original
    second.close()


def test_env_label_wins_over_stored(conn, monkeypatch):
    identity.set_instance_label(conn, "stored-name")
    monkeypatch.setenv(identity.ENV_LABEL, "deployed-name")
    assert identity.instance_label(conn) == "deployed-name"
    monkeypatch.setenv(identity.ENV_LABEL, "   ")
    assert identity.instance_label(conn) == "stored-name"


def test_digest_ignores_ingest_order(tmp_path: Path, two_exports):
    """Two installs that loaded the same exports agree, whichever order they arrived in.

    The resulting state does not depend on ingest order, so the fingerprint must not either —
    otherwise two people holding identical data would be told they disagree.
    """
    first, second = two_exports

    forward = db.connect(tmp_path / "forward.db")
    ingest.ingest_file(forward, first)
    ingest.ingest_file(forward, second)

    backward = db.connect(tmp_path / "backward.db")
    ingest.ingest_file(backward, second)
    ingest.ingest_file(backward, first)

    assert identity.fleet_digest(forward) == identity.fleet_digest(backward)
    forward.close()
    backward.close()


def test_digest_changes_when_a_snapshot_is_added(conn, two_exports):
    first, second = two_exports
    ingest.ingest_file(conn, first)
    before = identity.fleet_digest(conn)
    ingest.ingest_file(conn, second)
    assert identity.fleet_digest(conn) != before


def test_digest_ignores_db_id_and_label(tmp_path: Path, two_exports):
    """The digest describes the data, not the install. Two people must be able to compare."""
    first, _ = two_exports
    left = db.connect(tmp_path / "left.db")
    right = db.connect(tmp_path / "right.db")
    ingest.ingest_file(left, first)
    ingest.ingest_file(right, first)
    identity.set_instance_label(left, "shahbaz-laptop")
    identity.set_instance_label(right, "prod-exe")

    assert identity.get(left, identity.DB_ID) != identity.get(right, identity.DB_ID)
    assert identity.fleet_digest(left) == identity.fleet_digest(right)
    left.close()
    right.close()


def test_compare_explains_a_mismatch(conn, two_exports):
    first, second = two_exports
    ingest.ingest_file(conn, first)
    mine = identity.manifest(conn)
    ingest.ingest_file(conn, second)
    theirs = identity.manifest(conn)

    assert identity.compare(mine, mine)["match"] is True
    verdict = identity.compare(mine, theirs)
    assert verdict["match"] is False
    assert any("snapshot count" in reason for reason in verdict["reasons"])


def test_two_clocks_are_reported_separately(conn, two_exports):
    """'Last sync' is ambiguous: when the data was true and when we pulled it differ."""
    first, _ = two_exports
    ingest.ingest_file(conn, first)
    data = identity.manifest(conn)
    assert data["last_snapshot_at"].startswith("2026-08-15 10:00")
    assert data["last_ingest_at"] != data["last_snapshot_at"]
