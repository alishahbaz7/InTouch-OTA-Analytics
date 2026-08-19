"""Normalization rules for the platform export.

Each rule is a small pure function so it can be unit-tested in isolation. The source data has
specific, documented quirks (see docs/DATA_PROFILE.md) — every one of them is handled here and
nowhere else.
"""

from __future__ import annotations

import re
from datetime import datetime

# The export uses a literal '-' as its null marker across many columns.
NULL_MARKERS = {"-", "--", "n/a", "na", "null", "none"}

# Three spellings of one model in the sample data.
MODEL_ALIASES = {
    "ax1_scan": "AX1_SCAN",
    "scan_ax1": "AX1_SCAN",
    "ax1scan": "AX1_SCAN",
}

# Appears on 24,512 of 35,477 devices — a default, not a real vehicle identifier.
PLACEHOLDER_VINS = {"DL1CAB1234"}

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

# Devices_35477_15Aug26_1511.xlsx -> 2026-08-15 15:11
_FILENAME_RE = re.compile(
    r"(?P<day>\d{1,2})(?P<mon>[A-Za-z]{3})(?P<year>\d{2,4})[_-](?P<hh>\d{2})(?P<mm>\d{2})"
)

_VERSION_PART_RE = re.compile(r"^(\d*)(.*)$")


def clean(value: object) -> str | None:
    """Trim, and collapse the export's null markers to None. Run this before any other rule."""
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in NULL_MARKERS:
        return None
    return text


# What each stored status is called on screen. The database keeps the platform's own word,
# because that is what the export said and re-labelling stored data would make the two disagree.
# Only the reading changes.
#
# "Inactive" is the platform's term for a device that has never pinged, and on the real fleet the
# two are exactly the same 645 devices: `seen_at IS NULL` and `status = 'Inactive'` select an
# identical set. Those devices are onboarded and carry an IMEI and nothing else — no VIN, no
# ICCID, no first ping, never tasked — so they are waiting to be activated, not inactive in the
# sense of having gone quiet. "Inactive" reads like a device that stopped working; these have
# never started.
STATUS_LABELS = {
    "Online": "Online",
    "Offline": "Offline",
    "Inactive": "Activation-Pending",
}


def status_label(value: object) -> str:
    """The name for a stored status. Unknown values are shown as they are, not hidden."""
    text = clean(value)
    return STATUS_LABELS.get(text or "", text or "—")


