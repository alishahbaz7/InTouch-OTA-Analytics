"""Start with Windows.

Written against a temp folder rather than the real Startup directory: a test that installs a
real auto-start entry on the developer's machine, and leaves it there when it fails, is worse
than no test.
"""

from __future__ import annotations

import sys

import pytest

from ota_analytics import startup


@pytest.fixture
def fake_startup(tmp_path, monkeypatch):
    """A machine that refuses scheduled tasks — the case verified on the real box here."""
    monkeypatch.setattr(startup, "startup_dir", lambda: tmp_path / "Startup")
    monkeypatch.setattr(startup, "is_supported", lambda: True)
    monkeypatch.setattr(startup, "create_task", lambda delay: False)
    monkeypatch.setattr(startup, "task_exists", lambda: False)
    monkeypatch.setattr(startup, "delete_task", lambda: None)
    return tmp_path / "Startup" / startup.ENTRY_NAME


@pytest.fixture
def task_allowed(tmp_path, monkeypatch):
    """A machine where a scheduled task can be registered — the preferred mechanism."""
    registered: dict = {}
    monkeypatch.setattr(startup, "startup_dir", lambda: tmp_path / "Startup")
    monkeypatch.setattr(startup, "is_supported", lambda: True)
    monkeypatch.setattr(startup, "create_task",
                        lambda delay: registered.update(delay=delay) or True)
    monkeypatch.setattr(startup, "task_exists", lambda: bool(registered))
    monkeypatch.setattr(startup, "delete_task", lambda: registered.clear())
    monkeypatch.setattr(startup, "_run",
                        lambda args: (0, f"Delay: {registered.get('delay', 30):04d}:00"))
    return registered


def test_it_is_off_until_it_is_turned_on(fake_startup):
    state = startup.status()
    assert state.enabled is False
    assert not fake_startup.exists()


def test_enabling_writes_one_file_into_the_startup_folder(fake_startup):
    state = startup.enable(30)

    assert state.enabled is True
    assert state.delay_minutes == 30
    assert fake_startup.exists()
    assert fake_startup.parent.name == "Startup"


def test_the_delay_is_what_the_entry_actually_waits(fake_startup):
    """The number in the UI has to be the number in the script, or the toggle lies."""
    startup.enable(45)
    body = fake_startup.read_text(encoding="utf-8")

    assert "WScript.Sleep 2700000" in body           # 45 minutes in milliseconds
    assert startup.status().delay_minutes == 45


def test_the_entry_launches_without_a_console_window(fake_startup):
    startup.enable(30)
    body = fake_startup.read_text(encoding="utf-8")

    assert "main.py" in body
    assert "--no-browser" in body        # nothing should open a browser at logon
    assert ", 0, False" in body          # WScript.Shell.Run with a hidden window
    if sys.platform == "win32":
        assert "pythonw.exe" in body


def test_paths_with_spaces_survive_the_quoting(fake_startup):
    """This project lives in 'InTouchOTA analytics' — unquoted, the launcher silently fails."""
    startup.enable(30)
    body = fake_startup.read_text(encoding="utf-8")
    command = [line for line in body.splitlines() if ".Run " in line][0]
    assert command.count('""') >= 4      # both paths wrapped in escaped VBScript quotes


def test_the_file_says_how_to_remove_it_by_hand(fake_startup):
    """Anyone finding this in their Startup folder should learn what it is from the file."""
    startup.enable(30)
    body = fake_startup.read_text(encoding="utf-8")
    assert "InTouch OTA Analytics" in body
    assert "Delete this file" in body


def test_disabling_removes_it(fake_startup):
    startup.enable(30)
    state = startup.disable()

    assert state.enabled is False
    assert not fake_startup.exists()


def test_disabling_twice_is_harmless(fake_startup):
    startup.disable()
    assert startup.disable().enabled is False


def test_re_enabling_replaces_rather_than_duplicates(fake_startup):
    startup.enable(30)
    startup.enable(60)

    entries = list(fake_startup.parent.iterdir())
    assert len(entries) == 1
    assert startup.status().delay_minutes == 60


@pytest.mark.parametrize("given,expected", [
    (30, 30), (0, 0), (240, 240),
    (-5, 0),            # below the floor
    (9999, 240),        # above the ceiling: a 7-day delay is a silent disable
    ("bad", 30), (None, 30),
])
def test_the_delay_is_clamped_to_something_sensible(given, expected):
    assert startup.clamp_delay(given) == expected


def test_it_reports_unsupported_off_windows(monkeypatch):
    monkeypatch.setattr(startup, "is_supported", lambda: False)
    state = startup.status()
    assert state.supported is False
    assert state.enabled is False
    assert startup.enable(30).enabled is False      # and does nothing


# ─── mechanism: scheduled task preferred, Startup folder as the fallback ────

def test_a_scheduled_task_is_preferred_when_permitted(task_allowed, tmp_path):
    """It runs at boot without anyone signed in, which the Startup folder cannot do."""
    state = startup.enable(30)

    assert state.enabled is True
    assert state.mechanism == "scheduled-task"
    assert state.starts_at_boot is True
    assert task_allowed["delay"] == 30
    assert not (tmp_path / "Startup" / startup.ENTRY_NAME).exists()


def test_it_falls_back_to_the_startup_folder_when_refused(fake_startup):
    """Managed machines refuse schtasks without elevation — verified on this one."""
    state = startup.enable(30)

    assert state.enabled is True
    assert state.mechanism == "startup-folder"
    assert state.starts_at_boot is False
    assert fake_startup.exists()


def test_only_one_mechanism_is_ever_armed(task_allowed, fake_startup, monkeypatch):
    """Both at once would start two copies competing for the same database."""
    monkeypatch.setattr(startup, "create_task", lambda delay: False)
    startup.enable(30)
    assert fake_startup.exists()

    monkeypatch.setattr(startup, "create_task", lambda delay: True)
    monkeypatch.setattr(startup, "task_exists", lambda: True)
    monkeypatch.setattr(startup, "_run", lambda args: (0, "Delay: 0030:00"))
    startup.enable(30)
    assert not fake_startup.exists()


def test_disabling_clears_both_mechanisms(task_allowed, monkeypatch, tmp_path):
    entry = tmp_path / "Startup" / startup.ENTRY_NAME
    entry.parent.mkdir(parents=True, exist_ok=True)
    entry.write_text("leftover", encoding="utf-8")
    task_allowed["delay"] = 30

    startup.disable()

    assert not entry.exists()
    assert task_allowed == {}


# ─── the toggle in the UI ───────────────────────────────────────────────────

def test_the_toggle_turns_it_on_and_off(fake_startup, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from ota_analytics import api
    client = TestClient(api.app, follow_redirects=False)

    response = client.post("/update/startup", data={"enabled": "true", "startup_delay": "30"})
    assert response.status_code == 200
    assert fake_startup.exists()
    assert "start with Windows" in response.text

    response = client.post("/update/startup", data={"startup_delay": "30"})   # unchecked
    assert response.status_code == 200
    assert not fake_startup.exists()
