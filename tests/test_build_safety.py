r"""The build must not be able to destroy anything the user put in dist.

This install keeps its database in `dist\InTouchOTA-Analytics\data`, its bundles beside it in
`dist\`, and its reports below it — by choice. A build deletes `dist` to start clean, and it used
to take all of that with it: on the machine this was written for, 48 snapshots of live fleet
history, more than once.

A printed warning was tried first and was not enough — it scrolled past and the data went anyway.
The rule is therefore inverted: the build declares what *it* produced, and everything else is
moved aside and put back. A kind of file nobody anticipated is then preserved by omission rather
than destroyed by it.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import build as build_module  # noqa: E402


@pytest.fixture
def fake_dist(tmp_path, monkeypatch):
    """A dist folder shaped like the real one: program output plus the user's own files."""
    root = tmp_path / "dist"
    app = root / "InTouchOTA-Analytics"
    (app / "data").mkdir(parents=True)
    (app / "reports").mkdir()
    (app / "_internal").mkdir()
    (app / "Sample data").mkdir()

    (app / "data" / "ota_analytics.db").write_bytes(b"the fleet history")
    (app / "data" / "connection.json").write_text("{}", encoding="utf-8")
    (app / "reports" / "devices_19Aug26.xlsx").write_bytes(b"a report")
    (app / "Sample data" / "Devices_9_19Aug26_1000.xlsx").write_bytes(b"an export")
    (app / "InTouchOTA-Analytics.exe").write_bytes(b"MZ")
    (app / "InTouchOTA-Analytics-silent.exe").write_bytes(b"MZ")
    (app / "READ ME FIRST.txt").write_text("hello", encoding="utf-8")
    (app / "_internal" / "base_library.zip").write_bytes(b"PK")
    (root / "ota_Shahbaz_345efcf5_18Aug26_1438.otabundle").write_bytes(b"a bundle")
    (root / "InTouchOTA-Analytics-v1.3.3-win64.zip").write_bytes(b"PK")

    monkeypatch.setattr(build_module, "ROOT", tmp_path)
    monkeypatch.setattr(build_module, "DIST", app)
    monkeypatch.setattr(build_module, "PRESERVE", tmp_path / ".build-preserved-data")
    return app


def test_it_knows_which_files_are_not_its_own(fake_dist, tmp_path):
    theirs = {p.relative_to(tmp_path / "dist").as_posix() for p in build_module.user_files()}

    assert theirs == {
        "InTouchOTA-Analytics/data",
        "InTouchOTA-Analytics/reports",
        "InTouchOTA-Analytics/Sample data",
        "ota_Shahbaz_345efcf5_18Aug26_1438.otabundle",
    }
    # Its own output is not in the list, so it is free to delete it.
    for produced in ("InTouchOTA-Analytics/_internal", "InTouchOTA-Analytics/InTouchOTA-Analytics.exe"):
        assert produced not in theirs


def test_everything_of_the_users_survives_the_folder_being_cleared(fake_dist, tmp_path):
    parked = build_module.rescue_data()
    assert parked is not None

    build_module.clear(tmp_path / "dist")                 # what the build does next
    assert not (tmp_path / "dist").exists()

    # PyInstaller then recreates its own folder.
    fake_dist.mkdir(parents=True)
    (fake_dist / "InTouchOTA-Analytics.exe").write_bytes(b"MZ")

    build_module.restore_data(parked)

    assert (fake_dist / "data" / "ota_analytics.db").read_bytes() == b"the fleet history"
    assert (fake_dist / "data" / "connection.json").exists()
    assert (fake_dist / "reports" / "devices_19Aug26.xlsx").read_bytes() == b"a report"
    assert (fake_dist / "Sample data" / "Devices_9_19Aug26_1000.xlsx").exists()
    assert (tmp_path / "dist" / "ota_Shahbaz_345efcf5_18Aug26_1438.otabundle").read_bytes() \
        == b"a bundle"
    assert not (tmp_path / ".build-preserved-data").exists()


def test_a_bundle_stored_beside_the_app_is_not_deleted(fake_dist, tmp_path):
    """Bundles are kept in dist\\ on this install, and they are the only copy of shared history."""
    parked = build_module.rescue_data()
    build_module.clear(tmp_path / "dist")
    build_module.restore_data(parked)

    assert (tmp_path / "dist" / "ota_Shahbaz_345efcf5_18Aug26_1438.otabundle").exists()


def test_a_clean_dist_needs_no_rescue(tmp_path, monkeypatch):
    app = tmp_path / "dist" / "InTouchOTA-Analytics"
    (app / "_internal").mkdir(parents=True)
    (app / "InTouchOTA-Analytics.exe").write_bytes(b"MZ")
    monkeypatch.setattr(build_module, "ROOT", tmp_path)
    monkeypatch.setattr(build_module, "DIST", app)
    monkeypatch.setattr(build_module, "PRESERVE", tmp_path / ".build-preserved-data")

    assert build_module.rescue_data() is None
    build_module.restore_data(None)                        # a no-op, and must not raise


def test_the_users_copy_wins_if_the_build_made_the_same_name(fake_dist, tmp_path):
    """A fresh install ships an empty data folder; the rescued one is the only real copy."""
    parked = build_module.rescue_data()
    build_module.clear(tmp_path / "dist")
    fake_dist.mkdir(parents=True)
    (fake_dist / "data").mkdir()
    (fake_dist / "data" / "ota_analytics.db").write_bytes(b"a blank database")

    build_module.restore_data(parked)
    assert (fake_dist / "data" / "ota_analytics.db").read_bytes() == b"the fleet history"


def test_an_interrupted_build_refuses_rather_than_overwriting_the_rescue(fake_dist, tmp_path):
    """If a build died between moving the files out and putting them back, the parked copy is
    the only one there is. A second build must not move a fresh folder on top of it."""
    build_module.rescue_data()
    (fake_dist / "data").mkdir(parents=True)
    (fake_dist / "data" / "ota_analytics.db").write_bytes(b"a newer database")

    with pytest.raises(SystemExit) as raised:
        build_module.rescue_data()
    assert "already exists" in str(raised.value)
    assert (tmp_path / ".build-preserved-data" / "InTouchOTA-Analytics" / "data"
            / "ota_analytics.db").read_bytes() == b"the fleet history"
