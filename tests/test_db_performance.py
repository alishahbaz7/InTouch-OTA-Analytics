"""The things that keep the database fast, tested as behaviour rather than as timings.

A test that asserts a duration fails on a loaded CI box and passes on a fast one, which teaches
people to ignore it. These assert the *mechanisms* instead: that the planner has statistics, and
that no hot path resolves `device_state` — the view whose cost grows with every fetch the
install has ever taken.

The view is watched at runtime rather than by grepping the source, because naming it in a SQL
literal is correct and deliberate: `resolve.at` swaps the table name onto the finished statement
so the query text in the file stays readable. What must not happen is that statement reaching
SQLite with the view's name still in it.
"""

from __future__ import annotations

import sqlite3

from ota_analytics import db, ingest, metrics, registry, resolve, rollup
from tests.conftest import device


class Watcher:
    """Records every statement a connection actually executes."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.statements: list[str] = []

    def __enter__(self):
        self.conn.set_trace_callback(self.statements.append)
        return self

    def __exit__(self, *exc):
        self.conn.set_trace_callback(None)

    @property
    def resolved_the_view(self) -> list[str]:
        return [" ".join(s.split())[:120] for s in self.statements
                if "FROM device_state" in s or "JOIN device_state" in s]


def test_the_watcher_can_actually_see_a_view_read(conn, make_export):
    """Guards every assertion below. A tripwire that cannot trip passes for the wrong reason."""
    ingest.ingest_file(conn, make_export([device("111")]))
    with Watcher(conn) as watcher:
        conn.execute("SELECT COUNT(*) FROM device_state WHERE snapshot_id = 1").fetchone()
    assert watcher.resolved_the_view


# ─── query statistics ───────────────────────────────────────────────────────
#
# Without them SQLite guesses. On this schema it guessed catastrophically: the registry's
# prev_firmware update chose the `field` index over the `imei` one, so for each of 35,848
# devices it scanned all 21,097 rows with field='firmware'. Measured on the live database at
# **367 seconds for one statement**, on every fetch. With statistics, 0.21s.

def test_a_new_database_has_query_statistics(conn):
    assert conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name = 'sqlite_stat1'").fetchone(), (
        "migration must leave statistics behind — see db.migrate, v9")


def test_the_change_log_is_among_the_tables_analyzed(conn, make_export):
    """It is the table whose missing statistics caused the 367-second statement."""
    ingest.ingest_file(conn, make_export([device("111", firmware="1.0.0")]))
    ingest.ingest_file(conn, make_export([device("111", firmware="1.1.0")],
                                         name="Devices_1_15Aug26_1611.xlsx"))
    conn.execute("ANALYZE")
    analyzed = {r[0] for r in conn.execute("SELECT tbl FROM sqlite_stat1")}
    assert "device_change" in analyzed


def test_an_ingest_refreshes_the_statistics(conn, make_export, monkeypatch):
    """Statistics that are never refreshed go stale as the database grows."""
    calls = []
    original = db.refresh_statistics
    monkeypatch.setattr(db, "refresh_statistics", lambda c: calls.append(1) or original(c))
    ingest.ingest_file(conn, make_export([device("111")]))
    assert calls, "every ingest must leave the planner's statistics current"


# ─── nothing on a hot path resolves the view ────────────────────────────────

def test_an_ingest_never_resolves_the_view(conn, make_export):
    """A fetch used to resolve it four times over — twice to store the delta, and the rest
    inside the quality rules — plus once more per rule. That is the cost that turned a
    three-second ingest into a three-minute one as history accumulated.
    """
    ingest.ingest_file(conn, make_export([device("111"), device("222")]))

    with Watcher(conn) as watcher:
        ingest.ingest_file(
            conn,
            make_export([device("111", firmware="9.9.9"), device("222")],
                        name="Devices_2_15Aug26_1611.xlsx"))

    assert not watcher.resolved_the_view, (
        "a fetch resolved device_state, which reads every row of every snapshot ever stored:\n  "
        + "\n  ".join(watcher.resolved_the_view))


def test_the_quality_rules_resolve_the_snapshot_once_between_them(conn, make_export):
    """Twenty rules, one resolution. Each used to pay for its own."""
    from ota_analytics import quality

    ingest.ingest_file(conn, make_export([device("111"), device("222")]))

    with Watcher(conn) as watcher:
        quality.run_rules(conn, 1)

    built = [s for s in watcher.statements if "CREATE TEMP TABLE _snap" in s]
    assert len(built) <= 1, f"the snapshot was materialized {len(built)} times"
    assert not watcher.resolved_the_view


def test_a_rollup_never_resolves_the_view(conn, make_export):
    ingest.ingest_file(conn, make_export([device("111"), device("222")]))
    with Watcher(conn) as watcher:
        rollup.rollup_snapshot(conn, 1)
    assert not watcher.resolved_the_view


def test_folding_a_snapshot_into_the_registry_never_resolves_the_view(conn, make_export):
    ingest.ingest_file(conn, make_export([device("111"), device("222")]))
    with Watcher(conn) as watcher:
        registry.apply_snapshot(conn, 1)
    assert not watcher.resolved_the_view


def test_the_metrics_a_page_asks_for_never_resolve_the_view(conn, make_export):
    """The overview's numbers, which is where this was first noticed at 18 seconds a page."""
    ingest.ingest_file(conn, make_export([device("111"), device("222", status="Offline")]))
    rollup.rollup_snapshot(conn, 1)

    with Watcher(conn) as watcher:
        snapshot_id = metrics.latest_snapshot_id(conn)
        metrics.kpis(conn, snapshot_id)
        metrics.pending_online_devices(conn, snapshot_id)
        metrics.task_state_by(conn, snapshot_id, "model")

    assert not watcher.resolved_the_view, (
        "a page metric resolved device_state:\n  " + "\n  ".join(watcher.resolved_the_view))


def test_the_view_is_still_there_and_still_agrees(conn, make_export):
    """It is the specification `resolve` is checked against, so it must not be deleted.

    An optimization with nothing to check it against is just an assertion.
    """
    ingest.ingest_file(conn, make_export([device("111"), device("222")]))
    assert conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'view' AND name = 'device_state'").fetchone()

    through_view = [tuple(r) for r in conn.execute(
        "SELECT imei, firmware, status FROM device_state WHERE snapshot_id = 1 ORDER BY imei")]
    through_resolver = [tuple(r) for r in conn.execute(
        resolve.select(("imei", "firmware", "status"), with_snapshot_id=False,
                       with_seen_age=False) + " ORDER BY d.imei", (1,))]
    assert through_resolver == through_view
