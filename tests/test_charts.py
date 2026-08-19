"""Chart data: hourly activity and the donut breakdowns."""

from __future__ import annotations

import pytest

from ota_analytics import ingest, metrics, rollup
from tests.conftest import device


@pytest.fixture
def snapshot(conn, make_export):
    """Snapshot taken 15 Aug 2026 15:11, with pings spread across the preceding hours."""
    path = make_export([
        device("a", seen_at="15-08-26 15:05:00"),                    # current hour
        device("b", seen_at="15-08-26 15:00:30"),                    # current hour
        device("c", seen_at="15-08-26 14:20:00"),                    # one hour back
        device("d", seen_at="15-08-26 09:45:00"),                    # six hours back
        device("e", status="Offline", seen_at="14-08-26 20:00:00"),  # ~19 hours back
        device("f", status="Offline", seen_at="01-08-26 10:00:00"),  # far outside the window
        device("g", status="-", seen_at="-"),                        # never pinged
    ])
    result = ingest.ingest_file(conn, path)
    rollup.rollup_snapshot(conn, result.snapshot_id)
    return conn, result.snapshot_id


def test_hourly_activity_has_one_bucket_per_hour(snapshot):
    conn, sid = snapshot
    series = metrics.hourly_activity(conn, sid, hours=24)
    assert len(series) == 24
    assert series[-1]["is_current"] is True
    assert series[-1]["hour"] == "15:00"      # ends at the snapshot's clock hour
    assert series[0]["hour"] == "16:00"       # 23 hours earlier


def test_hourly_activity_counts_devices_in_the_right_hour(snapshot):
    conn, sid = snapshot
    by_hour = {p["hour"]: p["devices"] for p in metrics.hourly_activity(conn, sid, hours=24)}
    assert by_hour["15:00"] == 2      # a and b
    assert by_hour["14:00"] == 1      # c
    assert by_hour["09:00"] == 1      # d
    assert by_hour["20:00"] == 1      # e, previous evening


def test_hourly_activity_excludes_old_and_never_seen_devices(snapshot):
    """A device dark for two weeks and one that never pinged must not appear anywhere."""
    conn, sid = snapshot
    total = sum(p["devices"] for p in metrics.hourly_activity(conn, sid, hours=24))
    assert total == 5   # a, b, c, d, e — not f (too old) and not g (never pinged)


def test_status_breakdown_is_fixed_order(snapshot):
    conn, sid = snapshot
    segments = metrics.status_breakdown(conn, sid)
    assert [s["label"] for s in segments] == ["Online", "Offline", "Never pinged"]
    assert sum(s["value"] for s in segments) == 7


def test_task_breakdown_splits_pending_by_reachability(conn, make_export):
    path = make_export([
        device("stuck", queue=1, status="Online"),
        device("waiting", queue=1, status="Offline", seen_at="01-08-26 10:00:00"),
        device("done", queue=0),
        device("untouched", queue="-"),
    ])
    result = ingest.ingest_file(conn, path)
    segments = {s["label"]: s["value"] for s in metrics.task_breakdown(conn, result.snapshot_id)}

    assert segments["Task completed"] == 1
    assert segments["Task pending — Online"] == 1
    assert segments["Task pending — Offline"] == 1
    assert segments["No pending task"] == 1


def test_model_breakdown_folds_the_long_tail(conn, make_export):
    rows = [device(f"big{i}", model="LOCAT140VB") for i in range(5)]
    rows += [device(f"m{i}", model=f"MODEL_{i}") for i in range(6)]
    path = make_export(rows)
    result = ingest.ingest_file(conn, path)

    segments = metrics.model_breakdown(conn, result.snapshot_id, top=3)
    assert len(segments) == 4                       # 3 named + one "Other"
    assert segments[-1]["label"].startswith("Other")
    assert sum(s["value"] for s in segments) == 11  # nothing lost in the fold
