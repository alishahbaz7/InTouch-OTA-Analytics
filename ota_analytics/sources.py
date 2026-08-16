"""Getting new exports into the warehouse: file upload, or download from the OTA platform.

Credential handling, in short: the password goes to the OS credential store (Windows Credential
Manager via keyring) and never to a file in this repo, the database, or a log. Non-secret
settings — URL, username, auth mode — live in a small JSON file so the form can be pre-filled
without unlocking anything.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from . import config

SERVICE_NAME = "InTouchOTA-Analytics"
SETTINGS_PATH = config.DATA_DIR / "connection.json"

# xlsx files are zip archives; anything else is not the export we asked for.
XLSX_MAGIC = b"PK\x03\x04"
SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]")


class SourceError(Exception):
    """Anything that stops a new export from being retrieved."""


@dataclass
class Fetched:
    """What came back: a saved spreadsheet, or device records straight from the API."""
    path: Path | None = None
    records: list[dict] | None = None

    @property
    def kind(self) -> str:
        return "file" if self.path else "api"


@dataclass
class Preset:
    """A known platform, so its endpoints do not have to be typed in.

    Everything here was captured from the platform's own network traffic and verified against
    it — asking a user to re-enter facts we already know is just an opportunity for typos.
    """
    key: str
    label: str
    url: str = ""
    login_url: str = ""
    auth_mode: str = "token"
    login_encoding: str = "multipart"
    password_hash: str = "md5"
    user_field: str = "username"
    pass_field: str = "password"


PRESETS: dict[str, Preset] = {
    "intouch": Preset(
        key="intouch",
        label="InTouch OTA platform (fotaWeb)",
        url="https://ota-intouch.mappls.com/fotaAdminApi/api/user/devices",
        login_url="https://ota-intouch.mappls.com/fotaAdminApi/user/login",
        auth_mode="token",
        login_encoding="multipart",   # verified: the login posts multipart/form-data
        password_hash="md5",          # verified: the browser sends md5(password)
        user_field="username",
        pass_field="password",
    ),
    "custom": Preset(key="custom", label="Other platform — configure manually"),
}

DEFAULT_PRESET = "intouch"


@dataclass
class Connection:
    preset: str = DEFAULT_PRESET
    url: str = ""
    username: str = ""
    auth_mode: str = "token"     # token | form | basic | none
    login_url: str = ""
    verify_tls: bool = True
    user_field: str = "username"   # field name the login API expects for the user
    pass_field: str = "password"
    # How the login request is encoded. fotaWeb posts multipart/form-data; other platforms use
    # JSON or urlencoded, so this is explicit rather than assumed.
    login_encoding: str = "multipart"   # multipart | json | form
    # Some front-ends hash the password in the browser before sending. fotaWeb sends
    # md5(password), so the stored secret stays the real password and is hashed at send time.
    password_hash: str = "md5"          # md5 | none
    remembered: bool = False     # whether a password is held in the OS credential store


def apply_password_hash(password: str, algorithm: str) -> str:
    """Hash the password the way the platform's own login page does, if it does."""
    if algorithm == "md5":
        import hashlib
        # Already a 32-char hex digest? Then the caller stored the hash itself; do not re-hash.
        if len(password) == 32 and all(c in "0123456789abcdefABCDEF" for c in password):
            return password.lower()
        return hashlib.md5(password.encode("utf-8")).hexdigest()
    return password


# Where an API commonly puts the token in its login response. Searched depth-first, so
# {"data": {"accessToken": "..."}} is found as readily as {"token": "..."}.
TOKEN_KEYS = ("token", "accessToken", "access_token", "jwt", "idToken", "id_token",
              "authToken", "auth_token", "sessionToken")


def find_token(payload: object, depth: int = 0) -> str | None:
    """Pull a bearer token out of a login response without knowing the exact shape."""
    if depth > 4 or not isinstance(payload, dict):
        return None
    for key in TOKEN_KEYS:
        value = payload.get(key)
        if isinstance(value, str) and len(value) > 15:
            return value
    for value in payload.values():
        if isinstance(value, dict):
            found = find_token(value, depth + 1)
            if found:
                return found
    return None


# ─── stored settings ────────────────────────────────────────────────────────

