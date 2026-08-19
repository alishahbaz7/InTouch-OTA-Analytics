"""Metrics — especially the pending split, which is the dashboard's core claim."""

from __future__ import annotations

import pytest

from ota_analytics import ingest, metrics, rollup
from tests.conftest import device


@pytest.fixture
def snapshot(conn, make_export):
    """A fleet covering every task state and both reasons for pending."""
    path = make_export([
        # pending + online: powered and connected, task not delivered — the actionable case
        device("stuck1", queue=1, status="Online", seen_at="15-08-26 14:00:00"),
        device("stuck2", queue=2, status="Online", seen_at="15-08-26 14:30:00"),
        # pending + offline: waiting for power-on, expected
        device("waiting_short", queue=1, status="Offline", seen_at="12-08-26 10:00:00"),
        device("waiting_long", queue=1, status="Offline", seen_at="15-02-26 10:00:00"),
        # completed and never tasked
        device("done", queue=0, status="Online"),
        device("eol_device", queue="-", status="Online", model="TML_Ax1", firmware="2.0.0161"),
    ])
    result = ingest.ingest_file(conn, path)
    rollup.rollup_snapshot(conn, result.snapshot_id)
    return conn, result.snapshot_id


def test_kpis_split_pending_by_reason(snapshot):
    conn, sid = snapshot
    k = metrics.kpis(conn, sid)

    assert k["devices_pending"] == 4
    assert k["pending_reachable"] == 2   # online yet still pending
    assert k["pending_waiting"] == 2     # offline, parked by design
    assert k["pending_reachable"] + k["pending_waiting"] == k["devices_pending"]


def test_pending_online_devices_are_online_and_pending_only(snapshot):
    conn, sid = snapshot
    imeis = {d["imei"] for d in metrics.pending_online_devices(conn, sid)}
    assert imeis == {"stuck1", "stuck2"}


def test_pending_buckets_put_reachable_first_and_flag_it(snapshot):
    conn, sid = snapshot
    buckets = {b["bucket"]: b for b in metrics.pending_by_reason(conn, sid)}

    assert buckets["online now"]["devices"] == 2
    assert buckets["online now"]["actionable"] is True
    assert buckets["1-7 days"]["devices"] == 1
    assert buckets["90+ days"]["devices"] == 1
    assert buckets["1-7 days"]["actionable"] is False
    # Every pending device lands in exactly one bucket.
    assert sum(b["devices"] for b in buckets.values()) == 4


def test_pending_bucket_tasks_sum_queue_depth(snapshot):
    conn, sid = snapshot
    buckets = {b["bucket"]: b for b in metrics.pending_by_reason(conn, sid)}
    assert buckets["online now"]["tasks"] == 3   # queue 1 + 2


def test_compliance_needs_declared_targets(snapshot):
    """Nothing is inferred: with no target declared, everything is 'unknown'."""
    conn, sid = snapshot
    summary = metrics.compliance_summary(conn, sid)
    assert summary["undeclared"] == 6
    assert summary["on_target"] == 0
    assert summary["off_target"] == 0


def test_compliance_against_declared_target(snapshot):
    conn, sid = snapshot
    metrics.set_target(conn, "LOCAT140VB", "7.5.0.51A")
    summary = metrics.compliance_summary(conn, sid)

    assert summary["on_target"] == 5      # all LOCAT140VB devices are on 7.5.0.51A
    assert summary["off_target"] == 0
    assert summary["undeclared"] == 1     # the TML_Ax1 device


def test_eol_model_counts_as_compliant_not_as_a_gap(snapshot):
    """An end-of-life model on its final firmware is correct, not a coverage gap."""
    conn, sid = snapshot
    metrics.set_target(conn, "TML_Ax1", None, eol=True, note="no further releases")
    summary = metrics.compliance_summary(conn, sid)

    assert summary["eol_ok"] == 1
    assert metrics.coverage_gaps(conn, sid) == []


def test_coverage_gap_is_off_target_and_untasked(snapshot):
    conn, sid = snapshot
    metrics.set_target(conn, "TML_Ax1", "2.0.0999")   # a newer release exists
    gaps = metrics.coverage_gaps(conn, sid)

    assert len(gaps) == 1
    assert gaps[0]["device_model"] == "TML_Ax1"
    assert gaps[0]["firmware"] == "2.0.0161"
    assert gaps[0]["online"] == 1


def test_staleness_buckets_cover_the_whole_fleet(snapshot):
    conn, sid = snapshot
    buckets = metrics.staleness_buckets(conn, sid)
    assert sum(b["devices"] for b in buckets) == 6
