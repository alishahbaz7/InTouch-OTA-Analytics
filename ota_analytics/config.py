"""Paths and settings. Everything is derived from the app root so the project stays portable.

Two roots, and conflating them is what breaks a packaged build:

    ROOT      where the program lives and *writes* — the folder holding the .exe, or the repo
    RESOURCE  where the program's own files are *read* from — bundled, and read-only

Running from source they are the same directory, which is exactly why the difference is easy
to miss. PyInstaller unpacks bundled resources into its own folder (`sys._MEIPASS`) that is
separate from the .exe, and for a one-file build that folder is temporary and wiped on exit.
Deriving the database path from `__file__` therefore put `data/` inside it — every launch of
the packaged app would have started from an empty database and thrown the previous run away.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def is_frozen() -> bool:
    """Whether this is a packaged build rather than a source checkout."""
    return bool(getattr(sys, "frozen", False))


def _app_root() -> Path:
    """The folder the program writes into.

    Packaged, that is wherever the .exe was put — so the app is portable: copy the folder and
    the history goes with it, and `data\\ota_analytics.db` sits in plain sight next to the
    program that owns it. From source it is the repo root, unchanged.
    """
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


ROOT = _app_root()


def resource(*parts: str) -> Path:
    """Locate a file shipped *with* the program: schema.sql, the templates, the stylesheet.

    Read-only and never written to, so it is correct for these to live inside the bundle while
    the database lives beside the .exe.
    """
    base = getattr(sys, "_MEIPASS", None)
    root = Path(base) if base else Path(__file__).resolve().parent.parent
    return root.joinpath(*parts)


DATA_DIR = Path(os.environ.get("OTA_DATA_DIR", ROOT / "data"))
DB_PATH = Path(os.environ.get("OTA_DB_PATH", DATA_DIR / "ota_analytics.db"))
EXPORT_DIR = Path(os.environ.get("OTA_EXPORT_DIR", ROOT / "Sample data"))
REPORT_DIR = Path(os.environ.get("OTA_REPORT_DIR", ROOT / "reports"))

# Ingest tuning
BATCH_SIZE = 5_000

# The platform calls a device Online when it pinged within this window, Offline beyond it, and
# Inactive when it has never pinged. Used to cross-check STATUS against SEEN AT on ingest.
ONLINE_THRESHOLD_HOURS = 24

# Staleness buckets, in hours. Refinements of the platform's Offline bucket — "offline" covers
# everything from 25 hours to two years, which is not one operational category.
STALE_7D_HOURS = 7 * 24
STALE_30D_HOURS = 30 * 24

# A device counts as stalled when it carries a pending queue across this many consecutive
# snapshots with no firmware change. Provisional until the platform owner confirms what
# QUEUE actually means (see docs/DATA_PROFILE.md, open question 1).
STALL_SNAPSHOTS = 3


def ensure_dirs() -> None:
    """Create the writable directories. Safe to call repeatedly."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
