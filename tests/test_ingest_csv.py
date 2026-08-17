"""Loading a .csv export.

The point of these tests is that CSV is not a second, looser path into the warehouse. It goes
through the same header mapping and the same normalization as the spreadsheet, so every rule the
.xlsx obeys applies to it too — most are checked by comparing the two formats against each other
rather than against a hand-written expectation.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ota_analytics import ingest, quality

from tests.conftest import HEADERS, device


def state(conn, imei: str) -> dict:
    row = conn.execute("SELECT * FROM device_state WHERE imei = ?", (imei,)).fetchone()
    assert row is not None, f"{imei} was not ingested"
    return dict(row)


# ─── the same file in both formats must land identically ────────────────────

CELLS = ["device_model", "device_model_raw", "firmware", "firmware_raw", "fw_family",
         "configuration", "status", "queue", "queue_state", "seen_at", "iccid", "hw_ver",
         "vin", "vin_raw", "groups_raw", "first_ping"]


def test_csv_and_xlsx_produce_the_same_rows(tmp_path, make_export, make_csv):
    """One normalization contract, two containers. If these diverge, one format has a shortcut."""
    from ota_analytics import db

    rows = [
        device("111", queue="-", firmware="7.5.0.27", model="AX1_sCAN"),
        device("222", queue=0),
        device("333", queue=2, status="Offline", seen_at="01-08-26 10:00:00",
               model="-", firmware="-", groups="-", vin="DL1CAB1234"),
    ]
    xlsx = db.connect(tmp_path / "x.db")
    csv_conn = db.connect(tmp_path / "c.db")
    ingest.ingest_file(xlsx, make_export(rows, name="Devices_3_15Aug26_1511.xlsx"))
    ingest.ingest_file(csv_conn, make_csv(rows, name="Devices_3_15Aug26_1511.csv"))

    for imei in ("111", "222", "333"):
        left, right = state(xlsx, imei), state(csv_conn, imei)
        for cell in CELLS:
            assert left[cell] == right[cell], f"{imei}.{cell}: {left[cell]!r} vs {right[cell]!r}"
    xlsx.close()
    csv_conn.close()


def test_the_null_marker_still_becomes_null_in_csv(conn, make_csv):
    """'-' is the export's null marker and must never reach a chart as a category."""
    ingest.ingest_file(conn, make_csv([device("111", model="-", firmware="-", groups="-")]))
    row = state(conn, "111")
    assert row["device_model"] is None
    assert row["firmware"] is None
    assert row["groups_raw"] is None


def test_queue_states_survive_csv_strings(conn, make_csv):
    """CSV gives every cell as text, so '-' / '0' / '2' all arrive as strings."""
    ingest.ingest_file(conn, make_csv([
        device("111", queue="-"), device("222", queue=0), device("333", queue=2)]))

    assert (state(conn, "111")["queue"], state(conn, "111")["queue_state"]) == (None, "never_tasked")
    assert (state(conn, "222")["queue"], state(conn, "222")["queue_state"]) == (0, "completed")
    assert (state(conn, "333")["queue"], state(conn, "333")["queue_state"]) == (2, "pending")


def test_day_first_dates_are_not_read_month_first(conn, make_csv):
    """15-08-26 is 15 Aug 2026. A month-first misread would corrupt every date silently."""
    ingest.ingest_file(conn, make_csv([device("111", seen_at="15-08-26 10:00:00")]))
    assert state(conn, "111")["seen_at"].startswith("2026-08-15 10:00")


def test_the_snapshot_time_comes_from_the_csv_filename(conn, make_csv):
    result = ingest.ingest_file(conn, make_csv([device("111")],
                                               name="Devices_1_16Aug26_0930.csv"))
    assert result.ts_source == "filename"
    assert result.snapshot_at.strftime("%Y-%m-%d %H:%M") == "2026-08-16 09:30"


def test_a_csv_is_idempotent_like_a_spreadsheet(conn, make_csv):
    path = make_csv([device("111"), device("222")])
    first = ingest.ingest_file(conn, path)
    again = ingest.ingest_file(conn, path)

    assert first.status == "ingested"
    assert again.status == "already_ingested"
    assert again.snapshot_id == first.snapshot_id
    assert conn.execute("SELECT COUNT(*) FROM snapshot").fetchone()[0] == 1


