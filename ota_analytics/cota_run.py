"""Sequences: one device, several commands, sent one at a time until each has an outcome.

Agreed with the user (2026-10-06) and enforced here:

  * **One command at a time.** The next is sent only once the current one has an outcome, so
    every answer belongs to exactly one command — the cloud's records cannot untangle overlap.
  * **Each command has stages** — accepted ✓, in the cloud ✓✓, delivered, answered — read from
    the same records the console shows.
  * **2 s after an answer, the guard band after anything else.** Once the device has answered,
    the next command goes 2 s later. A resend of the same command, and the next command after
    one that gave up, wait the guard band — 30 s, 60 s for an unrecognised command: the device talks to the
    cloud every 10 s, so anything shorter would not mean anything; 30 s is three heartbeats.
    Between a send and its answer, up to 2 minutes, checked every 10 s.
  * **Up to 3 attempts, then move to the next command.** An attempt is a send the cloud
    accepted: a refused call is recorded but not counted — three in a row pause the run.
  * **Never duplicate.** A resend is always safe when there is proof the earlier attempt never
    reached the device — the send was refused, or the command never appeared in the cloud. Once
    it was delivered, resending a GET (`DA…`) or a SET (`DB…`) is harmless: re-reading or setting
    the same value again changes nothing. A CLR (`DD…`, e.g. `DDD76D66` CLR SOS) is treated
    the same — the user's decision (2026-10-06), with the trade-off on record: a resend minutes
    later could clear a *new* SOS raised in between. Only an unrecognised command is resent just
    when it provably never arrived.
  * **Pause rather than fail** when the session cannot be renewed or the API stops answering,
    and **run in the background, resumable**: state lives in cota_run / cota_run_step, a page
    reads it, and a run interrupted by a restart comes back paused.

The runner never sleeps for real in tests: `sleep` and `clock` are injected, and the cloud is a
simulated device that acts out every failure case (tests/test_cota_run.py).
"""

from __future__ import annotations

import json
import re
import threading
import time
from datetime import datetime, timedelta

from . import cota, cota_connection, db

# ── the rules ──────────────────────────────────────────────────────────────────────────────
POLL_SECONDS = 10                   # one device heartbeat: checking faster finds nothing new
NOT_IN_CLOUD_SECONDS = 30           # the cloud lists a command within seconds of accepting it
ANSWER_WAIT_SECONDS = 120           # per attempt: twelve heartbeats
# After the device answered: the next command goes this soon (the user's rule, 2026-10-06).
GAP_AFTER_ANSWER_SECONDS = 2
# The guard band: before resending the same command, and before the next command after one
# that gave up — any outcome that was not an answer.
GUARD_SECONDS = {"get": 30, "set": 30, "clear": 30, "unknown": 60}
MAX_ATTEMPTS = 3
API_ERRORS_TO_PAUSE = 3             # consecutive failed calls before a run pauses itself

KINDS = {"DA": "get", "DB": "set", "DD": "clear"}
KIND_WORDS = {"get": "GET", "set": "SET", "clear": "CLR", "unknown": "Unknown"}
# Resent even when the device may already have received it. CLR included, by the user's
# decision; see the docstring for the trade-off.
REPEATABLE = {"get", "set", "clear"}

OUTCOME_WORDS = {
    "answered": "answered",
    "answered_failure": "answered with a failure",
    "not_accepted": "not accepted by the cloud",
    "not_in_cloud": "never appeared in the cloud",
    "not_delivered": "not picked up by the device",
    "delivered_no_answer": "delivered, no answer",
}


class RunError(Exception):
    """A sequence that cannot be started, with a message that is safe to show."""


# ── reading commands ──────────────────────────────────────────────────────────────────────

