"""Jobs: many devices — up to 30,000 — each given the same sequence of commands.

Agreed with the user (2026-10-06):

  * **Each device moves through its own sequence** by the console's rules: one command at a time,
    the next no sooner than 2 s after an answer — at the scheduler's next tick, so that devices
    answering together are sent the next command together — the guard band (30 s) before a
    resend or after a step gave up,
    up to 3 attempts, never a duplicate (cota_run's rules — reused, not restated).
  * **A central scheduler ticks every 10 s.** It sends the same command to every device ready for
    it in **one call per batch** — a batch size set per job (default 50), to be raised during
    live runs as the cloud allows — and checks only the devices with an attempt outstanding.
  * **Calls are rate limited** per job (default 5 a second), and a job pauses itself after three
    failed calls in a row, or when more than 10% of finished commands fail (judged after 20).
  * **A canary first** for jobs over 20 devices: 1% (1–20 devices), the rest only once it has
    finished within the failure limit.
  * **A device that has not picked a command up is asleep.** The cloud holds the command and hands
    it over when the device wakes, so it is *not* resent — that would only queue copies. It is
    waited for, checked less and less often, until the job's validity window (default 12 h); then
    expired. A command delivered but unanswered, a failed answer, and one the cloud never listed
    are retried exactly as the console does.
  * **The day's COTA record lives for the day.** When the app starts on a later day, the earlier
    days' jobs, conversations and replies are cleared; groups and the device map are kept.
  * **A check reads one device per call.** `getGPRSCommand` refuses a list (HTTP 400, tried on the
    live cloud 2026-10-06), so checks — about one per device per command — not sends, decide how
    long a large job takes: 30,000 × 5 is ~160 send calls and ~150,000 checks. The rate is the
    lever, and the plan says so.

Time is injected (clock, sleep), and the cloud is a client object, so the whole thing runs against
a simulated fleet in tests — 30,000 devices included — with every wait its real length.
"""

from __future__ import annotations

import csv
import io
import json
import math
import re
import threading
import time
from datetime import datetime, timedelta

from . import cota, cota_connection, cota_run, db

TICK_SECONDS = 10
DEFAULT_BATCH = 50
DEFAULT_RATE = 5.0
DEFAULT_VALIDITY_HOURS = 12.0
MAX_FAIL_SHARE = 0.10
FAIL_SAMPLE = 20                 # finished commands before the automatic stop judges
CANARY_FROM = 21                 # jobs this size and up get a canary
CANARY_SHARE, CANARY_MIN, CANARY_MAX = 0.01, 1, 20
API_ERRORS_TO_PAUSE = 3

# How often a device with a command outstanding is checked, by how long it has been waiting: the
# first two minutes every heartbeat, then less and less — a device asleep for an hour does not
# need checking every 10 s, and 30,000 of them could not be.
POLL_SCHEDULE = [(120, 10), (600, 60), (3600, 300), (None, 900)]

RESULT_WORDS = {"queued": "Queued", "done": "Answered", "failed": "Failed",
                "expired": "Expired", "skipped": "Skipped", "cancelled": "Cancelled"}


class CampaignError(Exception):
    """A job that cannot be started, with a message that is safe to show."""


# ── groups ────────────────────────────────────────────────────────────────────────────────

def parse_device_ids(text: str) -> tuple[list[int], list[str], int]:
    """(ids in order, problems, duplicates dropped). Commas, spaces, new lines and semicolons all
    separate; a CSV's header (any non-numeric first line) is skipped."""
    ids, problems, seen, duplicates = [], [], set(), 0
    lines = (text or "").replace("\r", "").split("\n")
    for number, line in enumerate(lines, start=1):
        for token in re.split(r"[\s,;]+", line.strip()):
            if not token:
                continue
            token = token.strip("\"'")
            if not token.isdigit():
                if number == 1 and not ids:          # a header row: "Device ID", "deviceId" …
                    continue
                problems.append(f"line {number}: '{token[:20]}' is not a device id")
                continue
            device_id = int(token)
            if device_id in seen:
                duplicates += 1
                continue
            seen.add(device_id)
            ids.append(device_id)
    return ids, problems, duplicates


# The upload format is the cloud's own device list: `id,trackingCode` (the user's fleet export,
# 2026-10-06 — 23,103 devices). `id` is the cloud device id and is required; `trackingCode` is the
# IMEI and optional — when present it fills the device map too.
TEMPLATE_CSV = "id,trackingCode\n14906,865510083360422\n786,\n"
_ID_HEADERS = {"id", "deviceid", "device"}
_TRACKING_HEADERS = {"trackingcode", "trackercode", "tracking", "imei", "deviceuniqueno"}


def _flat(header: str) -> str:
    return re.sub(r"[\s_\-]", "", header.strip().lower())


def read_csv_devices(content: bytes) -> tuple[list[tuple[str, str]], bool]:
    """Rows of (id, trackingCode) from an upload, and whether it named its columns. Without a
    header the first column is the id and the second, if any, the tracking code."""
    text = content.decode("utf-8-sig", errors="replace")
    rows = [r for r in csv.reader(io.StringIO(text)) if any(c.strip() for c in r)]
    if not rows:
        return [], False
    header = [_flat(h) for h in rows[0]]
    has_header = not rows[0][0].strip().isdigit()
    id_col = next((i for i, h in enumerate(header) if h in _ID_HEADERS), 0) if has_header else 0
    track_col = (next((i for i, h in enumerate(header) if h in _TRACKING_HEADERS), None)
                 if has_header else (1 if len(rows[0]) > 1 else None))
    body = rows[1:] if has_header else rows
    out = []
    for r in body:
        device = r[id_col].strip() if len(r) > id_col else ""
        tracking = r[track_col].strip() if track_col is not None and len(r) > track_col else ""
        out.append((device, tracking))
    return out, has_header


