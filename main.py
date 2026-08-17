"""InTouch OTA Analytics — single entry point.

Run this file from VS Code (F5, or the ▷ Run button) and it will:

  1. create the database if it does not exist
  2. ingest any new exports sitting in "Sample data" (safe to re-run — ingest is idempotent)
  3. rebuild the derived metrics
  4. start the dashboard and open it in your browser

Command line equivalents, if you prefer them:

    python main.py                 # everything above
    python main.py --no-ingest     # just serve what is already in the database
    python main.py --port 8080     # different port
    python main.py --no-browser    # do not open a browser window
"""

from __future__ import annotations

import argparse
import os
import signal
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path

FROZEN = bool(getattr(sys, "frozen", False))
# Where the program lives. Packaged, that is the folder holding the .exe; from source, the
# folder holding this file. They are not the same thing once the code is bundled.
ROOT = Path(sys.executable).resolve().parent if FROZEN else Path(__file__).resolve().parent
if not FROZEN:
    sys.path.insert(0, str(ROOT))   # runs no matter what VS Code sets as the working dir

def _usable(stream: object) -> bool:
    """Whether a stream actually leads somewhere a library can write to."""
    if stream is None:
        return False
    try:
        return int(getattr(stream, "fileno")()) >= 0
    except (OSError, ValueError, AttributeError, TypeError):
        return False


