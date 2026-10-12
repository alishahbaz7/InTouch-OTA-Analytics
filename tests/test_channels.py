"""The release and a source copy run side by side without fighting.

The release is in daily use while the source copy is being worked on. Before channels, the dev
launch found the release answering on 8000, decided it was "already running", opened the
release in the browser and exited — so a code change looked like it had not taken. And because
browsers keep cookies per host rather than per port, signing in to one copy signed you out of
the other.
"""

from __future__ import annotations

import http.server
import importlib
import json
import threading

import pytest

from ota_analytics import config


@pytest.mark.parametrize("env, frozen, expected", [
    ("", False, "dev"),
    ("", True, "release"),
    ("release", False, "release"),      # a server deployed from source
    ("DEV", True, "dev"),
    ("nonsense", True, "release"),      # an unknown value falls back to the guess
])
def test_the_channel_is_guessed_from_packaging_and_can_be_overridden(monkeypatch, env, frozen,
                                                                      expected):
    monkeypatch.setenv("OTA_CHANNEL", env)
    monkeypatch.setattr(config, "is_frozen", lambda: frozen)
    assert config._channel() == expected


def test_the_two_channels_default_to_different_ports():
    assert config.DEFAULT_PORT == (8000 if config.CHANNEL == "release" else 8100)


def _serve(body: dict):
    """A stand-in for a running copy, answering /healthz with `body`."""
    payload = json.dumps(body).encode()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@pytest.mark.parametrize("theirs, mine, defer", [
    ("release", "release", True),
    ("dev", "dev", True),
    ("release", "dev", False),      # the release is running; a dev launch must still start
    ("dev", "release", False),
    (None, "release", True),        # a copy from before channels existed was a release
    (None, "dev", False),
])
def test_a_launch_defers_only_to_a_copy_of_its_own_channel(monkeypatch, theirs, mine, defer):
    import main as entry

    body = {"status": "ok", "app": entry.APP_MARKER}
    if theirs:
        body["channel"] = theirs
    server = _serve(body)
    try:
        monkeypatch.setattr(config, "CHANNEL", mine)
        assert entry.already_serving("127.0.0.1", server.server_port) is defer
    finally:
        server.shutdown()


def test_healthz_reports_the_channel():
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from ota_analytics import api
    assert TestClient(api.app).get("/healthz").json()["channel"] == config.CHANNEL


def test_each_channel_has_its_own_session_cookie(monkeypatch):
    from ota_analytics import auth

    names = {}
    try:
        for channel in ("release", "dev"):
            monkeypatch.setattr(config, "CHANNEL", channel)
            names[channel] = importlib.reload(auth).SESSION_COOKIE
    finally:
        monkeypatch.undo()
        importlib.reload(auth)
    # Release keeps the name it always had, so upgrading signs nobody out.
    assert names == {"release": "ota_session", "dev": "ota_session_dev"}