def apply_preset(conn: Connection) -> Connection:
    """Fill endpoints and encoding from the chosen preset. Custom leaves them alone."""
    preset = PRESETS.get(conn.preset)
    if preset is None or preset.key == "custom":
        return conn
    conn.url = preset.url
    conn.login_url = preset.login_url
    conn.auth_mode = preset.auth_mode
    conn.login_encoding = preset.login_encoding
    conn.password_hash = preset.password_hash
    conn.user_field = preset.user_field
    conn.pass_field = preset.pass_field
    return conn


def load_connection() -> Connection:
    """Stored settings, or a ready-to-use default so nothing has to be configured first."""
    if not SETTINGS_PATH.exists():
        return apply_preset(Connection())
    try:
        data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return apply_preset(Connection())

    known = {f: data[f] for f in Connection.__dataclass_fields__ if f in data}
    conn = Connection(**known)
    # A preset is the source of truth for its endpoints: if one is corrected in code, saved
    # settings pick the correction up instead of pinning a stale URL forever.
    conn = apply_preset(conn)
    conn.remembered = bool(conn.username) and load_password(conn.username) is not None
    return conn


def save_connection(conn: Connection) -> None:
    """Persist non-secret settings only. The password never passes through here."""
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    data = asdict(conn)
    data.pop("remembered", None)
    SETTINGS_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


# ─── credentials ────────────────────────────────────────────────────────────

# Where the password comes from on a server. `keyring` needs an OS credential store, and a
# headless Linux box has none — so on the intended deployment target save_password() returns
# False, load_password() returns None, and the scheduler silently never logs in again. An
# environment variable is how a service is given a secret there: systemd reads it from a
# root-owned EnvironmentFile, it never touches connection.json, the database or a log line, and
# it leaves no copy behind when the process exits.
ENV_PASSWORD = "OTA_PLATFORM_PASSWORD"


def _keyring():
    try:
        import keyring
        return keyring
    except ImportError:
        return None


def _env_password() -> str | None:
    """The password supplied by the environment, if any. Blank is treated as unset — an empty
    variable is almost always a misconfigured unit file rather than an empty password."""
    return os.environ.get(ENV_PASSWORD) or None


def save_password(username: str, password: str) -> bool:
    """Store the password in the OS credential store. False if no backend is available."""
    kr = _keyring()
    if kr is None or not username:
        return False
    try:
        kr.set_password(SERVICE_NAME, username, password)
        return True
    except Exception:
        return False


def load_password(username: str) -> str | None:
    """The stored password: the environment first, then the OS credential store.

    Environment wins deliberately. It is only ever set on purpose, by whoever deployed the
    service, and "what I configured is what runs" is the behaviour that makes a deployment
    debuggable — a stale keyring entry silently overriding it is not.
    """
    from_env = _env_password()
    if from_env:
        return from_env

    kr = _keyring()
    if kr is None or not username:
        return None
    try:
        return kr.get_password(SERVICE_NAME, username)
    except Exception:
        return None


def forget_password(username: str) -> None:
    kr = _keyring()
    if kr is None or not username:
        return
    try:
        kr.delete_password(SERVICE_NAME, username)
    except Exception:
        pass   # nothing stored, or no backend — both mean "not remembered"


def credential_store_name() -> str:
    if _env_password():
        return f"{ENV_PASSWORD} environment variable"
    kr = _keyring()
    if kr is None:
        return "unavailable"
    try:
        name = kr.get_keyring().__class__.__name__
    except Exception:
        return "unavailable"
    # keyring falls back to a no-op backend when the platform has no credential store, which
    # accepts a password and returns nothing later. Naming it beats reporting a store that
    # cannot store anything.
    return {"WinVaultKeyring": "Windows Credential Manager",
            "fail.Keyring": "unavailable (no OS credential store)",
            "chainer.ChainerBackend": "unavailable (no OS credential store)"}.get(name, name)


# ─── acquiring an export ────────────────────────────────────────────────────

def safe_filename(name: str, fallback_stem: str = "Devices_online") -> str:
    """Reduce an untrusted filename to something safe to write into the export folder."""
    name = Path(name or "").name                      # strip any directory part
    name = SAFE_NAME.sub("_", name).lstrip(".")
    if not name.lower().endswith(".xlsx"):
        name = f"{fallback_stem}.xlsx"
    return name or f"{fallback_stem}.xlsx"


def timestamped_name(prefix: str = "Devices_online", when: datetime | None = None) -> str:
    """Build a filename the snapshot-time parser understands: Devices_online_15Aug26_1530.xlsx"""
    when = when or datetime.now()
    return f"{prefix}_{when.strftime('%d%b%y_%H%M')}.xlsx"