def classify(val1: str) -> tuple[str, str | None]:
    """(kind, problem) for one command. Kind comes from the first byte; a problem means it will
    not be sent at all — a wrong command is caught here rather than discovered on the device."""
    value = (val1 or "").strip().upper()
    if not value:
        return "unknown", "empty"
    if not re.fullmatch(r"[0-9A-F]+", value):
        return "unknown", "not hexadecimal"
    if len(value) % 2:
        return "unknown", "an odd number of hex digits"
    if len(value) < 8 or value[2:4] != "D7":
        return "unknown", "does not look like a command (expected XX D7 then the parameter)"
    return KINDS.get(value[:2], "unknown"), None


def parse_lines(text: str) -> list[dict]:
    """One command per line. A leading "1." or "2)" is allowed, as in a pasted list."""
    steps = []
    for number, line in enumerate((text or "").splitlines(), start=1):
        value = re.sub(r"^\s*\d+\s*[.)\-:]\s*", "", line).strip().upper().replace(" ", "")
        if not value:
            continue
        kind, problem = classify(value)
        steps.append({"line": number, "val1": value, "kind": kind, "problem": problem})
    return steps


def create_run(conn, device_id: int, device_type: int, cmd_type: int, text: str) -> int:
    steps = parse_lines(text)
    if not steps:
        raise RunError("Enter at least one command, one per line.")
    bad = [s for s in steps if s["problem"]]
    if bad:
        raise RunError("Not sent — fix these first: " + "; ".join(
            f"line {s['line']} ({s['val1'][:16] or 'blank'}): {s['problem']}" for s in bad))
    if active_run(conn, device_id):
        raise RunError("A sequence is already running for this device — pause or cancel it first.")
    from . import cota_campaign
    if cota_campaign.busy_devices(conn, [device_id]):
        raise RunError("This device is in a running job — one thing at a time per device.")
    with conn:
        run_id = conn.execute("""
            INSERT INTO cota_run (device_id, device_type, cmd_type, state, created_at)
            VALUES (?, ?, ?, 'running', ?)
        """, (device_id, device_type, cmd_type, _now())).lastrowid
        conn.executemany("""
            INSERT INTO cota_run_step (run_id, seq, val1, kind, state) VALUES (?, ?, ?, ?, 'queued')
        """, [(run_id, i, s["val1"], s["kind"]) for i, s in enumerate(steps, start=1)])
    return run_id


# ── reading runs back ─────────────────────────────────────────────────────────────────────

def active_run(conn, device_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM cota_run WHERE device_id = ? AND state IN ('running', 'paused') "
                       "ORDER BY id DESC LIMIT 1", (device_id,)).fetchone()
    return dict(row) if row else None


def run_view(conn, run_id: int) -> dict | None:
    """Everything the page shows about a run."""
    row = conn.execute("SELECT * FROM cota_run WHERE id = ?", (run_id,)).fetchone()
    if not row:
        return None
    steps = []
    for s in conn.execute("SELECT * FROM cota_run_step WHERE run_id = ? ORDER BY seq", (run_id,)):
        step = dict(s)
        step["log"] = json.loads(step["log"] or "[]")
        step["kind_word"] = KIND_WORDS.get(step["kind"], step["kind"])
        step["name"] = cota.describe_command(step["val1"]) or step["kind_word"]
        step["outcome_word"] = OUTCOME_WORDS.get(step["outcome"] or "", step["outcome"] or "")
        steps.append(step)
    current = next((s for s in steps if s["state"] == "running"), None)
    done = sum(1 for s in steps if s["state"] in ("done", "failed", "cancelled"))
    return {**dict(row), "steps": steps, "current": current, "total": len(steps),
            "finished_steps": done,
            "succeeded": sum(1 for s in steps if s["state"] == "done"),
            "failed": sum(1 for s in steps if s["state"] == "failed")}


def latest_run(conn, device_id: int) -> dict | None:
    row = conn.execute("SELECT id FROM cota_run WHERE device_id = ? ORDER BY id DESC LIMIT 1",
                       (device_id,)).fetchone()
    return run_view(conn, row["id"]) if row else None


