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


# Windows reads the version out of a resource compiled into the .exe, not out of its name.
# Generated here from __version__ rather than kept as a file of its own, so the number on the
# file cannot drift from the number in the code — a version that lags is worse than none, because
# a bug report then points at the wrong build.
#
# The filenames stay unversioned on purpose. startup.launch_command() finds the windowless build
# by exact name, and a name carrying the version would break that lookup at every release unless
# it were derived; the zip already carries the version for handover.
VERSION_RESOURCE = """VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=({parts}),
    prodvers=({parts}),
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable(
        '040904B0',
        [StringStruct('CompanyName', 'MapmyIndia'),
         StringStruct('FileDescription', '{description}'),
         StringStruct('FileVersion', '{version}'),
         StringStruct('InternalName', '{internal}'),
         StringStruct('OriginalFilename', '{filename}'),
         StringStruct('ProductName', 'InTouch OTA Analytics'),
         StringStruct('ProductVersion', '{version}')])
    ]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"""


def write_version_resources() -> dict[str, Path]:
    """One resource per executable, so the two are told apart in Explorer and Task Manager."""
    number = version()
    parts = ", ".join((number.split(".") + ["0", "0", "0", "0"])[:4])

    written = {}
    for internal, description in (
        (NAME, "InTouch OTA Analytics - dashboard and CLI"),
        (f"{NAME}-silent", "InTouch OTA Analytics - no console window"),
    ):
        path = ROOT / f"version_{internal}.txt"
        path.write_text(VERSION_RESOURCE.format(
            parts=parts, version=number, description=description,
            internal=internal, filename=f"{internal}.exe"), encoding="utf-8")
        written[internal] = path
    return written


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


# Where anything of yours found inside dist\ is parked while the build runs. Outside dist,
# because dist itself is deleted.
PRESERVE = ROOT / ".build-preserved-data"

# The only things in dist\ that this build put there. Everything else belongs to whoever is
# using the app, and is moved out of the way rather than deleted.
#
# Listed as what to *delete* rather than what to keep, deliberately. The first version of this
# rescued a hard-coded `data` folder and nothing else, so a database survived a build but the
# bundles and reports sitting beside it did not. With the rule inverted, a kind of file nobody
# thought about is preserved by omission instead of destroyed by it — the same reason auth.py
# denies by default.
BUILD_OUTPUTS = {"_internal", f"{NAME}.exe", f"{NAME}-silent.exe", "READ ME FIRST.txt"}


def user_files() -> list[Path]:
    """Everything in dist that this build did not create."""
    root = ROOT / "dist"
    if not root.exists():
        return []

    theirs: list[Path] = []
    for entry in sorted(root.iterdir()):
        if entry.name == NAME and entry.is_dir():
            theirs.extend(inner for inner in sorted(entry.iterdir())
                          if inner.name not in BUILD_OUTPUTS)
        elif not (entry.name.startswith(f"{NAME}-v") and entry.suffix == ".zip"):
            theirs.append(entry)
    return theirs


def rescue_data() -> list[Path] | None:
    r"""Move anything of the user's out of dist before the build deletes it.

    This is not a nicety. Someone who runs the app from dist\ keeps their database in
    dist\InTouchOTA-Analytics\data, their bundles beside it, and their reports below it — and a
    build deleted the lot without asking, taking weeks of fleet history with it. Printing a
    warning first was not enough: the warning scrolled past and the data went anyway.

    A build rebuilds the *program*. It has no business touching anything else.
    """
    theirs = user_files()
    if not theirs:
        return None
    if PRESERVE.exists():
        raise SystemExit(
            f"{PRESERVE} already exists — an earlier build was interrupted before it could put\n"
            f"your files back. Move that folder somewhere safe by hand before building again.")

    parked: list[Path] = []
    total = 0
    for item in theirs:
        relative = item.relative_to(ROOT / "dist")
        target = PRESERVE / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(item), str(target))
        parked.append(relative)
        total += sum(f.stat().st_size for f in target.rglob("*") if f.is_file()) \
            if target.is_dir() else target.stat().st_size

    print(f"Your files are inside dist ({total / 1024 / 1024:.0f} MB). Moved aside for the")
    print(f"build and put back afterwards — nothing here is deleted:")
    for relative in parked:
        print(f"    {relative}")
    print()
    return parked


def restore_data(parked: list[Path] | None) -> None:
    """Put back everything rescue_data moved, at the same relative path."""
    if not parked:
        return
    for relative in parked:
        source = PRESERVE / relative
        if not source.exists():
            continue
        target = ROOT / "dist" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            # The build produced something with the same name. Theirs wins: it is the only copy.
            shutil.rmtree(target) if target.is_dir() else target.unlink()
        shutil.move(str(source), str(target))
        print(f"  put back  dist\\{relative}")
    shutil.rmtree(PRESERVE, ignore_errors=True)


def build() -> None:
    check_pyinstaller()
    parked = rescue_data()

    try:
        for stale in (ROOT / "build", ROOT / "dist"):
            clear(stale)
    except BaseException:
        restore_data(parked)
        raise

    resources = write_version_resources()
    print(f"Building {NAME} v{version()} with {sys.executable}")
    print(f"  stamping {len(resources)} version resource(s) with {version()}\n")
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
            restore_data(parked)
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

    for path in resources.values():
        path.unlink(missing_ok=True)      # generated per build; nothing kept in the tree

    # Only now the zip is written, so the handover file cannot carry a database with it.
    restore_data(parked)

    size = sum(f.stat().st_size for f in DIST.rglob("*") if f.is_file())
    print(f"\n  folder  {DIST}  ({size / 1024 / 1024:.0f} MB)")
    print(f"  zip     {archive}  ({archive.stat().st_size / 1024 / 1024:.0f} MB)")
    print("\nHand over the zip. It unpacks to one folder that holds the program and its data.")


if __name__ == "__main__":
    build()