def read_csv_ids(content: bytes) -> str:
    """The upload's device ids as text, for the same checks a typed list gets."""
    rows, _ = read_csv_devices(content)
    return "\n".join(device for device, _ in rows)


def learn_tracking_codes(conn, rows: list[tuple[str, str]],
                         device_type: int = cota.DEFAULT_DEVICE_TYPE) -> int:
    """Put each id's tracking code (IMEI) into the device map — an upload is a person's own,
    current list, so it updates the map. Returns how many were added or changed."""
    pairs = [(t, int(d)) for d, t in rows if d.isdigit() and t.isdigit()]
    if not pairs:
        return 0
    changed = 0
    now = cota._now()
    with conn:
        for imei, device_id in pairs:
            row = conn.execute("SELECT device_id FROM cota_device WHERE imei = ?", (imei,)).fetchone()
            if row and row["device_id"] == device_id:
                continue
            conn.execute("""
                INSERT INTO cota_device (imei, device_id, device_type, source, updated_at)
                VALUES (?, ?, ?, 'device list', ?)
                ON CONFLICT(imei) DO UPDATE SET device_id = excluded.device_id,
                    source = excluded.source, updated_at = excluded.updated_at
            """, (imei, device_id, device_type, now))
            changed += 1
    return changed


def save_group(conn, name: str, device_ids: list[int]) -> int:
    name = (name or "").strip()
    if not name:
        raise CampaignError("Give the group a name.")
    if not device_ids:
        raise CampaignError("A group needs at least one device id.")
    with conn:
        row = conn.execute("SELECT id FROM cota_group WHERE name = ?", (name,)).fetchone()
        if row:
            group_id = row["id"]
            conn.execute("UPDATE cota_group SET created_at = ? WHERE id = ?", (cota._now(), group_id))
        else:
            group_id = conn.execute("INSERT INTO cota_group (name, created_at) VALUES (?, ?)",
                                    (name, cota._now())).lastrowid
        conn.execute("DELETE FROM cota_group_member WHERE group_id = ?", (group_id,))
        conn.executemany("INSERT INTO cota_group_member (group_id, device_id, seq) VALUES (?, ?, ?)",
                         [(group_id, d, i) for i, d in enumerate(device_ids)])
    return group_id


def groups(conn) -> list[dict]:
    return [dict(r) for r in conn.execute("""
        SELECT g.id, g.name, g.created_at, COUNT(m.device_id) AS devices
        FROM cota_group g LEFT JOIN cota_group_member m ON m.group_id = g.id
        GROUP BY g.id ORDER BY g.name COLLATE NOCASE
    """)]


def group_devices(conn, group_id: int) -> list[int]:
    return [r[0] for r in conn.execute(
        "SELECT device_id FROM cota_group_member WHERE group_id = ? ORDER BY seq", (group_id,))]


def delete_group(conn, group_id: int) -> None:
    with conn:
        conn.execute("DELETE FROM cota_group WHERE id = ?", (group_id,))


# ── planning ──────────────────────────────────────────────────────────────────────────────

def canary_size(devices: int) -> int:
    if devices < CANARY_FROM:
        return 0
    return max(CANARY_MIN, min(CANARY_MAX, math.ceil(devices * CANARY_SHARE)))


def busy_devices(conn, device_ids: list[int]) -> list[int]:
    """Devices already in a live job or a live sequence — one thing at a time per device."""
    wanted = set(device_ids)
    busy = {r[0] for r in conn.execute("""
        SELECT d.device_id FROM cota_campaign_device d JOIN cota_campaign c ON c.id = d.campaign_id
        WHERE c.state IN ('running', 'paused') AND d.state IN ('ready', 'waiting')
    """)}
    busy |= {r[0] for r in conn.execute(
        "SELECT device_id FROM cota_run WHERE state IN ('running', 'paused')")}
    return sorted(wanted & busy)


def plan(conn, device_ids: list[int], commands_text: str, *, batch_size: int = DEFAULT_BATCH,
         rate_per_sec: float = DEFAULT_RATE) -> dict:
    """What a job would do, before anything is sent: the preview."""
    steps = cota_run.parse_lines(commands_text)
    bad = [s for s in steps if s["problem"]]
    batch_size = max(1, int(batch_size))
    sends = math.ceil(len(device_ids) / batch_size) * len(steps) if device_ids else 0
    # Awake devices: one send per batch per command, and a few checks each while they answer.
    checks = len(device_ids) * len(steps) * 2
    seconds = (sends + checks) / max(rate_per_sec, 0.1) + len(steps) * (20 + 2)
    return {
        "devices": len(device_ids), "steps": steps, "bad": bad,
        "names": [cota.describe_command(s["val1"]) or s["val1"][:16] for s in steps],
        "canary": canary_size(len(device_ids)),
        "send_calls": sends, "check_calls": checks,
        "estimate_minutes": max(1, round(seconds / 60)),
        "busy": busy_devices(conn, device_ids),
    }