def find_records(payload: object, depth: int = 0) -> list[dict] | None:
    """Locate the list of device records in an API response.

    Platforms wrap their data differently — a bare array, {"data": [...]}, {"result": {"rows":
    [...]}} — so the list of dicts is found by shape rather than by an assumed key.
    """
    if isinstance(payload, list):
        return payload if payload and isinstance(payload[0], dict) else None
    if depth > 4 or not isinstance(payload, dict):
        return None
    for key in ("data", "result", "results", "rows", "records", "devices", "content", "list"):
        found = find_records(payload.get(key), depth + 1)
        if found:
            return found
    for value in payload.values():                      # fall back to any list of objects
        found = find_records(value, depth + 1)
        if found:
            return found
    return None


def _validate_xlsx(content: bytes, source: str, *, from_url: bool = False) -> None:
    """Reject anything that is not really a spreadsheet, with a message that fits the source."""
    if not content:
        raise SourceError(f"{source} is empty.")
    if content.startswith(XLSX_MAGIC):
        return

    head = content[:400].decode("utf-8", errors="replace").lower()
    if "<html" in head or "<!doctype" in head:
        if from_url:
            raise SourceError(
                f"{source} is a web page, not a spreadsheet — the login step most likely "
                "failed, or the URL points at a screen rather than the export file. Open the "
                "URL in a browser: it should download an .xlsx straight away.")
        raise SourceError(
            f"{source} is a web page, not a spreadsheet. This usually means a login page was "
            "saved instead of the export — download the file again from the platform.")

    hint = ("Check that the URL is the export endpoint." if from_url
            else "Export it again from the platform as .xlsx.")
    raise SourceError(f"{source} is not an .xlsx file (it starts with {content[:8]!r}). {hint}")


def store_upload(filename: str, content: bytes) -> Path:
    """Write an uploaded export into the export folder, ready to ingest."""
    _validate_xlsx(content, "The uploaded file")

    name = safe_filename(filename)
    from . import normalize
    if normalize.snapshot_at_from_filename(name) is None:
        # Without a parseable date in the name the snapshot time would fall back to the file's
        # mtime, which is when it was uploaded rather than when the export was taken.
        name = timestamped_name(Path(name).stem[:40] or "Devices_upload")

    config.EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    target = config.EXPORT_DIR / name
    if target.exists():
        target = config.EXPORT_DIR / f"{target.stem}_{datetime.now().strftime('%H%M%S')}.xlsx"
    target.write_bytes(content)
    return target


