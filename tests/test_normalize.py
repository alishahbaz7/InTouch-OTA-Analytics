"""Normalization rules — one test per documented quirk in the source data."""

from __future__ import annotations

from datetime import datetime

import pytest

from ota_analytics import normalize as n


@pytest.mark.parametrize("value,expected", [
    ("-", None), ("  -  ", None), ("", None), ("   ", None), (None, None),
    ("N/A", None), ("7.5.0.51A", "7.5.0.51A"), ("  spaced  ", "spaced"), (0, "0"),
])
def test_clean_collapses_null_markers(value, expected):
    assert n.clean(value) == expected


@pytest.mark.parametrize("value,expected", [
    ("AX1_SCAN", "AX1_SCAN"), ("AX1_sCAN", "AX1_SCAN"), ("sCAN_AX1", "AX1_SCAN"),
    ("LOCAT140VB", "LOCAT140VB"), ("-", None),
])
def test_canon_model_collapses_spelling_variants(value, expected):
    assert n.canon_model(value) == expected


@pytest.mark.parametrize("value,expected", [
    ("V7.2.2", "7.2.2"), ("7.2.2", "7.2.2"), ("v7.2.2", "7.2.2"),
    ("VERSION2", "VERSION2"),  # only strip V when a digit follows
    ("-", None),
])
def test_canon_firmware_strips_v_prefix(value, expected):
    assert n.canon_firmware(value) == expected


@pytest.mark.parametrize("status,expected", [
    ("Online", "Online"), ("Offline", "Offline"), ("Inactive", "Inactive"),
    ("-", "Inactive"), ("", "Inactive"), ("  online ", "Online"),
])
def test_canon_status_treats_dash_as_inactive(status, expected):
    """'-' and 'Inactive' are the same state: the device has never pinged."""
    assert n.canon_status(status) == expected


def test_fw_sortkey_orders_versions_correctly():
    versions = ["7.5.0.9", "7.5.0.51A", "7.5.0.27", "7.5.0.49A", "7.5.0.40A", "7.5.0.40"]
    ordered = sorted(versions, key=n.fw_sortkey)
    assert ordered == ["7.5.0.9", "7.5.0.27", "7.5.0.40", "7.5.0.40A", "7.5.0.49A", "7.5.0.51A"]


def test_fw_sortkey_handles_zero_padded_builds():
    assert n.fw_sortkey("2.0.0161") < n.fw_sortkey("2.0.0162")
    assert n.fw_sortkey("2.0.099") < n.fw_sortkey("2.0.0125")


@pytest.mark.parametrize("firmware,family", [
    ("7.5.0.51A", "7.5.x"), ("2.0.0161", "2.0.x"), ("5.00.01.03R", "5.00.x"), (None, None),
])
def test_fw_family(firmware, family):
    assert n.fw_family(firmware) == family


def test_parse_dt_is_day_first():
    """15-08-26 must be 15 August 2026 — a month-first misread would corrupt every date."""
    assert n.parse_dt("15-08-26 15:11:17") == "2026-08-15 15:11:17"
    assert n.parse_dt("09-05-26 15:25:58") == "2026-05-09 15:25:58"
    assert n.parse_dt("-") is None
    assert n.parse_dt("garbage") is None


def test_parse_dt_accepts_real_datetimes():
    assert n.parse_dt(datetime(2026, 8, 15, 15, 11, 17)) == "2026-08-15 15:11:17"


@pytest.mark.parametrize("raw,expected", [
    ("-", (None, "never_tasked")),
    (None, (None, "never_tasked")),
    (0, (0, "completed")),
    ("0", (0, "completed")),
    (1, (1, "pending")),
    (5, (5, "pending")),
    ("oops", (None, "unknown")),
])
def test_parse_queue_distinguishes_never_tasked_from_completed(raw, expected):
    """'-' (never assigned a task) and 0 (assigned and finished) are different facts."""
    assert n.parse_queue(raw) == expected


def test_split_groups():
    assert n.split_groups("49A 7k, 51A 4K") == ["49A 7k", "51A 4K"]
    assert n.split_groups("-") == []
    assert n.split_groups("a, -, a, b") == ["a", "b"]


def test_snapshot_at_from_filename():
    assert n.snapshot_at_from_filename("Devices_35477_15Aug26_1511.xlsx") == \
        datetime(2026, 8, 15, 15, 11)
    assert n.snapshot_at_from_filename("export.xlsx") is None


def test_canon_vin_drops_placeholder():
    assert n.canon_vin("DL1CAB1234") is None
    assert n.canon_vin("MA3ABCDE12345") == "MA3ABCDE12345"


def test_hours_between():
    assert n.hours_between("2026-08-15 12:00:00", "2026-08-14 12:00:00") == 24.0
    assert n.hours_between("2026-08-15 12:00:00", None) is None
