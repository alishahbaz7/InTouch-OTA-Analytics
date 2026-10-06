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


# This machine's own databases: the dev copy's beside the repo, and the release's in dist/. A
# test that forgot to point the database at a temp file opened the dev one and migrated it — so
# opening either is now a failure, not a quiet write to someone's live history.
_REPO = Path(__file__).resolve().parent.parent
_LIVE_DATABASES = {(_REPO / "data" / "ota_analytics.db").resolve(),
                   (_REPO / "dist" / "InTouchOTA-Analytics" / "data" / "ota_analytics.db").resolve()}


@pytest.fixture(autouse=True)
def isolated_data(tmp_path_factory, monkeypatch):
    """Every file the app writes under data/ goes to a temp folder for every test — the database,
    both connection settings, the scheduler's state, the error log and the session key. Seven
    tests rendered pages without this and were writing to the dev copy's real data."""
    from ota_analytics import config, cota_connection, errors, scheduler, sources

    data = tmp_path_factory.mktemp("data")
    monkeypatch.setattr(config, "DATA_DIR", data)
    monkeypatch.setattr(config, "DB_PATH", data / "ota_analytics.db")
    monkeypatch.setattr(config, "EXPORT_DIR", data / "exports")
    monkeypatch.setattr(config, "REPORT_DIR", data / "reports")
    monkeypatch.setattr(cota_connection, "SETTINGS_PATH", data / "cota_connection.json")
    monkeypatch.setattr(errors, "LOG_PATH", data / "errors.log")
    monkeypatch.setattr(scheduler, "STATE_PATH", data / "scheduler.json")
    monkeypatch.setattr(sources, "SETTINGS_PATH", data / "connection.json")
    monkeypatch.setattr(scheduler, "_scheduler", None)
    return data


class _MemoryKeyring:
    """An empty credential store per test, in place of Windows Credential Manager."""

    def __init__(self):
        self.store = {}

    def set_password(self, service, user, secret):
        self.store[(service, user)] = secret

    def get_password(self, service, user):
        return self.store.get((service, user))

    def delete_password(self, service, user):
        self.store.pop((service, user), None)


@pytest.fixture(autouse=True)
def no_real_network_or_credentials(monkeypatch):
    """No test reaches the network or the real credential store.

    A runner test once started a real background run, which used the real cloud client and the
    real stored token — one live request to the desk device. Now every HTTP client a test did not
    give a fake transport fails before anything leaves the machine, and every test starts with an
    empty in-memory credential store. Tests that fake the cloud pass their own transport (or
    patch the client) as before; that still works, because it is checked first.
    """
    import httpx

    from ota_analytics import sources

    real_client = httpx.Client

    def refuse(request):
        raise RuntimeError(f"tests may not reach the network: {request.method} {request.url}")

    def guarded(*args, **kwargs):
        kwargs.setdefault("transport", httpx.MockTransport(refuse))
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", guarded)
    keyring = _MemoryKeyring()
    monkeypatch.setattr(sources, "_keyring", lambda: keyring)
    monkeypatch.delenv("OTA_COTA_TOKEN", raising=False)
    monkeypatch.delenv("OTA_PLATFORM_PASSWORD", raising=False)
    return keyring


@pytest.fixture(autouse=True)
def never_the_live_database(isolated_data, monkeypatch):
    from ota_analytics import config

    real_connect = db.connect

    def guarded(db_path=None, **kwargs):
        target = Path(db_path or config.DB_PATH).resolve()
        if target in _LIVE_DATABASES:
            pytest.fail(f"a test opened the live database {target} — use the `client` or "
                        "`conn` fixture, which point it at a temp file")
        return real_connect(db_path, **kwargs)

    monkeypatch.setattr(db, "connect", guarded)


@pytest.fixture
def conn(tmp_path: Path):
    connection = db.connect(tmp_path / "test.db")
    yield connection
    connection.close()
