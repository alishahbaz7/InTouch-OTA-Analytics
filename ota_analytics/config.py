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


def _channel() -> str:
    """Which copy this is: the packaged `release`, or `dev` running from source.

    Both are meant to run at the same time on one machine — the release in daily use, the
    source copy being worked on — so everything they could fight over is keyed on this: the
    default port, which running copy a new launch defers to, and the session cookie name.
    Their data is already apart, because ROOT is. A server deployed from source is a release
    in every sense but packaging, so OTA_CHANNEL overrides the guess.
    """
    chosen = (os.environ.get("OTA_CHANNEL") or "").strip().lower()
    if chosen in ("release", "dev"):
        return chosen
    return "release" if is_frozen() else "dev"


CHANNEL = _channel()
# Release keeps 8000 so existing bookmarks and the deploy units stay right; dev sits beside it.
DEFAULT_PORT = 8000 if CHANNEL == "release" else 8100


def command_hint(*args: str) -> str:
    """How to run this program from a terminal, spelled the way the reader would type it.

    Packaged, `python -m ota_analytics.cli` does not exist and printing it sends someone to
    install Python to fix a problem they do not have. The executable is the CLI.
    """
    prefix = [Path(sys.executable).name] if is_frozen() else ["python", "-m", "ota_analytics.cli"]
    return " ".join(f'"{part}"' if " " in part else part for part in (*prefix, *args))


def resource(*parts: str) -> Path:
    """Locate a file shipped *with* the program: schema.sql, the templates, the stylesheet.

    Read-only and never written to, so it is correct for these to live inside the bundle while
    the database lives beside the .exe.
    """
    base = getattr(sys, "_MEIPASS", None)
    root = Path(base) if base else Path(__file__).resolve().parent.parent
    return root.joinpath(*parts)


def _path_from_env(name: str, default: Path) -> Path:
    """Read a path override, treating a blank value as unset.

    `OTA_DATA_DIR=` in a unit file or a shell is a misconfiguration, not a request to write to
    the current working directory — which is where `Path("")` resolves, silently creating a
    second empty database somewhere nobody will look for it. Same rule as
    OTA_PLATFORM_PASSWORD: blank counts as absent.
    """
    value = (os.environ.get(name) or "").strip()
    return Path(value) if value else default


DATA_DIR = _path_from_env("OTA_DATA_DIR", ROOT / "data")
DB_PATH = _path_from_env("OTA_DB_PATH", DATA_DIR / "ota_analytics.db")
EXPORT_DIR = _path_from_env("OTA_EXPORT_DIR", ROOT / "Sample data")
REPORT_DIR = _path_from_env("OTA_REPORT_DIR", ROOT / "reports")

# Ingest tuning
BATCH_SIZE = 5_000

# The platform calls a device Online when it pinged within this window, Offline beyond it, and
# Inactive when it has never pinged. Used to cross-check STATUS against SEEN AT on ingest.
ONLINE_THRESHOLD_HOURS = 24

# Staleness buckets, in hours. Refinements of the platform's Offline bucket — "offline" covers
# everything from 25 hours to two years, which is not one operational category.
STALE_7D_HOURS = 7 * 24
STALE_30D_HOURS = 30 * 24

# A device counts as stalled when its task has been pending for this long with no firmware
# change in the meantime.
#
# Measured in hours rather than in snapshots, which is what this used to be. A snapshot count
# only means something if the cadence is fixed, and it is not: at the original daily export
# "3 snapshots" was three days, but at the 15-minute cadence the tool actually runs at it became
# 45 minutes — so three quarters of the fleet qualified (26,481 devices) and the list stopped
# being a list of anything. An hour is an hour whatever the cadence.
STALL_HOURS = 24


def ensure_dirs() -> None:
    """Create the writable directories. Safe to call repeatedly."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