def fetch_export(conn: Connection, password: str, timeout: float = 180.0) -> Path:
    """Download an export from the platform and place it in the export folder.

    Supports the two shapes an internal platform normally offers: a form login that sets a
    session cookie, or HTTP Basic auth. The response is verified to actually be a spreadsheet,
    because the usual failure is a 200 OK containing the login page.
    """
    try:
        import httpx
    except ImportError as exc:                                  # pragma: no cover
        raise SourceError("httpx is not installed — run: pip install -r requirements.txt") from exc

    if not conn.url:
        raise SourceError("No export URL configured.")

    headers: dict[str, str] = {}
    try:
        with httpx.Client(follow_redirects=True, timeout=timeout,
                          verify=conn.verify_tls) as client:
            if conn.auth_mode == "bearer":
                # A token pasted straight from the browser. Useful immediately, but it expires,
                # so it suits a one-off pull rather than a scheduled one.
                headers["Authorization"] = f"Bearer {password}"

            if conn.auth_mode in {"form", "token"}:
                if not conn.login_url:
                    raise SourceError(
                        f"{conn.auth_mode.title()} login selected but no login URL was given. "
                        "Provide the URL the sign-in request goes to, or switch to Basic auth.")

                secret = apply_password_hash(password, conn.password_hash)
                credentials = {conn.user_field or "username": conn.username,
                               conn.pass_field or "password": secret}

                # Marks the request as the one call that legitimately carries no bearer token.
                # Inert on platforms that do not look for it.
                login_headers = {"no-auth": "True", "accept": "application/json, text/plain, */*"}

                if conn.auth_mode == "form":
                    # Classic server-rendered login: form post that sets a session cookie.
                    login = client.post(conn.login_url, data=credentials, headers=login_headers)
                elif conn.login_encoding == "json":
                    login = client.post(conn.login_url, json=credentials, headers=login_headers)
                elif conn.login_encoding == "form":
                    login = client.post(conn.login_url, data=credentials, headers=login_headers)
                else:
                    # multipart/form-data — what fotaWeb's login posts.
                    login = client.post(
                        conn.login_url,
                        files={k: (None, v) for k, v in credentials.items()},
                        headers=login_headers)

                if login.status_code >= 400:
                    raise SourceError(
                        f"Login failed with HTTP {login.status_code}. Check the credentials, "
                        "the login URL, and the field names the API expects.")

                if conn.auth_mode == "token":
                    try:
                        payload = login.json()
                    except ValueError:
                        raise SourceError(
                            "The login URL did not return JSON, so no token could be read. "
                            "If sign-in sets a cookie instead, choose Form login."
                        ) from None
                    token = find_token(payload)
                    if not token:
                        keys = ", ".join(list(payload)[:8]) if isinstance(payload, dict) else "—"
                        raise SourceError(
                            "Logged in, but no token was found in the response. Fields "
                            f"returned: {keys}. Send me the login response shape and I will "
                            "map it.")
                    headers["Authorization"] = f"Bearer {token}"

            auth = (conn.username, password) if conn.auth_mode == "basic" else None
            response = client.get(conn.url, auth=auth, headers=headers)

            if response.status_code in (401, 403):
                # Say what was actually attempted. "Credentials rejected" sends people to
                # re-check a password that was never the problem.
                status = response.status_code
                if conn.auth_mode == "bearer":
                    raise SourceError(
                        f"The platform rejected the token (HTTP {status}). Two usual causes: "
                        "the password box holds something other than the token — it must be "
                        "the value after 'Bearer ' in the authorization header, like "
                        "'2aeedd18-d083-...', not your account password — or the token has "
                        "expired, since logging out of the platform invalidates it.")
                if conn.auth_mode == "basic":
                    raise SourceError(
                        f"The platform rejected HTTP Basic auth (HTTP {status}). This API "
                        "authenticates with a bearer token, not a username and password over "
                        "Basic. Switch the sign-in method to 'Paste a bearer token'.")
                if conn.auth_mode == "none":
                    raise SourceError(
                        f"The export needs authentication (HTTP {status}), but the sign-in "
                        "method is set to 'No authentication'.")
                raise SourceError(
                    f"Signed in, but the export was refused (HTTP {status}). The login "
                    "succeeded and yet the token it returned does not open this URL — check "
                    "that the export URL is right and that this account may reach it.")
            if response.status_code >= 400:
                raise SourceError(f"Download failed with HTTP {response.status_code}.")

            content = response.content
            disposition = response.headers.get("content-disposition", "")
            content_type = response.headers.get("content-type", "")

            # A web app that builds its spreadsheet in the browser serves JSON here. That is
            # not an error — the records can be ingested directly, skipping the file entirely.
            if "json" in content_type.lower() or content[:1] in (b"[", b"{"):
                try:
                    payload = response.json()
                except ValueError:
                    raise SourceError(
                        "The export URL returned something that is neither a spreadsheet nor "
                        "valid JSON.") from None
                records = find_records(payload)
                if not records:
                    shape = (", ".join(list(payload)[:10]) if isinstance(payload, dict)
                             else type(payload).__name__)
                    raise SourceError(
                        "The URL returned JSON with no list of device records in it "
                        f"(top-level: {shape}). This is probably a summary endpoint rather "
                        "than the device list.")
                return Fetched(records=records)
    except SourceError:
        raise
    except Exception as exc:
        # Never surface the raw exception text — request objects can carry the URL with
        # credentials embedded.
        raise SourceError(f"Could not reach the platform: {type(exc).__name__}. "
                          "Check the URL, the network, and whether a VPN is required.") from exc

    _validate_xlsx(content, "The downloaded file", from_url=True)

    name = ""
    match = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', disposition)
    if match:
        name = safe_filename(match.group(1))

    from . import normalize
    if not name or normalize.snapshot_at_from_filename(name) is None:
        name = timestamped_name()

    config.EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    target = config.EXPORT_DIR / name
    if target.exists():
        target = config.EXPORT_DIR / f"{target.stem}_{datetime.now().strftime('%H%M%S')}.xlsx"
    target.write_bytes(content)
    return Fetched(path=target)
