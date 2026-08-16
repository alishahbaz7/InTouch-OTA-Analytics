"""Acquiring exports: upload handling, download validation, credential storage."""

from __future__ import annotations

from datetime import datetime

import pytest

from ota_analytics import config, normalize, sources
from tests.conftest import HEADERS, device


@pytest.fixture(autouse=True)
def isolate_export_dir(tmp_path, monkeypatch):
    """Never write test files into the real export folder."""
    monkeypatch.setattr(config, "EXPORT_DIR", tmp_path / "exports")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(sources, "SETTINGS_PATH", tmp_path / "data" / "connection.json")


def xlsx_bytes(make_export, name="Devices_1_15Aug26_1511.xlsx") -> bytes:
    return make_export([device("111")], name=name).read_bytes()


# ─── filenames ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw", [
    "../../etc/passwd.xlsx",
    "C:\\Windows\\System32\\evil.xlsx",
    "..\\..\\secrets.xlsx",
    "/absolute/path.xlsx",
    "....//traversal.xlsx",
])
def test_safe_filename_cannot_escape_the_export_folder(raw):
    """The property that matters: an uploaded name can never steer the write elsewhere.

    Asserted as an invariant rather than a literal string, because basename extraction is
    platform-dependent — a Windows path is one filename on Linux and two components here.
    """
    safe = sources.safe_filename(raw)
    assert "/" not in safe and "\\" not in safe
    assert not safe.startswith(".")
    assert ".." not in safe.replace("..", "", 1) or safe.count("..") == 0 or "/" not in safe
    assert (config.EXPORT_DIR / safe).resolve().parent == config.EXPORT_DIR.resolve()


@pytest.mark.parametrize("raw,expected", [
    ("Devices_35477_15Aug26_1511.xlsx", "Devices_35477_15Aug26_1511.xlsx"),
    ("report.pdf", "Devices_online.xlsx"),      # wrong type falls back
    ("", "Devices_online.xlsx"),
])
def test_safe_filename_keeps_good_names_and_enforces_xlsx(raw, expected):
    assert sources.safe_filename(raw) == expected


def test_timestamped_name_is_parseable_by_the_snapshot_clock():
    """A generated name must still yield a snapshot time, or trends silently misalign."""
    name = sources.timestamped_name(when=datetime(2026, 8, 15, 15, 30))
    assert name == "Devices_online_15Aug26_1530.xlsx"
    assert normalize.snapshot_at_from_filename(name) == datetime(2026, 8, 15, 15, 30)


# ─── uploads ────────────────────────────────────────────────────────────────

def test_store_upload_writes_the_file(make_export):
    path = sources.store_upload("Devices_1_15Aug26_1511.xlsx", xlsx_bytes(make_export))
    assert path.exists()
    assert path.parent == config.EXPORT_DIR
    assert normalize.snapshot_at_from_filename(path.name) == datetime(2026, 8, 15, 15, 11)


def test_upload_without_a_date_in_the_name_gets_one(make_export):
    path = sources.store_upload("export.xlsx", xlsx_bytes(make_export))
    assert normalize.snapshot_at_from_filename(path.name) is not None


def test_upload_never_overwrites_an_existing_file(make_export):
    content = xlsx_bytes(make_export)
    first = sources.store_upload("Devices_1_15Aug26_1511.xlsx", content)
    second = sources.store_upload("Devices_1_15Aug26_1511.xlsx", content)
    assert first != second
    assert first.exists() and second.exists()


def test_upload_rejects_a_non_xlsx_file():
    with pytest.raises(sources.SourceError, match="not an .xlsx"):
        sources.store_upload("evil.xlsx", b"#!/bin/sh\nrm -rf /\n")


def test_upload_rejects_an_empty_file():
    with pytest.raises(sources.SourceError, match="empty"):
        sources.store_upload("empty.xlsx", b"")


