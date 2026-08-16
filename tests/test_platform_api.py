"""Mapping the real fotaWeb API response shape.

Field names, types and quirks here were verified against a live response from
GET /fotaAdminApi/api/user/devices (35,477 records) on 2026-08-15.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from ota_analytics import ingest, metrics, normalize, rollup


def api_record(device_id: str, **overrides) -> dict:
    """A record shaped exactly as the platform API returns it."""
    record = {
        "deviceId": device_id,
        "deviceName": device_id,
        "createdBy": 25830,
        "createdByName": "riya",
        "creationTime": 1720009238354,          # epoch MILLIseconds
        "lastPingTime": 1755264000000,          # epoch MILLIseconds
        "firstPing": 1770381652,                # epoch SECONDS — different unit!
        "currFirmVer": "7.5.0.51A",
        "updateFirmVer": "7.5.0.51A",
        "baseFirm": "7.5.0.27",
        "currConfigVersion": "2.2.2",
        "newConfigVersion": "2.2.2",
        "baseConfig": "1.1",
        "model": "LOCAT140VB",
        "hwVer": "1.2.0",
        "vin": "MAT562014RKP83714",
        "iccid": "8991922305932268741F",
        "type_Task": {},                        # tasks done, nothing pending
    }
    record.update(overrides)
    return record


# ─── epoch timestamps ───────────────────────────────────────────────────────

def test_parse_epoch_detects_seconds_versus_milliseconds():
    """The API mixes units: lastPingTime is millis, firstPing is seconds."""
    assert normalize.parse_epoch(1755264000000).startswith("2025-08-15")   # millis
    assert normalize.parse_epoch(1770381652).startswith("2026-02-06")      # seconds
    assert normalize.parse_epoch(0) is None
    assert normalize.parse_epoch(None) is None
    assert normalize.parse_epoch("not a number") is None
    assert normalize.parse_epoch(True) is None      # bools are ints in Python; reject them


def test_epoch_fields_survive_ingest_as_real_dates(conn):
    result = ingest.ingest_records(conn, [api_record("111")], source_name="API",
                                   snapshot_at=datetime(2026, 8, 15, 15, 11))
    row = conn.execute("SELECT seen_at, first_ping FROM device_snapshot").fetchone()
    assert row["seen_at"].startswith("2025-08-15")
    assert row["first_ping"].startswith("2026-02-06")     # seconds, not 1970
    assert result.rows == 1


# ─── type_Task carries the queue ────────────────────────────────────────────

@pytest.mark.parametrize("task,expected", [
    ({}, (0, "completed")),                 # 22,080 devices in the live response
    ({"1": 1400}, (1, "pending")),          #  7,849 devices
    ({"1": 1400, "2": 9}, (2, "pending")),
    (None, (None, "never_tasked")),         #  5,548 devices omit the key entirely
])
def test_type_task_maps_onto_the_queue_vocabulary(task, expected):
    """The API expresses task state as a dict; the spreadsheet as '-', 0, n. Same three states."""
    assert normalize.parse_queue(task) == expected


def test_queue_states_from_api_records(conn):
    ingest.ingest_records(conn, [
        api_record("done", type_Task={}),
        api_record("pending", type_Task={"1": 1400}),
        dict_without(api_record("never"), "type_Task"),
    ], source_name="API")

    states = {r["imei"]: r["queue_state"] for r in
              conn.execute("SELECT imei, queue_state FROM device_snapshot")}
    assert states == {"done": "completed", "pending": "pending", "never": "never_tasked"}


def dict_without(record: dict, key: str) -> dict:
    record.pop(key, None)
    return record


# ─── status is derived, since the API does not send it ──────────────────────

def test_status_is_derived_from_last_ping(conn):
    """No status field in the API, but the 24h rule is known — so derive it."""
    snapshot_at = datetime(2026, 8, 15, 12, 0)
    recent = int(datetime(2026, 8, 15, 6, 0).timestamp() * 1000)     # 6h ago
    old = int(datetime(2026, 8, 1, 12, 0).timestamp() * 1000)        # 14 days ago

    ingest.ingest_records(conn, [
        api_record("online", lastPingTime=recent),
        api_record("offline", lastPingTime=old),
        dict_without(api_record("never"), "lastPingTime"),
    ], source_name="API", snapshot_at=snapshot_at)

    statuses = {r["imei"]: r["status"] for r in
                conn.execute("SELECT imei, status FROM device_snapshot")}
    assert statuses == {"online": "Online", "offline": "Offline", "never": "Inactive"}


def test_a_spreadsheet_status_is_never_overwritten_by_derivation(conn, make_export):
    """Excel rows carry STATUS; that value wins over anything we would infer."""
    from tests.conftest import device
    path = make_export([device("111", status="Offline", seen_at="15-08-26 15:10:00")],
                       name="Devices_1_15Aug26_1511.xlsx")
    ingest.ingest_file(conn, path)
    # seen_age_hours is derived by device_state, not stored — storing it made every row differ
    # on every fetch, which is what stopped the snapshot table from compacting at all.
    row = conn.execute("SELECT status, seen_age_hours FROM device_state").fetchone()
    assert row["status"] == "Offline"          # kept, even though the ping is minutes old
    assert row["seen_age_hours"] < 1


# ─── rollout intent, which the spreadsheet drops ────────────────────────────

def test_target_and_base_firmware_are_captured(conn):
    ingest.ingest_records(conn, [
        api_record("behind", currFirmVer="7.5.0.27", updateFirmVer="7.5.0.49A",
                   baseFirm="7.5.0.27"),
        api_record("current", currFirmVer="7.5.0.51A", updateFirmVer="7.5.0.51A",
                   baseFirm="7.5.0.27"),
    ], source_name="API")

    rows = {r["imei"]: r for r in conn.execute(
        "SELECT imei, firmware, update_firmware, base_firmware FROM device_snapshot")}
    assert rows["behind"]["update_firmware"] == "7.5.0.49A"
    assert rows["behind"]["base_firmware"] == "7.5.0.27"
    assert rows["current"]["firmware"] == rows["current"]["update_firmware"]


def test_records_with_different_field_sets_all_map(conn):
    """The API omits keys it has no value for — sampling only the first record loses columns."""
    sparse = {"deviceId": "222", "deviceName": "222", "createdBy": 1,
              "creationTime": 1703653880191}
    result = ingest.ingest_records(conn, [sparse, api_record("111")], source_name="API")

    assert result.rows == 2
    rows = {r["imei"]: r for r in conn.execute(
        "SELECT imei, firmware, device_model FROM device_snapshot")}
    assert rows["111"]["firmware"] == "7.5.0.51A"   # would be dropped if only record 0 was read
    assert rows["222"]["firmware"] is None


def test_api_and_spreadsheet_snapshots_are_comparable(conn, make_export):
    """A device loaded from each source must normalize to the same analytical row."""
    from tests.conftest import device

    ingest.ingest_file(conn, make_export(
        [device("111", model="AX1_sCAN", firmware="7.5.0.51A", queue=1, status="Online",
                seen_at="15-08-26 15:00:00")], name="Devices_1_15Aug26_1511.xlsx"))
    ingest.ingest_records(conn, [api_record(
        "111", model="AX1_sCAN", currFirmVer="7.5.0.51A", type_Task={"1": 1400},
        lastPingTime=int(datetime(2026, 8, 16, 15, 0).timestamp() * 1000))],
        source_name="API", snapshot_at=datetime(2026, 8, 16, 15, 11))

    rows = list(conn.execute(
        "SELECT device_model, firmware, queue_state, status FROM device_snapshot "
        "ORDER BY snapshot_id"))
    assert rows[0]["device_model"] == rows[1]["device_model"] == "AX1_SCAN"
    assert rows[0]["firmware"] == rows[1]["firmware"] == "7.5.0.51A"
    assert rows[0]["queue_state"] == rows[1]["queue_state"] == "pending"
    assert rows[0]["status"] == rows[1]["status"] == "Online"


def test_metrics_work_identically_on_an_api_snapshot(conn):
    result = ingest.ingest_records(conn, [
        api_record("stuck", type_Task={"1": 1}, lastPingTime=int(datetime.now().timestamp() * 1000)),
        api_record("waiting", type_Task={"1": 1}, lastPingTime=1600000000000),
        api_record("done", type_Task={}),
    ], source_name="API")
    rollup.rollup_snapshot(conn, result.snapshot_id)

    kpis = metrics.kpis(conn, result.snapshot_id)
    assert kpis["pending_reachable"] == 1
    assert kpis["pending_waiting"] == 1
    assert kpis["devices_completed"] == 1