def attach_log_when_headless() -> None:
    """Give the windowless build real output streams, pointed at a log file.

    A windowed executable has no console, so stdout and stderr lead nowhere. That is not merely
    cosmetic: uvicorn installs a logging handler on `sys.stdout` and cannot start without one,
    so the windowless build — the one auto-start runs — exited a few seconds after launch,
    every reboot, with nothing recorded anywhere to say why.

    An unattended process that fails at 3am has to leave something behind, so this is also the
    only log that copy will ever produce.
    """
    if not FROZEN or (_usable(sys.stdout) and _usable(sys.stderr)):
        return

    log_dir = Path(os.environ.get("OTA_DATA_DIR", ROOT / "data"))
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        handle = open(log_dir / "app.log", "a", encoding="utf-8", errors="replace", buffering=1)
    except OSError:
        return                      # read-only folder: carry on silently rather than refuse

    handle.write(f"\n--- started {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
    if not _usable(sys.stdout):
        sys.stdout = handle
    if not _usable(sys.stderr):
        sys.stderr = handle


def use_utf8_console() -> None:
    """Windows consoles default to a legacy code page that mangles the dashes this program
    prints, turning an em dash into a replacement character mid-table."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
    except (AttributeError, OSError):
        pass
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass


attach_log_when_headless()
use_utf8_console()

# Import name -> pip name, for packages this program cannot start without.
REQUIRED_PACKAGES = {
    "openpyxl": "openpyxl",
    "fastapi": "fastapi",
    "uvicorn": "uvicorn",
    "jinja2": "jinja2",
}


def check_dependencies() -> None:
    """Fail with an actionable message instead of a traceback from inside a library.

    Several Python installations usually coexist on a Windows machine, and VS Code may well run
    a different one from the terminal where the packages were installed. When that happens the
    real problem is *which interpreter*, so print it — a bare ImportError sends people looking
    in the wrong place.

    Skipped in a packaged build: the dependencies are inside the executable, so there is no
    interpreter to have picked wrongly and nothing for `pip install` to fix. A missing module
    there means the build was made wrong, which is not a message for whoever is running it.
    """
    import importlib.util

    if FROZEN:
        return

    missing = [pip_name for module, pip_name in REQUIRED_PACKAGES.items()
               if importlib.util.find_spec(module) is None]
    if not missing:
        return

    print("Missing required package(s): " + ", ".join(missing), file=sys.stderr)
    print(f"\nThis is running Python {sys.version.split()[0]} at:\n  {sys.executable}\n",
          file=sys.stderr)
    print("Install them into THAT interpreter with:\n", file=sys.stderr)
    print(f'  & "{sys.executable}" -m pip install -r "{ROOT / "requirements.txt"}"\n',
          file=sys.stderr)
    print("If you meant to use a different Python, switch it in VS Code with\n"
          "Ctrl+Shift+P -> 'Python: Select Interpreter'.", file=sys.stderr)
    raise SystemExit(1)


check_dependencies()

from ota_analytics import config, db, ingest, rollup  # noqa: E402

DEFAULT_HOST = "127.0.0.1"

# Identifies a copy of *this* app answering on a port. Kept in step with api.APP_ID by a test.
APP_MARKER = "intouch-ota-analytics"

# Addresses that only this machine can reach. Anything else means other computers can too.
LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def check_exposure(host: str) -> None:
    """Refuse to serve the fleet to the network with no password set.

    Binding to 0.0.0.0 is how colleagues reach the dashboard, and it is also how everything else
    on the network reaches it. Every page here lists IMEIs, VINs and ICCIDs, and /update posts to
    the production OTA platform — so the one configuration that must not be possible by accident
    is "reachable by everyone, protected by nothing".

    Loopback stays open with no password: that is one person on their own machine, and demanding
    a login there would only teach people to disable this.
    """
    if host in LOOPBACK:
        return

    from ota_analytics import auth
    if auth.is_configured():
        return

    print(f"Refusing to serve on {host} with no password configured.\n", file=sys.stderr)
    print("This would publish every IMEI, VIN and ICCID to anyone on the network.\n",
          file=sys.stderr)
    print("Set a password first:\n", file=sys.stderr)
    print(f'  & "{sys.executable}" passwd --role admin' if FROZEN else
          f'  & "{sys.executable}" -m ota_analytics.cli passwd --role admin', file=sys.stderr)
    print(f"\nthen put the printed line in the environment as {auth.ENV_ADMIN_HASH}, or run on\n"
          "127.0.0.1 for local-only use.", file=sys.stderr)
    raise SystemExit(2)
DEFAULT_PORT = 8000


def load_new_exports() -> int:
    """Ingest any export not already in the warehouse. Returns how many were added."""
    conn = db.connect()

    if not config.EXPORT_DIR.exists():
        print(f"  ! export folder not found: {config.EXPORT_DIR}")
        print("    create it and drop the platform's .xlsx exports in there")
        return 0

    files = [p for p in sorted(config.EXPORT_DIR.glob("*.xlsx")) if not p.name.startswith("~$")]
    if not files:
        print(f"  ! no .xlsx exports found in {config.EXPORT_DIR}")
        return 0

    added = 0
    for result in ingest.ingest_dir(conn, config.EXPORT_DIR):
        if result.status == "ingested":
            added += 1
            print(f"  + {result.path.name} -> snapshot {result.snapshot_id} "
                  f"({result.rows:,} devices, {result.duration_ms / 1000:.1f}s)")
            if result.ts_source != "filename":
                print("    ! snapshot time guessed from file date — trend spacing may be wrong")
        else:
            print(f"  = {result.path.name} (already loaded)")

    if added:
        rollup.rollup_all(conn)
        print("  * metrics rebuilt")
    return added


def summarize() -> None:
    """Print the headline numbers so the terminal is useful on its own."""
    conn = db.connect()
    from ota_analytics import metrics

    snapshot_id = metrics.latest_snapshot_id(conn)
    if snapshot_id is None:
        print("\n  No data loaded yet — the dashboard will show setup instructions.\n")
        return

    k = metrics.kpis(conn, snapshot_id)
    total = k["devices_total"]
    print(f"\n  Snapshot {snapshot_id} — {k['snapshot_at']}")
    print(f"    devices              {total:,}")
    print(f"    online now           {k['devices_online']:,} ({k['online_pct']:.1%})")
    print(f"    stuck while online   {k['pending_reachable']:,}   <- reachable, task undelivered")
    print(f"    waiting for power-on {k['pending_waiting']:,}   (expected)")
    print(f"    never tasked         {k['devices_never_tasked']:,}")

    if len(metrics.snapshots(conn)) < 2:
        print("\n    Only one snapshot loaded. Trends, return rates and task outcomes need a")
        print("    second export — drop tomorrow's file into 'Sample data' and run this again.")
    print()


def _pid_file() -> Path:
    """Where a detached instance records its pid and port."""
    return config.DATA_DIR / "agent.pid"


def _read_pid() -> tuple[int, int] | None:
    """Return (pid, port) of a running background instance, or None."""
    path = _pid_file()
    if not path.exists():
        return None
    try:
        pid_text, port_text = path.read_text(encoding="utf-8").split(",", 1)
        pid, port = int(pid_text), int(port_text)
    except (ValueError, OSError):
        return None

    # A PID alone is not proof: the number may have been recycled by an unrelated process.
    # Confirm something is actually answering on the port it claimed.
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return pid, port
    except OSError:
        path.unlink(missing_ok=True)
        return None


def start_background(args: argparse.Namespace) -> int:
    """Launch the dashboard detached, so it keeps fetching after this window closes."""
    import subprocess

    running = _read_pid()
    if running:
        pid, port = running
        print(f"Already running in the background (pid {pid}) at http://{args.host}:{port}")
        print("Stop it with:  python main.py --stop")
        return 0

    config.ensure_dirs()
    log_path = config.DATA_DIR / "agent.log"
    port = find_free_port(args.host, args.port)

    # Packaged, the program *is* the executable and there is no main.py to hand it.
    command = ([sys.executable] if FROZEN else [sys.executable, str(ROOT / "main.py")])
    command += ["--host", args.host, "--port", str(port), "--no-browser"]
    if args.no_ingest:
        command.append("--no-ingest")

    # DETACHED_PROCESS + CREATE_NEW_PROCESS_GROUP: survives this console closing, and is not
    # killed when VS Code stops the debug session.
    creationflags = 0
    if sys.platform == "win32":
        creationflags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP

    with open(log_path, "ab") as log:
        process = subprocess.Popen(command, stdout=log, stderr=log, stdin=subprocess.DEVNULL,
                                   creationflags=creationflags, cwd=str(ROOT),
                                   close_fds=True)

    _pid_file().write_text(f"{process.pid},{port}", encoding="utf-8")

    # Wait until it is actually serving before claiming success.
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((args.host, port), timeout=0.5):
                print(f"Running in the background (pid {process.pid})")
                print(f"  dashboard  http://{args.host}:{port}")
                print(f"  log        {log_path}")
                print(f"  stop with  python main.py --stop")
                print("\nIt keeps fetching on its schedule even with this window closed.")
                return 0
        except OSError:
            if process.poll() is not None:
                print(f"It exited immediately. See {log_path}", file=sys.stderr)
                _pid_file().unlink(missing_ok=True)
                return 1
            time.sleep(0.25)

    print(f"Started (pid {process.pid}) but it did not answer within 90s. See {log_path}",
          file=sys.stderr)
    return 1


def stop_background() -> int:
    running = _read_pid()
    if not running:
        print("No background instance is running.")
        return 0

    pid, port = running
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as exc:
        print(f"Could not stop pid {pid}: {exc}", file=sys.stderr)
        return 1

    for _ in range(40):
        if _read_pid() is None:
            break
        time.sleep(0.25)
    _pid_file().unlink(missing_ok=True)
    print(f"Stopped the background instance (pid {pid}, port {port}).")
    return 0


def background_status() -> int:
    running = _read_pid()
    if not running:
        print("Not running in the background.")
        return 1
    pid, port = running
    print(f"Running in the background — pid {pid}, http://127.0.0.1:{port}")
    return 0


def already_serving(host: str, port: int, timeout: float = 1.5) -> bool:
    """Whether *this* application is already answering on that port.

    Starting a second copy is the normal outcome of double-clicking the app when auto-start has
    already launched the windowless one: the port is taken, so the new copy quietly moves to
    8001 and shows a different URL. Nothing errors, both copies share one database, and the
    person is left with two dashboards, two schedulers fetching the same data, and no way to
    tell which window belongs to which — while the copy holding :8000 has no window at all.

    Checked by asking, not by probing the socket, because something unrelated may hold the
    port and that case still deserves the old fall-back-to-another-port behaviour.
    """
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(f"http://{host}:{port}/healthz", timeout=timeout) as reply:
            body = reply.read(4096).decode("utf-8", errors="replace")
    except (OSError, urllib.error.URLError, ValueError):
        return False
    # Matched as text rather than parsed: /healthz is a fixed, tiny document, and the marker is
    # specific enough that nothing else would answer with it on loopback.
    return APP_MARKER in body


def find_free_port(host: str, preferred: int, attempts: int = 20) -> int:
    """Return the first port at or after `preferred` that nothing else is holding.

    A leftover server from an earlier run is the usual reason the default is taken, and
    "address already in use" is a confusing way to discover that.
    """
    for candidate in range(preferred, preferred + attempts):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.bind((host, candidate))
                return candidate
            except OSError:
                continue
    return preferred


def open_browser_when_ready(url: str, host: str, port: int, timeout: float = 90.0) -> None:
    """Open the browser only once the server actually accepts connections.

    Opening on a fixed timer is a race: under the VS Code debugger startup easily takes
    longer than any delay worth waiting, and the tab then loads before the port is bound —
    which shows up as "127.0.0.1 refused to connect".
    """
    def wait_then_open() -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                with socket.create_connection((host, port), timeout=0.5):
                    webbrowser.open(url)
                    return
            except OSError:
                time.sleep(0.25)
        print(f"  ! server did not come up within {timeout:.0f}s — open {url} manually")

    threading.Thread(target=wait_then_open, daemon=True).start()


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)

    # `InTouchOTA-Analytics.exe db-export ...` — from source these live behind
    # `python -m ota_analytics.cli`, which a packaged build has no way to reach. Without this,
    # sharing a database, setting a password and declaring firmware targets would all be
    # source-only features.
    if argv:
        from ota_analytics.cli import command_names, main as cli_main
        if argv[0] in command_names():
            return cli_main(argv)

    parser = argparse.ArgumentParser(description="Run the OTA analytics dashboard")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--no-ingest", action="store_true",
                        help="skip loading new exports, just serve the database")
    parser.add_argument("--no-browser", action="store_true",
                        help="do not open a browser window")
    parser.add_argument("--reload", action="store_true",
                        help="restart on code changes (for development)")
    parser.add_argument("--background", action="store_true",
                        help="run detached so fetching continues after this window closes")
    parser.add_argument("--stop", action="store_true",
                        help="stop the detached instance")
    parser.add_argument("--status", action="store_true",
                        help="report whether a detached instance is running")
    args = parser.parse_args(argv)

    # Show progress as it happens rather than in one burst at the end — Python block-buffers
    # stdout whenever it is not a terminal (piped output, VS Code tasks, log files).
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is not None:
        reconfigure(line_buffering=True)

    config.ensure_dirs()
    if args.stop:
        return stop_background()
    if args.status:
        return background_status()

    # Checked before the background branch too, so `--background --host 0.0.0.0` cannot slip an
    # unprotected instance past this and then detach from the terminal showing the error.
    check_exposure(args.host)
    if args.background:
        return start_background(args)

    # Show the copy that is already running rather than quietly becoming a second one. This is
    # the ordinary case once auto-start is on: the windowless copy is already holding the port,
    # invisibly, and double-clicking the app would otherwise open a dashboard on a different
    # port with a second scheduler behind it. --reload is exempt: that is a developer
    # deliberately restarting.
    if not args.reload and already_serving(args.host, args.port):
        url = f"http://{args.host}:{args.port}"
        print(f"InTouch OTA Analytics is already running at {url}")
        print("  Opening that one instead of starting a second copy.\n")
        # Deliberately not "--stop": that only knows about a copy started with --background,
        # so it would be wrong advice for the common cases (a window left open on another
        # desktop, or the windowless build started by hand).
        print("  Close the window it is running in, or end InTouchOTA-Analytics in Task")
        print("  Manager, if you meant to restart it.")
        if not args.no_browser:
            webbrowser.open(url)
        return 0

    print("InTouch OTA Analytics")
    print(f"  database   {config.DB_PATH}")
    print(f"  exports    {config.EXPORT_DIR}")

    # An entry armed by an earlier version outlives the feature being withdrawn: it lives in the
    # Startup folder, not in this program. Left alone it would keep launching a windowless copy
    # at every sign-in, with nothing in the UI to switch it off.
    from ota_analytics import startup
    removed = startup.purge_if_withdrawn()
    if removed:
        print(f"  * removed the old start-with-Windows entry ({removed.replace('-', ' ')})")

    if args.no_ingest:
        db.connect()
    else:
        print("\nLoading exports...")
        load_new_exports()

    summarize()

    port = find_free_port(args.host, args.port)
    if port != args.port:
        print(f"  ! port {args.port} is already in use — using {port} instead")
    url = f"http://{args.host}:{port}"
    print(f"  Dashboard  {url}")
    print("  Press Ctrl+C to stop.\n")

    if not args.no_browser:
        open_browser_when_ready(url, args.host, port)

    import uvicorn
    try:
        # The import string form is required for --reload; without it we hand over the app object.
        if args.reload:
            uvicorn.run("ota_analytics.api:app", host=args.host, port=port,
                        reload=True, log_level="warning")
        else:
            from ota_analytics.api import app
            uvicorn.run(app, host=args.host, port=port, log_level="warning")
    except KeyboardInterrupt:
        print("\nStopped.")
    return 0


if __name__ == "__main__":
    # A frozen executable re-runs this file in every child process it spawns. Without this the
    # child restarts the dashboard instead of doing its job, which forks endlessly.
    if FROZEN:
        import multiprocessing
        multiprocessing.freeze_support()
    raise SystemExit(main())