def test_upload_rejects_an_html_page():
    """The classic failure: a login page saved with an .xlsx extension."""
    with pytest.raises(sources.SourceError, match="web page"):
        sources.store_upload("export.xlsx", b"<!DOCTYPE html><html><body>Sign in</body></html>")


# ─── connection settings ────────────────────────────────────────────────────

def test_connection_settings_round_trip():
    """Custom platforms keep exactly what was entered."""
    sources.save_connection(sources.Connection(
        preset="custom", url="https://platform.internal/export.xlsx", username="shahbaz",
        auth_mode="basic", login_url="", verify_tls=True))
    loaded = sources.load_connection()
    assert loaded.url == "https://platform.internal/export.xlsx"
    assert loaded.username == "shahbaz"
    assert loaded.auth_mode == "basic"


def test_a_known_platform_supplies_its_own_endpoints():
    """The point of a preset: nobody should have to type a URL we already know."""
    conn = sources.apply_preset(sources.Connection(preset="intouch", username="shahbaz"))
    preset = sources.PRESETS["intouch"]

    assert conn.url == preset.url
    assert conn.login_url == preset.login_url
    assert conn.login_encoding == "multipart"
    assert conn.password_hash == "md5"
    assert conn.username == "shahbaz"          # only the credentials are the user's to give


def test_a_preset_overrides_stale_saved_endpoints():
    """If a preset URL is corrected in code, saved settings must pick the correction up."""
    sources.save_connection(sources.Connection(preset="intouch", username="shahbaz",
                                               url="https://old-and-wrong.example/devices"))
    loaded = sources.load_connection()
    assert loaded.url == sources.PRESETS["intouch"].url


def test_custom_preset_does_not_overwrite_manual_settings():
    conn = sources.apply_preset(sources.Connection(
        preset="custom", url="https://mine.example/devices", auth_mode="basic"))
    assert conn.url == "https://mine.example/devices"
    assert conn.auth_mode == "basic"


def test_saved_settings_never_contain_a_secret(tmp_path):
    """The settings file may name a password *field*, but must never hold a password value."""
    import json

    secret = "sup3r-s3cret-value"
    sources.save_connection(sources.Connection(
        url="https://platform.internal/export.xlsx", username="shahbaz",
        pass_field="password"))

    text = sources.SETTINGS_PATH.read_text(encoding="utf-8")
    assert secret not in text

    # Pinned deliberately: anything added to the settings file has to be reviewed here first,
    # so a secret can never quietly join the list.
    stored = json.loads(text)
    assert set(stored) == {"preset", "url", "username", "auth_mode", "login_url", "verify_tls",
                           "user_field", "pass_field", "login_encoding", "password_hash"}
    # pass_field holds the API's field NAME and password_hash the algorithm — neither is secret
    assert stored["pass_field"] == "password"
    assert stored["password_hash"] in {"md5", "none"}
    assert all(isinstance(v, (str, bool)) for v in stored.values())


def test_first_run_is_already_configured():
    """With no settings file yet, the default preset makes the form usable immediately."""
    conn = sources.load_connection()
    assert conn.preset == sources.DEFAULT_PRESET
    assert conn.url == sources.PRESETS[sources.DEFAULT_PRESET].url
    assert conn.username == ""      # credentials are still the user's to supply


# ─── credentials ────────────────────────────────────────────────────────────

def test_credentials_go_to_the_os_store_not_a_file(monkeypatch, tmp_path):
    vault: dict[tuple[str, str], str] = {}

    class FakeKeyring:
        @staticmethod
        def set_password(service, user, password): vault[(service, user)] = password
        @staticmethod
        def get_password(service, user): return vault.get((service, user))
        @staticmethod
        def delete_password(service, user): vault.pop((service, user), None)
        @staticmethod
        def get_keyring(): return FakeKeyring()

    monkeypatch.setattr(sources, "_keyring", lambda: FakeKeyring)

    assert sources.save_password("shahbaz", "s3cret") is True
    assert sources.load_password("shahbaz") == "s3cret"
    assert vault[(sources.SERVICE_NAME, "shahbaz")] == "s3cret"

    # and nothing on disk holds it
    sources.save_connection(sources.Connection(url="https://x/e.xlsx", username="shahbaz"))
    assert "s3cret" not in sources.SETTINGS_PATH.read_text(encoding="utf-8")

    sources.forget_password("shahbaz")
    assert sources.load_password("shahbaz") is None


