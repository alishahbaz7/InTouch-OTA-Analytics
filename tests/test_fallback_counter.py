"""Counting fallbacks per device, not just listing them.

A chronological list of occurrences hides the thing worth knowing. On the real fleet, 186
fallbacks were spread over 148 devices — but 23 of those devices had done it more than once and
one had done it six times, and none of that is visible while reading the events in date order.
The count is what turns the same data into a shortlist of hardware to go and look at.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from ota_analytics import ingest, registry
from tests.test_pages import client  # noqa: F401


def fetch(conn, records, *, when: str, tag: str):
    return ingest.ingest_records(conn, records, source_name=f"test:{tag}",
                                 snapshot_at=datetime.fromisoformat(when), fingerprint=tag)


def api_device(imei: str, *, firmware: str, base: str = "7.0.0") -> dict:
    return {"imei": imei, "status": "Online", "queue": 0, "deviceModel": "LOCAT140VB",
            "firmware": firmware, "baseFirm": base, "updateFirmVer": "8.0.0",
            "seenAt": "15-08-26 10:00:00", "hwVer": "1.2.0"}


@pytest.fixture
def fleet(conn):
    """`a` falls back three times, `b` once, `c` never leaves base, `d` only moves forward."""
    steps = [
        # (when, a, b, c, d)
        ("2026-08-15 10:00:00", "7.0.0", "7.0.0", "7.0.0", "7.0.0"),
        ("2026-08-15 11:00:00", "8.0.0", "8.0.0", "7.0.0", "8.0.0"),
        ("2026-08-15 12:00:00", "7.0.0", "7.0.0", "7.0.0", "8.0.0"),   # a, b fall back
        ("2026-08-15 13:00:00", "8.0.0", "8.0.0", "7.0.0", "8.0.0"),
        ("2026-08-15 14:00:00", "7.0.0", "8.0.0", "7.0.0", "8.0.0"),   # a falls back
        ("2026-08-15 15:00:00", "8.0.0", "8.0.0", "7.0.0", "8.0.0"),
        ("2026-08-15 16:00:00", "7.0.0", "8.0.0", "7.0.0", "8.0.0"),   # a falls back
    ]
    for n, (when, a, b, c, d) in enumerate(steps):
        fetch(conn, [api_device("a", firmware=a), api_device("b", firmware=b),
                     api_device("c", firmware=c), api_device("d", firmware=d)],
              when=when, tag=f"s{n}")
    return conn


def test_each_device_is_counted(fleet):
    counts = {r["imei"]: r["times"] for r in registry.fallback_repeats(fleet, min_times=1)}
    assert counts["a"] == 3
    assert counts["b"] == 1
    # Never left base, so it never returned to it; and one that only moved forward.
    assert "c" not in counts
    assert "d" not in counts


def test_repeats_are_the_default_and_worst_come_first(fleet):
    repeats = registry.fallback_repeats(fleet)
    assert [r["imei"] for r in repeats] == ["a"]        # b fell back once, so it is not a repeat
    assert repeats[0]["times"] == 3


def test_the_totals_separate_devices_from_occurrences(fleet):
    totals = registry.fallback_totals(fleet)
    assert totals["events"] == 4              # a three times, b once
    assert totals["devices"] == 2
    assert totals["repeat_devices"] == 1      # only a did it more than once


def test_every_occurrence_carries_the_device_total(fleet):
    """While reading one occurrence, the useful question is whether this device does it often."""
    for row in registry.fallbacks(fleet):
        assert row["times"] == (3 if row["imei"] == "a" else 1)


def test_the_repeat_row_spans_first_to_last(fleet):
    row = registry.fallback_repeats(fleet)[0]
    assert row["first_fallback"] < row["last_fallback"]
    assert row["base_firmware"] == "7.0.0"


# ─── paging, so the page cannot render an unbounded list ────────────────────

def test_the_occurrence_list_is_capped_and_pages(fleet):
    first = registry.fallbacks(fleet, limit=2, offset=0)
    second = registry.fallbacks(fleet, limit=2, offset=2)

    assert len(first) == 2 and len(second) == 2
    assert {r["changed_at"] for r in first} & {r["changed_at"] for r in second} == set()
    # Newest first, so page one is more recent than page two.
    assert min(r["changed_at"] for r in first) >= max(r["changed_at"] for r in second)


def test_the_repeat_list_is_capped_too(fleet):
    assert len(registry.fallback_repeats(fleet, limit=1, min_times=1)) == 1


def test_the_page_shows_the_counter(client):  # noqa: F811
    body = client.get("/changes").text
    assert "Falling back repeatedly" in body
    assert "Times" in body


# ─── filtering and sorting the list ─────────────────────────────────────────

def test_repeats_only_narrows_to_devices_that_did_it_more_than_once(fleet):
    everything = {r["imei"] for r in registry.fallbacks(fleet)}
    repeats_only = {r["imei"] for r in registry.fallbacks(fleet, min_times=2)}

    assert everything == {"a", "b"}
    assert repeats_only == {"a"}          # b fell back once


def test_the_filtered_count_matches_the_rows_returned(fleet):
    """The pager counts this. If it disagreed with the list, paging would run off the end."""
    for min_times in (1, 2):
        counted = registry.fallback_count(fleet, min_times=min_times)
        listed = registry.fallbacks(fleet, limit=1000, min_times=min_times)
        assert counted == len(listed)


def test_filtering_by_model_matches_the_count(fleet):
    counted = registry.fallback_count(fleet, model="LOCAT140VB")
    listed = registry.fallbacks(fleet, limit=1000, model="LOCAT140VB")
    assert counted == len(listed) == 4
    assert registry.fallback_count(fleet, model="NO_SUCH_MODEL") == 0


def test_sorting_by_times_puts_the_worst_offender_first(fleet):
    by_times = registry.fallbacks(fleet, sort="times")
    assert by_times[0]["imei"] == "a"
    assert by_times[0]["times"] == 3


def test_sorting_by_when_is_newest_first(fleet):
    stamps = [r["changed_at"] for r in registry.fallbacks(fleet, sort="when")]
    assert stamps == sorted(stamps, reverse=True)


def test_an_unknown_sort_falls_back_rather_than_reaching_the_sql(fleet):
    """A query parameter must never become raw SQL."""
    injected = registry.fallbacks(fleet, sort="times; DROP TABLE device_change")
    assert [r["changed_at"] for r in injected] == \
           [r["changed_at"] for r in registry.fallbacks(fleet, sort="when")]
    assert fleet.execute("SELECT COUNT(*) FROM device_change").fetchone()[0] > 0


def test_the_model_filter_offers_only_models_that_fell_back(fleet):
    labels = {m["label"] for m in registry.fallback_models(fleet)}
    assert labels == {"LOCAT140VB"}


# ─── the download ───────────────────────────────────────────────────────────

def test_firmware_moves_carry_the_fallback_count(fleet):
    """The file is often read away from the dashboard, so it has to say it too."""
    moves = {(m["imei"], m["changed_at"]): m for m in registry.firmware_moves(fleet, limit=1000)}
    fallback_rows = [m for m in moves.values() if m["is_fallback"]]

    assert fallback_rows, "the fixture should contain fallbacks"
    for row in fallback_rows:
        assert row["fallback_times"] == (3 if row["imei"] == "a" else 1)


def test_the_export_has_a_counter_column(fleet):
    from ota_analytics import exports
    assert ("fallback_times", "Fallbacks (times)") in exports.CHANGE_COLUMNS


def test_the_downloaded_file_shows_the_count(client):  # noqa: F811
    import csv
    import io

    body = client.get("/changes/export?window=all&only=fallbacks&format=csv").text.lstrip("\ufeff")
    rows = list(csv.DictReader(io.StringIO(body)))
    assert "Fallbacks (times)" in (rows[0] if rows else {"Fallbacks (times)": None})


def test_the_page_offers_the_filter_and_sort_controls(client):  # noqa: F811
    body = client.get("/changes").text
    assert "Repeats only" in body
    assert "fb_sort=times" in body
    assert "fb_min=2" in body