def test_column_order_does_not_matter(conn, make_csv):
    """Headers are matched by name, so a colleague's reordered columns still load."""
    reordered = list(reversed(HEADERS))
    row = list(reversed(device("111", firmware="7.5.0.51A", model="LOCAT140VB")))
    ingest.ingest_file(conn, make_csv([row], headers=reordered))

    assert state(conn, "111")["firmware"] == "7.5.0.51A"
    assert state(conn, "111")["device_model"] == "LOCAT140VB"


def test_a_csv_missing_a_required_column_is_refused(conn, make_csv):
    with pytest.raises(ingest.IngestError, match="missing required column"):
        ingest.ingest_file(conn, make_csv([["111", "Online"]], headers=["IMEI", "STATUS"]))


def test_both_formats_are_picked_up_from_a_folder(conn, make_export, make_csv, tmp_path):
    make_export([device("111")], name="Devices_1_15Aug26_1000.xlsx")
    make_csv([device("222")], name="Devices_1_15Aug26_1100.csv")

    results = ingest.ingest_dir(conn, tmp_path)
    assert [r.status for r in results] == ["ingested", "ingested"]
    # Oldest first, so the diff between them builds in the right direction.
    assert [r.path.suffix for r in results] == [".xlsx", ".csv"]


# ─── a report of our own is loadable, and says so ───────────────────────────

REPORT_HEADERS = ["IMEI", "Model", "Firmware", "Fallback", "Previous firmware",
                  "Target firmware", "Base firmware", "Configuration", "Hardware", "Status",
                  "Task state", "Pending tasks", "Last seen", "Hours since seen",
                  "Last firmware change", "Last checked", "Groups", "VIN", "ICCID"]


def report_row(imei: str) -> list:
    return [imei, "LOCAT140VB", "7.5.0.51A", "", "7.5.0.27", "7.5.0.51A", "7.5.0.27",
            "2.2.2", "1.2.0", "Online", "completed", 0, "2026-08-15 10:00:00", 5.2,
            "2026-08-15 09:00:00", "2026-08-15 15:11:00", "49A 7k", "MAT562014RKP83714",
            "8991119018554142514"]


def test_a_dashboard_report_loads(conn, make_csv):
    """This is what was asked for: a colleague's data as a CSV this dashboard wrote."""
    path = make_csv([report_row("111")], name="devices_17Aug26_1029.csv",
                    headers=REPORT_HEADERS)
    result = ingest.ingest_file(conn, path)

    assert result.status == "ingested"
    row = state(conn, "111")
    assert row["device_model"] == "LOCAT140VB"
    assert row["firmware"] == "7.5.0.51A"
    assert row["hw_ver"] == "1.2.0"                     # 'Hardware', not 'hwVer'
    assert row["update_firmware"] == "7.5.0.51A"
    assert row["queue_state"] == "completed"
    assert row["seen_at"].startswith("2026-08-15 10:00")  # ISO, not the platform's day-first


def test_loading_a_report_is_recorded_as_second_hand(conn, make_csv):
    """Its values have been normalized once already, and it drops columns the platform sends.

    None of that is visible from the numbers afterwards, so it is written down at ingest.
    """
    result = ingest.ingest_file(conn, make_csv(
        [report_row("111")], name="devices_17Aug26_1029.csv", headers=REPORT_HEADERS))

    rules = {rule for rule, *_ in result.findings}
    assert "loaded_from_report" in rules

    stored = {r["rule"] for r in conn.execute(
        "SELECT rule FROM quality_issue WHERE snapshot_id = ?", (result.snapshot_id,))}
    assert "loaded_from_report" in stored


def test_a_platform_export_is_not_flagged_as_a_report(conn, make_csv):
    """The flag has to mean something, so the ordinary case must not trip it."""
    result = ingest.ingest_file(conn, make_csv([device("111")]))
    assert "loaded_from_report" not in {rule for rule, *_ in result.findings}


def test_report_detection_needs_more_than_one_derived_column():
    """One shared heading is a coincidence; several are a signature."""
    assert ingest.looks_like_dashboard_report(tuple(REPORT_HEADERS)) is True
    assert ingest.looks_like_dashboard_report(tuple(HEADERS)) is False
    assert ingest.looks_like_dashboard_report(("IMEI", "Fallback")) is False


def test_the_quality_rule_reads_as_advice_not_an_error(conn, make_csv):
    result = ingest.ingest_file(conn, make_csv(
        [report_row("111")], name="devices_17Aug26_1029.csv", headers=REPORT_HEADERS))
    finding = next(f for f in result.findings if f[0] == "loaded_from_report")
    severity, detail = finding[1], finding[4]

    assert severity == "medium"           # not high: the data is usable, just second-hand
    assert "second-hand" in detail
    assert "bundle" in detail
