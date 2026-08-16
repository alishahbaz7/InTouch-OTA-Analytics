"""Paths and settings. Everything is derived from the repo root so the project stays portable."""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

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
