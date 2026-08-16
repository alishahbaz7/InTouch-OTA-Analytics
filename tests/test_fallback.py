"""Fallback detection, as the platform owner defines it.

A FALLBACK is a device returning to its BASE firmware — the build it shipped with, reported by
the API as `baseFirm`. That is a specific event, distinct from simply moving backwards.

It needs API-sourced data: the spreadsheet export carries no base-firmware column, so fallbacks
are undetectable from Excel snapshots alone, and the system must say nothing rather than guess.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from ota_analytics import ingest, registry


def api_device(imei: str, firmware: str, base: str, target: str | None = None, **extra) -> dict:
    record = {
        "deviceId": imei, "deviceName": imei, "createdBy": 1, "createdByName": "riya",
        "creationTime": 1720009238354, "lastPingTime": 1755264000000,
        "currFirmVer": firmware, "baseFirm": base,
        "updateFirmVer": target or firmware,
        "currConfigVersion": "2.2.2", "model": "LOCAT140VB", "hwVer": "1.2.0",
        "type_Task": {},
    }
    record.update(extra)
    return record


@pytest.fixture
def rollout(conn):
    """Devices shipped on 7.5.0.27, moved up, then various ways back down."""
    ingest.ingest_records(conn, [
        api_device("falls_to_base", "7.5.0.51A", base="7.5.0.27"),
        api_device("goes_partway_back", "7.5.0.51A", base="7.5.0.27"),
        api_device("climbs", "7.5.0.27", base="7.5.0.27"),
        api_device("steady", "7.5.0.51A", base="7.5.0.27"),
    ], source_name="API day1", snapshot_at=datetime(2026, 8, 15, 10, 0))

    ingest.ingest_records(conn, [
        api_device("falls_to_base", "7.5.0.27", base="7.5.0.27"),        # back to base
        api_device("goes_partway_back", "7.5.0.49A", base="7.5.0.27"),   # down, not to base
        api_device("climbs", "7.5.0.51A", base="7.5.0.27"),              # up
        api_device("steady", "7.5.0.51A", base="7.5.0.27"),
    ], source_name="API day2", snapshot_at=datetime(2026, 8, 16, 10, 0))
    return conn


def _move(conn, imei):
    return next(m for m in registry.firmware_moves(conn) if m["imei"] == imei)


def test_returning_to_base_firmware_is_a_fallback(rollout):
    move = _move(rollout, "falls_to_base")
    assert move["direction"] == "downgrade"
    assert move["is_fallback"] == 1


def test_a_downgrade_that_is_not_to_base_is_not_a_fallback(rollout):
    """Still a revert worth seeing, but it is not the event the owner means by fallback."""
    move = _move(rollout, "goes_partway_back")
    assert move["direction"] == "downgrade"
    assert move["is_fallback"] == 0


def test_upgrades_are_never_fallbacks(rollout):
    move = _move(rollout, "climbs")
    assert move["direction"] == "upgrade"
    assert move["is_fallback"] == 0


def test_fallback_list_comes_from_the_change_log(rollout):
    """Read from the change log so no fetch cadence can hide an occurrence."""
    found = registry.fallbacks(rollout)
    assert [f["imei"] for f in found] == ["falls_to_base"]
    assert found[0]["from_firmware"] == "7.5.0.51A"
    assert found[0]["to_firmware"] == "7.5.0.27"
    assert found[0]["base_firmware"] == "7.5.0.27"


def test_summary_counts_both_reverts_but_one_fallback(rollout):
    summary = registry.movement_summary(rollout)
    assert summary["downgrades"] == 2       # both went backwards
    assert summary["fallbacks"] == 1        # only one returned to base
    assert summary["upgrades"] == 1


def test_devices_sitting_on_base_are_reported_with_their_ambiguity(conn):
    """On-base-with-a-newer-target mixes fallbacks with devices that never moved."""
    ingest.ingest_records(conn, [
        api_device("never_moved", "7.5.0.27", base="7.5.0.27", target="7.5.0.51A"),
        api_device("on_target", "7.5.0.51A", base="7.5.0.27", target="7.5.0.51A"),
    ], source_name="API", snapshot_at=datetime(2026, 8, 15, 10, 0))

    state = registry.at_base_firmware(conn)
    assert state["at_base"] == 1                      # only 'never_moved'
    assert state["confirmed_fallbacks"] == 0          # no observed move to base
    assert state["never_moved_or_unobserved"] == 1


def test_spreadsheet_snapshots_cannot_detect_fallbacks(conn, make_export):
    """The Excel export has no base-firmware column, so this must not silently claim any."""
    from tests.conftest import device

    ingest.ingest_file(conn, make_export([device("111", firmware="7.5.0.51A")],
                                         name="Devices_1_15Aug26_1000.xlsx"))
    ingest.ingest_file(conn, make_export([device("111", firmware="7.5.0.27")],
                                         name="Devices_1_16Aug26_1000.xlsx"))

    move = _move(conn, "111")
    assert move["direction"] == "downgrade"
    assert move["is_fallback"] == 0         # unknown base, so no claim is made
    assert registry.fallbacks(conn) == []


def test_configuration_changes_are_logged_separately_from_firmware(conn):
    ingest.ingest_records(conn, [
        api_device("x", "7.5.0.51A", base="7.5.0.27", currConfigVersion="2.2.2"),
    ], source_name="API", snapshot_at=datetime(2026, 8, 15, 10, 0))
    ingest.ingest_records(conn, [
        api_device("x", "7.5.0.51A", base="7.5.0.27", currConfigVersion="2.1.0"),
    ], source_name="API", snapshot_at=datetime(2026, 8, 16, 10, 0))

    fields = {r["field"]: (r["old_value"], r["new_value"]) for r in
              conn.execute("SELECT field, old_value, new_value FROM device_change")}
    assert fields["configuration"] == ("2.2.2", "2.1.0")
    assert "firmware" not in fields          # firmware did not move
