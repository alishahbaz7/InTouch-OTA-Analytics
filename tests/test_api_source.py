"""Pulling device data straight from the platform API, with no spreadsheet in between.

The fotaWeb app builds its export in the browser, so the device list arrives as JSON. These
tests cover finding the records inside whatever wrapper the API uses, mapping camelCase field
names onto our columns, and ingesting with the same rules the spreadsheet path uses.
"""

from __future__ import annotations

import pytest

from ota_analytics import ingest, metrics, rollup, sources


def api_device(imei: str, **overrides) -> dict:
    """A device record shaped the way a JSON API would send it."""
    record = {
        "imei": imei,
        "deviceStatus": "Online",
        "queue": 0,
        "deviceModel": "LOCAT140VB",
        "firmwareVersion": "7.5.0.51A",
        "configVersion": "2.2.2",
        "lastSeen": "15-08-26 10:00:00",
        "hwVersion": "1.2.0",
        "iccid": "8991119018554142514",
        "vin": "DL1CAB1234",
        "groupNames": "49A 7k, 51A 4K",
        "firstPing": "09-05-26 15:25:58",
    }
    record.update(overrides)
    return record


# ─── locating records in the response ───────────────────────────────────────

@pytest.mark.parametrize("payload", [
    [{"imei": "1"}],
    {"data": [{"imei": "1"}]},
    {"result": {"rows": [{"imei": "1"}]}},
    {"response": {"payload": {"devices": [{"imei": "1"}]}}},
])
def test_find_records_handles_common_wrappers(payload):
    found = sources.find_records(payload)
    assert found == [{"imei": "1"}]


@pytest.mark.parametrize("payload", [
    {"Offline": 16195, "Inactive": 644, "Online": 18638, "totalCount": 35477},  # the real
    {},                                                                          # summary
    [],                                                                          # endpoint
    {"data": []},
    "text",
])
def test_find_records_rejects_responses_without_a_device_list(payload):
    assert sources.find_records(payload) is None


# ─── field mapping ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("field,column", [
    ("IMEI", "imei"), ("imei", "imei"), ("deviceImei", "imei"),
    ("Device Model", "device_model_raw"), ("deviceModel", "device_model_raw"),
    ("FIRMWARE", "firmware_raw"), ("firmwareVersion", "firmware_raw"),
    ("fwVersion", "firmware_raw"),
    ("SEEN AT", "seen_at"), ("lastSeen", "seen_at"), ("last_seen_at", "seen_at"),
    ("hwVer", "hw_ver"), ("hwVersion", "hw_ver"),
    ("Groups", "groups_raw"), ("groupNames", "groups_raw"),
    ("somethingElse", None),
])
def test_one_alias_map_serves_spreadsheet_headers_and_api_keys(field, column):
    """'Device Name/VIN' and 'deviceNameVin' must land on the same column."""
    assert ingest.map_field(field) == column


# ─── ingesting API records ──────────────────────────────────────────────────

def test_ingest_records_normalizes_exactly_like_the_spreadsheet_path(conn):
    result = ingest.ingest_records(conn, [
        api_device("111", queue="-"),
        api_device("222", queue=3, deviceStatus="Offline", lastSeen="01-08-26 10:00:00"),
        api_device("333", deviceModel="AX1_sCAN", firmwareVersion="V7.2.2"),
    ], source_name="API test")

    assert result.status == "ingested"
    assert result.rows == 3

    rows = {r["imei"]: r for r in conn.execute("SELECT * FROM device_snapshot")}
    assert rows["111"]["queue_state"] == "never_tasked"      # '-' handled identically
    assert rows["222"]["queue_state"] == "pending"
    assert rows["333"]["device_model"] == "AX1_SCAN"         # spelling canonicalized
    assert rows["333"]["firmware"] == "7.2.2"                # V prefix stripped
    assert rows["111"]["vin"] is None                        # placeholder VIN dropped
    assert conn.execute("SELECT COUNT(*) FROM device_group").fetchone()[0] == 6


def test_ingest_records_is_idempotent(conn):
    records = [api_device("111"), api_device("222")]
    first = ingest.ingest_records(conn, records, source_name="API test")
    second = ingest.ingest_records(conn, list(records), source_name="API test")

    assert first.status == "ingested"
    assert second.status == "already_ingested"
    assert conn.execute("SELECT COUNT(*) FROM snapshot").fetchone()[0] == 1


def test_a_changed_fleet_creates_a_new_snapshot(conn):
    ingest.ingest_records(conn, [api_device("111", firmwareVersion="7.5.0.27")],
                          source_name="API test")
    second = ingest.ingest_records(conn, [api_device("111", firmwareVersion="7.5.0.51A")],
                                   source_name="API test")
    assert second.status == "ingested"
    assert conn.execute("SELECT COUNT(*) FROM snapshot").fetchone()[0] == 2


def test_missing_required_field_is_reported_with_what_was_received(conn):
    with pytest.raises(ingest.IngestError) as exc:
        ingest.ingest_records(conn, [{"imei": "111", "somethingElse": 1}],
                              source_name="API test")
    assert "firmware_raw" in str(exc.value)
    assert "somethingElse" in str(exc.value)      # tells you what the API actually sent


def test_empty_response_is_rejected(conn):
    with pytest.raises(ingest.IngestError, match="no device records"):
        ingest.ingest_records(conn, [], source_name="API test")


def test_api_snapshot_feeds_the_metrics_unchanged(conn):
    result = ingest.ingest_records(conn, [
        api_device("111", queue=1, deviceStatus="Online"),
        api_device("222", queue=1, deviceStatus="Offline", lastSeen="01-01-26 10:00:00"),
        api_device("333", queue=0),
    ], source_name="API test")
    rollup.rollup_snapshot(conn, result.snapshot_id)

    kpis = metrics.kpis(conn, result.snapshot_id)
    assert kpis["devices_total"] == 3
    assert kpis["pending_reachable"] == 1      # the stuck-while-online signal still works
    assert kpis["pending_waiting"] == 1
