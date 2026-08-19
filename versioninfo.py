"""Stamp both executables with a Windows version resource."""
from pathlib import Path


def patch(path, old, new, label):
    p = Path(path)
    s = p.read_text(encoding="utf-8")
    assert old in s, f"anchor missing in {path}: {label}"
    p.write_text(s.replace(old, new, 1), encoding="utf-8")
    print("patched", label)


patch("build.py",
      'def check_pyinstaller() -> None:',
      '''# Windows reads the version out of a resource compiled into the .exe, not out of its name.
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
        (NAME, "InTouch OTA Analytics — dashboard and CLI"),
        (f"{NAME}-silent", "InTouch OTA Analytics — no console window"),
    ):
        path = ROOT / f"version_{internal}.txt"
        path.write_text(VERSION_RESOURCE.format(
            parts=parts, version=number, description=description,
            internal=internal, filename=f"{internal}.exe"), encoding="utf-8")
        written[internal] = path
    return written


def check_pyinstaller() -> None:''', "version resource generator")

patch("build.py",
      '''    print(f"Building {NAME} v{version()} with {sys.executable}\\n")''',
      '''    resources = write_version_resources()
    print(f"Building {NAME} v{version()} with {sys.executable}")
    print(f"  stamping {len(resources)} version resource(s) with {version()}\\n")''',
      "generate before build")

patch("build.py",
      '''    # Only now the zip is written, so the handover file cannot carry a database either.
    restore_data(parked)''',
      '''    for path in resources.values():
        path.unlink(missing_ok=True)      # generated per build; nothing to keep in the tree

    # Only now the zip is written, so the handover file cannot carry a database either.
    restore_data(parked)''', "clean up resources")

# ── the spec uses them when they exist ──────────────────────────────────────
patch("InTouchOTA-Analytics.spec",
      'from PyInstaller.utils.hooks import collect_submodules\n\nNAME = "InTouchOTA-Analytics"',
      '''import os

from PyInstaller.utils.hooks import collect_submodules

NAME = "InTouchOTA-Analytics"


def version_resource(internal):
    """The version resource build.py generated for this executable, if it ran.

    Optional so the spec still builds when PyInstaller is invoked directly — it produces an
    unstamped exe rather than failing, and build.py is the documented entry point.
    """
    path = f"version_{internal}.txt"
    return path if os.path.exists(path) else None''', "spec helper")

patch("InTouchOTA-Analytics.spec",
      '''    name=NAME,
    console=True,
    debug=False,
    strip=False,
    upx=False,               # UPX-packed binaries trip antivirus far more often than they save
)''',
      '''    name=NAME,
    console=True,
    debug=False,
    strip=False,
    upx=False,               # UPX-packed binaries trip antivirus far more often than they save
    version=version_resource(NAME),
)''', "console version")

patch("InTouchOTA-Analytics.spec",
      '''    name=f"{NAME}-silent",
    console=False,
    debug=False,
    strip=False,
    upx=False,
)''',
      '''    name=f"{NAME}-silent",
    console=False,
    debug=False,
    strip=False,
    upx=False,
    version=version_resource(f"{NAME}-silent"),
)''', "silent version")
