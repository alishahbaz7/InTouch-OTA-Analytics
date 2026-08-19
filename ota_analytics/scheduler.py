"""Background agent that pulls new data on a timer.

One worker thread, woken by an Event rather than a plain sleep, so interval changes and stop
requests take effect immediately instead of after the current wait. State is persisted, so an
enabled schedule resumes when the app restarts.

Auth note: unattended fetching needs credentials the agent can use on its own.
  * `token` mode — username + password with a login URL. The agent signs in on every run, so an
    expired token is a non-event. This is the mode to use for automation.
  * `bearer` mode — a pasted token. It works until the token expires and then cannot recover,
    because nothing can regenerate it. The status bar says so rather than failing silently.
"""

from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta

from . import config, db, ingest, rollup, sources

MIN_INTERVAL = 60           # 1 minute
MAX_INTERVAL = 24 * 60 * 60  # 24 hours
DEFAULT_INTERVAL = 3600      # 1 hour

STATE_PATH = config.DATA_DIR / "scheduler.json"


@dataclass
class SchedulerState:
    enabled: bool = False
    interval_seconds: int = DEFAULT_INTERVAL
    last_run: str | None = None
    last_status: str = "never"          # never | ok | unchanged | error
    last_message: str = ""
    last_snapshot_id: int | None = None
    next_run: str | None = None
    runs: int = 0
    failures: int = 0
    consecutive_failures: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


def clamp_interval(seconds: int) -> int:
    """Keep the timer inside 1 minute … 24 hours."""
    try:
        seconds = int(seconds)
    except (TypeError, ValueError):
        return DEFAULT_INTERVAL
    return max(MIN_INTERVAL, min(MAX_INTERVAL, seconds))


def describe_interval(seconds: int) -> str:
    if seconds < 3600:
        minutes = seconds // 60
        return f"{minutes} minute{'s' if minutes != 1 else ''}"
    if seconds < 86400:
        hours = seconds / 3600
        return f"{hours:g} hour{'s' if hours != 1 else ''}"
    return "24 hours"


