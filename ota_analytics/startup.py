"""Start with Windows, after a delay.

Unattended fetching that does not survive a reboot is not unattended: a Windows update at 3am
ends collection silently, and the gap is only noticed days later when a trend looks wrong.

**Why the Startup folder and not Task Scheduler.** Task Scheduler is the better mechanism — it
enforces the delay itself and can wait for a network — but creating a task is refused without
elevation on a managed machine (verified here: `schtasks /Create` returns "Access is denied").
A shortcut in the per-user Startup folder needs no admin rights and no policy exception, so it
is what actually works.

**Why the delay.** Logon is the busiest moment a machine has: antivirus, sync clients and
domain policy all run at once. Fetching 35,000 devices into it competes for the same disk and
network. Waiting lets the machine settle first, and nothing here is time-critical to the minute.

The wait is done by a tiny wscript process rather than by Python sleeping, because a suspended
`wscript.exe` costs about a megabyte where an idle interpreter costs fifteen.
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ENTRY_NAME = "InTouch OTA Analytics.vbs"
DEFAULT_DELAY_MINUTES = 30
MIN_DELAY, MAX_DELAY = 0, 240


def startup_dir() -> Path:
    """The per-user Startup folder. No admin rights are needed to write here."""
    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata) / "Microsoft/Windows/Start Menu/Programs/Startup"
    return Path.home() / "AppData/Roaming/Microsoft/Windows/Start Menu/Programs/Startup"


def entry_path() -> Path:
    return startup_dir() / ENTRY_NAME


def _launcher() -> tuple[Path, Path]:
    """(interpreter, script). pythonw runs without a console window."""
    root = Path(__file__).resolve().parent.parent
    windowless = Path(sys.executable).with_name("pythonw.exe")
    return (windowless if windowless.exists() else Path(sys.executable)), root / "main.py"


def is_supported() -> bool:
    return sys.platform == "win32"


def clamp_delay(minutes) -> int:
    try:
        minutes = int(minutes)
    except (TypeError, ValueError):
        return DEFAULT_DELAY_MINUTES
    return max(MIN_DELAY, min(MAX_DELAY, minutes))


def _script(delay_minutes: int) -> str:
    interpreter, script = _launcher()
    # Quotes are doubled for VBScript string literals; both paths contain spaces on this machine.
    command = f'""{interpreter}"" ""{script}"" --no-browser'
    return (
        "' Created by InTouch OTA Analytics. Delete this file, or use the toggle on the\n"
        "' Update Data page, to stop the dashboard starting with Windows.\n"
        f"WScript.Sleep {delay_minutes * 60 * 1000}\n"
        f'CreateObject("WScript.Shell").Run "{command}", 0, False\n'
    )


TASK_NAME = "InTouch OTA Analytics"


def _run(args: list[str]) -> tuple[int, str]:
    """Run a console tool quietly. Returns (exit code, combined output)."""
    try:
        done = subprocess.run(args, capture_output=True, text=True, timeout=30,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return done.returncode, (done.stdout or "") + (done.stderr or "")
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, str(exc)


def task_exists() -> bool:
    return is_supported() and _run(["schtasks", "/Query", "/TN", TASK_NAME])[0] == 0


def create_task(delay_minutes: int) -> bool:
    """Register a scheduled task, if this machine allows it.

    Preferred over the Startup folder when available: it runs at **boot** rather than at
    sign-in, so a machine that reboots and sits at the lock screen still fetches, and Windows
    enforces the delay itself. Managed machines often refuse this without elevation — hence the
    fallback rather than an error.
    """
    if not is_supported():
        return False
    interpreter, script = _launcher()
    command = f'"{interpreter}" "{script}" --no-browser'
    delay = f"{min(delay_minutes, 9999):04d}:00"
    code, _ = _run(["schtasks", "/Create", "/TN", TASK_NAME, "/TR", command,
                    "/SC", "ONSTART", "/DELAY", delay, "/F"])
    if code != 0:
        # Not every Windows build accepts /DELAY with ONSTART; fall back to sign-in, which is
        # still better than nothing and still honours the delay.
        code, _ = _run(["schtasks", "/Create", "/TN", TASK_NAME, "/TR", command,
                        "/SC", "ONLOGON", "/DELAY", delay, "/F"])
    return code == 0


def delete_task() -> None:
    if is_supported():
        _run(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"])


@dataclass
class StartupState:
    supported: bool
    enabled: bool
    delay_minutes: int = DEFAULT_DELAY_MINUTES
    path: str = ""
    detail: str = ""
    mechanism: str = ""          # scheduled-task | startup-folder | none
    starts_at_boot: bool = False
    warning: str = ""


def _environment_warning() -> str:
    """Auth is configured by environment variable; a logon-launched process may not see it.

    Worth saying out loud: the dashboard would come back up unauthenticated, which is the
    opposite of what someone who set a password expects.
    """
    from . import auth

    if not auth.is_configured():
        return ""
    if os.environ.get("OTA_STARTUP_ENV_PERSISTED"):
        return ""
    return ("A dashboard password is set through environment variables. Unless those are set "
            "at Windows user level (setx), the auto-started copy will come up with no "
            "password. See docs/DEPLOY.md.")


def status() -> StartupState:
    if not is_supported():
        return StartupState(supported=False, enabled=False, mechanism="none",
                            detail="Only available on Windows.")

    if task_exists():
        delay = DEFAULT_DELAY_MINUTES
        code, out = _run(["schtasks", "/Query", "/TN", TASK_NAME, "/FO", "LIST", "/V"])
        for line in out.splitlines():
            if "Delay" in line and ":" in line:
                digits = "".join(c for c in line.split(":")[-2][-4:] if c.isdigit())
                if digits:
                    delay = int(digits)
                break
        return StartupState(
            supported=True, enabled=True, delay_minutes=delay, mechanism="scheduled-task",
            starts_at_boot=True, path=TASK_NAME, warning=_environment_warning(),
            detail=f"Starts with Windows via a scheduled task, {delay} minutes after boot.")

    path = entry_path()
    if not path.exists():
        return StartupState(supported=True, enabled=False, path=str(path), mechanism="none",
                            detail="The dashboard does not start with Windows.")

    delay = DEFAULT_DELAY_MINUTES
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("WScript.Sleep"):
                delay = int(line.split()[1]) // 60000
                break
    except (OSError, ValueError, IndexError):
        pass

    return StartupState(
        supported=True, enabled=True, delay_minutes=delay, path=str(path),
        mechanism="startup-folder", starts_at_boot=False, warning=_environment_warning(),
        detail=(f"Starts {delay} minutes after you sign in to Windows."
                if delay else "Starts as soon as you sign in to Windows."))


def enable(delay_minutes: int = DEFAULT_DELAY_MINUTES) -> StartupState:
    """Turn auto-start on by the best mechanism this machine permits."""
    if not is_supported():
        return status()
    delay = clamp_delay(delay_minutes)

    # A scheduled task runs at boot and does not need anyone signed in, so try it first. On a
    # managed machine it is usually refused, and the Startup folder always works.
    if create_task(delay):
        entry_path().unlink(missing_ok=True)     # never leave both mechanisms armed
        return status()

    folder = startup_dir()
    folder.mkdir(parents=True, exist_ok=True)
    entry_path().write_text(_script(delay), encoding="utf-8")
    return status()


def disable() -> StartupState:
    """Remove every mechanism, not just the one currently reported."""
    if is_supported():
        delete_task()
        entry_path().unlink(missing_ok=True)
    return status()


def run_now() -> bool:
    """Launch exactly what the startup entry launches, without waiting. Used to prove it works."""
    if not is_supported():
        return False
    interpreter, script = _launcher()
    try:
        subprocess.Popen([str(interpreter), str(script), "--no-browser"],
                         creationflags=subprocess.DETACHED_PROCESS
                         | subprocess.CREATE_NEW_PROCESS_GROUP,
                         cwd=str(script.parent), close_fds=True)
        return True
    except OSError:
        return False