def request(conn, run_id: int, control: str) -> None:
    """The page asks a run to pause, resume or cancel; the runner acts on it between steps of
    its loop (within a second), so a request never interrupts a call half-made."""
    if control == "resume":
        with conn:
            conn.execute("UPDATE cota_run SET control = NULL, state = 'running', pause_reason = NULL "
                         "WHERE id = ? AND state = 'paused'", (run_id,))
        start(run_id)
    elif control in ("pause", "cancel"):
        with conn:
            conn.execute("UPDATE cota_run SET control = ? WHERE id = ? AND state IN ('running', 'paused')",
                         (control, run_id))
            # A paused run has no thread to notice: cancel it here.
            if control == "cancel":
                conn.execute("UPDATE cota_run SET state = 'cancelled', control = NULL, finished_at = ? "
                             "WHERE id = ? AND state = 'paused'", (_now(), run_id))
                conn.execute("UPDATE cota_run_step SET state = 'cancelled' WHERE run_id = ? "
                             "AND state IN ('queued', 'running') AND "
                             "(SELECT state FROM cota_run WHERE id = ?) = 'cancelled'", (run_id, run_id))


# ── the runner ────────────────────────────────────────────────────────────────────────────

_threads: dict[int, threading.Thread] = {}
_lock = threading.Lock()


def start(run_id: int) -> None:
    """Run in the background. One thread per run; starting a running one does nothing."""
    with _lock:
        alive = _threads.get(run_id)
        if alive and alive.is_alive():
            return
        thread = threading.Thread(target=lambda: Runner(run_id).run(), daemon=True,
                                  name=f"cota-run-{run_id}")
        _threads[run_id] = thread
        thread.start()


def recover(conn) -> int:
    """A run left 'running' with no thread behind it was interrupted — the app restarted. It
    comes back paused, saying so, rather than appearing to run or silently vanishing."""
    stale = [r["id"] for r in conn.execute("SELECT id FROM cota_run WHERE state = 'running'")
             if not (_threads.get(r["id"]) and _threads[r["id"]].is_alive())]
    with conn:
        for run_id in stale:
            conn.execute("UPDATE cota_run SET state = 'paused', control = NULL, "
                         "pause_reason = 'The app was restarted — resume to carry on' WHERE id = ?",
                         (run_id,))
    return len(stale)


class _Paused(Exception):
    pass


class _Cancelled(Exception):
    pass


