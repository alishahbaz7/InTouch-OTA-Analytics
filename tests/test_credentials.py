"""Where the platform password comes from.

`keyring` needs an OS credential store. A headless Linux server has none, so on the intended
deployment target save_password() returns False and load_password() returns None — the scheduler
would come up and silently never log in again. An environment variable is how a service is given
a secret there.
"""

from __future__ import annotations

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
