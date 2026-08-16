"""Who may see the dashboard, and what they may do.

Every page here exposes the fleet: 35,477 IMEIs, VINs and ICCIDs, plus an /update section that
posts to the production OTA admin API. Until this module existed all of it was open to anyone
who could reach the port, which was survivable only because the port was loopback on one laptop.

Two roles, because they answer different questions:

  admin   — full access, including /update/* and anything else that changes state
  viewer  — reads everything, changes nothing; every unsafe method is refused

Note what "viewer" does and does not buy: it limits *actions*, not *exposure*. A viewer still
reads every IMEI and VIN on the devices page. It is the right role for a colleague who should
not be able to trigger a fetch or edit a target, not a way to reduce who sees identifiers.

No new dependency: sessions are a signed cookie (stdlib hmac), passwords are scrypt from
hashlib. Starlette's own SessionMiddleware needs `itsdangerous`, which is not installed, and a
signed cookie is about twenty lines.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import time
from dataclasses import dataclass

from . import config

SESSION_COOKIE = "ota_session"
SESSION_MAX_AGE = 12 * 3600          # a working day; long enough not to nag, short enough to lapse

ENV_SECRET_KEY = "OTA_SECRET_KEY"
ENV_ADMIN_HASH = "OTA_ADMIN_PASSWORD_HASH"
ENV_VIEWER_HASH = "OTA_VIEWER_PASSWORD_HASH"
ENV_TRUST_PROXY = "OTA_TRUST_PROXY_AUTH"

# Cloudflare Access (and equivalents) put the signed-in identity here once the request has passed
# their login. Trusted only when explicitly enabled — a header is trivially forged by anything
# that can reach the port directly, so believing it by default would be worse than no auth.
PROXY_EMAIL_HEADER = "cf-access-authenticated-user-email"

ROLES = ("admin", "viewer")

# Reachable without a session. Everything else is refused: default-deny, so a route added later
# is protected by omission rather than exposed by it.
PUBLIC_PATHS = ("/login", "/logout", "/healthz", "/static", "/favicon.ico")

# Methods that only read. Anything else changes state and is refused to a viewer.
SAFE_METHODS = ("GET", "HEAD", "OPTIONS")


@dataclass(frozen=True)
class User:
    name: str
    role: str

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


# ─── passwords ──────────────────────────────────────────────────────────────

def hash_password(password: str) -> str:
    """scrypt with a random salt, in a self-describing string.

    Parameters travel with the hash so raising the cost later does not invalidate what is
    already deployed — verify reads n/r/p from the stored value rather than assuming today's.
    """
    salt = secrets.token_bytes(16)
    n, r, p = 2 ** 14, 8, 1
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=32)
    return "scrypt${}${}${}${}${}".format(
        n, r, p, base64.b64encode(salt).decode(), base64.b64encode(digest).decode())


def verify_password(password: str, stored: str) -> bool:
    """Constant-time check of a password against a stored hash. False on anything malformed."""
    try:
        scheme, n, r, p, salt_b64, digest_b64 = stored.split("$")
        if scheme != "scrypt":
            return False
        digest = hashlib.scrypt(
            password.encode("utf-8"), salt=base64.b64decode(salt_b64),
            n=int(n), r=int(r), p=int(p), dklen=len(base64.b64decode(digest_b64)))
    except (ValueError, TypeError, AttributeError):
        return False
    return hmac.compare_digest(digest, base64.b64decode(digest_b64))


def configured_hashes() -> dict[str, str]:
    """Role -> password hash, from the environment. Absent roles simply cannot log in."""
    found = {}
    for role, name in (("admin", ENV_ADMIN_HASH), ("viewer", ENV_VIEWER_HASH)):
        value = os.environ.get(name)
        if value:
            found[role] = value
    return found


def is_configured() -> bool:
    return bool(configured_hashes())


def authenticate(password: str) -> User | None:
    """Match a password against the configured roles, admin first.

    There is no username: with a handful of people and one shared credential per role, asking
    for a name would imply an accountability the system cannot deliver. Per-person identity
    comes from the proxy (see PROXY_EMAIL_HEADER), which can actually attest to it.
    """
    if not password:
        return None
    for role in ROLES:
        stored = configured_hashes().get(role)
        if stored and verify_password(password, stored):
            return User(name=role, role=role)
    return None


# ─── session cookie ─────────────────────────────────────────────────────────

def secret_key() -> bytes:
    """The key sessions are signed with, stable across restarts.

    From the environment when deployed. Otherwise generated once and kept beside the database,
    so restarting locally does not log everyone out — and so a key never lands in the repo.
    """
    from_env = os.environ.get(ENV_SECRET_KEY)
    if from_env:
        return from_env.encode("utf-8")

    path = config.DATA_DIR / "secret.key"
    if path.exists():
        return path.read_bytes()

    config.ensure_dirs()
    key = secrets.token_bytes(32)
    path.write_bytes(key)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass            # Windows, or a filesystem without POSIX modes
    return key


def issue(user: User, *, now: float | None = None) -> str:
    """A signed `role|expiry` token. The payload is readable, which is fine — it is signed,
    not secret, and there is nothing in it worth hiding."""
    expires = int((now or time.time()) + SESSION_MAX_AGE)
    payload = f"{user.role}|{expires}"
    signature = hmac.new(secret_key(), payload.encode("utf-8"), hashlib.sha256).digest()
    return f"{payload}|{base64.urlsafe_b64encode(signature).decode()}"


def read(token: str | None, *, now: float | None = None) -> User | None:
    """The user a token names, or None if it is missing, forged, malformed or expired."""
    if not token:
        return None
    try:
        role, expires, signature_b64 = token.rsplit("|", 2)
        expected = hmac.new(secret_key(), f"{role}|{expires}".encode("utf-8"),
                            hashlib.sha256).digest()
        signature = base64.urlsafe_b64decode(signature_b64)
    except (ValueError, TypeError):
        return None

    if not hmac.compare_digest(expected, signature):
        return None
    if float(expires) < (now or time.time()):
        return None
    if role not in ROLES:
        return None
    return User(name=role, role=role)


# ─── request-level decisions ────────────────────────────────────────────────

def trust_proxy_header() -> bool:
    return os.environ.get(ENV_TRUST_PROXY, "").strip().lower() in ("1", "true", "yes", "on")


def is_public(path: str) -> bool:
    return any(path == p or path.startswith(p + "/") or path.startswith(p)
               for p in PUBLIC_PATHS)


def identify(request) -> User | None:
    """Who is making this request: the proxy's verified identity, or our own session cookie."""
    if trust_proxy_header():
        email = request.headers.get(PROXY_EMAIL_HEADER)
        if email:
            # The proxy has already authenticated them; it does not carry our roles, so anyone
            # it lets through is an operator. Restricting *who* it lets through is configured
            # at the proxy, which is the only place that can enforce it.
            return User(name=email, role="admin")
    return read(request.cookies.get(SESSION_COOKIE))
