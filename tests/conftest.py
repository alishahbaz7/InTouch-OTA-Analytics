"""Test fixtures.

Tests build tiny exports on the fly rather than reading the 22 MB sample — the fixture carries
every quirk found in the real data, so it exercises the same code paths in milliseconds.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from openpyxl import Workbook

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ota_analytics import db  # noqa: E402

HEADERS = [
    "IMEI", "STATUS", "QUEUE", "Device Name/VIN", "Created By", "Device Model", "FIRMWARE",
    "CONFIGURATION", "SEEN AT", "ICCID", "hwVer", "vin", "Groups", "First Ping",
]


def device(
    imei: str,
    *,
    status: str = "Online",
    queue: object = 0,
    model: str = "LOCAT140VB",
    firmware: str = "7.5.0.51A",
    configuration: str = "2.2.2",
    seen_at: str = "15-08-26 10:00:00",
    iccid: str = "8991119018554142514",
    hw_ver: str = "1.2.0",
    vin: str = "DL1CAB1234",
    groups: str = "49A 7k",
    first_ping: str = "09-05-26 15:25:58",
) -> list:
    """One export row, defaulting to a healthy up-to-date device."""
    return [imei, status, queue, imei, "riya", model, firmware, configuration,
            seen_at, iccid, hw_ver, vin, groups, first_ping]


@pytest.fixture
def make_export(tmp_path: Path):
    """Write rows to an .xlsx whose filename encodes the snapshot timestamp."""
    def _make(rows: list[list], name: str = "Devices_3_15Aug26_1511.xlsx",
              headers: list[str] | None = None) -> Path:
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "data"
        sheet.append(headers if headers is not None else HEADERS)
        for row in rows:
            sheet.append(row)
        path = tmp_path / name
        workbook.save(path)
        return path
    return _make


@pytest.fixture
def make_csv(tmp_path: Path):
    """Write rows to a .csv whose filename encodes the snapshot timestamp.

    Mirrors make_export so the two formats can be checked against each other rather than each
    against its own expectations.
    """
    import csv as _csv

    def _make(rows: list[list], name: str = "Devices_3_15Aug26_1511.csv",
              headers: list[str] | None = None) -> Path:
        path = tmp_path / name
        with open(path, "w", encoding="utf-8-sig", newline="") as handle:
            writer = _csv.writer(handle, lineterminator="\n")
            writer.writerow(headers if headers is not None else HEADERS)
            writer.writerows(rows)
        return path
    return _make


@pytest.fixture
def conn(tmp_path: Path):
    connection = db.connect(tmp_path / "test.db")
    yield connection
    connection.close()
