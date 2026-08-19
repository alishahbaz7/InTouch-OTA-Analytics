r"""The build must not be able to destroy a database.

Someone who unpacks the app and runs it from `dist\` has their real database in
`dist\InTouchOTA-Analytics\data`. A build deletes `dist` to start clean, and it used to take
that folder with it — on the machine this was written for, 48 snapshots of live fleet history,
more than once. A printed warning was not enough: it scrolled past and the data went anyway.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import build as build_module  # noqa: E402


@pytest.fixture
def fake_dist(tmp_path, monkeypatch):
    """A dist folder holding a program and, as on a real machine, someone's database."""
    dist = tmp_path / "dist" / "InTouchOTA-Analytics"
    (dist / "data").mkdir(parents=True)
    (dist / "data" / "ota_analytics.db").write_bytes(b"the fleet history")
    (dist / "data" / "connection.json").write_text("{}", encoding="utf-8")
    (dist / "InTouchOTA-Analytics.exe").write_bytes(b"MZ")

    monkeypatch.setattr(build_module, "ROOT", tmp_path)
    monkeypatch.setattr(build_module, "DIST", dist)
    monkeypatch.setattr(build_module, "PRESERVE", tmp_path / ".build-preserved-data")
    return dist


def test_the_database_survives_the_folder_being_cleared(fake_dist, tmp_path):
    parked = build_module.rescue_data()

    assert parked is not None
    assert not (fake_dist / "data").exists()          # out of harm's way before the delete
    assert (parked / "ota_analytics.db").read_bytes() == b"the fleet history"

    build_module.clear(tmp_path / "dist")             # what the build does next
    assert not (tmp_path / "dist").exists()

    build_module.restore_data(parked)
    assert (fake_dist / "data" / "ota_analytics.db").read_bytes() == b"the fleet history"
    assert (fake_dist / "data" / "connection.json").exists()
    assert not parked.exists()


def test_a_build_with_no_database_present_is_unaffected(tmp_path, monkeypatch):
    dist = tmp_path / "dist" / "InTouchOTA-Analytics"
    dist.mkdir(parents=True)
    monkeypatch.setattr(build_module, "ROOT", tmp_path)
    monkeypatch.setattr(build_module, "DIST", dist)
    monkeypatch.setattr(build_module, "PRESERVE", tmp_path / ".build-preserved-data")

    assert build_module.rescue_data() is None
    build_module.restore_data(None)                   # a no-op, and must not raise


def test_an_interrupted_build_refuses_rather_than_overwriting_the_rescue(fake_dist, tmp_path):
    """If a previous build died between moving the data out and putting it back, the parked
    copy is the only one there is. A second build must not move a fresh folder on top of it."""
    build_module.rescue_data()
    (fake_dist / "data").mkdir(parents=True)
    (fake_dist / "data" / "ota_analytics.db").write_bytes(b"a newer database")

    with pytest.raises(SystemExit) as raised:
        build_module.rescue_data()
    assert "already exists" in str(raised.value)
    assert (tmp_path / ".build-preserved-data" / "ota_analytics.db").read_bytes() \
        == b"the fleet history"
