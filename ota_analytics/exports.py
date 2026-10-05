"""Downloadable views of whatever is on screen.

Three formats, because they serve different jobs:

    csv   opens anywhere, good for sharing and for pivoting
    xlsx  keeps column widths and a frozen header for someone reading it directly
    txt   just the IMEIs, one per line — the format you paste back into the OTA platform
          to act on a cohort

Whatever is exported matches the filters in force on the page. An export that quietly returned
something else would be worse than none, because the action taken from it would be wrong.
"""

from __future__ import annotations

import csv
import io
import re
from datetime import datetime

# Characters a .xlsx file may not contain. openpyxl refuses the whole workbook rather than
# writing them, so one bad cell failed the entire download with a 500 while CSV was unaffected —
# which is why the spreadsheet option looked broken and the others did not.
#
# This is real data, not a hypothetical: the platform export carries 128 devices whose ICCID has
# an embedded backspace followed by stray bytes ('8991922406995209166F\x08\x08Áá'), and one
# device whose CONFIGURATION has the same shape. Tab, newline and carriage return are legal and
# deliberately left alone. The database keeps the original; only the file being written is
# cleaned, and quality.py reports the devices so the corruption is visible rather than papered
# over.
ILLEGAL_IN_XLSX = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")

# (key in the row dict, column header)
DEVICE_COLUMNS = [
    ("imei", "IMEI"),
    ("device_model", "Model"),
    ("firmware", "Firmware"),
    ("fallback_tag", "Fallback"),
    ("prev_firmware", "Previous firmware"),
    ("update_firmware", "Target firmware"),
    ("base_firmware", "Base firmware"),
    ("configuration", "Configuration"),
    ("hw_ver", "Hardware"),
    ("status", "Status"),
    ("queue_state", "Task state"),
    ("queue", "Pending tasks"),
    ("seen_at", "Last seen"),
    ("seen_age_hours", "Hours since seen"),
    ("last_fw_change_at", "Last firmware change"),
    ("last_checked_at", "Last checked"),
    ("groups_raw", "Groups"),
    ("vin", "VIN"),
    ("iccid", "ICCID"),
]

CHANGE_COLUMNS = [
    ("changed_at", "Changed at"),
    ("imei", "IMEI"),
    ("device_model", "Model"),
    ("from_firmware", "From"),
    ("to_firmware", "To"),
    ("direction", "Direction"),
    ("verdict", "Verdict"),
    # Beside the verdict, because "fallback to base" reads very differently when it is the
    # device's sixth time than its first, and the file is often read away from the dashboard.
    ("fallback_times", "Fallbacks (times)"),
    ("update_firmware", "Target"),
    ("base_firmware", "Base"),
    ("hw_ver", "Hardware"),
    ("status", "Status"),
    ("queue_state", "Task state"),
    ("groups_raw", "Groups"),
]


def timestamped(stem: str, suffix: str) -> str:
    return f"{stem}_{datetime.now().strftime('%d%b%y_%H%M')}.{suffix}"


def describe(filters: dict) -> str:
    """A short slug describing the filters, so a downloaded file says what it holds."""
    parts = [str(value).replace(" ", "-") for value in filters.values() if value]
    return "_".join(parts)[:60]


def to_csv(rows: list[dict], columns) -> str:
    _label_status(rows)
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow([header for _, header in columns])
    for row in rows:
        writer.writerow([_clean(row.get(key)) for key, _ in columns])
    return buffer.getvalue()


def to_imei_list(rows: list[dict]) -> str:
    """One IMEI per line — ready to paste into the platform's device selector."""
    return "\n".join(str(row["imei"]) for row in rows if row.get("imei")) + "\n"


# Order matters: this is read top to bottom by someone comparing two files.
PROVENANCE_FIELDS = [
    ("instance_label", "Produced by"),
    ("digest_short", "Fleet digest"),
    ("snapshots", "Snapshots held"),
    ("first_snapshot_at", "Coverage from"),
    ("last_snapshot_at", "Coverage to"),
    ("last_ingest_at", "Last fetched from platform"),
    ("db_id", "Database id"),
    ("created_at", "Database created"),
    ("fleet_digest", "Fleet digest (full)"),
]


def _bold():
    from openpyxl.styles import Font
    return Font(bold=True)