def canon_status(value: object) -> str | None:
    """Canonical device status.

    Confirmed by the platform owner (2026-08-15):

        Online     last ping within 24 hours
        Offline    no ping for more than 24 hours
        Inactive   never pinged at all  (shown as '-' in the platform UI)

    Status is therefore a 24-hour recency bucket over SEEN AT, measured against the moment the
    export was taken — not an independent liveness signal. `-` and `Inactive` are the same
    state, so both normalize to Inactive.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in NULL_MARKERS:
        return "Inactive"
    lowered = text.lower()
    if lowered.startswith("online"):
        return "Online"
    if lowered.startswith("offline"):
        return "Offline"
    if lowered.startswith("inactive"):
        return "Inactive"
    return text


def canon_model(value: object) -> str | None:
    """Canonical device model. Collapses AX1_SCAN / AX1_sCAN / sCAN_AX1 into one identity."""
    text = clean(value)
    if text is None:
        return None
    return MODEL_ALIASES.get(text.lower().replace("-", "_"), text)


def canon_firmware(value: object) -> str | None:
    """Canonical firmware string. Strips the inconsistent 'V' prefix (V7.2.2 == 7.2.2)."""
    text = clean(value)
    if text is None:
        return None
    if len(text) > 1 and text[0] in "Vv" and text[1].isdigit():
        text = text[1:]
    return text


def fw_family(firmware: str | None) -> str | None:
    """Coarse family for grouping: 7.5.0.51A -> 7.5.x, 2.0.0161 -> 2.0.x.

    Families are only comparable within a single device model — the models use unrelated
    versioning schemes.
    """
    if not firmware:
        return None
    parts = firmware.split(".")
    if len(parts) < 2:
        return f"{parts[0]}.x"
    return f"{parts[0]}.{parts[1]}.x"


def fw_sortkey(firmware: str | None) -> str | None:
    """Zero-padded key that sorts firmware correctly as text.

    7.5.0.51A -> '00007.00005.00000.00051A'. Lets SQL ORDER BY and >/< comparisons work on
    versions, which is what upgrade-vs-downgrade detection is built on.
    """
    if not firmware:
        return None
    out = []
    for part in firmware.split("."):
        match = _VERSION_PART_RE.match(part.strip())
        digits, rest = match.group(1), match.group(2)
        out.append(f"{int(digits) if digits else 0:05d}{rest.upper()}")
    return ".".join(out)


def parse_dt(value: object) -> str | None:
    """Parse the export's DD-MM-YY HH:MM:SS timestamps into ISO8601.

    Day-first and explicit: 15-08-26 15:11:17 is 15 Aug 2026. No guessing parser is used,
    because a month-first misread would silently corrupt every date in the fleet.
    """
    if isinstance(value, datetime):
        return value.isoformat(sep=" ", timespec="seconds")
    text = clean(value)
    if text is None:
        return None
    for fmt in ("%d-%m-%y %H:%M:%S", "%d-%m-%Y %H:%M:%S", "%d-%m-%y %H:%M", "%d-%m-%Y %H:%M",
                "%Y-%m-%d %H:%M:%S"):   # ISO, as produced by parse_epoch
        try:
            return datetime.strptime(text, fmt).isoformat(sep=" ", timespec="seconds")
        except ValueError:
            continue
    return None


def canon_vin(value: object) -> str | None:
    """Real VINs only — the shared placeholder becomes None."""
    text = clean(value)
    if text is None or text.upper() in PLACEHOLDER_VINS:
        return None
    return text


def split_groups(value: object) -> list[str]:
    """Explode the comma-separated Groups column, preserving order and dropping null markers."""
    text = clean(value)
    if text is None:
        return []
    seen: set[str] = set()
    groups = []
    for part in text.split(","):
        name = clean(part)
        if name and name not in seen:
            seen.add(name)
            groups.append(name)
    return groups


def parse_epoch(value: object) -> str | None:
    """Parse an epoch timestamp, in seconds or milliseconds, into ISO8601.

    The platform API mixes both: `lastPingTime` and `creationTime` are milliseconds while
    `firstPing` is seconds. Guessing wrong puts a date in 1970 or far in the future, so the
    unit is decided by magnitude rather than by field name.
    """
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, str):
        text = clean(value)
        if text is None or not text.lstrip("-").isdigit():
            return None
        value = int(text)
    if not isinstance(value, int):
        return None
    if value <= 0:
        return None

    # Anything past ~2001 in milliseconds exceeds 1e12; seconds-based values are ~1.7e9.
    seconds = value / 1000 if value > 1e11 else value
    try:
        return datetime.fromtimestamp(seconds).isoformat(sep=" ", timespec="seconds")
    except (OSError, OverflowError, ValueError):
        return None


def status_from_age(seen_age_hours: float | None, threshold_hours: float = 24.0) -> str:
    """Derive the platform's STATUS from last-contact age.

    The API returns no status field, but the rule is known and confirmed: Online within 24
    hours, Offline beyond it, Inactive when the device has never pinged. Deriving it keeps API
    snapshots directly comparable with spreadsheet ones.
    """
    if seen_age_hours is None:
        return "Inactive"
    return "Online" if seen_age_hours <= threshold_hours else "Offline"


def parse_queue(value: object) -> tuple[int | None, str]:
    """Interpret the QUEUE column into (pending_count, state).

    Confirmed by the platform owner (2026-08-15):

        '-'          no OTA task has ever been assigned to this device
        0            tasks were assigned and completed; nothing pending
        1 or more    that many tasks are still pending

    The distinction between '-' and 0 carries real meaning — never targeted versus targeted
    and finished — so it must never be flattened into a single "no pending work" bucket.
    Returns (None, 'never_tasked') for '-', since 0 is a genuine value here.

    The API expresses the same three states through `type_Task`: the key is absent when no task
    was ever assigned, `{}` when tasks are done, and `{"1": 1400}` when one is outstanding. The
    counts line up with the spreadsheet almost exactly, so both sources map to one vocabulary.
    """
    if isinstance(value, dict):
        return (len(value), "pending") if value else (0, "completed")

    text = clean(value)
    if text is None:
        return None, "never_tasked"
    try:
        count = int(float(text))
    except ValueError:
        return None, "unknown"
    if count <= 0:
        return 0, "completed"
    return count, "pending"


def row_count_from_filename(filename: str) -> int:
    """The device count the platform puts in its export names: Devices_35477_15Aug26_1511.xlsx.

    Used only to give a progress bar a denominator while the file is being read — the real count
    is whatever the file turns out to hold. Returns 0 when the name does not carry one, which
    leaves the bar counting up without a target rather than showing a wrong one.
    """
    match = re.search(r"_(\d{2,7})_\d{1,2}[A-Za-z]{3}\d{2}_", filename)
    return int(match.group(1)) if match else 0


def snapshot_at_from_filename(filename: str) -> datetime | None:
    """Recover the snapshot timestamp from names like Devices_35477_15Aug26_1511.xlsx.

    The export carries no timestamp column, so this is the only trustworthy source of when the
    snapshot was taken — and every trend in the system depends on it being right.
    """
    match = _FILENAME_RE.search(filename)
    if not match:
        return None
    mon = _MONTHS.get(match.group("mon").lower())
    if mon is None:
        return None
    year = int(match.group("year"))
    if year < 100:
        year += 2000
    try:
        return datetime(year, mon, int(match.group("day")), int(match.group("hh")), int(match.group("mm")))
    except ValueError:
        return None


def hours_between(later_iso: str, earlier_iso: str | None) -> float | None:
    """Age in hours between two ISO timestamps. None when the earlier one is missing."""
    if not earlier_iso:
        return None
    try:
        later = datetime.fromisoformat(later_iso)
        earlier = datetime.fromisoformat(earlier_iso)
    except ValueError:
        return None
    return round((later - earlier).total_seconds() / 3600.0, 2)