class Scheduler:
    """A single background fetcher. One instance per process."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._stopping = False
        self.state = self._load()

    # ─── persistence ────────────────────────────────────────────────────────

    def _load(self) -> SchedulerState:
        if STATE_PATH.exists():
            try:
                data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
                known = {f: data[f] for f in SchedulerState.__dataclass_fields__ if f in data}
                return SchedulerState(**known)
            except (json.JSONDecodeError, OSError, TypeError):
                pass
        return SchedulerState()

    def _save(self) -> None:
        try:
            config.DATA_DIR.mkdir(parents=True, exist_ok=True)
            STATE_PATH.write_text(json.dumps(self.state.to_dict(), indent=2), encoding="utf-8")
        except OSError:
            pass    # a failed status write must never take down the fetch loop

    # ─── control ────────────────────────────────────────────────────────────

    def configure(self, *, enabled: bool, interval_seconds: int) -> SchedulerState:
        with self._lock:
            self.state.enabled = enabled
            self.state.interval_seconds = clamp_interval(interval_seconds)
            if enabled:
                self.state.next_run = (
                    datetime.now() + timedelta(seconds=self.state.interval_seconds)
                ).isoformat(sep=" ", timespec="seconds")
            else:
                self.state.next_run = None
            self._save()

        if enabled:
            self.start()
        self._wake.set()        # apply the new interval immediately
        return self.state

    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stopping = False
            self._thread = threading.Thread(target=self._loop, name="ota-fetch-agent",
                                            daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stopping = True
        self._wake.set()

    def run_now(self, job=None) -> SchedulerState:
        """Trigger a fetch immediately, whether or not the timer is enabled."""
        self._run_once(job=job)
        return self.state

    # ─── the loop ───────────────────────────────────────────────────────────

    def _loop(self) -> None:
        while not self._stopping:
            interval = self.state.interval_seconds
            # Event-based wait: a change to the interval or a stop lands at once.
            woken = self._wake.wait(timeout=interval)
            self._wake.clear()

            if self._stopping:
                return
            if woken:
                # Re-read config and restart the wait rather than firing early.
                if not self.state.enabled:
                    return
                continue
            if not self.state.enabled:
                return
            self._run_once()

    def _run_once(self, job=None) -> None:
        started = datetime.now()
        try:
            message, status, snapshot_id = self._fetch_and_ingest(job=job)
        except Exception as exc:                     # never let the loop die
            status, message, snapshot_id = "error", f"{type(exc).__name__}: {exc}", None
            from . import errors
            errors.record("agent", exc, path="scheduled fetch")

        with self._lock:
            self.state.last_run = started.isoformat(sep=" ", timespec="seconds")
            self.state.last_status = status
            self.state.last_message = message
            self.state.runs += 1
            if status == "error":
                self.state.failures += 1
                self.state.consecutive_failures += 1
            else:
                self.state.consecutive_failures = 0
                if snapshot_id:
                    self.state.last_snapshot_id = snapshot_id
            if self.state.enabled:
                self.state.next_run = (
                    datetime.now() + timedelta(seconds=self.state.interval_seconds)
                ).isoformat(sep=" ", timespec="seconds")
            self._save()

    def _fetch_and_ingest(self, job=None) -> tuple[str, str, int | None]:
        job = job or _SilentJob()
        job.begin("Signing in")
        connection = sources.load_connection()
        if not connection.url:
            return "No platform URL configured — set one on the Update Data page.", "error", None

        secret = sources.load_password(connection.username)
        if not secret and connection.auth_mode != "none":
            return ("No saved credentials. Tick 'Remember me' on the Update Data page so the "
                    "agent can sign in on its own."), "error", None

        job.begin("Downloading from the platform", detail=connection.url)
        fetched = sources.fetch_export(connection, secret or "")
        conn = db.connect()

        if fetched.records is not None:
            job.begin("Reading devices", total=len(fetched.records),
                      detail=f"{len(fetched.records):,} devices")
            result = ingest.ingest_records(conn, fetched.records,
                                           source_name=f"API {connection.url}", job=job)
        else:
            result = ingest.ingest_file(conn, fetched.path, job=job)
            if result.status == "already_ingested" and fetched.path:
                fetched.path.unlink(missing_ok=True)

        if result.status == "already_ingested":
            return ("Nothing has changed on the platform since the last pull.",
                    "unchanged", result.snapshot_id)

        job.begin("Rebuilding metrics")
        rollup.rollup_snapshot(conn, result.snapshot_id)

        job.begin("Thinning old snapshots")
        # Retention runs with every fetch: at a short interval the database would otherwise
        # grow by gigabytes a week, and nobody would notice until it hurt.
        from . import retention
        pruned = retention.prune(conn, vacuum=False)
        note = f" Pruned {pruned.removed} old snapshot(s)." if pruned.removed else ""

        return (f"Loaded {result.rows:,} devices as snapshot {result.snapshot_id}.{note}",
                "ok", result.snapshot_id)


# Phases of a fetch, and what each is worth on the bar. Signing in and downloading
# dominate the wall clock on a slow link; folding the snapshot into the registry
# dominates on a fast one.
FETCH_STEPS = [
    ("Signing in", 1.0),
    ("Downloading from the platform", 6.0),
    ("Reading devices", 4.0),
    ("Storing what changed", 2.0),
    ("Checking data quality", 1.0),
    ("Updating the device registry", 6.0),
    ("Rebuilding metrics", 2.0),
    ("Thinning old snapshots", 1.0),
]


class _SilentJob:
    """Stands in for a job when a scheduled fetch runs with nobody watching."""

    def begin(self, name, total=0, detail=""):
        pass

    def advance(self, done=None, detail=None):
        pass


_scheduler: Scheduler | None = None


def get_scheduler() -> Scheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = Scheduler()
        if _scheduler.state.enabled:
            _scheduler.start()      # resume a schedule that was running before a restart
    return _scheduler


def auth_status() -> dict:
    """What the status bar needs to know about the platform connection."""
    connection = sources.load_connection()
    has_password = sources.load_password(connection.username) is not None

    if not connection.url:
        return {"level": "none", "label": "Not connected",
                "detail": "No platform URL configured yet.",
                "can_automate": False, "username": ""}

    if not has_password and connection.auth_mode != "none":
        return {"level": "warn", "label": "Signed out",
                "detail": f"{connection.url} is configured, but no credentials are saved — "
                          "the agent cannot fetch on its own.",
                "can_automate": False, "username": connection.username}

    if connection.auth_mode == "bearer":
        return {"level": "warn", "label": "Token only",
                "detail": "A pasted bearer token is saved. It works until it expires, and "
                          "nothing can renew it — give the agent a login URL with username and "
                          "password for unattended fetching.",
                "can_automate": False, "username": connection.username}

    if connection.auth_mode in {"token", "form"} and connection.login_url:
        return {"level": "ok", "label": "Signed in",
                "detail": f"{connection.username} — the agent signs in again on every run, so "
                          "an expired token recovers by itself.",
                "can_automate": True, "username": connection.username}

    return {"level": "ok", "label": "Credentials saved",
            "detail": f"{connection.username} via {connection.auth_mode} auth.",
            "can_automate": True, "username": connection.username}