def _write_provenance(workbook, provenance: dict) -> None:
    """Record which dataset produced the file, on its own sheet.

    On a sheet rather than above the data because a report is opened, filtered and pivoted —
    header rows in the way break every one of those. Two people holding the same numbers can
    still be looking at different snapshot sets, and once a file leaves the dashboard nothing
    else says which one it came from.
    """
    from openpyxl.cell import WriteOnlyCell

    sheet = workbook.create_sheet("Source")
    sheet.column_dimensions["A"].width = 30
    sheet.column_dimensions["B"].width = 46

    heading = [WriteOnlyCell(sheet, value=v) for v in ("Field", "Value")]
    for cell in heading:
        cell.font = _bold()
    sheet.append(heading)

    for key, label in PROVENANCE_FIELDS:
        value = provenance.get(key)
        sheet.append([label, "" if value is None else str(value)])
    sheet.append(["Report generated", datetime.now().strftime("%d %b %Y %H:%M")])
    # Every row is two cells wide, including the blank one and the closing note. Ordinary mode
    # padded short rows out to the width of the sheet; write-only writes exactly what it is
    # given, so a one-cell row comes back as a one-element tuple and anything reading the sheet
    # column-wise trips over it.
    sheet.append(["", ""])
    sheet.append(["Two reports agree only if the fleet digest above matches. It is a "
                  "fingerprint of the exports loaded, not of the file.", ""])


def to_xlsx(rows: list[dict], columns, sheet_name: str = "Devices",
            provenance: dict | None = None) -> bytes:
    """The rows as a spreadsheet, written a row at a time rather than assembled in memory.

    `write_only=True` is the whole difference. The ordinary mode keeps a `Cell` object for every
    value until the workbook is saved: a full device export is 35,848 rows across 19 columns, so
    that is ~680,000 objects, and it measured at **190 MB of Python objects to produce a 3.1 MB
    file** — on top of the row dicts, in a process that may be ingesting at the same time. In
    write-only mode each row is serialized and released as it is appended.

    The cost is that nothing can be revisited after it is written: no `sheet["A"]`, no
    `sheet.dimensions`. Anything that used to be applied by going back over the finished sheet
    is set up front instead — column widths and the freeze before the first row, the IMEI text
    format on the cell as it is created, the filter range computed from the counts.
    """
    from openpyxl import Workbook
    from openpyxl.cell import WriteOnlyCell
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    _label_status(rows)
    workbook = Workbook(write_only=True)
    sheet = workbook.create_sheet(sheet_name[:31])

    for index, (_, header) in enumerate(columns, start=1):
        width = max(len(header) + 2, 12)
        if header in {"Groups", "Last seen", "Last firmware change", "Last checked"}:
            width = 22
        sheet.column_dimensions[get_column_letter(index)].width = width

    sheet.freeze_panes = "A2"
    # Computed rather than read back off the sheet: `sheet.dimensions` is not available until
    # the workbook is saved, and by then it is too late to set the filter.
    sheet.auto_filter.ref = f"A1:{get_column_letter(len(columns))}{len(rows) + 1}"

    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill("solid", fgColor="2A313B")
    heading = []
    for _, header in columns:
        cell = WriteOnlyCell(sheet, value=header)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(vertical="center")
        heading.append(cell)
    sheet.append(heading)

    for row in rows:
        values = [_clean(row.get(key)) for key, _ in columns]
        # IMEIs are identifiers, not numbers: Excel would render 865510082004294 in scientific
        # notation and a copy-paste back into the platform would then be wrong. Applied as the
        # cell is created, because in write-only mode there is no going back for column A.
        first = WriteOnlyCell(sheet, value=values[0])
        first.number_format = "@"
        sheet.append([first, *values[1:]])

    if provenance:
        _write_provenance(workbook, provenance)

    stream = io.BytesIO()
    workbook.save(stream)
    return stream.getvalue()


def _label_status(rows: list[dict]) -> None:
    """Rewrite the status column to the name used on screen.

    A download that says "Inactive" where the dashboard says "Activation-Pending" makes two
    sources of truth out of one number. The stored value is untouched; only the file changes.
    """
    from . import normalize

    for row in rows:
        if "status" in row:
            row["status"] = normalize.status_label(row["status"])


def _clean(value):
    """Excel and CSV both prefer plain scalars; round the one float we carry.

    Control characters are stripped for both formats, not just for Excel: they are never
    meaningful in an identifier, and a CSV carrying a raw backspace is just as broken — it simply
    fails later, in whatever opens it, instead of here.
    """
    if value is None:
        return ""
    if isinstance(value, float):
        return round(value, 1)
    if isinstance(value, str):
        return ILLEGAL_IN_XLSX.sub("", value)
    return value
