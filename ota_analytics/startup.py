"""Start with Windows, after a delay. **Withdrawn — see AUTO_START_AVAILABLE below.**

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

from . import config

# Withdrawn in 1.2.1. The mechanism worked, but what it produced in practice was a copy of the
# dashboard running with no window, holding the port, that nobody could see or account for:
# launching the app then appeared to do nothing, and two schedulers fetched into one database.
# Single-instance detection (main.already_serving) fixes the symptom, but an invisible process
# that starts itself is the wrong default for a tool someone opens when they want to look at it.
#
# The code below is kept intact and tested. Turning this back on restores the feature — but it
# should come back with the dashboard able to show that the hidden copy exists and stop it.
AUTO_START_AVAILABLE = False

ENTRY_NAME = "InTouch OTA Analytics.vbs"
DEFAULT_DELAY_MINUTES = 30
MIN_DELAY, MAX_DELAY = 0, 240

# The windowless twin of the packaged executable, the same way pythonw.exe is python.exe's.
# Auto-start uses it so a reboot does not leave a console window sitting on the desktop.
SILENT_EXE = "InTouchOTA-Analytics-silent.exe"


def startup_dir() -> Path:
    """The per-user Startup folder. No admin rights are needed to write here."""
    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata) / "Microsoft/Windows/Start Menu/Programs/Startup"
    return Path.home() / "AppData/Roaming/Microsoft/Windows/Start Menu/Programs/Startup"


def entry_path() -> Path:
    return startup_dir() / ENTRY_NAME


def launch_command() -> list[str]:
    """The exact argv that starts the dashboard with no window — packaged or from source.

    Returned as argv rather than as (interpreter, script), because a packaged build has no
    script: the program *is* the executable, and `sys.executable` stops being an interpreter.
    Every mechanism below quotes this one list, so the two cases cannot drift apart and leave
    auto-start registered against a command that does not exist.
    """
    if config.is_frozen():
        exe = Path(sys.executable).resolve()
        silent = exe.with_name(SILENT_EXE)
        return [str(silent if silent.exists() else exe), "--no-browser"]

    windowless = Path(sys.executable).with_name("pythonw.exe")
    interpreter = windowless if windowless.exists() else Path(sys.executable)
    return [str(interpreter), str(config.ROOT / "main.py"), "--no-browser"]


def _quote(parts: list[str]) -> str:
    """Join argv into one command line, quoting every path unconditionally.

    Not only when a space is present: the path is chosen by whoever unpacks the app, and
    `C:\\Program Files\\...` or a folder with an `&` in it would otherwise be split by the
    shell into a command that does not exist. Quoting a path that did not need it costs
    nothing; missing one breaks auto-start silently, on a machine nobody is watching.
    """
    return " ".join(part if part.startswith("-") else f'"{part}"' for part in parts)


def launch_target() -> str:
    """The program auto-start would run. Compared against what is armed, to catch a moved app."""
    return launch_command()[0]


def is_supported() -> bool:
    return sys.platform == "win32"


def clamp_delay(minutes) -> int:
    try:
        minutes = int(minutes)
    except (TypeError, ValueError):
        return DEFAULT_DELAY_MINUTES
    return max(MIN_DELAY, min(MAX_DELAY, minutes))


def _script(delay_minutes: int) -> str:
    # Quotes are doubled because the whole command sits inside a VBScript string literal, and
    # the paths contain spaces both from source ("InTouchOTA analytics") and packaged.
    command = _quote(launch_command()).replace('"', '""')
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
    command = _quote(launch_command())
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


def _first_quoted(text: str) -> str:
    """The first quoted token of a command line — the program it runs."""
    if '"' in text:
        parts = text.split('"')
        return parts[1] if len(parts) > 1 else ""
    return text.strip().split(" ")[0]


def _armed_target(mechanism: str) -> str:
    """Which program the currently-armed mechanism actually runs.

    Read back rather than assumed, because the app is portable: the folder can be moved or
    copied after auto-start was switched on, and the entry keeps pointing at where it used to
    be. Nothing fails loudly when that happens — the machine simply stops collecting.
    """
    try:
        if mechanism == "scheduled-task":
            code, out = _run(["schtasks", "/Query", "/TN", TASK_NAME, "/FO", "LIST", "/V"])
            if code != 0:
                return ""
            for line in out.splitlines():
                if line.strip().lower().startswith("task to run:"):
                    return _first_quoted(line.split(":", 1)[1].strip())
            return ""

        for line in entry_path().read_text(encoding="utf-8").splitlines():
            if ".Run " in line:
                # `.Run "<command>", 0, False`, where <command> has its quotes doubled because
                # it sits inside a VBScript string literal. Peel the literal, then un-double.
                inner = line.split(".Run ", 1)[1].rsplit(", 0,", 1)[0].strip()
                if inner.startswith('"') and inner.endswith('"'):
                    inner = inner[1:-1]
                return _first_quoted(inner.replace('""', '"'))
    except (OSError, IndexError, ValueError):
        return ""
    return ""


def _relocation_warning(mechanism: str) -> str:
    armed = _armed_target(mechanism)
    if not armed:
        return ""
    current = launch_target()
    if os.path.normcase(os.path.abspath(armed)) == os.path.normcase(os.path.abspath(current)):
        return ""
    if not Path(armed).exists():
        return (f"Auto-start still points at {armed}, which is no longer there — the app has "
                f"been moved or renamed, so it will not start. Switch it off and on again to "
                f"re-point it at this copy.")
    return (f"Auto-start runs a different copy of the app ({armed}), which has its own "
            f"database. Switch it off and on again to make this copy the one that starts.")


def _warnings(mechanism: str) -> str:
    return " ".join(w for w in (_relocation_warning(mechanism), _environment_warning()) if w)


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
            starts_at_boot=True, path=TASK_NAME, warning=_warnings("scheduled-task"),
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
        mechanism="startup-folder", starts_at_boot=False, warning=_warnings("startup-folder"),
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


def purge_if_withdrawn() -> str:
    """Take down an entry left armed by a version that still offered auto-start.

    Withdrawing the feature is not enough on its own: the entry lives in the Startup folder, not
    in this program, so an install that had it switched on would go on launching a windowless
    copy at every sign-in for ever — the exact thing being removed, now with no toggle to turn
    it off. Returns what was removed, for the caller to report.
    """
    if AUTO_START_AVAILABLE or not is_supported():
        return ""
    state = status()
    if not state.enabled:
        return ""
    mechanism = state.mechanism
    disable()
    return mechanism


def run_now() -> bool:
    """Launch exactly what the startup entry launches, without waiting. Used to prove it works."""
    if not is_supported():
        return False
    try:
        subprocess.Popen(launch_command(),
                         creationflags=subprocess.DETACHED_PROCESS
                         | subprocess.CREATE_NEW_PROCESS_GROUP,
                         cwd=str(config.ROOT), close_fds=True)
        return True
    except OSError:
        return False
