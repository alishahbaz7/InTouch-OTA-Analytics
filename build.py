"""Build the distributable application.

    .\\.venv\\Scripts\\python.exe build.py

Produces `dist\\InTouchOTA-Analytics\\` and zips it for handing over. The zip is what a
colleague receives: they unpack it anywhere, run the .exe, and the database is created in a
`data` folder beside it — so the whole install is one folder that can be copied or backed up.

Deliberately not a one-file build. One-file unpacks itself to a temp directory on every launch,
which costs several seconds each time, and a single large unsigned executable is what antivirus
quarantines most readily.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
NAME = "InTouchOTA-Analytics"
SPEC = ROOT / f"{NAME}.spec"
DIST = ROOT / "dist" / NAME


def version() -> str:
    sys.path.insert(0, str(ROOT))
    from ota_analytics import __version__
    return __version__


def check_pyinstaller() -> None:
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("PyInstaller is not installed in this interpreter.\n", file=sys.stderr)
        print(f'  & "{sys.executable}" -m pip install pyinstaller\n', file=sys.stderr)
        raise SystemExit(1)


def clear(folder: Path) -> None:
    """Remove a previous build, refusing rather than half-removing it.

    `ignore_errors=True` is the wrong choice here and cost a real debugging session: a copy of
    the app running out of `dist\\` holds its own files open, so the delete removed some of them
    and skipped the rest. PyInstaller then rebuilt over the wreckage, and the app started but
    failed on the first page whose template had been deleted — a TemplateNotFound from a folder
    that plainly existed.

    A build that cannot start clean must say so instead.
    """
    if not folder.exists():
        return
    try:
        shutil.rmtree(folder)
    except (PermissionError, OSError) as exc:
        locked = getattr(exc, "filename", "") or folder
        print(f"Cannot clear {folder}\n", file=sys.stderr)
        print(f"  something is holding {locked}\n", file=sys.stderr)
        print("A copy of the app is almost certainly still running. Close it — or:\n",
              file=sys.stderr)
        print('  Get-Process -Name "InTouchOTA*" | Stop-Process -Force\n', file=sys.stderr)
        raise SystemExit(1)


def build() -> None:
    check_pyinstaller()

    # Anyone testing from dist\ has a database in there, and it is about to be deleted.
    stale_db = DIST / "data"
    if stale_db.exists():
        print(f"Note: removing the test database in {stale_db}")
        print("      (a real install keeps its data beside its own copy of the .exe)\n")

    for stale in (ROOT / "build", ROOT / "dist"):
        clear(stale)

    print(f"Building {NAME} v{version()} with {sys.executable}\n")
    result = subprocess.run(
        [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", str(SPEC)],
        cwd=str(ROOT))
    if result.returncode != 0:
        raise SystemExit(result.returncode)

    console = DIST / f"{NAME}.exe"
    silent = DIST / f"{NAME}-silent.exe"
    for required in (console, silent):
        if not required.exists():
            print(f"Build finished but {required.name} is missing.", file=sys.stderr)
            raise SystemExit(1)

    # A README next to the exe, because the person who receives the zip has no other
    # instructions and the data-folder behaviour is the thing they need to know.
    (DIST / "READ ME FIRST.txt").write_text(
        f"InTouch OTA Analytics v{version()}\n"
        f"{'=' * 40}\n\n"
        f"Run {NAME}.exe to start the dashboard. It opens at http://127.0.0.1:8000\n\n"
        "YOUR DATA LIVES IN THE 'data' FOLDER NEXT TO THIS FILE.\n"
        "Keep the whole folder together. Copy it to move or back up your history;\n"
        "delete it and you start from an empty database.\n\n"
        "Drop platform exports (.xlsx) into the 'Sample data' folder and they load\n"
        "on the next start, or use the Update Data page in the dashboard.\n\n"
        "Auto-fetch keeps the data current on a schedule while the app is open — set it\n"
        "on the Update Data page. Starting the app twice is safe: it opens the copy that\n"
        "is already running rather than starting another.\n\n"
        "Command line (same program):\n"
        f"  {NAME}.exe db-info                 who this install is and what it holds\n"
        f"  {NAME}.exe db-export --out share.otabundle    hand your history to a colleague\n"
        f"  {NAME}.exe db-import share.otabundle          merge theirs into yours\n"
        f"  {NAME}.exe passwd --role admin     set a dashboard password\n"
        f"  {NAME}.exe --help                  everything else\n\n"
        f"{NAME}-silent.exe is the same program with no console window. Running it\n"
        "shows nothing at all, which looks like it failed — use the one above instead.\n"
        "It writes to data\\app.log.\n",
        encoding="utf-8")

    archive = ROOT / "dist" / f"{NAME}-v{version()}-win64.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as bundle:
        for path in sorted(DIST.rglob("*")):
            if path.is_file():
                bundle.write(path, Path(NAME) / path.relative_to(DIST))

    size = sum(f.stat().st_size for f in DIST.rglob("*") if f.is_file())
    print(f"\n  folder  {DIST}  ({size / 1024 / 1024:.0f} MB)")
    print(f"  zip     {archive}  ({archive.stat().st_size / 1024 / 1024:.0f} MB)")
    print("\nHand over the zip. It unpacks to one folder that holds the program and its data.")


if __name__ == "__main__":
    build()
