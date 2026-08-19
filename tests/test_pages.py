"""Render every page against a real database.

Two production bugs got through a green suite because nothing here ever rendered a template:
a window tuple changed shape and `/changes` raised on every request, and a mid-ingest snapshot
with no rows made the overview blow up. Unit tests cannot catch either — only rendering can.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from ota_analytics import config, db, ingest, registry, rollup, scheduler, sources  # noqa: E402


def api_device(imei: str, firmware: str, *, base="7.5.0.27", target=None, model="LOCAT140VB",
               task=None, ping_hours_ago=2.0) -> dict:
    when = datetime.now() - timedelta(hours=ping_hours_ago)
    return {
        "deviceId": imei, "deviceName": imei, "createdBy": 1, "createdByName": "riya",
        "creationTime": 1720009238354,
        "lastPingTime": int(when.timestamp() * 1000),
        "currFirmVer": firmware, "baseFirm": base,
        "updateFirmVer": target or firmware,
        "currConfigVersion": "2.2.2", "model": model, "hwVer": "1.2.0",
        "vin": "MAT562014RKP83714", "iccid": "8991922305932268741F",
        "type_Task": {} if task is None else task,
        "groupNames": "49A 7k, 51A 4K",
    }


@pytest.fixture
def client(tmp_path, monkeypatch):
    """A dashboard backed by a temp database holding two snapshots and a real change."""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(config, "EXPORT_DIR", tmp_path / "exports")
    monkeypatch.setattr(scheduler, "STATE_PATH", tmp_path / "scheduler.json")
    monkeypatch.setattr(sources, "SETTINGS_PATH", tmp_path / "connection.json")
    monkeypatch.setattr(scheduler, "_scheduler", None)

    conn = db.connect()
    first = ingest.ingest_records(conn, [
        api_device("111", "7.5.0.27", target="7.5.0.51A", task={"1": 1400}),
        api_device("222", "7.5.0.51A"),
        api_device("333", "2.0.0125", model="TML_Ax1", base="2.0.0125", target="2.0.0162",
                   task={"1": 1400}, ping_hours_ago=900),
        api_device("444", "7.5.0.51A", ping_hours_ago=0.2),
    ], source_name="API test", snapshot_at=datetime.now() - timedelta(hours=6))

    second = ingest.ingest_records(conn, [
        api_device("111", "7.5.0.51A", target="7.5.0.51A"),        # completed an upgrade
        api_device("222", "7.5.0.27", base="7.5.0.27", target="7.5.0.27"),   # fell back to base
        api_device("333", "2.0.0125", model="TML_Ax1", base="2.0.0125", target="2.0.0162",
                   task={"1": 1400}, ping_hours_ago=900),
        api_device("444", "7.5.0.51A", ping_hours_ago=0.2),
    ], source_name="API test", snapshot_at=datetime.now())

    for result in (first, second):
        rollup.rollup_snapshot(conn, result.snapshot_id)

    from ota_analytics import api
    return TestClient(api.app, raise_server_exceptions=False)


PAGES = ["/", "/pending", "/firmware", "/changes", "/devices", "/reachability",
         "/groups", "/quality", "/update", "/errors"]


def test_the_model_picker_is_the_same_control_on_every_page(client):
    """Overview and Firmware both slice by model, so they must not do it two different ways.

    Firmware used an always-open multi-select list box that needed "ctrl+click to pick several"
    written underneath it; the overview had a dropdown. Same job, same control.
    """
    for path in ("/", "/firmware"):
        body = client.get(path).text
        assert 'id="model-picker"' in body, f"{path} has no model dropdown"
        assert 'class="dropdown-list"' in body
        assert 'type="checkbox" name="model"' in body
        assert "ctrl+click" not in body, f"{path} still explains a multi-select"
        assert "<select name=\"model\" multiple" not in body


def test_the_firmware_picker_keeps_the_snapshot_being_viewed(client):
    """Narrowing by model must not silently jump back to the latest snapshot."""
    from ota_analytics import db, metrics

    oldest = metrics.snapshots(db.connect())[-1]["id"]
    body = client.get(f"/firmware?snapshot={oldest}").text
    assert f'name="snapshot" value="{oldest}"' in body


def test_selecting_a_model_on_the_firmware_page_filters_it(client):
    from ota_analytics import db, metrics

    conn = db.connect()
    models = [r["label"] for r in metrics.task_state_by(conn, metrics.latest_snapshot_id(conn),
                                                        "model")]
    assert len(models) > 1, "fixture needs at least two models to prove filtering"

    body = client.get(f"/firmware?model={models[0]}").text
    assert body.count(f'value="{models[0]}"') >= 1

    # The summary reports the narrowing rather than still claiming the whole fleet. Compared
    # with whitespace collapsed, because the template wraps the line and the exact run of
    # spaces is not the thing under test.
    import re
    flat = re.sub(r"\s+", " ", body)
    assert f"in 1 of {len(models)} models" in flat
    assert "all {} models".format(len(models)) not in flat


def test_the_firmware_table_names_each_denominator(client):
    """Three percentage columns sit side by side and measure against different totals.

    One repeated "Share (%)" would put the same word on three different meanings in adjacent
    columns. The group row carries the subject, the row under it carries the denominator.
    """
    import re

    body = client.get("/firmware").text
    flat = re.sub(r"\s+", " ", body)

    assert "% of fleet" in flat and "% of version" in flat
    assert flat.count("% of version") == 2          # online and offline, not the fleet share
    assert "Share (%)" not in flat                  # the ambiguous label it replaced

    # Read the group row itself rather than searching the whole page, so a heading appearing
    # somewhere else cannot make this pass.
    header = flat[flat.index('<tr class="group-row">'):flat.index("</thead>")]
    for heading in ("Model", "Firmware", "Devices", "Online", "Offline", "Inactive",
                    "Task pending", "Distribution"):
        assert f">{heading}<" in header, f"{heading} is missing from the header"
    assert header.count("Count") == 3               # one under each two-column group


def test_inactive_is_shown_rather_than_left_as_the_remainder(client):
    """Online + Offline does not reach the row total: STATUS has a third value.

    On the real fleet 643 devices have never pinged, so a row showing only online and offline
    percentages appears to lose 1.8% of the fleet with no explanation.
    """
    from ota_analytics import db, metrics

    conn = db.connect()
    rows = metrics.firmware_mix(conn, metrics.latest_snapshot_id(conn))
    assert rows, "fixture has no firmware rows"
    for row in rows:
        assert row["online"] + row["offline"] + row["inactive"] == row["devices"], row

    assert "Inactive" in client.get("/firmware").text


@pytest.mark.parametrize("path", PAGES)
def test_every_page_renders(client, path):
    response = client.get(path)
    assert response.status_code == 200, response.text[:400]
    assert "Something went wrong" not in response.text


@pytest.mark.parametrize("path,query", [
    ("/", "window=1h"), ("/", "window=yesterday"), ("/", "window=all"),
    ("/", "model=LOCAT140VB"), ("/", "model=LOCAT140VB&model=TML_Ax1"),
    ("/changes", "window=6h"), ("/changes", "window=month"),
    ("/devices", "sort=firmware&dir=asc"), ("/devices", "status=Online&changed=24h"),
    ("/devices", "queue_state=pending&sort=seen&dir=desc"),
    ("/firmware", "model=LOCAT140VB"),
])
def test_pages_render_with_their_filters(client, path, query):
    response = client.get(f"{path}?{query}")
    assert response.status_code == 200, response.text[:400]


def test_overview_shows_the_change_that_happened(client):
    """111 upgraded and 222 fell back to base — both must appear."""
    body = client.get("/?window=all").text
    assert "What changed" in body
    assert "Fell back" in body


def test_changes_page_lists_the_fallback(client):
    body = client.get("/changes?window=all").text
    assert "222" in body                    # the device that returned to base
    assert "7.5.0.27" in body               # its base firmware


def test_json_endpoints_respond(client):
    for path in ("/api/kpis", "/api/version", "/api/agent", "/api/pending", "/api/quality"):
        response = client.get(path)
        assert response.status_code == 200, path
        assert response.json() is not None


def test_a_snapshot_still_being_written_is_not_served(client, tmp_path):
    """Ingest writes the snapshot row before its devices; that gap broke every page."""
    conn = db.connect()
    conn.execute("INSERT INTO snapshot (source_file, file_sha256, snapshot_at, ts_source, "
                 "row_count, ingested_at) VALUES ('half done','sha-x',?,'api',0,?)",
                 (datetime.now().isoformat(sep=" ", timespec="seconds"),
                  datetime.now().isoformat(sep=" ", timespec="seconds")))
    conn.commit()

    response = client.get("/")
    assert response.status_code == 200
    assert "half done" not in response.text


def test_an_unknown_window_falls_back_instead_of_failing(client):
    assert client.get("/changes?window=nonsense").status_code == 200
    assert client.get("/?window=nonsense").status_code == 200


def test_an_unknown_sort_column_is_ignored(client):
    assert client.get("/devices?sort=DROP+TABLE&dir=sideways").status_code == 200


def test_version_is_reported_everywhere(client):
    from ota_analytics import __version__

    assert client.get("/api/version").json()["version"] == __version__
    assert f"v{__version__}" in client.get("/").text
