"""Access control.

Every page exposes the fleet — 35,477 IMEIs, VINs and ICCIDs — and /update/* posts to the
production OTA admin API. These tests pin the two things that matter: nothing is reachable
without a session, and a viewer cannot change anything however it is asked.
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from ota_analytics import auth
from ota_analytics.api import app

ADMIN = "admin-secret"
VIEWER = "viewer-secret"


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv(auth.ENV_SECRET_KEY, "test-key-not-a-real-one")
    monkeypatch.setenv(auth.ENV_ADMIN_HASH, auth.hash_password(ADMIN))
    monkeypatch.setenv(auth.ENV_VIEWER_HASH, auth.hash_password(VIEWER))
    monkeypatch.delenv(auth.ENV_TRUST_PROXY, raising=False)
    return TestClient(app, follow_redirects=False)


@pytest.fixture
def open_client(monkeypatch):
    """The app with no password configured — the state a fresh install runs in."""
    monkeypatch.delenv(auth.ENV_ADMIN_HASH, raising=False)
    monkeypatch.delenv(auth.ENV_VIEWER_HASH, raising=False)
    monkeypatch.delenv(auth.ENV_TRUST_PROXY, raising=False)
    return TestClient(app, follow_redirects=False, raise_server_exceptions=False)


def sign_in(client, password: str) -> None:
    response = client.post("/login", data={"password": password, "next": "/"})
    assert response.status_code == 303, response.text


# ─── the sign-in page itself ────────────────────────────────────────────────
#
# This page shipped broken once: it extended the main layout, which needs context the login
# route has no reason to build, and every visit raised "'qs' is undefined". Rendering it in
# both configurations is what stops that returning.

@pytest.mark.parametrize("fixture_name", ["client", "open_client"])
def test_the_sign_in_page_renders_configured_or_not(request, fixture_name):
    client = request.getfixturevalue(fixture_name)
    response = client.get("/login")

    assert response.status_code == 200, response.text[:300]
    assert "Sign in" in response.text
    assert "Something went wrong" not in response.text


def test_a_failed_sign_in_still_renders_the_page(client):
    """The error path renders the same template, and broke separately from the happy path."""
    response = client.post("/login", data={"password": "nope", "next": "/"})
    assert response.status_code == 401
    assert "Sign in" in response.text
    assert "Something went wrong" not in response.text


def test_the_sign_in_page_shows_nothing_about_the_fleet(client):
    """A visitor who has not signed in must not see device counts, nav or agent status."""
    body = client.get("/login").text
    for leak in ("Overview", "Task pending", "Auto-fetch", "devices tracked", "Update data"):
        assert leak not in body, leak


def test_an_unconfigured_install_warns_on_the_sign_in_page(open_client):
    body = open_client.get("/login").text
    assert "No password is configured" in body


# ─── passwords ──────────────────────────────────────────────────────────────

def test_a_password_verifies_against_its_own_hash():
    stored = auth.hash_password("correct horse")
    assert auth.verify_password("correct horse", stored)
    assert not auth.verify_password("Correct Horse", stored)


def test_the_same_password_hashes_differently_each_time():
    """A random salt per hash, so two people choosing the same password are not visibly equal."""
    assert auth.hash_password("same") != auth.hash_password("same")


def test_a_malformed_hash_is_rejected_rather_than_crashing():
    for junk in ("", "not-a-hash", "scrypt$bad", "md5$1$2$3$4$5"):
        assert not auth.verify_password("anything", junk)


# ─── sessions ───────────────────────────────────────────────────────────────

def test_a_tampered_session_is_refused(monkeypatch):
    monkeypatch.setenv(auth.ENV_SECRET_KEY, "test-key")
    token = auth.issue(auth.User(name="viewer", role="viewer"))
    assert auth.read(token).role == "viewer"

    # Promote yourself to admin by editing the cookie, and the signature stops matching.
    forged = token.replace("viewer", "admin", 1)
    assert auth.read(forged) is None


def test_an_expired_session_is_refused(monkeypatch):
    monkeypatch.setenv(auth.ENV_SECRET_KEY, "test-key")
    token = auth.issue(auth.User(name="admin", role="admin"),
                       now=time.time() - auth.SESSION_MAX_AGE - 60)
    assert auth.read(token) is None


# ─── the app ────────────────────────────────────────────────────────────────

def test_pages_are_not_reachable_without_signing_in(client):
    for path in ("/", "/devices", "/pending", "/update"):
        response = client.get(path)
        assert response.status_code == 303, path
        assert response.headers["location"].startswith("/login"), path


def test_the_json_api_is_not_reachable_either(client):
    """The API mirrors every page, so leaving it open would leave the data open."""
    response = client.get("/api/kpis")
    assert response.status_code == 401


def test_the_login_page_and_health_check_stay_public(client):
    assert client.get("/login").status_code == 200
    assert client.get("/healthz").status_code == 200


def test_a_wrong_password_is_refused_and_never_echoed(client):
    response = client.post("/login", data={"password": "wrong-guess", "next": "/"})
    assert response.status_code == 401
    assert "wrong-guess" not in response.text


def test_an_admin_can_reach_the_dashboard(client):
    sign_in(client, ADMIN)
    assert client.get("/api/kpis").status_code == 200


def test_a_viewer_can_read(client):
    sign_in(client, VIEWER)
    assert client.get("/api/kpis").status_code == 200


def test_a_viewer_cannot_post_anything(client):
    """Enforced by method on the server. Hiding the buttons is not a permission."""
    sign_in(client, VIEWER)
    for path in ("/update/run-now", "/update/schedule", "/update/forget"):
        response = client.post(path, data={})
        assert response.status_code == 403, path


def test_signing_out_ends_the_session(client):
    sign_in(client, ADMIN)
    assert client.get("/api/kpis").status_code == 200
    client.get("/logout")
    assert client.get("/api/kpis").status_code == 401


def test_login_will_not_redirect_off_site(client):
    """An open redirect here turns the login page into a phishing hop."""
    response = client.post("/login", data={"password": ADMIN, "next": "https://evil.example/x"})
    assert response.headers["location"] == "/"


def test_the_proxy_identity_header_is_ignored_unless_trusted(client):
    """Anything that can reach the port can send this header, so it is off by default."""
    response = client.get("/api/kpis", headers={auth.PROXY_EMAIL_HEADER: "someone@example.com"})
    assert response.status_code == 401


def test_the_proxy_identity_header_is_accepted_when_trusted(client, monkeypatch):
    monkeypatch.setenv(auth.ENV_TRUST_PROXY, "1")
    response = client.get("/api/kpis",
                          headers={auth.PROXY_EMAIL_HEADER: "shahbaz@mapmyindia.com"})
    assert response.status_code == 200


def test_with_no_password_configured_the_app_stays_open(monkeypatch):
    """Upgrading a local install must not brick it; the login page says so instead."""
    monkeypatch.delenv(auth.ENV_ADMIN_HASH, raising=False)
    monkeypatch.delenv(auth.ENV_VIEWER_HASH, raising=False)
    local = TestClient(app, follow_redirects=False)
    assert local.get("/api/kpis").status_code == 200


# ─── refusing to publish the fleet by accident ──────────────────────────────

def test_binding_to_the_network_without_a_password_is_refused(monkeypatch):
    """0.0.0.0 is how colleagues reach it, and how everything else on the network does too."""
    import main

    monkeypatch.delenv(auth.ENV_ADMIN_HASH, raising=False)
    monkeypatch.delenv(auth.ENV_VIEWER_HASH, raising=False)

    with pytest.raises(SystemExit) as exit_info:
        main.check_exposure("0.0.0.0")
    assert exit_info.value.code == 2


def test_loopback_stays_usable_without_a_password(monkeypatch):
    """One person on their own machine. Demanding a login here teaches people to disable it."""
    import main

    monkeypatch.delenv(auth.ENV_ADMIN_HASH, raising=False)
    monkeypatch.delenv(auth.ENV_VIEWER_HASH, raising=False)

    for host in ("127.0.0.1", "localhost", "::1"):
        main.check_exposure(host)          # must not raise


def test_binding_to_the_network_is_allowed_once_a_password_exists(monkeypatch):
    import main

    monkeypatch.setenv(auth.ENV_ADMIN_HASH, auth.hash_password("something"))
    main.check_exposure("0.0.0.0")         # must not raise
