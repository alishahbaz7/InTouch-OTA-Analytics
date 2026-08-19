"""InTouch OTA Analytics — snapshot warehouse and analytics for the OTA platform."""

# Semantic versioning: MAJOR for a breaking change to the data model or API, MINOR for new
# capability, PATCH for fixes. Bump this in the same commit as the change it describes — a
# version that lags is worse than none, because it makes a bug report point at the wrong code.
__version__ = "1.5.0"

# What shipped in this version, shown in the UI so a screenshot is self-identifying.
RELEASED = "2026-08-19"
CODENAME = "package"

VERSION_HISTORY = [
    ("1.5.0", "2026-08-19", "Determinate progress bars for import, merge and fetch: the POST "
                            "returns at once and the page polls a server-side job, so the work "
                            "is visible and survives the tab being closed"),
    ("1.4.0", "2026-08-19", "Devices-per-firmware table gains online, offline, inactive and "
                            "task-pending columns, under a grouped header that names each "
                            "percentage's denominator"),
    ("1.3.3", "2026-08-17", "The Firmware page uses the same model dropdown as the overview, "
                            "instead of an always-open multi-select list"),
    ("1.3.2", "2026-08-17", "The connection form says when a password is already saved, instead "
                            "of leaving a blank box that looks like it needs filling"),
    ("1.3.1", "2026-08-17", "The interleave option asks the question it means — 'their data is "
                            "older than mine' — instead of naming the mechanism"),
    ("1.3.0", "2026-08-17", "CSV exports can be loaded as well as .xlsx, columns matched by "
                            "name; uploads and merges no longer freeze the whole dashboard "
                            "while they run"),
    ("1.2.3", "2026-08-17", "XLSX downloads work again — 128 devices carry a corrupt ICCID that "
                            "a spreadsheet may not contain, and the quality page now reports "
                            "them; clearer messages when a bundle or upload is a no-op"),
    ("1.2.2", "2026-08-17", "Interleaved bundle merge finishes instead of hanging, and is 5.6x "
                            "faster; every fetch is faster too — the registry resolved the "
                            "device_state view fifteen times per snapshot instead of once"),
    ("1.2.1", "2026-08-17", "Launching the app shows the copy already running instead of "
                            "starting a second one; 'Start with Windows' withdrawn — it left a "
                            "windowless copy holding the port that could not be seen or stopped"),
    ("1.2.0", "2026-08-16", "Packaged Windows application: portable one-folder build with its "
                            "data beside the .exe, a windowless twin for auto-start, and the "
                            "CLI reachable from the executable"),
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