def create(conn, *, name: str, device_ids: list[int], commands_text: str,
           device_type: int = cota.DEFAULT_DEVICE_TYPE, cmd_type: int = cota.DEFAULT_CMD_TYPE,
           batch_size: int = DEFAULT_BATCH, rate_per_sec: float = DEFAULT_RATE,
           validity_hours: float = DEFAULT_VALIDITY_HOURS,
           max_fail_share: float = MAX_FAIL_SHARE) -> int:
    the_plan = plan(conn, device_ids, commands_text, batch_size=batch_size,
                    rate_per_sec=rate_per_sec)
    if not device_ids:
        raise CampaignError("Add at least one device id.")
    if not the_plan["steps"]:
        raise CampaignError("Enter at least one command, one per line.")
    if the_plan["bad"]:
        raise CampaignError("Not started — fix these commands first: " + "; ".join(
            f"line {s['line']} ({s['val1'][:16] or 'blank'}): {s['problem']}" for s in the_plan["bad"]))
    if active(conn):
        raise CampaignError("A job is already running — let it finish, pause or cancel it first.")
    if the_plan["busy"]:
        shown = ", ".join(map(str, the_plan["busy"][:10]))
        raise CampaignError(f"{len(the_plan['busy'])} device(s) are in a live sequence or job "
                            f"({shown}) — one thing at a time per device.")
    commands = [s["val1"] for s in the_plan["steps"]]
    canary = the_plan["canary"]
    now = cota._now()
    with conn:
        job_id = conn.execute("INSERT INTO cota_job (name, source_file, created_at) VALUES (?, ?, ?)",
                              (name or "Job", "campaign", now)).lastrowid
        campaign_id = conn.execute("""
            INSERT INTO cota_campaign (name, job_id, device_type, cmd_type, commands, batch_size,
                rate_per_sec, validity_hours, canary_size, active_wave, max_fail_share, state,
                created_at, started_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'running', ?, ?)
        """, (name or f"Job · {len(device_ids)} devices", job_id, device_type, cmd_type,
              json.dumps(commands), max(1, int(batch_size)), max(0.1, float(rate_per_sec)),
              float(validity_hours), canary, 0 if canary else 1, float(max_fail_share),
              now, now)).lastrowid
        conn.executemany("""
            INSERT INTO cota_campaign_device (campaign_id, device_id, seq, wave, state, due_at)
            VALUES (?, ?, ?, ?, 'ready', 0)
        """, [(campaign_id, d, i, 0 if i < canary else 1) for i, d in enumerate(device_ids)])
        conn.executemany("""
            INSERT INTO cota_campaign_result (campaign_id, device_id, step, state)
            VALUES (?, ?, ?, 'queued')
        """, [(campaign_id, d, s) for d in device_ids for s in range(len(commands))])
    return campaign_id


# ── reading jobs back ─────────────────────────────────────────────────────────────────────

def active(conn) -> dict | None:
    row = conn.execute("SELECT * FROM cota_campaign WHERE state IN ('running', 'paused') "
                       "ORDER BY id DESC LIMIT 1").fetchone()
    return dict(row) if row else None


def campaigns(conn, limit: int = 50) -> list[dict]:
    out = []
    for r in conn.execute("SELECT * FROM cota_campaign ORDER BY id DESC LIMIT ?", (limit,)):
        out.append(summary(conn, r["id"]))
    return out