def test_credential_helpers_degrade_without_a_backend(monkeypatch):
    monkeypatch.setattr(sources, "_keyring", lambda: None)
    assert sources.save_password("u", "p") is False
    assert sources.load_password("u") is None
    sources.forget_password("u")                      # must not raise
    assert sources.credential_store_name() == "unavailable"


# ─── downloading ────────────────────────────────────────────────────────────

def test_fetch_requires_a_url():
    with pytest.raises(sources.SourceError, match="No export URL"):
        sources.fetch_export(sources.Connection(), "pw")


def test_form_login_requires_a_login_url():
    conn = sources.Connection(url="https://platform.internal/e.xlsx", auth_mode="form")
    with pytest.raises(sources.SourceError, match="login URL"):
        sources.fetch_export(conn, "pw")


# ─── token sign-in (the shape a SPA like fotaWeb uses) ──────────────────────

TOKEN = "eyJhbGciOiJIUzI1NiJ9.payload.signature"


@pytest.mark.parametrize("payload,expected", [
    ({"token": TOKEN}, TOKEN),
    ({"accessToken": TOKEN}, TOKEN),
    ({"data": {"access_token": TOKEN}}, TOKEN),
    ({"result": {"auth": {"jwt": TOKEN}}}, TOKEN),
    ({"status": "ok", "message": "short"}, None),      # nothing token-shaped
    ({"token": "tiny"}, None),                          # too short to be a token
    ("not a dict", None),
])
def test_find_token_handles_common_response_shapes(payload, expected):
    assert sources.find_token(payload) == expected


@pytest.fixture
def mock_api(monkeypatch, make_export):
    """A stand-in platform: JSON login returns a bearer token, export needs that token."""
    import httpx

    xlsx = make_export([device("111")], name="src.xlsx").read_bytes()
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(f"{request.method} {request.url.path}")
        if request.url.path == "/api/login":
            import json as _json
            body = _json.loads(request.content or b"{}")
            if body.get("email") == "user@example.com" and body.get("pass") == "correct":
                return httpx.Response(200, json={"data": {"accessToken": TOKEN}})
            return httpx.Response(401, json={"error": "invalid credentials"})
        if request.url.path == "/api/export":
            if request.headers.get("Authorization") == f"Bearer {TOKEN}":
                return httpx.Response(200, content=xlsx, headers={
                    "content-disposition": 'attachment; filename="Devices_1_16Aug26_0900.xlsx"'})
            return httpx.Response(401, text="unauthorized")
        return httpx.Response(404)

    real_client = httpx.Client

    def factory(**kwargs):
        kwargs.pop("verify", None)
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(**kwargs)

    monkeypatch.setattr(httpx, "Client", factory)
    return calls


def _token_connection(**overrides) -> sources.Connection:
    base = dict(url="https://platform.test/api/export", login_url="https://platform.test/api/login",
                username="user@example.com", auth_mode="token",
                user_field="email", pass_field="pass",
                login_encoding="json", password_hash="none")
    base.update(overrides)
    return sources.Connection(**base)


def test_token_login_then_download(mock_api):
    fetched = sources.fetch_export(_token_connection(), "correct")

    assert fetched.kind == "file"
    assert fetched.records is None
    assert fetched.path.exists()
    assert fetched.path.name == "Devices_1_16Aug26_0900.xlsx"   # honours content-disposition
    assert fetched.path.read_bytes().startswith(sources.XLSX_MAGIC)
    assert mock_api == ["POST /api/login", "GET /api/export"]


