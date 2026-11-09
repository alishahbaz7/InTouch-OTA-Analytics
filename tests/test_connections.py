"""Every request and background job closes the connections it opened.

Since Python 3.11 a sqlite3 connection keeps its statement cache in an lru_cache that refers back
to the connection, so dropping the last reference does not free it — only close() or the cyclic
garbage collector does, and on a quiet server the collector can be hours away. On 09-11-2026 a
fetch failed part-way through a write; its open transaction held the write lock for three hours
and every fetch after it died with "database is locked".
"""

from __future__ import annotations

import sqlite3
import time

import pytest
from fastapi.testclient import TestClient

from ota_analytics import api, config, db, progress, scheduler, sources


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(scheduler, "STATE_PATH", tmp_path / "scheduler.json")
    monkeypatch.setattr(sources, "SETTINGS_PATH", tmp_path / "connection.json")
    monkeypatch.setattr(scheduler, "_scheduler", None)
    db.connect().close()
    return TestClient(api.app, raise_server_exceptions=False)


def nothing_holds_the_write_lock() -> None:
    other = sqlite3.connect(config.DB_PATH, timeout=0.5)
    try:
        other.execute("BEGIN IMMEDIATE")      # raises "database is locked" if anything still does
        other.rollback()
    finally:
        other.close()


def test_a_request_that_fails_mid_write_gives_the_lock_back(client):
    def fails_mid_write():
        api.get_conn().execute("UPDATE snapshot SET duration_ms = duration_ms")
        raise RuntimeError("route failed part-way")

    api.app.add_api_route("/__test/fails-mid-write", fails_mid_write, methods=["POST"])
    try:
        assert client.post("/__test/fails-mid-write").status_code == 500
        nothing_holds_the_write_lock()
    finally:
        api.app.router.routes[:] = [r for r in api.app.router.routes
                                    if getattr(r, "path", "") != "/__test/fails-mid-write"]


def test_a_page_closes_the_connections_it_opened(client, monkeypatch):
    opened = []
    real = db.connect

    def recording(*args, **kwargs):
        conn = real(*args, **kwargs)
        opened.append(conn)
        return conn

    monkeypatch.setattr(db, "connect", recording)
    assert client.get("/firmware").status_code == 200
    assert opened
    for conn in opened:
        with pytest.raises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")          # "Cannot operate on a closed database."


def test_a_job_that_fails_mid_write_gives_the_lock_back_and_is_recorded(client):
    def work(job):
        api.get_conn().execute("UPDATE snapshot SET duration_ms = duration_ms")
        raise RuntimeError("job failed part-way")

    job = progress.start("import", "Test import", [("Working", 1.0)])
    try:
        api.run_job(job, work)
        deadline = time.monotonic() + 20
        while job.status == "running" and time.monotonic() < deadline:
            time.sleep(0.05)
        assert job.status == "error"
        nothing_holds_the_write_lock()
        # Recorded on the Errors page too: before, the log's own insert waited out the lock and
        # was dropped, so a failure like 09-11-2026's reached only the text file.
        conn = db.connect()
        assert conn.execute("SELECT COUNT(*) FROM app_error WHERE message LIKE "
                            "'job failed part-way%'").fetchone()[0] == 1
        conn.close()
    finally:
        progress.clear(job)