class Runner:
    def __init__(self, run_id: int, *, client_factory=None, renew=None, sleep=time.sleep,
                 clock=time.monotonic, connect=db.connect):
        self.run_id = run_id
        self.client_factory = client_factory or cota_connection.client
        self.renew = renew or cota_connection.renew
        self.sleep = sleep
        self.clock = clock
        self.conn = connect()
        self.api_errors = 0

    # ── the loop ──
    def run(self) -> None:
        conn = self.conn
        run = conn.execute("SELECT * FROM cota_run WHERE id = ?", (self.run_id,)).fetchone()
        if not run or run["state"] != "running":
            return
        self.device_id, self.device_type, self.cmd_type = (run["device_id"], run["device_type"],
                                                           run["cmd_type"])
        try:
            while True:
                step = conn.execute("""
                    SELECT * FROM cota_run_step WHERE run_id = ? AND state IN ('queued', 'running')
                    ORDER BY seq LIMIT 1
                """, (self.run_id,)).fetchone()
                if step is None:
                    break
                answered = self._step(dict(step))
                more = conn.execute("SELECT 1 FROM cota_run_step WHERE run_id = ? AND state = 'queued'",
                                    (self.run_id,)).fetchone()
                if more:
                    self._wait(GAP_AFTER_ANSWER_SECONDS if answered
                               else GUARD_SECONDS.get(step["kind"], 30))
            self._finish("done")
        except _Paused as why:
            with conn:
                conn.execute("UPDATE cota_run SET state = 'paused', control = NULL, pause_reason = ? "
                             "WHERE id = ?", (str(why), self.run_id))
        except _Cancelled:
            with conn:
                conn.execute("UPDATE cota_run_step SET state = 'cancelled', pending_task_id = NULL "
                             "WHERE run_id = ? AND state IN ('queued', 'running')", (self.run_id,))
            self._finish("cancelled")
        except Exception as exc:                                    # noqa: BLE001
            # A runner that dies must not leave its run looking alive. Paused, with the reason,
            # resumable — and on the Errors page, since nobody may be watching this run.
            from . import errors
            try:
                errors.record("cota-run", exc, path=f"run {self.run_id}")
            except Exception:                                       # noqa: BLE001
                pass
            with conn:
                conn.execute("UPDATE cota_run SET state = 'paused', control = NULL, pause_reason = ? "
                             "WHERE id = ?", (f"Stopped by an unexpected error "
                                              f"({type(exc).__name__}) — resume to carry on",
                                              self.run_id))

    def _finish(self, state: str) -> None:
        with self.conn:
            self.conn.execute("UPDATE cota_run SET state = ?, control = NULL, finished_at = ? "
                              "WHERE id = ?", (state, _now(), self.run_id))

    # ── one command ──
    def _step(self, step: dict) -> bool:
        """Send one command until it has an outcome. True when the device answered it."""
        conn = self.conn
        if step["state"] == "queued":
            with conn:
                conn.execute("UPDATE cota_run_step SET state = 'running', started_at = ? WHERE id = ?",
                             (_now(), step["id"]))
        log = json.loads(step["log"] or "[]")
        attempts = step["attempts"]
        task_ids = [e["task_id"] for e in log if e.get("task_id")]
        pending = step["pending_task_id"]

        while True:
            if pending is None:
                if attempts >= MAX_ATTEMPTS:
                    return self._end(step, "failed", log[-1]["outcome"] if log else None)
                if attempts and not self._may_resend(step["kind"], log):
                    return self._end(step, "failed", log[-1]["outcome"])
                if attempts and log and log[-1]["outcome"] != "not_accepted":
                    # The guard band before the same command goes again.
                    self._wait(GUARD_SECONDS.get(step["kind"], 30))
                task = self._send(step["val1"])
                if task["state"] == "send_failed":
                    # Recorded, but not an attempt: the command never left. Three refusals in a
                    # row pause the run — an outage must not use up the command's attempts.
                    log.append({"attempt": attempts + 1, "task_id": task["id"],
                                "outcome": "not_accepted", "at": _now(), "detail": task["error"]})
                    self._save(step, attempts, log, None)
                    self._wait(cota_run_guard(step["kind"]))
                    continue
                attempts += 1
                pending = task["id"]
                task_ids.append(pending)
                log.append({"attempt": attempts, "task_id": pending, "outcome": None, "at": _now()})
                self._save(step, attempts, log, pending)

            outcome = self._watch(pending, task_ids)
            log[-1]["outcome"] = outcome
            pending = None
            self._save(step, attempts, log, None)
            if outcome == "answered":
                return self._end(step, "done", outcome)

    def _may_resend(self, kind: str, log: list[dict]) -> bool:
        """The never-duplicate rule. Proof it never arrived makes any resend safe; otherwise only
        commands whose repetition changes nothing may go again."""
        last = log[-1]["outcome"] if log else None
        if last in ("not_accepted", "not_in_cloud"):
            return True
        return kind in REPEATABLE

    def _send(self, val1: str) -> dict:
        """One attempt, recorded as an ordinary console task. A rejected session is renewed and
        the attempt repeated once — the cloud refused it, so nothing reached the device."""
        for _ in (1, 2):
            client = self.client_factory()
            try:
                task = cota.console_send(self.conn, client, self.device_id, self.device_type,
                                         self.cmd_type, {"val1": val1})
            finally:
                client.close()
            if task["state"] == "send_failed" and "rejected the token" in (task["error"] or ""):
                if self.renew():
                    continue
                raise _Paused("Session expired — sign in again, then resume")
            break
        if task["state"] == "send_failed":
            self.api_errors += 1
            if self.api_errors >= API_ERRORS_TO_PAUSE:
                raise _Paused(f"The cloud did not accept {API_ERRORS_TO_PAUSE} calls in a row "
                              "— resume when it is answering again")
        else:
            self.api_errors = 0
        return task

    def _watch(self, task_id: int, all_task_ids: list[int]) -> str:
        """Check every heartbeat until this attempt has an outcome. An answer to an *earlier*
        attempt of the same step also counts — a late answer is still the answer."""
        started = self.clock()
        sent_at = cota._parse(self.conn.execute("SELECT sent_at FROM cota_task WHERE id = ?",
                                                (task_id,)).fetchone()[0])
        while True:
            self._wait(POLL_SECONDS)
            self._check(sent_at)
            states = [self._cloud_state(t) for t in all_task_ids]
            answered = [s for s in states if s and s["answer"]]
            if answered:
                return "answered_failure" if answered[-1]["reading"] == "failed" else "answered"
            mine = self._cloud_state(task_id)
            elapsed = self.clock() - started
            if mine is None and elapsed >= NOT_IN_CLOUD_SECONDS:
                return "not_in_cloud"
            if elapsed >= ANSWER_WAIT_SECONDS:
                return "delivered_no_answer" if mine and mine["delivered"] else "not_delivered"

    def _check(self, sent_at: datetime) -> None:
        # The same clock the sends were stamped with, so the window always holds them.
        start = sent_at - cota.POLL_MARGIN - timedelta(minutes=10)
        end = cota._parse(_now()) + cota.POLL_MARGIN
        for _ in (1, 2):
            client = self.client_factory()
            try:
                outcome = cota.console_check(self.conn, client, self.device_id, start, end)
            except cota.CotaError as exc:
                if "rejected the token" in str(exc) and self.renew():
                    continue
                if "rejected the token" in str(exc):
                    raise _Paused("Session expired — sign in again, then resume") from None
                outcome = {"ok": False}
            finally:
                client.close()
            break
        if outcome.get("ok"):
            self.api_errors = 0
        else:
            self.api_errors += 1
            if self.api_errors >= API_ERRORS_TO_PAUSE:
                raise _Paused(f"The cloud did not answer {API_ERRORS_TO_PAUSE} checks in a row "
                              "— resume when it is answering again")

    def _cloud_state(self, task_id: int) -> dict | None:
        row = self.conn.execute("""
            SELECT cloud_id, status, val1, first_seen_at, api_response, raw, device_response,
                   device_response_at, missing_since
            FROM cota_command WHERE task_id = ?
        """, (task_id,)).fetchone()
        view = cota._cloud_view(row) if row else None
        if not view:
            return None
        return {"answer": view["device_response"], "reading": view["reading"],
                "delivered": view["delivered"], "status": view["status"]}

    def _save(self, step: dict, attempts: int, log: list[dict], pending: int | None) -> None:
        with self.conn:
            self.conn.execute("UPDATE cota_run_step SET attempts = ?, log = ?, pending_task_id = ? "
                              "WHERE id = ?", (attempts, json.dumps(log), pending, step["id"]))

    def _end(self, step: dict, state: str, outcome: str | None) -> bool:
        with self.conn:
            self.conn.execute("UPDATE cota_run_step SET state = ?, outcome = ?, pending_task_id = NULL, "
                              "finished_at = ? WHERE id = ?", (state, outcome, _now(), step["id"]))
        return state == "done"

    def _wait(self, seconds: float) -> None:
        """Sleep in one-second slices, acting on a pause or cancel within a second."""
        waited = 0.0
        while waited < seconds:
            control = self.conn.execute("SELECT control FROM cota_run WHERE id = ?",
                                        (self.run_id,)).fetchone()[0]
            if control == "cancel":
                raise _Cancelled()
            if control == "pause":
                raise _Paused("Paused")
            self.sleep(1)
            waited += 1


def cota_run_guard(kind: str) -> float:
    return GUARD_SECONDS.get(kind, 30)


def _now() -> str:
    return cota._now()