def test_token_login_uses_the_configured_field_names(mock_api):
    """APIs disagree on username vs email vs userName, so the names are configurable."""
    with pytest.raises(sources.SourceError, match="Login failed with HTTP 401"):
        sources.fetch_export(_token_connection(user_field="username", pass_field="password"),
                             "correct")


def test_wrong_password_is_reported_clearly(mock_api):
    with pytest.raises(sources.SourceError, match="Login failed with HTTP 401"):
        sources.fetch_export(_token_connection(), "wrong")


def test_login_without_a_token_in_the_response(monkeypatch, make_export):
    import httpx

    def handler(request):
        return httpx.Response(200, json={"status": "ok", "userId": 42})

    real_client = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kw: real_client(
        **{**{k: v for k, v in kw.items() if k != "verify"},
           "transport": httpx.MockTransport(handler)}))

    with pytest.raises(sources.SourceError, match="no token was found"):
        sources.fetch_export(_token_connection(), "correct")


def test_password_is_md5_hashed_when_the_platform_expects_it():
    """fotaWeb hashes in the browser, so the stored secret is the real password."""
    assert sources.apply_password_hash("Mmi@12345", "md5") == \
        "cfa0f6a70bb920f5b195e36e4546d73e"
    assert sources.apply_password_hash("Mmi@12345", "none") == "Mmi@12345"


def test_an_already_hashed_password_is_not_hashed_twice():
    """If someone stores the digest instead of the password, do not MD5 the MD5."""
    digest = "cfa0f6a70bb920f5b195e36e4546d73e"
    assert sources.apply_password_hash(digest, "md5") == digest
    assert sources.apply_password_hash(digest.upper(), "md5") == digest


def test_multipart_login_with_hashed_password(monkeypatch, make_export):
    """The exact contract fotaWeb uses: multipart body, md5 password, no-auth header."""
    import httpx

    xlsx_records = {"devices": [{"deviceId": "111", "currFirmVer": "7.5.0.51A",
                                 "model": "LOCAT140VB"}]}
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/user/login":
            body = request.content.decode("utf-8", errors="replace")
            seen["content_type"] = request.headers.get("content-type", "")
            seen["no_auth"] = request.headers.get("no-auth")
            seen["body"] = body
            if "cfa0f6a70bb920f5b195e36e4546d73e" in body:
                return httpx.Response(200, json={"data": {"access_token": TOKEN,
                                                          "displayname": "Shahbaz Khan"}})
            return httpx.Response(401, json={"error": "bad credentials"})
        if request.headers.get("Authorization") == f"Bearer {TOKEN}":
            return httpx.Response(200, json=xlsx_records)
        return httpx.Response(401)

    real_client = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kw: real_client(
        **{**{k: v for k, v in kw.items() if k != "verify"},
           "transport": httpx.MockTransport(handler)}))

    conn = sources.Connection(
        url="https://platform.test/api/user/devices",
        login_url="https://platform.test/user/login",
        username="shahbaz.khan@mapmyindia.com", auth_mode="token",
        login_encoding="multipart", password_hash="md5")

    fetched = sources.fetch_export(conn, "Mmi@12345")

    assert "multipart/form-data" in seen["content_type"]
    assert seen["no_auth"] == "True"
    assert "Mmi@12345" not in seen["body"]          # the plaintext never goes over the wire
    assert fetched.kind == "api"
    assert fetched.records == xlsx_records["devices"]


def test_login_returning_html_suggests_form_mode(monkeypatch):
    import httpx

    def handler(request):
        return httpx.Response(200, text="<!DOCTYPE html><html>sign in</html>")

    real_client = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kw: real_client(
        **{**{k: v for k, v in kw.items() if k != "verify"},
           "transport": httpx.MockTransport(handler)}))

    with pytest.raises(sources.SourceError, match="did not return JSON"):
        sources.fetch_export(_token_connection(), "correct")
