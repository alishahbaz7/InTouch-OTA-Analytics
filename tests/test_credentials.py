"""Where the platform password comes from.

`keyring` needs an OS credential store. A headless Linux server has none, so on the intended
deployment target save_password() returns False and load_password() returns None — the scheduler
would come up and silently never log in again. An environment variable is how a service is given
a secret there.
"""

from __future__ import annotations

import pytest as _pytest


def test_a_saved_password_is_announced_not_just_hinted(tmp_path, monkeypatch):
    """A blank password box reads as "type here" however good the placeholder is.

    The password is already in the OS credential store, so retyping it is wasted effort and one
    more chance to get it wrong. The page has to say so in words, not only in grey placeholder
    text inside the field.
    """
    _pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from ota_analytics import api, config, db, scheduler, sources

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(scheduler, "STATE_PATH", tmp_path / "scheduler.json")
    monkeypatch.setattr(sources, "SETTINGS_PATH", tmp_path / "connection.json")
    monkeypatch.setattr(scheduler, "_scheduler", None)
    db.connect()

    saved = sources.Connection(username="someone@example.com")
    sources.save_connection(saved)
    monkeypatch.setattr(sources, "load_password", lambda username: "secret-value")

    body = TestClient(api.app, raise_server_exceptions=False).get("/update").text

    assert "Password already saved in" in body
    assert "leave the box empty" in body.lower()
    assert "Change password" in body            # the field is the exception, not the norm
    # And the secret itself never reaches the page.
    assert "secret-value" not in body

import json

import pytest

from ota_analytics import sources


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    monkeypatch.delenv(sources.ENV_PASSWORD, raising=False)


def test_the_environment_supplies_the_password_with_no_credential_store(monkeypatch):
    monkeypatch.setattr(sources, "_keyring", lambda: None)     # as on a headless server
    monkeypatch.setenv(sources.ENV_PASSWORD, "s3cret")

    assert sources.load_password("shahbaz") == "s3cret"


def test_the_environment_wins_over_a_stored_password(monkeypatch):
    class Fake:
        def get_password(self, service, user): return "stale-from-keyring"
    monkeypatch.setattr(sources, "_keyring", lambda: Fake())
    monkeypatch.setenv(sources.ENV_PASSWORD, "from-env")

    # What the deployment configured is what runs; a leftover keyring entry must not shadow it.
    assert sources.load_password("shahbaz") == "from-env"


def test_a_blank_environment_variable_is_treated_as_unset(monkeypatch):
    monkeypatch.setattr(sources, "_keyring", lambda: None)
    monkeypatch.setenv(sources.ENV_PASSWORD, "")

    # An empty variable is a misconfigured unit file, not an empty password.
    assert sources.load_password("shahbaz") is None


def test_the_credential_store_is_named_so_the_page_can_show_it(monkeypatch):
    monkeypatch.setattr(sources, "_keyring", lambda: None)
    assert sources.credential_store_name() == "unavailable"

    monkeypatch.setenv(sources.ENV_PASSWORD, "s3cret")
    assert sources.ENV_PASSWORD in sources.credential_store_name()


def test_a_password_is_never_written_to_the_settings_file(monkeypatch, tmp_path):
    """connection.json holds URL, username and auth mode — never the secret."""
    monkeypatch.setattr(sources, "SETTINGS_PATH", tmp_path / "connection.json")
    monkeypatch.setenv(sources.ENV_PASSWORD, "s3cret")

    conn = sources.load_connection()
    conn.username = "shahbaz"
    sources.save_connection(conn)

    written = (tmp_path / "connection.json").read_text(encoding="utf-8")
    assert "s3cret" not in written
    assert "password" not in json.loads(written)
