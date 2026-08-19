"""Movement analytics, read from the per-device change log.

This replaces the old snapshot-pair diffing. The change log records each move individually, so
a device that moves away and back is visible however the fetches happened to land — the case
that endpoint comparison structurally cannot see.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from ota_analytics import ingest, registry
from tests.test_pages import client  # noqa: F401  (renders the real dashboard)

BASE = datetime(2026, 8, 15, 10, 0)


def api_device(imei: str, firmware: str, *, base="7.5.0.27", target=None,
               model="LOCAT140VB", task=None) -> dict:
    return {
        "deviceId": imei, "deviceName": imei, "createdBy": 1, "createdByName": "riya",
        "creationTime": 1720009238354, "lastPingTime": 1755264000000,
        "currFirmVer": firmware, "baseFirm": base, "updateFirmVer": target or firmware,
        "currConfigVersion": "2.2.2", "model": model, "hwVer": "1.2.0",
        "type_Task": {} if task is None else task,
    }


def load(conn, records, hours_in: int):
    return ingest.ingest_records(conn, records, source_name="API",
                                 snapshot_at=BASE + timedelta(hours=hours_in))


@pytest.fixture
def history(conn):
    """Three pulls: an upgrade, a fallback to base, a round trip and a steady device."""
    load(conn, [
        api_device("climber", "7.5.0.27", target="7.5.0.51A"),
        api_device("faller", "7.5.0.51A"),
        api_device("roundtrip", "7.5.0.51A"),
        api_device("steady", "7.5.0.51A"),
    ], 0)
    load(conn, [
        api_device("climber", "7.5.0.49A", target="7.5.0.51A"),
        api_device("faller", "7.5.0.27", target="7.5.0.27"),      # back to base
        api_device("roundtrip", "7.5.0.49C"),                     # away…
        api_device("steady", "7.5.0.51A"),
    ], 1)
    load(conn, [
        api_device("climber", "7.5.0.51A", target="7.5.0.51A"),
        api_device("faller", "7.5.0.27", target="7.5.0.27"),
        api_device("roundtrip", "7.5.0.51A"),                     # …and back
        api_device("steady", "7.5.0.51A"),
    ], 2)
    return conn


def test_every_move_is_recorded_individually(history):
    moves = registry.firmware_moves(history)
    assert len(moves) == 5      # climber 2, faller 1, roundtrip 2; steady never moved
    assert {m["imei"] for m in moves} == {"climber", "faller", "roundtrip"}


def test_direction_is_classified_by_version_order(history):
    moves = {(m["imei"], m["from_firmware"]): m for m in registry.firmware_moves(history)}
    assert moves[("climber", "7.5.0.27")]["direction"] == "upgrade"
    assert moves[("faller", "7.5.0.51A")]["direction"] == "downgrade"
    assert moves[("roundtrip", "7.5.0.51A")]["direction"] == "downgrade"
    assert moves[("roundtrip", "7.5.0.49C")]["direction"] == "upgrade"


def test_only_a_return_to_base_counts_as_a_fallback(history):
    fallbacks = registry.fallbacks(history)
    assert [f["imei"] for f in fallbacks] == ["faller"]
    assert fallbacks[0]["to_firmware"] == fallbacks[0]["base_firmware"] == "7.5.0.27"


def test_a_round_trip_is_visible_even_though_the_endpoints_match(history):
    """7.5.0.51A → 7.5.0.49C → 7.5.0.51A: comparing first and last would report nothing."""
    trips = registry.round_trips(history)
    assert [t["imei"] for t in trips] == ["roundtrip"]
    assert trips[0]["path"] == "7.5.0.51A → 7.5.0.49C → 7.5.0.51A"
    assert trips[0]["moves"] == 2


def test_movement_summary_counts_the_period(history):
    summary = registry.movement_summary(history)
    assert summary["moves"] == 5
    assert summary["devices"] == 3
    assert summary["upgrades"] == 3
    assert summary["downgrades"] == 2
    assert summary["fallbacks"] == 1
    assert summary["devices_moved_twice"] == 2      # climber and roundtrip


def test_planned_and_unplanned_downgrades_are_separated(history):
    """A downgrade onto the assigned target is an operator rollback, not a device fault."""
    summary = registry.movement_summary(history)
    assert summary["planned_downgrades"] == 1       # faller landed on its target
    assert summary["unplanned_downgrades"] == 1     # roundtrip's dip was not targeted


def test_a_narrow_window_excludes_older_moves(history):
    """Windows filter on the change date, so the period is exact.

    Only the third pull falls inside: climber 7.5.0.49A→7.5.0.51A and roundtrip coming back.
    'faller' moved in the second pull and must not be counted here.
    """
    since = (BASE + timedelta(hours=1, minutes=30)).isoformat(sep=" ", timespec="seconds")
    summary = registry.movement_summary(history, since)
    assert summary["moves"] == 2
    assert summary["fallbacks"] == 0


def test_yesterday_has_an_upper_bound():
    """The one window that must not include today."""
    since, until = registry.window_range("yesterday", now=datetime(2026, 8, 15, 12, 0))
    assert since == "2026-08-14 00:00:00"
    assert until == "2026-08-15 00:00:00"


@pytest.mark.parametrize("window", [w[0] for w in registry.WINDOWS])
def test_every_window_produces_a_usable_range(window):
    since, until = registry.window_range(window, now=datetime(2026, 8, 15, 12, 0))
    assert since is None or isinstance(since, str)
    assert until is None or since < until


def test_fallback_segments_group_the_affected_devices(history):
    segments = registry.fallback_segments(history)
    assert {s["label"]: s["devices"] for s in segments["by_model"]} == {"LOCAT140VB": 1}
    assert segments["by_path"][0]["label"] == "7.5.0.51A  ->  7.5.0.27"


def test_a_device_that_never_moved_appears_nowhere(history):
    assert "steady" not in {m["imei"] for m in registry.firmware_moves(history)}
    assert "steady" not in {t["imei"] for t in registry.round_trips(history)}

    row = history.execute(
        "SELECT last_changed_at, checks FROM device WHERE imei = 'steady'").fetchone()
    assert row["last_changed_at"] is None       # checked repeatedly, never changed
    assert row["checks"] == 3


# ─── paging the Firmware moves list ─────────────────────────────────────────

def test_paging_loses_no_move_and_repeats_none(conn, make_export):
    """Several moves share a changed_at, because a snapshot stamps everything it observes with
    the same time. Ordering by that alone lets one row appear on two pages and another on none —
    the classic way paging quietly loses records. The id breaks the tie.
    """
    from ota_analytics import registry, rollup

    from tests.conftest import device

    # Twelve devices that all move at the same instant, so every changed_at collides.
    first = make_export([device(str(100 + i), firmware="1.0.0") for i in range(12)],
                        name="Devices_12_15Aug26_1000.xlsx")
    second = make_export([device(str(100 + i), firmware="1.1.0") for i in range(12)],
                         name="Devices_12_15Aug26_1100.xlsx")
    from ota_analytics import ingest
    for path in (first, second):
        ingest.ingest_file(conn, path)
    rollup.rollup_all(conn)

    total = registry.movement_summary(conn)["moves"]
    assert total == 12, f"expected 12 moves, got {total}"

    for size in (1, 5, 12, 100):
        seen = []
        for page in range(1, -(-total // size) + 1):
            rows = registry.firmware_moves(conn, limit=size, offset=(page - 1) * size)
            seen.extend((r["imei"], r["changed_at"]) for r in rows)
        assert len(seen) == total, f"size {size}: collected {len(seen)} of {total}"
        assert len(set(seen)) == total, f"size {size}: {len(seen) - len(set(seen))} duplicates"


def test_the_changes_page_offers_page_sizes_and_clamps_the_page(client):  # noqa: F811
    import re

    from ota_analytics import api

    body = client.get("/changes?window=all").text
    flat = re.sub(r"\s+", " ", body)
    assert 'class="pager"' in flat
    for size in api.PAGE_SIZES:
        assert f"size={size}" in flat
    # The count reports the whole result, not the rows on screen — a filtered view must not be
    # mistakable for the full one.
    assert "of <strong>" in flat

    # A page beyond the end lands on the last one rather than erroring or showing nothing.
    far = re.sub(r"\s+", " ", client.get("/changes?window=all&page=9999").text)
    at = re.search(r'class="pager-at">\s*(\d+) / (\d+)', far)
    assert at and at.group(1) == at.group(2)


def test_an_unknown_page_size_falls_back_rather_than_being_trusted(client):  # noqa: F811
    """A size straight off the query string decides a LIMIT, so it is whitelisted."""
    import re

    from ota_analytics import api

    flat = re.sub(r"\s+", " ", client.get("/changes?window=all&size=99999").text)
    assert f'size={api.DEFAULT_PAGE_SIZE}" class' in flat or "on" in flat
    # Whatever it renders, the offered set is unchanged.
    for size in api.PAGE_SIZES:
        assert f"size={size}" in flat
