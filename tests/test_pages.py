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
