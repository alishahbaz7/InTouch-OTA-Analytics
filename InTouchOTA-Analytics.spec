# PyInstaller build. Run it with:  python build.py
#
# One executable, with a console: interactive use and the CLI, and it prints its log.
#
# There used to be a second, windowless twin (InTouchOTA-Analytics-silent.exe) for auto-start to
# run after a reboot. Auto-start is withdrawn (startup.AUTO_START_AVAILABLE = False), so nothing
# launched it any more — and run by hand it showed nothing at all, which reads as a program that
# failed to start. It was dropped from the build in 1.9.1. Bringing auto-start back means
# bringing a windowless build back with it; startup.launch_command() still prefers one if present.
#
# One-folder rather than one-file on purpose: one-file unpacks itself to a temp directory on
# every launch, which costs seconds each time and is what antivirus flags hardest on an
# unsigned binary.

import os

from PyInstaller.utils.hooks import collect_submodules

NAME = "InTouchOTA-Analytics"


def version_resource(internal):
    """The version resource build.py generated for this executable, if it ran.

    Optional so the spec still builds when PyInstaller is invoked directly — it produces an
    unstamped exe rather than failing, and build.py is the documented entry point.
    """
    path = f"version_{internal}.txt"
    return path if os.path.exists(path) else None

# Files the program reads at runtime. They are not modules, so nothing imports them and
# PyInstaller cannot find them by analysis — config.resource() locates them inside the bundle.
DATAS = [
    ("ota_analytics/schema.sql", "ota_analytics"),
    ("ota_analytics/web/templates", "ota_analytics/web/templates"),
    ("ota_analytics/web/static", "ota_analytics/web/static"),
]

# Imported by name at runtime, so static analysis cannot see them. Left out, the build succeeds
# and then fails when it is run: uvicorn cannot start a server, and keyring reports that no
# credential store exists — which would send the platform password to a file instead.
HIDDEN = [
    "uvicorn.logging", "uvicorn.loops.auto", "uvicorn.loops.asyncio",
    "uvicorn.protocols.http.auto", "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets.auto", "uvicorn.protocols.websockets.wsproto_impl",
    "uvicorn.lifespan.on", "uvicorn.lifespan.off",
    "keyring.backends.Windows", "keyring.backends.chainer", "keyring.backends.fail",
    "win32ctypes.core",
    "anyio._backends._asyncio",
]
HIDDEN += collect_submodules("ota_analytics")

analysis = Analysis(
    ["main.py"],
    pathex=["."],
    binaries=[],
    datas=DATAS,
    hiddenimports=HIDDEN,
    hookspath=[],
    runtime_hooks=[],
    # Pulled in by openpyxl/httpx probing for optional extras; none of them are used, and each
    # adds tens of megabytes.
    excludes=["tkinter", "matplotlib", "numpy", "pandas", "PIL", "pytest", "PyQt5", "PySide2"],
    noarchive=False,
)
pyz = PYZ(analysis.pure)

console_exe = EXE(
    pyz, analysis.scripts, [],
    exclude_binaries=True,
    name=NAME,
    console=True,
    debug=False,
    strip=False,
    upx=False,               # UPX-packed binaries trip antivirus far more often than they save
    version=version_resource(NAME),
)

COLLECT(
    console_exe,
    analysis.binaries, analysis.datas,
    strip=False, upx=False,
    name=NAME,
)
