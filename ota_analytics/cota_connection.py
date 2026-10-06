"""Signing in to the COTA cloud — which cloud, and how this install gets its bearer token.

The COTA engine (`cota.py`) only needs a bearer token and a base URL. This module is how the
Sign in tab provides both, mirroring the FOTA connection on /update:

    token     paste the token (the "auth code") copied from the portal. Works today; it expires
              when the portal session does.
    password  username + password, exchanged for a token at the cloud's login URL. The cloud's
              login call has not been captured yet (COTA.md, open question 5), so the URL is
              entered rather than guessed, and every detail of the request is configurable.

Rules carried over from `sources.py`, which must not be relaxed here either:

  * Secrets live in the OS credential store or the environment — never in
    `cota_connection.json`, the database, a log line or an HTML value attribute.
  * Raw exception text from an HTTP call is never shown: the request can carry the password.
  * The COTA password is stored under its own account name, so it can never overwrite the FOTA
    platform's password for the same username.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, fields
from datetime import datetime

from . import config, cota, sources

SETTINGS_PATH = config.DATA_DIR / "cota_connection.json"
ENV_PASSWORD = "OTA_COTA_PASSWORD"
PASSWORD_ACCOUNT = "cota-password:{username}"     # keyring username under sources.SERVICE_NAME

@dataclass(frozen=True)
class CloudPreset:
    """A cloud this install knows, so none of its endpoints have to be typed in."""
    label: str
    base_url: str
    login_url: str = ""
    login_encoding: str = "json"
    password_hash: str = "none"
    user_field: str = "username"
    pass_field: str = "password"
    # The portal's own sign-in *page*, where a person gets a fresh token. Not a login API:
    # everything after '#' never reaches the server.
    portal_url: str = ""


# The InTouch cloud's login is taken from the portal's own sign-in request, captured by the user
# on 2026-10-06: POST multipart/form-data to IntouchAdminApi/user/login with `username` and
# `password`, the password sent as its MD5 hex digest — the same scheme as Web FOTA's login.
PRESETS = {
    "intouch": CloudPreset(
        "InTouch cloud — ctvms.mappls.com", cota.BASE_URL,
        login_url="https://ctvms.mappls.com/IntouchAdminApi/user/login",
        login_encoding="multipart", password_hash="md5",
        portal_url="https://ctvms.mappls.com/adminnextgen/#/login"),
    "custom": CloudPreset("Other cloud — enter the API URL", ""),
}
CLOUDS = {key: (p.label, p.base_url) for key, p in PRESETS.items()}
PORTAL_URLS = {key: p.portal_url for key, p in PRESETS.items() if p.portal_url}
DEFAULT_CLOUD = "intouch"
METHODS = ("token", "password")


@dataclass
class CotaConnection:
    cloud: str = DEFAULT_CLOUD
    base_url: str = cota.BASE_URL
    method: str = "token"              # token | password
    username: str = ""
    login_url: str = ""
    login_encoding: str = "json"       # json | multipart | form
    password_hash: str = "none"        # none | md5
    user_field: str = "username"
    pass_field: str = "password"
    signed_in_at: str = ""             # last time a token was obtained or pasted
    rejected_at: str = ""              # last time the cloud refused the held token (401/403)
    rejected_status: int = 0


class CotaSignInError(Exception):
    """A sign-in failure with a message that is safe to show as it is."""


def load() -> CotaConnection:
    try:
        raw = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return CotaConnection()
    known = {f.name for f in fields(CotaConnection)}
    conn = CotaConnection(**{k: v for k, v in raw.items() if k in known})
    return apply_cloud(conn)


def save(conn: CotaConnection) -> None:
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_PATH.write_text(json.dumps(asdict(conn), indent=2), encoding="utf-8")


def apply_cloud(conn: CotaConnection) -> CotaConnection:
    """A known cloud supplies its own endpoints and sign-in details, so nothing typed — or
    posted — can misconfigure it or send a password somewhere else."""
    if conn.cloud not in PRESETS:
        conn.cloud = "custom"
    if conn.cloud != "custom":
        preset = PRESETS[conn.cloud]
        conn.base_url = preset.base_url
        if preset.login_url:
            conn.login_url = preset.login_url
            conn.login_encoding = preset.login_encoding
            conn.password_hash = preset.password_hash
            conn.user_field = preset.user_field
            conn.pass_field = preset.pass_field
    conn.base_url = conn.base_url.strip().rstrip("/")
    if conn.method not in METHODS:
        conn.method = "token"
    return conn


# ─── the password ───────────────────────────────────────────────────────────

def _account(username: str) -> str:
    return PASSWORD_ACCOUNT.format(username=username)


def save_password(username: str, password: str) -> bool:
    kr = sources._keyring()
    if kr is None or not username:
        return False
    try:
        kr.set_password(sources.SERVICE_NAME, _account(username), password)
        return True
    except Exception:
        return False


def load_password(username: str) -> str | None:
    """The environment first — what a deployment configured is what runs — then the store."""
    from_env = (os.environ.get(ENV_PASSWORD) or "").strip()
    if from_env:
        return from_env
    kr = sources._keyring()
    if kr is None or not username:
        return None
    try:
        return kr.get_password(sources.SERVICE_NAME, _account(username))
    except Exception:
        return None


def forget_password(username: str) -> None:
    kr = sources._keyring()
    if kr is None or not username:
        return
    try:
        kr.delete_password(sources.SERVICE_NAME, _account(username))
    except Exception:
        pass


# ─── getting a token ────────────────────────────────────────────────────────

def _store_token(token: str) -> None:
    token = cota.clean_token(token)
    if not token:
        raise CotaSignInError("No token given.")
    if not cota.save_token(token):
        raise CotaSignInError(
            "Could not save the token to the OS credential store. Set it in the "
            f"{cota.ENV_TOKEN} environment variable instead.")


def record_auth(accepted: bool, http_status: int) -> None:
    """What the cloud said about the token on its last answer. A refusal is remembered until a
    fresh token is saved or a later call is accepted; nothing is written when nothing changed."""
    conn = load()
    if not accepted:
        conn.rejected_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn.rejected_status = http_status
        save(conn)
    elif conn.rejected_at:
        conn.rejected_at, conn.rejected_status = "", 0
        save(conn)


def use_token(conn: CotaConnection, token: str) -> None:
    """Keep a token pasted from the portal."""
    _store_token(token)
    conn.signed_in_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn.rejected_at, conn.rejected_status = "", 0
    save(conn)


def sign_in(conn: CotaConnection, password: str, *, timeout: float = 30.0) -> None:
    """Exchange username + password for a token at the login URL, and keep the token."""
    if not conn.login_url:
        raise CotaSignInError(
            "No login URL. The cloud's sign-in call has not been captured yet, so it cannot be "
            "filled in for you — enter it under Sign-in details, or paste a token instead.")
    if not conn.username or not password:
        raise CotaSignInError("Enter the username and password.")

    import httpx

    secret = sources.apply_password_hash(password, conn.password_hash)
    credentials = {conn.user_field or "username": conn.username,
                   conn.pass_field or "password": secret}
    headers = {"no-auth": "True", "accept": "application/json, text/plain, */*"}
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            if conn.login_encoding == "multipart":
                reply = client.post(conn.login_url, headers=headers,
                                    files={k: (None, v) for k, v in credentials.items()})
            elif conn.login_encoding == "form":
                reply = client.post(conn.login_url, headers=headers, data=credentials)
            else:
                reply = client.post(conn.login_url, headers=headers, json=credentials)
    except Exception as exc:
        # The type only: the exception can carry the request, and the request the password.
        raise CotaSignInError(f"Could not reach the login URL: {type(exc).__name__}. Check the "
                              "address and whether a VPN is required.") from None

    if reply.status_code >= 400:
        raise CotaSignInError(f"Sign-in failed with HTTP {reply.status_code}. Check the "
                              "username, password, login URL and field names.")
    try:
        payload = reply.json()
    except ValueError:
        raise CotaSignInError("The login URL did not answer with JSON, so no token could be "
                              "read from it.") from None
    token = sources.find_token(payload)
    if not token:
        keys = ", ".join(list(payload)[:8]) if isinstance(payload, dict) else "—"
        raise CotaSignInError(f"Signed in, but the reply carried no token. Fields returned: "
                              f"{keys}.")
    _store_token(token)
    conn.signed_in_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn.rejected_at, conn.rejected_status = "", 0
    save(conn)


def renew(conn: CotaConnection | None = None) -> bool:
    """Sign in again by itself with the saved username and password, after the cloud rejected
    the token. True when a fresh token is now held.

    Only for the password method with a remembered password, and never when the token comes from
    the environment — that one would still win over a renewed one, so renewing would only hide
    the real problem.
    """
    conn = conn or load()
    if conn.method != "password" or not conn.username or not conn.login_url:
        return False
    if (os.environ.get(cota.ENV_TOKEN) or "").strip():
        return False
    password = load_password(conn.username)
    if not password:
        return False
    try:
        sign_in(conn, password)
    except CotaSignInError:
        return False
    return True


def sign_out(conn: CotaConnection) -> None:
    cota.forget_token()
    forget_password(conn.username)
    conn.signed_in_at = ""
    conn.rejected_at, conn.rejected_status = "", 0
    save(conn)


def status(conn: CotaConnection | None = None) -> dict:
    """What the header chip and the Sign in tab say. Whether a token is held, never the token."""
    conn = conn or load()
    from_env = bool((os.environ.get(cota.ENV_TOKEN) or "").strip())
    held = bool(cota.load_token())
    rejected = bool(held and conn.rejected_at)
    if rejected:
        # "Session expired" — what a person sees happen: the portal session the token came from
        # has ended. The tooltip carries the detail.
        label = "Cloud: session expired"
        level = "error"
        title = (f"The cloud rejected the token at {conn.rejected_at[11:16]} "
                 f"(HTTP {conn.rejected_status}). "
                 + (f"It comes from {cota.ENV_TOKEN} — replace it there."
                    if from_env else "Sign in again with a fresh token from the portal."))
    elif held:
        label = "Cloud: token from environment" if from_env else "Cloud: signed in"
        level = "ok"
        title = f"Cloud sign-in — {conn.base_url}" + (
            f" · since {conn.signed_in_at}" if conn.signed_in_at else "")
    else:
        label, level, title = "Cloud: not signed in", "none", "Sign in to the cloud to send"
    return {
        "level": level,
        "label": label,
        "title": title,
        "rejected": rejected,
        "rejected_at": conn.rejected_at if rejected else "",
        "rejected_status": conn.rejected_status if rejected else 0,
        "token_held": held,
        "token_from_env": from_env,
        "password_saved": bool(conn.username and load_password(conn.username)),
        # A rejected token renews itself when this is true — the chip can say so.
        "renews": conn.method == "password" and bool(conn.username and load_password(conn.username))
                  and not from_env,
        "signed_in_at": conn.signed_in_at,
        "base_url": conn.base_url,
    }


def client(conn: CotaConnection | None = None) -> cota.Client:
    """A COTA client for the configured cloud with the held token."""
    conn = conn or load()
    return cota.Client(cota.load_token() or "", base_url=conn.base_url or cota.BASE_URL,
                       on_auth=record_auth)