def summary(conn, campaign_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM cota_campaign WHERE id = ?", (campaign_id,)).fetchone()
    if not row:
        return None
    c = dict(row)
    c["commands"] = json.loads(c["commands"])
    c["names"] = [cota.describe_command(v) or v[:16] for v in c["commands"]]
    counts = {r["state"]: r["n"] for r in conn.execute(
        "SELECT state, COUNT(*) AS n FROM cota_campaign_result WHERE campaign_id = ? GROUP BY state",
        (campaign_id,))}
    total = sum(counts.values())
    finished = total - counts.get("queued", 0)
    devices = {r["state"]: r["n"] for r in conn.execute(
        "SELECT state, COUNT(*) AS n FROM cota_campaign_device WHERE campaign_id = ? GROUP BY state",
        (campaign_id,))}
    waiting = conn.execute("""
        SELECT COUNT(*) FROM cota_campaign_device WHERE campaign_id = ? AND state = 'waiting'
    """, (campaign_id,)).fetchone()[0]
    c.update(results=counts, total=total, finished=finished,
             percent=round(100 * finished / total, 1) if total else 0.0,
             devices=sum(devices.values()), device_states=devices, waiting=waiting)
    return c


def grid(conn, campaign_id: int) -> list[dict]:
    """Command × outcome: for each command, how many devices are where."""
    c = summary(conn, campaign_id)
    rows = []
    for step, (value, name) in enumerate(zip(c["commands"], c["names"])):
        counts = {r["state"]: r["n"] for r in conn.execute(
            "SELECT state, COUNT(*) AS n FROM cota_campaign_result WHERE campaign_id = ? AND step = ? "
            "GROUP BY state", (campaign_id, step))}
        in_flight = conn.execute("""
            SELECT COUNT(*) FROM cota_campaign_device
            WHERE campaign_id = ? AND step = ? AND state = 'waiting'
        """, (campaign_id, step)).fetchone()[0]
        rows.append({"step": step, "value": value, "name": name, "counts": counts,
                     "in_flight": in_flight})
    return rows


def device_rows(conn, campaign_id: int, *, state: str = "", search: str = "", page: int = 1,
                size: int = 50) -> tuple[list[dict], int]:
    """The job's devices, for the group-SMS view: where each one is, newest outcome first."""
    where, params = ["d.campaign_id = ?"], [campaign_id]
    if state in ("ready", "waiting", "done", "expired", "cancelled"):
        where.append("d.state = ?")
        params.append(state)
    elif state == "failed":
        where.append("EXISTS (SELECT 1 FROM cota_campaign_result r WHERE r.campaign_id = d.campaign_id "
                     "AND r.device_id = d.device_id AND r.state = 'failed')")
    if search.strip().isdigit():
        where.append("CAST(d.device_id AS TEXT) LIKE ?")
        params.append(f"%{search.strip()}%")
    clause = " AND ".join(where)
    total = conn.execute(f"SELECT COUNT(*) FROM cota_campaign_device d WHERE {clause}", params).fetchone()[0]
    rows = []
    for r in conn.execute(f"""
        SELECT d.device_id, d.state, d.step, d.attempt,
               (SELECT COUNT(*) FROM cota_campaign_result x WHERE x.campaign_id = d.campaign_id
                  AND x.device_id = d.device_id AND x.state = 'done') AS answered,
               (SELECT COUNT(*) FROM cota_campaign_result x WHERE x.campaign_id = d.campaign_id
                  AND x.device_id = d.device_id AND x.state = 'failed') AS failed,
               (SELECT imei FROM cota_device m WHERE m.device_id = d.device_id LIMIT 1) AS imei
        FROM cota_campaign_device d WHERE {clause}
        ORDER BY d.seq LIMIT ? OFFSET ?
    """, (*params, size, (max(1, page) - 1) * size)):
        rows.append(dict(r))
    return rows, total


EXPORT_COLUMNS = [
    ("device_id", "Device ID"), ("imei", "IMEI"), ("step", "Step"), ("name", "Command"),
    ("value", "Value"), ("state", "Outcome"), ("attempts", "Attempts"), ("detail", "Detail"),
    ("answer", "Device answer"), ("finished_at", "Finished at"),
]


def export_rows(conn, campaign_id: int):
    """Every device × command, streamed — 150,000 rows never sit in memory at once."""
    c = summary(conn, campaign_id)
    imeis = {r[0]: r[1] for r in conn.execute("SELECT device_id, imei FROM cota_device")}
    for r in conn.execute("""
        SELECT device_id, step, state, attempts, outcome, answer, finished_at
        FROM cota_campaign_result WHERE campaign_id = ? ORDER BY device_id, step
    """, (campaign_id,)):
        yield {"device_id": r["device_id"], "imei": imeis.get(r["device_id"]),
               "step": r["step"] + 1, "name": c["names"][r["step"]],
               "value": c["commands"][r["step"]], "state": RESULT_WORDS.get(r["state"], r["state"]),
               "attempts": r["attempts"],
               "detail": cota_run.OUTCOME_WORDS.get(r["outcome"] or "", r["outcome"] or ""),
               "answer": cota.visible(r["answer"]), "finished_at": r["finished_at"]}


def request(conn, campaign_id: int, control: str) -> None:
    """Pause, resume or cancel from the page; the scheduler acts on it within a second."""
    if control == "resume":
        done = conn.execute("SELECT COALESCE(SUM(state = 'failed'), 0), "
                            "COALESCE(SUM(state IN ('done', 'failed')), 0) "
                            "FROM cota_campaign_result WHERE campaign_id = ?", (campaign_id,)).fetchone()
        with conn:
            # The automatic stop judges what happens after the resume, not what made it stop.
            conn.execute("""
                UPDATE cota_campaign SET control = NULL, state = 'running', pause_reason = NULL,
                       fail_base_failed = ?, fail_base_finished = ?
                WHERE id = ? AND state = 'paused'
            """, (done[0], done[1], campaign_id))
            # Resuming after a canary stop is the review: the rest may now go.
            conn.execute("""
                UPDATE cota_campaign SET active_wave = 1 WHERE id = ? AND active_wave = 0
                  AND NOT EXISTS (SELECT 1 FROM cota_campaign_device WHERE campaign_id = ?
                                  AND wave = 0 AND state IN ('ready', 'waiting'))
            """, (campaign_id, campaign_id))
        start(campaign_id)
    elif control in ("pause", "cancel"):
        with conn:
            conn.execute("UPDATE cota_campaign SET control = ? WHERE id = ? "
                         "AND state IN ('running', 'paused')", (control, campaign_id))
            if control == "cancel":
                paused = conn.execute("SELECT 1 FROM cota_campaign WHERE id = ? AND state = 'paused'",
                                      (campaign_id,)).fetchone()
                if paused:
                    _cancel_rows(conn, campaign_id)


def _cancel_rows(conn, campaign_id: int) -> None:
    conn.execute("UPDATE cota_campaign_device SET state = 'cancelled' WHERE campaign_id = ? "
                 "AND state IN ('ready', 'waiting')", (campaign_id,))
    conn.execute("UPDATE cota_campaign_result SET state = 'cancelled' WHERE campaign_id = ? "
                 "AND state = 'queued'", (campaign_id,))
    conn.execute("UPDATE cota_campaign SET state = 'cancelled', control = NULL, finished_at = ? "
                 "WHERE id = ?", (cota._now(), campaign_id))


# ── the day's record ──────────────────────────────────────────────────────────────────────

_purged_this_process = False


def purge_previous_days(conn, today: datetime | None = None) -> int:
    """At the start of a session on a later day, clear the earlier days' COTA record — jobs,
    sequences, conversations, cloud records. Groups and the device map are set-up and stay.
    Runs once per process: a session that runs past midnight keeps its day."""
    global _purged_this_process
    if _purged_this_process:
        return 0
    _purged_this_process = True
    start = (today or datetime.now()).replace(hour=0, minute=0, second=0, microsecond=0)
    cutoff = start.strftime(cota.TIME_FORMAT)
    old = sum(conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {column} < ?", (cutoff,)).fetchone()[0]
              for table, column in (("cota_job", "created_at"), ("cota_campaign", "created_at"),
                                    ("cota_run", "created_at"), ("cota_command", "first_seen_at")))
    if not old:
        return 0
    with conn:
        conn.execute("DELETE FROM cota_campaign WHERE created_at < ?", (cutoff,))
        conn.execute("DELETE FROM cota_run WHERE created_at < ?", (cutoff,))
        conn.execute("DELETE FROM cota_command WHERE first_seen_at < ?", (cutoff,))
        conn.execute("DELETE FROM cota_job WHERE created_at < ?", (cutoff,))   # tasks, polls cascade
    return old


# ── the scheduler ─────────────────────────────────────────────────────────────────────────

_threads: dict[int, threading.Thread] = {}
_lock = threading.Lock()


def start(campaign_id: int) -> None:
    with _lock:
        alive = _threads.get(campaign_id)
        if alive and alive.is_alive():
            return
        thread = threading.Thread(target=lambda: Scheduler(campaign_id).run(), daemon=True,
                                  name=f"cota-job-{campaign_id}")
        _threads[campaign_id] = thread
        thread.start()


def recover(conn) -> int:
    """A job left running with no scheduler behind it was interrupted by a restart: paused."""
    stale = [r["id"] for r in conn.execute("SELECT id FROM cota_campaign WHERE state = 'running'")
             if not (_threads.get(r["id"]) and _threads[r["id"]].is_alive())]
    with conn:
        for campaign_id in stale:
            conn.execute("UPDATE cota_campaign SET state = 'paused', control = NULL, pause_reason = "
                         "'The app was restarted — resume to carry on' WHERE id = ?", (campaign_id,))
    return len(stale)


class _Paused(Exception):
    pass


class _Cancelled(Exception):
    pass


class Scheduler:
    def __init__(self, campaign_id: int, *, client_factory=None, renew=None, sleep=time.sleep,
                 connect=db.connect):
        self.id = campaign_id
        self.client_factory = client_factory or cota_connection.client
        self.renew = renew or cota_connection.renew
        self.sleep = sleep
        self.conn = connect()
        self.api_errors = 0
        self.last_reply: dict[int, str] = {}      # device → its last raw answer: skip if unchanged
        c = self.conn.execute("SELECT * FROM cota_campaign WHERE id = ?", (campaign_id,)).fetchone()
        self.c = dict(c) if c else None
        if self.c:
            self.commands = json.loads(self.c["commands"])
            self.kinds = [cota_run.classify(v)[0] for v in self.commands]
            self.window_from = cota._parse(self.c["created_at"]) - timedelta(minutes=15)

    @staticmethod
    def now() -> float:
        return cota._parse(cota._now()).timestamp()

    # ── the loop ──
    def run(self) -> None:
        if not self.c or self.c["state"] != "running":
            return
        try:
            while True:
                self._control()
                tick_started = self.now()
                self.budget = max(1, int(self.c["rate_per_sec"] * TICK_SECONDS))
                self._advance_wave()
                self._send_due()
                self._poll_due()
                self._expire()
                self._breaker()
                left = self.conn.execute("""
                    SELECT COUNT(*) FROM cota_campaign_device WHERE campaign_id = ?
                      AND state IN ('ready', 'waiting')
                """, (self.id,)).fetchone()[0]
                if not left:
                    with self.conn:
                        self.conn.execute("UPDATE cota_campaign SET state = 'done', control = NULL, "
                                          "finished_at = ? WHERE id = ?", (cota._now(), self.id))
                    return
                # Fixed 10 s ticks. Devices that became ready within a tick are sent together in
                # one call, so in a job the next command goes at the first tick at least 2 s after
                # its answer — batching is what keeps the calls down. A busy tick has already
                # spent its time pacing calls; an idle one waits it out.
                self._wait(max(0, TICK_SECONDS - (self.now() - tick_started)))
        except _Paused as why:
            with self.conn:
                self.conn.execute("UPDATE cota_campaign SET state = 'paused', control = NULL, "
                                  "pause_reason = ? WHERE id = ?", (str(why), self.id))
        except _Cancelled:
            with self.conn:
                _cancel_rows(self.conn, self.id)
        except Exception as exc:                                   # noqa: BLE001
            from . import errors
            try:
                errors.record("cota-job", exc, path=f"job {self.id}")
            except Exception:                                      # noqa: BLE001
                pass
            with self.conn:
                self.conn.execute("UPDATE cota_campaign SET state = 'paused', control = NULL, "
                                  "pause_reason = ? WHERE id = ?",
                                  (f"Stopped by an unexpected error ({type(exc).__name__}) — "
                                   "resume to carry on", self.id))

    def _control(self) -> None:
        control = self.conn.execute("SELECT control FROM cota_campaign WHERE id = ?",
                                    (self.id,)).fetchone()[0]
        if control == "cancel":
            raise _Cancelled()
        if control == "pause":
            raise _Paused("Paused")

    def _wait(self, seconds: float) -> None:
        waited = 0.0
        while waited < seconds:
            self._control()
            self.sleep(1)
            waited += 1

    def _pace(self) -> None:
        """Spread calls out at the job's rate rather than bursting them."""
        self.sleep(1.0 / self.c["rate_per_sec"])

    # ── waves and the automatic stop ──
    def _advance_wave(self) -> None:
        wave = self.conn.execute("SELECT active_wave FROM cota_campaign WHERE id = ?",
                                 (self.id,)).fetchone()[0]
        if wave >= 1:
            return
        left = self.conn.execute("""
            SELECT COUNT(*) FROM cota_campaign_device WHERE campaign_id = ? AND wave = 0
              AND state IN ('ready', 'waiting')
        """, (self.id,)).fetchone()[0]
        if left:
            return
        failed, finished = self.conn.execute("""
            SELECT COALESCE(SUM(r.state = 'failed'), 0), COALESCE(SUM(r.state IN ('done', 'failed')), 0)
            FROM cota_campaign_result r JOIN cota_campaign_device d
              ON d.campaign_id = r.campaign_id AND d.device_id = r.device_id
            WHERE r.campaign_id = ? AND d.wave = 0
        """, (self.id,)).fetchone()
        if finished and failed / finished > self.c["max_fail_share"]:
            raise _Paused(f"The canary failed {failed} of {finished} commands — above the "
                          f"{self.c['max_fail_share']:.0%} limit. Check them, then resume to "
                          "send to the rest.")
        with self.conn:
            self.conn.execute("UPDATE cota_campaign SET active_wave = 1 WHERE id = ?", (self.id,))

    def _breaker(self) -> None:
        c = self.conn.execute("SELECT fail_base_failed, fail_base_finished, max_fail_share "
                              "FROM cota_campaign WHERE id = ?", (self.id,)).fetchone()
        failed, finished = self.conn.execute("""
            SELECT COALESCE(SUM(state = 'failed'), 0), COALESCE(SUM(state IN ('done', 'failed')), 0)
            FROM cota_campaign_result WHERE campaign_id = ?
        """, (self.id,)).fetchone()
        failed -= c["fail_base_failed"]
        finished -= c["fail_base_finished"]
        if finished >= FAIL_SAMPLE and failed / finished > c["max_fail_share"]:
            raise _Paused(f"{failed} of {finished} commands failed ({failed / finished:.0%}) — "
                          f"above the {c['max_fail_share']:.0%} limit. Check, then resume.")

    # ── sending: one call per batch of devices ready for the same command ──
    def _send_due(self) -> None:
        now = self.now()
        wave = self.conn.execute("SELECT active_wave FROM cota_campaign WHERE id = ?",
                                 (self.id,)).fetchone()[0]
        due = self.conn.execute("""
            SELECT device_id, step, seq FROM cota_campaign_device
            WHERE campaign_id = ? AND state = 'ready' AND due_at <= ? AND wave <= ?
            ORDER BY due_at, seq LIMIT ?
        """, (self.id, now, wave, self.budget * self.c["batch_size"])).fetchall()
        by_step: dict[int, list] = {}
        for r in due:                       # longest-waiting first, then group order within a call
            by_step.setdefault(r["step"], []).append((r["seq"], r["device_id"]))
        by_step = {k: [d for _, d in sorted(v)] for k, v in by_step.items()}
        for step, devices in sorted(by_step.items()):
            for i in range(0, len(devices), self.c["batch_size"]):
                if self.budget <= 0:
                    return
                self._send_batch(step, devices[i:i + self.c["batch_size"]])

    def _send_batch(self, step: int, devices: list[int]) -> None:
        conn, value = self.conn, self.commands[step]
        now_s = cota._now()
        with conn:
            base = conn.execute("SELECT COALESCE(MAX(seq), 0) FROM cota_task WHERE job_id = ?",
                                (self.c["job_id"],)).fetchone()[0]
            batch_no = conn.execute("SELECT COALESCE(MAX(batch_no), 0) + 1 FROM cota_task WHERE job_id = ?",
                                    (self.c["job_id"],)).fetchone()[0]
            conn.executemany("""
                INSERT INTO cota_task (job_id, seq, imei, device_id, device_type, cmd_type, params,
                                       batch_no, state)
                VALUES (?, ?, '', ?, ?, ?, ?, ?, 'planned')
            """, [(self.c["job_id"], base + i + 1, d, self.c["device_type"], self.c["cmd_type"],
                   json.dumps({"val1": value}), batch_no) for i, d in enumerate(devices)])
        rows = conn.execute("SELECT * FROM cota_task WHERE job_id = ? AND batch_no = ? ORDER BY seq",
                            (self.c["job_id"], batch_no)).fetchall()
        ok = False
        for _ in (1, 2):
            client = self.client_factory()
            try:
                ok = cota._send_call(conn, client, rows, stop_on_rejected_token=False)
            finally:
                client.close()
            self.budget -= 1
            self._pace()
            error = conn.execute("SELECT error FROM cota_task WHERE id = ?", (rows[0]["id"],)).fetchone()[0]
            if not ok and "rejected the token" in (error or ""):
                if self.renew():
                    with conn:
                        conn.execute("UPDATE cota_task SET state = 'planned' WHERE job_id = ? AND batch_no = ?",
                                     (self.c["job_id"], batch_no))
                    continue
                raise _Paused("Session expired — sign in again, then resume")
            break
        with conn:
            conn.execute("UPDATE cota_campaign SET send_calls = send_calls + 1 WHERE id = ?", (self.id,))
        now = self.now()
        task_of = {r["device_id"]: r["id"] for r in rows}
        if ok:
            self.api_errors = 0
            with conn:
                conn.executemany("""
                    UPDATE cota_campaign_device SET state = 'waiting', attempt = attempt + 1,
                           pending_task_id = ?, wait_started_at = ?, next_poll_at = ?,
                           step_started_at = COALESCE(step_started_at, ?)
                    WHERE campaign_id = ? AND device_id = ?
                """, [(task_of[d], now, now + POLL_SCHEDULE[0][1], now, self.id, d) for d in devices])
                self._add_tasks(devices, step, task_of)
            return
        self.api_errors += 1
        if self.api_errors >= API_ERRORS_TO_PAUSE:
            # Count this attempt first, so a resume does not lose it.
            self._after_attempt(devices, step, "not_accepted", task_of, now_s)
            raise _Paused(f"The cloud did not accept {API_ERRORS_TO_PAUSE} calls in a row — "
                          "resume when it is answering again")
        self._after_attempt(devices, step, "not_accepted", task_of, now_s)

    def _record_refused(self, devices: list[int], step: int, task_of: dict[int, int]) -> None:
        for d in devices:
            row = self.conn.execute("SELECT task_ids FROM cota_campaign_result WHERE "
                                    "campaign_id = ? AND device_id = ? AND step = ?",
                                    (self.id, d, step)).fetchone()
            self.conn.execute("UPDATE cota_campaign_result SET task_ids = ? WHERE campaign_id = ? "
                              "AND device_id = ? AND step = ?",
                              (json.dumps(json.loads(row["task_ids"]) + [task_of[d]]), self.id, d, step))

    def _add_tasks(self, devices: list[int], step: int, task_of: dict[int, int]) -> None:
        for d in devices:
            row = self.conn.execute("SELECT task_ids, attempts FROM cota_campaign_result WHERE "
                                    "campaign_id = ? AND device_id = ? AND step = ?",
                                    (self.id, d, step)).fetchone()
            ids = json.loads(row["task_ids"]) + [task_of[d]]
            self.conn.execute("UPDATE cota_campaign_result SET task_ids = ?, attempts = ? WHERE "
                              "campaign_id = ? AND device_id = ? AND step = ?",
                              (json.dumps(ids), row["attempts"] + 1, self.id, d, step))

    def _after_attempt(self, devices: list[int], step: int, outcome: str, task_of: dict, at: str):
        """An attempt ended without an answer: retry after the guard band, or give the step up and
        move on after it — by cota_run's rules."""
        kind = self.kinds[step]
        guard = cota_run.GUARD_SECONDS.get(kind, 30)
        now = self.now()
        with self.conn:
            if outcome == "not_accepted":
                # Recorded, but not counted as an attempt: the command never left the cloud's
                # door. Three refusals in a row pause the job instead — an outage must not use
                # up every device's attempts.
                self._record_refused(devices, step, task_of)
            for d in devices:
                dev = self.conn.execute("SELECT attempt FROM cota_campaign_device WHERE "
                                        "campaign_id = ? AND device_id = ?", (self.id, d)).fetchone()
                may = outcome in ("not_accepted", "not_in_cloud") or kind in cota_run.REPEATABLE
                if dev["attempt"] < cota_run.MAX_ATTEMPTS and may:
                    self.conn.execute("""
                        UPDATE cota_campaign_device SET state = 'ready', due_at = ?,
                               pending_task_id = NULL WHERE campaign_id = ? AND device_id = ?
                    """, (now + guard, self.id, d))
                    self.conn.execute("UPDATE cota_campaign_result SET outcome = ? WHERE campaign_id = ? "
                                      "AND device_id = ? AND step = ?", (outcome, self.id, d, step))
                else:
                    self._finish_step(d, step, "failed", outcome, None, at, delay=guard)

    def _finish_step(self, device: int, step: int, state: str, outcome: str | None,
                     answer: str | None, at: str, *, delay: float) -> None:
        self.conn.execute("""
            UPDATE cota_campaign_result SET state = ?, outcome = ?, answer = ?, finished_at = ?
            WHERE campaign_id = ? AND device_id = ? AND step = ?
        """, (state, outcome, answer, at, self.id, device, step))
        last = step + 1 >= len(self.commands)
        self.conn.execute("""
            UPDATE cota_campaign_device SET step = ?, attempt = 0, pending_task_id = NULL,
                   step_started_at = NULL, wait_started_at = NULL, next_poll_at = NULL,
                   state = ?, due_at = ? WHERE campaign_id = ? AND device_id = ?
        """, (step if last else step + 1, "done" if last else "ready", self.now() + delay,
              self.id, device))

    # ── checking: only devices with an attempt outstanding, less often the longer they wait ──
    def _poll_due(self) -> None:
        now = self.now()
        due = self.conn.execute("""
            SELECT device_id, step, pending_task_id, wait_started_at, step_started_at
            FROM cota_campaign_device
            WHERE campaign_id = ? AND state = 'waiting' AND next_poll_at <= ?
            ORDER BY next_poll_at LIMIT ?
        """, (self.id, now, max(0, self.budget))).fetchall()
        start = int(self.window_from.timestamp())
        for d in due:
            if self.budget <= 0:
                return
            self._control()
            end = int(self.now() + cota.POLL_MARGIN.total_seconds())
            status, raw = None, None
            for _ in (1, 2):
                client = self.client_factory()
                try:
                    status, raw = client.responses(d["device_id"], start, end)
                except cota.CotaError as exc:
                    if "rejected the token" in str(exc):
                        if self.renew():
                            continue
                        raise _Paused("Session expired — sign in again, then resume") from None
                    status, raw = None, None
                finally:
                    client.close()
                break
            self.budget -= 1
            self._pace()
            with self.conn:
                self.conn.execute("UPDATE cota_campaign SET poll_calls = poll_calls + 1 WHERE id = ?",
                                  (self.id,))
            if status is None or not 200 <= status < 300:
                self.api_errors += 1
                if self.api_errors >= API_ERRORS_TO_PAUSE:
                    raise _Paused(f"The cloud did not answer {API_ERRORS_TO_PAUSE} checks in a row — "
                                  "resume when it is answering again")
                with self.conn:
                    self.conn.execute("UPDATE cota_campaign_device SET next_poll_at = ? WHERE "
                                      "campaign_id = ? AND device_id = ?",
                                      (self.now() + POLL_SCHEDULE[0][1], self.id, d["device_id"]))
                continue
            self.api_errors = 0
            # Store only what changed: a sleeping device answers the same thing every check.
            if raw != self.last_reply.get(d["device_id"]):
                self.last_reply[d["device_id"]] = raw
                records = cota.reply_records(raw)
                if records is not None:
                    cota.store_cloud_records(self.conn, d["device_id"], records)
            self._judge(dict(d))

    def _judge(self, d: dict) -> None:
        """Where this device's attempt stands, and what happens next."""
        step = d["step"]
        row = self.conn.execute("SELECT task_ids FROM cota_campaign_result WHERE campaign_id = ? "
                                "AND device_id = ? AND step = ?", (self.id, d["device_id"], step)).fetchone()
        task_ids = json.loads(row["task_ids"])
        states = [self._cloud_state(t) for t in task_ids]
        answered = [s for s in states if s and s["answer"]]
        now = self.now()
        at = cota._now()
        if answered:
            last = answered[-1]
            if last["reading"] == "failed":
                return self._after_attempt([d["device_id"]], step, "answered_failure", {}, at)
            with self.conn:
                self._finish_step(d["device_id"], step, "done", "answered", last["answer"], at,
                                  delay=cota_run.GAP_AFTER_ANSWER_SECONDS)
            return
        mine = self._cloud_state(d["pending_task_id"])
        waited = now - d["wait_started_at"]
        if mine is None and waited >= cota_run.NOT_IN_CLOUD_SECONDS:
            return self._after_attempt([d["device_id"]], step, "not_in_cloud", {}, at)
        if mine and mine["delivered"] and waited >= cota_run.ANSWER_WAIT_SECONDS:
            return self._after_attempt([d["device_id"]], step, "delivered_no_answer", {}, at)
        # Not picked up yet: the device is asleep and the cloud holds the command for it. Wait —
        # checking less often the longer it sleeps — until the validity window ends.
        with self.conn:
            self.conn.execute("UPDATE cota_campaign_device SET next_poll_at = ? WHERE campaign_id = ? "
                              "AND device_id = ?", (now + _poll_interval(waited), self.id, d["device_id"]))

    def _cloud_state(self, task_id: int | None) -> dict | None:
        if not task_id:
            return None
        row = self.conn.execute("""
            SELECT cloud_id, status, val1, first_seen_at, api_response, raw, device_response,
                   device_response_at, missing_since
            FROM cota_command WHERE task_id = ?
        """, (task_id,)).fetchone()
        view = cota._cloud_view(row) if row else None
        return {"answer": view["device_response"], "reading": view["reading"],
                "delivered": view["delivered"]} if view else None

    def _expire(self) -> None:
        """Devices still asleep at the end of the validity window: expired, the rest skipped."""
        cutoff = self.now() - self.c["validity_hours"] * 3600
        late = self.conn.execute("""
            SELECT device_id, step FROM cota_campaign_device WHERE campaign_id = ? AND state = 'waiting'
              AND step_started_at <= ?
        """, (self.id, cutoff)).fetchall()
        if not late:
            return
        at = cota._now()
        with self.conn:
            for d in late:
                self.conn.execute("""
                    UPDATE cota_campaign_result SET state = 'expired', outcome = 'not_delivered',
                           finished_at = ? WHERE campaign_id = ? AND device_id = ? AND step = ?
                """, (at, self.id, d["device_id"], d["step"]))
                self.conn.execute("""
                    UPDATE cota_campaign_result SET state = 'skipped' WHERE campaign_id = ?
                      AND device_id = ? AND step > ? AND state = 'queued'
                """, (self.id, d["device_id"], d["step"]))
                self.conn.execute("UPDATE cota_campaign_device SET state = 'expired', pending_task_id = NULL "
                                  "WHERE campaign_id = ? AND device_id = ?", (self.id, d["device_id"]))


def _poll_interval(waited: float) -> float:
    for until, every in POLL_SCHEDULE:
        if until is None or waited < until:
            return every
    return POLL_SCHEDULE[-1][1]
