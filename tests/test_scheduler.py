"""The background fetch agent: interval handling, state, and honest auth reporting."""

from __future__ import annotations

import pytest

from ota_analytics import config, scheduler, sources


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(scheduler, "STATE_PATH", tmp_path / "scheduler.json")
    monkeypatch.setattr(sources, "SETTINGS_PATH", tmp_path / "connection.json")
    monkeypatch.setattr(sources, "_keyring", lambda: None)   # no credentials saved


@pytest.mark.parametrize("given,expected", [
    (60, 60),               # 1 minute — the floor
    (30, 60),               # below the floor is raised
    (0, 60),
    (-5, 60),
    (3600, 3600),           # 1 hour
    (86400, 86400),         # 24 hours — the ceiling
    (100000, 86400),        # above the ceiling is capped
    ("bad", 3600),          # unparseable falls back to the default
])
def test_interval_is_clamped_to_one_minute_through_24_hours(given, expected):
    assert scheduler.clamp_interval(given) == expected


@pytest.mark.parametrize("seconds,label", [
    (60, "1 minute"), (300, "5 minutes"), (3600, "1 hour"),
    (7200, "2 hours"), (1800, "30 minutes"), (86400, "24 hours"),
])
def test_interval_labels_read_naturally(seconds, label):
    assert scheduler.describe_interval(seconds) == label


def test_state_persists_across_restarts(tmp_path):
    agent = scheduler.Scheduler()
    agent.configure(enabled=True, interval_seconds=1800)
    agent.stop()

    reloaded = scheduler.Scheduler()
    assert reloaded.state.enabled is True
    assert reloaded.state.interval_seconds == 1800


def test_disabling_clears_the_next_run():
    agent = scheduler.Scheduler()
    agent.configure(enabled=True, interval_seconds=600)
    assert agent.state.next_run is not None
    agent.configure(enabled=False, interval_seconds=600)
    assert agent.state.next_run is None
    agent.stop()


def test_a_failing_fetch_is_recorded_not_raised():
    """The loop must survive a bad run — a dead network cannot stop the agent forever."""
    agent = scheduler.Scheduler()
    sources.save_connection(sources.Connection(preset="custom"))    # nothing configured
    agent.run_now()

    assert agent.state.last_status == "error"
    assert "URL" in agent.state.last_message
    assert agent.state.failures == 1
    assert agent.state.consecutive_failures == 1


def test_auth_status_reports_nothing_configured():
    sources.save_connection(sources.Connection(preset="custom"))
    status = scheduler.auth_status()
    assert status["level"] == "none"
    assert status["can_automate"] is False


def test_a_known_platform_needs_only_credentials():
    """With a preset, the endpoint half is already done — only sign-in is missing."""
    status = scheduler.auth_status()          # no settings saved at all
    assert status["level"] == "warn"          # URL known, credentials are not
    assert status["can_automate"] is False


def test_auth_status_flags_missing_credentials():
    sources.save_connection(sources.Connection(
        url="https://platform.test/devices", username="user", auth_mode="token"))
    status = scheduler.auth_status()
    assert status["level"] == "warn"
    assert status["can_automate"] is False


def test_auth_status_says_a_pasted_token_cannot_be_automated(monkeypatch):
    """A bearer token expires and nothing can renew it — say so instead of failing later."""
    monkeypatch.setattr(sources, "load_password", lambda u: "some-token")
    sources.save_connection(sources.Connection(
        preset="custom", url="https://platform.test/devices", username="user",
        auth_mode="bearer"))

    status = scheduler.auth_status()
    assert status["can_automate"] is False
    assert "renew" in status["detail"]


def test_auth_status_confirms_automation_with_a_login_url(monkeypatch):
    monkeypatch.setattr(sources, "load_password", lambda u: "password")
    sources.save_connection(sources.Connection(
        url="https://platform.test/devices", login_url="https://platform.test/login",
        username="user", auth_mode="token"))

    status = scheduler.auth_status()
    assert status["level"] == "ok"
    assert status["can_automate"] is True


