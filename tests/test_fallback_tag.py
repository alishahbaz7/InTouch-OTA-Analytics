"""The fallback tag, exactly as the platform owner defines it.

    FALLBACK = the OTA task completed (QUEUE = 0) AND the device is on its BASE firmware.

The task closed, yet the device sits on the build it shipped with — so it went back. The rule
needs no history, which is its point: a device that fell back before this system existed is
still tagged, and watching for the move could never do that.
"""

from __future__ import annotations

from datetime import datetime

import pytest

pytest.importorskip("fastapi")

from ota_analytics import ingest, metrics  # noqa: E402
from tests.test_pages import client  # noqa: F401,E402


def api_device(imei: str, *, firmware: str, base: str, target: str | None = None,
               task=None, model="LOCAT140VB") -> dict:
    return {
        "deviceId": imei, "deviceName": imei, "createdBy": 1, "createdByName": "riya",
        "creationTime": 1720009238354, "lastPingTime": 1755264000000,
        "currFirmVer": firmware, "baseFirm": base, "updateFirmVer": target or firmware,
        "currConfigVersion": "2.2.2", "model": model, "hwVer": "1.2.0",
        "type_Task": {} if task is None else task,
    }


@pytest.fixture
def fleet(conn):
    """One of each case the rule has to separate."""
    result = ingest.ingest_records(conn, [
        # completed + on base, targeted higher -> the unambiguous fallback
        api_device("fell_back", firmware="7.5.0.27", base="7.5.0.27", target="7.5.0.49A"),
        # completed + on base, but base IS the target -> satisfied its instruction
        api_device("target_is_base", firmware="7.5.0.27", base="7.5.0.27", target="7.5.0.27"),
        # completed, moved off base -> a successful update
        api_device("updated", firmware="7.5.0.51A", base="7.5.0.27", target="7.5.0.51A"),
        # still pending, on base -> has not finished, so nothing is concluded yet
        api_device("pending", firmware="7.5.0.27", base="7.5.0.27", target="7.5.0.49A",
                   task={"1": 1400}),
        # never tasked, on base -> was never asked to move
        api_device("never_tasked", firmware="7.5.0.27", base="7.5.0.27"),
    ], source_name="API", snapshot_at=datetime(2026, 8, 15, 12, 0))
    # 'never_tasked' must genuinely have no task recorded
    conn.execute("UPDATE device_snapshot SET queue_state='never_tasked', queue=NULL "
                 "WHERE imei='never_tasked'")
    conn.commit()
    return conn, result.snapshot_id


def tagged(conn, snapshot_id) -> set[str]:
    return {d["imei"] for d in metrics.fallback_devices(conn, snapshot_id)}


# ─── the rule ───────────────────────────────────────────────────────────────

def test_completed_and_on_base_is_tagged(fleet):
    conn, sid = fleet
    assert "fell_back" in tagged(conn, sid)


def test_a_completed_update_that_moved_off_base_is_not(fleet):
    conn, sid = fleet
    assert "updated" not in tagged(conn, sid)


def test_a_pending_task_is_not_tagged(fleet):
    """Nothing has concluded while the task is still outstanding."""
    conn, sid = fleet
    assert "pending" not in tagged(conn, sid)


def test_a_never_tasked_device_is_not_tagged(fleet):
    """It shipped on base and was never asked to move — that is not falling back."""
    conn, sid = fleet
    assert "never_tasked" not in tagged(conn, sid)


def test_target_equal_to_base_is_tagged_but_separated(fleet):
    """It satisfies the rule literally, so it is tagged — and split out, not silently dropped."""
    conn, sid = fleet
    summary = metrics.fallback_summary(conn, sid)

    assert summary["total"] == 2                 # fell_back + target_is_base
    assert summary["missed_target"] == 1         # only fell_back was owed something newer
    assert summary["target_was_base"] == 1

    strict = {d["imei"] for d in metrics.fallback_devices(conn, sid, missed_target_only=True)}
    assert strict == {"fell_back"}


def test_a_device_with_no_base_firmware_is_never_tagged(conn):
    """The spreadsheet export carries no base firmware, so it must make no claim."""
    from tests.conftest import device

    ingest.ingest_records(conn, [{
        "deviceId": "111", "currFirmVer": "7.5.0.27", "model": "LOCAT140VB",
        "type_Task": {}, "lastPingTime": 1755264000000,
    }], source_name="API", snapshot_at=datetime(2026, 8, 15, 12, 0))
    sid = metrics.latest_snapshot_id(conn)
    assert tagged(conn, sid) == set()
    assert device  # keep the import meaningful for the reader


def test_the_breakdown_groups_by_version_path(fleet):
    conn, sid = fleet
    rows = metrics.fallback_breakdown(conn, sid)
    paths = {(r["base_firmware"], r["update_firmware"]): r["devices"] for r in rows}
    assert paths[("7.5.0.27", "7.5.0.49A")] == 1


# ─── the tag on the page and in the file ────────────────────────────────────

def test_the_devices_page_shows_the_tag(client):  # noqa: F811
    body = client.get("/devices?fallback=yes").text
    assert "tag-fallback" in body
    assert "fallback" in body.lower()


def test_the_filter_narrows_to_tagged_devices(client):  # noqa: F811
    all_devices = client.get("/devices").text
    only_fallback = client.get("/devices?fallback=yes").text
    assert only_fallback.count("tag-fallback") <= all_devices.count("tag-fallback")
    assert client.get("/devices?fallback=missed").status_code == 200


def test_the_export_spells_the_tag_out(client):  # noqa: F811
    """A '1' in a column called is_fallback means nothing to someone reading the file."""
    import csv
    import io

    body = client.get("/devices/export?format=csv").text.lstrip("﻿")
    rows = list(csv.DictReader(io.StringIO(body)))
    assert "Fallback" in rows[0]
    for row in rows:
        assert row["Fallback"] in ("", "FALLBACK — missed target", "FALLBACK — target is base")


def test_tagged_devices_can_be_downloaded_as_an_imei_list(client):  # noqa: F811
    response = client.get("/devices/export?fallback=yes&format=txt")
    assert response.status_code == 200
    assert not response.content.startswith(b"\xef\xbb\xbf")   # pasteable
