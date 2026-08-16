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