# ─── one fetch at a time ────────────────────────────────────────────────────
#
# The live error log recorded "database is locked" twice, both at the very first INSERT of a
# scheduled fetch. The cause was that a timed fetch was invisible to `progress`: it ran
# `_fetch_and_ingest` directly with a silent job, so "one job at a time" did not cover it and
# pressing "Fetch now" during one started a second, concurrent writer.

def test_a_timed_fetch_registers_as_a_job(monkeypatch):
    """So anything else that writes can see it and stand aside."""
    from ota_analytics import progress

    progress.clear()
    seen = {}

    def fake_fetch(self, job=None):
        seen["job"] = progress.current()
        return "done", "ok", 7

    monkeypatch.setattr(scheduler.Scheduler, "_fetch_and_ingest", fake_fetch)
    agent = scheduler.Scheduler()
    agent._run_once()

    assert seen["job"] is not None, "a scheduled fetch must be visible to progress while it runs"
    assert seen["job"].kind == "fetch"
    assert agent.state.last_status == "ok"


def test_a_timed_fetch_stands_aside_for_a_job_already_running(monkeypatch):
    """It skips rather than queueing: the next tick is along soon and its data is fresher."""
    from ota_analytics import progress

    progress.clear()
    progress.start("import", "Merging a bundle", [("Reading the bundle", 1.0)])

    called = []
    monkeypatch.setattr(scheduler.Scheduler, "_fetch_and_ingest",
                        lambda self, job=None: called.append(1) or ("done", "ok", 1))
    agent = scheduler.Scheduler()
    agent.configure(enabled=True, interval_seconds=3600)
    agent._run_once()

    assert called == [], "it must not start a second writer"
    assert agent.state.last_status == "skipped"
    assert "Merging a bundle" in agent.state.last_message
    assert agent.state.failures == 0, "standing aside is not a failure"
    assert agent.state.next_run, "and the timer must keep running"
    progress.clear()


def test_a_manual_fetch_is_refused_while_a_timed_one_is_running(monkeypatch):
    """The other direction — this is the collision that actually happened."""
    from ota_analytics import progress

    progress.clear()
    blocked = {}

    def fake_fetch(self, job=None):
        # While the scheduled fetch is mid-flight, the button's own guard must refuse.
        try:
            progress.start("fetch", "Fetching from the platform", scheduler.FETCH_STEPS)
            blocked["refused"] = False
        except progress.Busy:
            blocked["refused"] = True
        return "done", "ok", 3

    monkeypatch.setattr(scheduler.Scheduler, "_fetch_and_ingest", fake_fetch)
    scheduler.Scheduler()._run_once()
    assert blocked["refused"] is True


def test_a_finished_timed_fetch_does_not_leave_its_panel_behind(monkeypatch):
    """Nobody asked for it, so nobody should have to dismiss it every hour."""
    from ota_analytics import progress

    progress.clear()
    monkeypatch.setattr(scheduler.Scheduler, "_fetch_and_ingest",
                        lambda self, job=None: ("Loaded 10 devices.", "ok", 2))
    scheduler.Scheduler()._run_once()
    assert progress.snapshot() == {"active": False}


def test_a_fetch_the_user_started_keeps_its_panel(monkeypatch):
    """That one was asked for, so its result waits to be read and dismissed."""
    from ota_analytics import progress

    progress.clear()
    monkeypatch.setattr(scheduler.Scheduler, "_fetch_and_ingest",
                        lambda self, job=None: ("Loaded 10 devices.", "ok", 2))
    job = progress.start("fetch", "Fetching from the platform", scheduler.FETCH_STEPS)
    scheduler.Scheduler().run_now(job=job)

    assert progress.snapshot()["active"] is True
    assert progress.current() is job
    progress.clear()
