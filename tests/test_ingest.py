"""Ingest behavior: idempotency, normalization at rest, and resilience to export changes."""

from __future__ import annotations

import pytest

from ota_analytics import ingest, rollup
from tests.conftest import HEADERS, device


def test_ingest_populates_snapshot(conn, make_export):
    path = make_export([
        device("111", queue="-", firmware="7.5.0.27"),
        device("222", queue=0),
        device("333", queue=2, status="Offline", seen_at="01-08-26 10:00:00"),
    ])
    result = ingest.ingest_file(conn, path)

    assert result.status == "ingested"
    assert result.rows == 3
    snapshot = conn.execute("SELECT * FROM snapshot").fetchone()
    assert snapshot["snapshot_at"] == "2026-08-15 15:11:00"
    assert snapshot["ts_source"] == "filename"


def test_ingest_is_idempotent(conn, make_export):
    path = make_export([device("111"), device("222")])
    first = ingest.ingest_file(conn, path)
    second = ingest.ingest_file(conn, path)

    assert first.status == "ingested"
    assert second.status == "already_ingested"
    assert second.snapshot_id == first.snapshot_id
    assert conn.execute("SELECT COUNT(*) FROM snapshot").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM device_snapshot").fetchone()[0] == 2


def test_queue_states_are_stored_distinctly(conn, make_export):
    """'-' never tasked, 0 completed, n pending — three different facts."""
    path = make_export([
        device("111", queue="-"), device("222", queue=0), device("333", queue=3),
    ])
    ingest.ingest_file(conn, path)

    rows = {r["imei"]: r for r in conn.execute(
        "SELECT imei, queue, queue_state FROM device_snapshot")}
    assert (rows["111"]["queue"], rows["111"]["queue_state"]) == (None, "never_tasked")
    assert (rows["222"]["queue"], rows["222"]["queue_state"]) == (0, "completed")
    assert (rows["333"]["queue"], rows["333"]["queue_state"]) == (3, "pending")


def test_null_markers_become_null(conn, make_export):
    path = make_export([
        device("111", model="-", firmware="-", groups="-", first_ping="-", seen_at="-",
               status="-", vin="DL1CAB1234"),
    ])
    ingest.ingest_file(conn, path)

    row = conn.execute("SELECT * FROM device_snapshot").fetchone()
    assert row["device_model"] is None
    assert row["firmware"] is None
    assert row["seen_at"] is None
    assert row["seen_age_hours"] is None
    assert row["first_ping"] is None
    assert row["vin"] is None
    assert row["vin_raw"] == "DL1CAB1234"   # original preserved
    assert row["status"] == "Inactive"      # '-' status means never pinged
    assert conn.execute("SELECT COUNT(*) FROM device_group").fetchone()[0] == 0


def test_rows_without_imei_are_skipped_and_flagged(conn, make_export):
    path = make_export([device("111"), device(""), device("222")])
    result = ingest.ingest_file(conn, path)

    assert result.rows == 2
    assert result.skipped_no_imei == 1
    rules = {r["rule"] for r in conn.execute("SELECT rule FROM quality_issue")}
    assert "missing_imei" in rules


def test_duplicate_imei_keeps_first_and_flags(conn, make_export):
    path = make_export([
        device("111", firmware="7.5.0.51A"), device("111", firmware="7.5.0.27"),
    ])
    result = ingest.ingest_file(conn, path)

    assert result.rows == 1
    assert result.duplicate_imei == 1
    assert conn.execute("SELECT firmware FROM device_snapshot").fetchone()[0] == "7.5.0.51A"


def test_groups_are_exploded(conn, make_export):
    path = make_export([device("111", groups="49A 7k, 51A 4K, Chakan_7.5.0.40A")])
    ingest.ingest_file(conn, path)

    groups = [r["group_name"] for r in conn.execute(
        "SELECT group_name FROM device_group ORDER BY group_name")]
    assert groups == ["49A 7k", "51A 4K", "Chakan_7.5.0.40A"]


def test_columns_are_matched_by_name_not_position(conn, make_export):
    """The platform may reorder columns; ingest must not care."""
    reordered = ["FIRMWARE", "IMEI", "Device Model", "STATUS", "QUEUE"]
    path = make_export([["7.5.0.27", "111", "LOCAT140VB", "Online", 1]], headers=reordered)
    ingest.ingest_file(conn, path)

    row = conn.execute("SELECT imei, firmware, queue_state FROM device_snapshot").fetchone()
    assert (row["imei"], row["firmware"], row["queue_state"]) == ("111", "7.5.0.27", "pending")


def test_missing_required_column_is_fatal(conn, make_export):
    headers = [h for h in HEADERS if h != "FIRMWARE"]
    path = make_export([], headers=headers)
    with pytest.raises(ingest.IngestError, match="firmware"):
        ingest.ingest_file(conn, path)


def test_unknown_columns_are_tolerated_and_reported(conn, make_export):
    path = make_export([device("111") + ["surprise"]], headers=HEADERS + ["New Column"])
    result = ingest.ingest_file(conn, path)

    assert result.rows == 1
    assert result.unknown_columns == ["New Column"]


def test_rollup_counts_match_raw(conn, make_export):
    path = make_export([
        device("111", queue="-", status="Online"),
        device("222", queue=0, status="Online"),
        device("333", queue=1, status="Offline", seen_at="01-01-26 10:00:00"),
        device("444", queue=2, status="-", seen_at="-"),
    ])
    result = ingest.ingest_file(conn, path)
    rollup.rollup_snapshot(conn, result.snapshot_id)

    kpi = conn.execute("SELECT * FROM fact_snapshot_kpi").fetchone()
    assert kpi["devices_total"] == 4
    assert kpi["devices_online"] == 2
    assert kpi["devices_inactive"] == 1
    assert kpi["devices_never_tasked"] == 1
    assert kpi["devices_completed"] == 1
    assert kpi["devices_pending"] == 2
    assert kpi["pending_tasks_total"] == 3   # 1 + 2
    assert kpi["never_seen"] == 1
