"""InTouch OTA Analytics — snapshot warehouse and analytics for the OTA platform."""

# Semantic versioning: MAJOR for a breaking change to the data model or API, MINOR for new
# capability, PATCH for fixes. Bump this in the same commit as the change it describes — a
# version that lags is worse than none, because it makes a bug report point at the wrong code.
__version__ = "1.1.0"

# What shipped in this version, shown in the UI so a screenshot is self-identifying.
RELEASED = "2026-08-16"
CODENAME = "digest"

VERSION_HISTORY = [
    ("1.1.0", "2026-08-16", "Fleet digest and database identity on every page and report; "
                            "snapshot bundles so two installs can merge history and prove "
                            "they agree"),
    ("1.0.0", "2026-08-15", "Device registry with last-checked/last-changed, change log, "
                            "fallback-to-base detection, auto-fetch agent, error log"),
    ("0.3.0", "2026-08-15", "Platform API source, token sign-in, retention policy"),
    ("0.2.0", "2026-08-15", "Dashboard, change and fallback detection"),
    ("0.1.0", "2026-08-15", "Snapshot warehouse and ingest"),
]


def build_info() -> dict:
    """Everything needed to identify exactly what is running."""
    import sys

    from . import db

    return {
        "version": __version__,
        "released": RELEASED,
        "codename": CODENAME,
        "schema_version": db.SCHEMA_VERSION,
        "python": sys.version.split()[0],
    }
