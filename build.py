"""Build the distributable application.

    .\\.venv\\Scripts\\python.exe build.py

Produces `dist\\InTouchOTA-Analytics-v<version>\\InTouchOTA-Analytics-v<version>.exe` and zips the
folder as `InTouchOTA-Analytics-v<version>.zip`, which unpacks to a folder of the same name. The
zip is what a colleague receives: they unpack it anywhere, run the .exe, and the database is
created in a `data` folder beside it — so the whole install is one folder that can be copied or
backed up.

A release is named `InTouchOTA-Analytics-v2.1.1` — the version and nothing else, no platform
suffix — and the zip, the folder and the program all carry that one name (the user, 12-10-2026:
"use the same name as the zip for the folder and the utility"). It carries one executable. That
naming is the user's rule for every release, in this project and their others; see the `ship`
skill.

The working folder in `dist\\` is renamed with it, and this install keeps its database inside
that folder — so a build carries the user's files from the previous release's folder into the new
one (`restore_data`), rather than leaving the history behind under the old name.

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


def version() -> str:
    sys.path.insert(0, str(ROOT))
    from ota_analytics import __version__
    return __version__


def release_name() -> str:
    """What a release is called — the zip, the folder, and the program: `InTouchOTA-Analytics-v2.1.1`.

    One function, so the zip, the folder inside it and the .exe cannot disagree about the version.
    The spec derives the same name from the same __version__.
    """
    return f"{NAME}-v{version()}"


# The program folder this build writes in dist\ — named for the release, like everything else.
DIST = ROOT / "dist" / release_name()


def is_app_folder(entry: Path) -> bool:
    r"""A program folder in dist\: this release's, an earlier one's, or the unversioned one used
    before 2.1.1 — each holds the program (the build's) and possibly the user's data (theirs)."""
    return entry.is_dir() and (entry.name == NAME or entry.name.startswith(f"{NAME}-v"))


# Windows reads the version out of a resource compiled into the .exe, not out of its name.
# Generated here from __version__ rather than kept as a file of its own, so the number on the
# file cannot drift from the number in the code — a version that lags is worse than none, because
# a bug report then points at the wrong build.
#
# The executable is named for the release too (the user, 12-10-2026), so Task Manager and the
# taskbar say which version is running; a shortcut to it is re-pointed at each upgrade.
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
    """The executable's version resource, which Explorer and Task Manager report."""
    number = version()
    parts = ", ".join((number.split(".") + ["0", "0", "0", "0"])[:4])

    written = {}
    for internal, description in (
        (NAME, "InTouch OTA Analytics - dashboard and CLI"),
    ):
        path = ROOT / f"version_{internal}.txt"
        path.write_text(VERSION_RESOURCE.format(
            parts=parts, version=number, description=description,
            internal=internal, filename=f"{release_name()}.exe"), encoding="utf-8")
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
#
# Every executable this build or an earlier one named — `InTouchOTA-Analytics.exe` before 2.1.1,
# `InTouchOTA-Analytics-v<version>.exe` since, and the `-silent.exe` twin dropped in 1.9.1 — is
# the build's. Unlisted, an old one would be "rescued" as the user's file and put back beside
# every new build for ever.
BUILD_OUTPUTS = {"_internal", "READ ME FIRST.txt"}


def is_build_output(inner: Path) -> bool:
    return inner.name in BUILD_OUTPUTS or (inner.suffix.lower() == ".exe"
                                           and inner.name.startswith(NAME))


def user_files() -> list[Path]:
    """Everything in dist that this build did not create."""
    root = ROOT / "dist"
    if not root.exists():
        return []

    theirs: list[Path] = []
    for entry in sorted(root.iterdir()):
        if is_app_folder(entry):
            theirs.extend(inner for inner in sorted(entry.iterdir()) if not is_build_output(inner))
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
    # Their files from a program folder move into the new release's folder (restore_data). With
    # two program folders holding files there is no telling which database is the live one, and
    # guessing could put the wrong history in front of them — so refuse and say which.
    holders = sorted({item.parent.name for item in theirs
                      if item.parent != ROOT / "dist" and is_app_folder(item.parent)})
    if len(holders) > 1:
        raise SystemExit(
            "More than one program folder in dist holds your files:\n    "
            + "\n    ".join(f"dist\\{name}" for name in holders)
            + "\nKeep the one you use (its 'data' folder is your history) and move the others out "
              "of dist, then build again.")
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
    """Put back everything rescue_data moved — files from a program folder into this release's
    folder (the data moves with the program), everything else at the same relative path."""
    if not parked:
        return
    for relative in parked:
        source = PRESERVE / relative
        if not source.exists():
            continue
        if len(relative.parts) > 1 and (relative.parts[0] == NAME
                                        or relative.parts[0].startswith(f"{NAME}-v")):
            relative = Path(DIST.name, *relative.parts[1:])
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

    console = DIST / f"{release_name()}.exe"
    for required in (console,):
        if not required.exists():
            print(f"Build finished but {required.name} is missing.", file=sys.stderr)
            restore_data(parked)
            raise SystemExit(1)

    # A README next to the exe, because the person who receives the zip has no other
    # instructions and the data-folder behaviour is the thing they need to know.
    exe = f"{release_name()}.exe"
    (DIST / "READ ME FIRST.txt").write_text(
        f"InTouch OTA Analytics v{version()}\n"
        f"{'=' * 40}\n\n"
        f"Run {exe} to start the dashboard. It opens at http://127.0.0.1:8000\n\n"
        "YOUR DATA LIVES IN THE 'data' FOLDER NEXT TO THIS FILE.\n"
        "Keep the whole folder together. Copy it to move or back up your history;\n"
        "delete it and you start from an empty database.\n\n"
        "Drop platform exports (.xlsx) into the 'Sample data' folder and they load\n"
        "on the next start, or use the Update Data page in the dashboard.\n\n"
        "Auto-fetch keeps the data current on a schedule while the app is open — set it\n"
        "on the Update Data page. Starting the app twice is safe: it opens the copy that\n"
        "is already running rather than starting another.\n\n"
        "Command line (same program):\n"
        f"  {exe} db-info                 who this install is and what it holds\n"
        f"  {exe} db-export --out share.otabundle    hand your history to a colleague\n"
        f"  {exe} db-import share.otabundle          merge theirs into yours\n"
        f"  {exe} passwd --role admin     set a dashboard password\n"
        f"  {exe} --help                  everything else\n\n"
        "UPGRADING FROM AN EARLIER VERSION\n"
        "Each release unpacks to its own folder, named for the version, and its program\n"
        "carries the same name. Close the old copy, then move its 'data' folder into this\n"
        "one before the first start — that folder is your whole history. The database is\n"
        "upgraded on first start. A shortcut to the old program needs pointing at this one.\n",
        encoding="utf-8")

    archive = ROOT / "dist" / f"{release_name()}.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as bundle:
        for path in sorted(DIST.rglob("*")):
            if path.is_file():
                bundle.write(path, Path(release_name()) / path.relative_to(DIST))

    for path in resources.values():
        path.unlink(missing_ok=True)      # generated per build; nothing kept in the tree

    # Only now the zip is written, so the handover file cannot carry a database with it.
    restore_data(parked)

    size = sum(f.stat().st_size for f in DIST.rglob("*") if f.is_file())
    print(f"\n  folder  {DIST}  ({size / 1024 / 1024:.0f} MB)")
    print(f"  zip     {archive}  ({archive.stat().st_size / 1024 / 1024:.0f} MB)")
    print(f"\nHand over the zip. It unpacks to {release_name()}\\ — one folder that holds the "
          "program and its data.")


if __name__ == "__main__":
    build()
