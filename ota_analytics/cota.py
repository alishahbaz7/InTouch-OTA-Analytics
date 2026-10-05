"""COTA — configuration over the air, sent through the cloud's own API.

The cloud (ctvms IntouchAdminApi) can already send a command to a device and show what the
device replied, one screen at a time. This module drives the same two calls in bulk:

    saveCOTAConfig/0   send one command to a list of devices
    getGPRSCommand     what one device received and replied within a time window

and keeps every request and reply in the local database, so a job of many devices × many
commands can be sent, watched, exported and analysed. COTA.md explains the design and lists
what is still unknown about the API.

Rules carried over from the rest of the app:

  * The bearer token lives in the environment or the OS credential store — never in a file, the
    database or a log line. Raw exception text from an HTTP call is never shown either.
  * Nothing is sent without being asked for explicitly. Planning a job touches no network.
  * People work with IMEIs; the cloud works with internal device ids. An IMEI the device map
    does not know is recorded as `unmapped`, never guessed and never silently dropped.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from . import exports, normalize, sources

BASE_URL = "https://ctvms.mappls.com/IntouchAdminApi/api"
SEND_PATH = "/saveCOTAConfig/0"          # what the trailing 0 selects is not known yet
RESPONSES_PATH = "/getGPRSCommand"

ENV_TOKEN = "OTA_COTA_TOKEN"
TOKEN_ACCOUNT = "cota-bearer"            # keyring username under sources.SERVICE_NAME

# The API takes a list of devices per call. How long a list it accepts is not known, so stay
# well inside anything plausible until it has been measured.
MAX_DEVICES_PER_CALL = 50
# Pause between calls to the cloud, so a large job cannot look like a flood.
CALL_INTERVAL_SECONDS = 0.5
# Poll a little either side of the send time: the two clocks are not the same clock.
POLL_MARGIN = timedelta(minutes=5)
# Excel refuses a cell longer than this; the database keeps the full reply.
XLSX_CELL_LIMIT = 32_000

IMEI_HEADERS = {"imei", "deviceuniqueno", "deviceuniquenumber", "uniqueno"}
DEVICE_ID_HEADERS = {"deviceid", "id"}
DEVICE_TYPE_HEADERS = {"devicetype", "devicetypeid"}
COMMAND_HEADERS = {"type", "command", "commandtype", "cmdtype", "commandcode"}
MAX_VALUES = 9                           # val1 … val9


class CotaError(Exception):
    """Anything that stops a COTA step. The message is safe to show."""


# ─── token ──────────────────────────────────────────────────────────────────

def load_token() -> str | None:
    """The bearer token: the environment first, then the OS credential store."""
    from_env = (os.environ.get(ENV_TOKEN) or "").strip()
    if from_env:
        return from_env
    kr = sources._keyring()
    if kr is None:
        return None
    try:
        return kr.get_password(sources.SERVICE_NAME, TOKEN_ACCOUNT)
    except Exception:
        return None


def save_token(token: str) -> bool:
    kr = sources._keyring()
    if kr is None:
        return False
    try:
        kr.set_password(sources.SERVICE_NAME, TOKEN_ACCOUNT, clean_token(token))
        return True
    except Exception:
        return False


def forget_token() -> None:
    kr = sources._keyring()
    if kr is None:
        return
    try:
        kr.delete_password(sources.SERVICE_NAME, TOKEN_ACCOUNT)
    except Exception:
        pass


def clean_token(token: str) -> str:
    """Accept the token however it was copied: bare, or with 'Bearer ' still in front."""
    token = (token or "").strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    return token


# ─── talking to the cloud ───────────────────────────────────────────────────

class Client:
    """The two COTA calls. Tests replace this with a fake that records what it was asked."""

    def __init__(self, token: str, base_url: str = BASE_URL, timeout: float = 30.0):
        import httpx

        if not token:
            raise CotaError(f"No COTA token. Set one with: cota token set "
                            f"(or the {ENV_TOKEN} environment variable).")
        self.base_url = base_url.rstrip("/")
        self._http = httpx.Client(timeout=timeout, headers={
            "Authorization": f"Bearer {clean_token(token)}",
            "Accept": "application/json, text/plain, */*",
        })

    def close(self) -> None:
        self._http.close()

    def send(self, payload: dict) -> tuple[int, str]:
        return self._call("POST", SEND_PATH, json=payload)

    def responses(self, device_id: int, start: int, end: int) -> tuple[int, str]:
        return self._call("GET", RESPONSES_PATH,
                          params={"deviceId": device_id, "startTime": start, "endTime": end})

    def _call(self, method: str, path: str, **kwargs) -> tuple[int, str]:
        try:
            response = self._http.request(method, self.base_url + path, **kwargs)
        except Exception as exc:
            # Never the exception text: a request object can carry the token in its headers.
            raise CotaError(f"Could not reach the COTA cloud: {type(exc).__name__}. "
                            "Check the network and whether a VPN is required.") from None
        if response.status_code in (401, 403):
            raise CotaError(f"The cloud rejected the token (HTTP {response.status_code}). "
                            "It has most likely expired — copy a fresh one from the portal "
                            "and run: cota token set")
        return response.status_code, response.text


# ─── reading sheets ─────────────────────────────────────────────────────────

def _imei(value: object) -> str | None:
    """An IMEI as text. Excel hands a 15-digit number back as an int or a float."""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return normalize.clean(value)


def _int(value: object) -> int | None:
    text = normalize.clean(value)
    if text is None:
        return None
    try:
        return int(float(text))
    except ValueError:
        return None


def _value(value: object) -> str | None:
    """A command value exactly as written — a MAC like 'DBD76B82D531' must stay text."""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _read_sheet(path: Path) -> tuple[dict[str, int], list[tuple]]:
    """Header map (flattened name → position) and the data rows of a .xlsx or .csv."""
    from .ingest import _table_rows, flatten_key

    path = Path(path)
    if not path.exists():
        raise CotaError(f"{path} does not exist.")
    with _table_rows(path) as rows:
        header = next(rows, None)
        if not header:
            raise CotaError(f"{path.name} is empty.")
        positions = {flatten_key(h): i for i, h in enumerate(header) if h is not None}
        data = [tuple(r) for r in rows if r and any(c not in (None, "") for c in r)]
    return positions, data


def _column(positions: dict[str, int], names: set[str], label: str, path: Path) -> int:
    for name in names:
        if name in positions:
            return positions[name]
    raise CotaError(f"{Path(path).name} has no {label} column "
                    f"(looked for: {', '.join(sorted(names))}).")


def _cell(row: tuple, index: int) -> object:
    return row[index] if index < len(row) else None


# ─── device map: IMEI → cloud id + type ─────────────────────────────────────

@dataclass
class MapResult:
    added: int = 0
    updated: int = 0
    skipped: list[str] = field(default_factory=list)


def import_device_map(conn, path: Path) -> MapResult:
    """Load IMEI → Device ID + Device Type from a sheet. Re-importing updates in place."""
    positions, rows = _read_sheet(path)
    c_imei = _column(positions, IMEI_HEADERS, "IMEI / Device Unique No", path)
    c_id = _column(positions, DEVICE_ID_HEADERS, "Device ID", path)
    c_type = _column(positions, DEVICE_TYPE_HEADERS, "Device Type", path)

    result = MapResult()
    now = _now()
    known = {r[0] for r in conn.execute("SELECT imei FROM cota_device")}
    with conn:
        for number, row in enumerate(rows, start=2):
            imei = _imei(_cell(row, c_imei))
            device_id, device_type = _int(_cell(row, c_id)), _int(_cell(row, c_type))
            if not imei or device_id is None or device_type is None:
                result.skipped.append(f"row {number}")
                continue
            conn.execute("""
                INSERT INTO cota_device (imei, device_id, device_type, source, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(imei) DO UPDATE SET device_id = excluded.device_id,
                    device_type = excluded.device_type, source = excluded.source,
                    updated_at = excluded.updated_at
            """, (imei, device_id, device_type, Path(path).name, now))
            if imei in known:
                result.updated += 1
            else:
                result.added += 1
                known.add(imei)
    return result


# ─── planning a job ─────────────────────────────────────────────────────────

@dataclass
class PlannedTask:
    seq: int
    imei: str
    cmd_type: int
    params: dict[str, str]
    device_id: int | None = None
    device_type: int | None = None
    batch_no: int | None = None


@dataclass
class Plan:
    tasks: list[PlannedTask]
    problems: list[str]                  # rows that could not be read at all
    duplicates: int = 0

    @property
    def mapped(self) -> list[PlannedTask]:
        return [t for t in self.tasks if t.device_id is not None]

    @property
    def unmapped(self) -> list[str]:
        return sorted({t.imei for t in self.tasks if t.device_id is None})

    @property
    def batches(self) -> int:
        return len({t.batch_no for t in self.mapped})

    @property
    def devices(self) -> int:
        return len({t.imei for t in self.mapped})


def read_commands(path: Path) -> tuple[list[PlannedTask], list[str], int]:
    """Rows of IMEI, Command (type code), Val1 … Val9. Anything else in the sheet is ignored."""
    positions, rows = _read_sheet(path)
    c_imei = _column(positions, IMEI_HEADERS, "IMEI", path)
    c_type = _column(positions, COMMAND_HEADERS, "Command (type code)", path)
    c_vals = [(f"val{n}", positions[f"val{n}"]) for n in range(1, MAX_VALUES + 1)
              if f"val{n}" in positions]
    if not c_vals:
        raise CotaError(f"{Path(path).name} has no Val1 column.")

    tasks: list[PlannedTask] = []
    problems: list[str] = []
    seen: set[tuple] = set()
    duplicates = 0
    for number, row in enumerate(rows, start=2):
        imei, cmd_type = _imei(_cell(row, c_imei)), _int(_cell(row, c_type))
        if not imei or cmd_type is None:
            problems.append(f"row {number}: needs an IMEI and a numeric command type")
            continue
        params = {key: v for key, i in c_vals if (v := _value(_cell(row, i))) is not None}
        if not params:
            problems.append(f"row {number}: no value given")
            continue
        key = (imei, cmd_type, json.dumps(params, sort_keys=True))
        if key in seen:
            duplicates += 1
            continue
        seen.add(key)
        tasks.append(PlannedTask(seq=number, imei=imei, cmd_type=cmd_type, params=params))
    return tasks, problems, duplicates


def plan(conn, path: Path, max_per_call: int = MAX_DEVICES_PER_CALL) -> Plan:
    """Resolve IMEIs and group tasks into API calls. Touches no network.

    One call carries one command to many devices of one type, so tasks sharing (device type,
    command, values) go into the same call, up to `max_per_call` devices. Calls are ordered by
    the first sheet row they contain, so a device given two commands receives them in sheet order.
    """
    tasks, problems, duplicates = read_commands(path)
    mapping = {r["imei"]: (r["device_id"], r["device_type"])
               for r in conn.execute("SELECT imei, device_id, device_type FROM cota_device")}

    groups: dict[tuple, list[PlannedTask]] = {}
    for task in tasks:
        if task.imei in mapping:
            task.device_id, task.device_type = mapping[task.imei]
            key = (task.device_type, task.cmd_type, json.dumps(task.params, sort_keys=True))
            groups.setdefault(key, []).append(task)

    batch_no = 0
    for members in groups.values():                 # dicts keep first-seen order
        for start in range(0, len(members), max_per_call):
            batch_no += 1
            for task in members[start:start + max_per_call]:
                task.batch_no = batch_no
    return Plan(tasks=tasks, problems=problems, duplicates=duplicates)


def create_job(conn, the_plan: Plan, *, name: str | None = None,
               source_file: str | None = None) -> int:
    """Store a plan as a job. Every task starts `planned`, or `unmapped` if its IMEI is unknown."""
    with conn:
        job_id = conn.execute(
            "INSERT INTO cota_job (name, source_file, created_at) VALUES (?, ?, ?)",
            (name, source_file, _now())).lastrowid
        conn.executemany("""
            INSERT INTO cota_task (job_id, seq, imei, device_id, device_type, cmd_type, params,
                                   batch_no, state)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, [(job_id, t.seq, t.imei, t.device_id, t.device_type, t.cmd_type,
               json.dumps(t.params), t.batch_no,
               "planned" if t.device_id is not None else "unmapped")
              for t in the_plan.tasks])
    return job_id


# ─── sending ────────────────────────────────────────────────────────────────

def payload_for(device_type: int, device_ids: list[int], cmd_type: int, params: dict) -> dict:
    """The body saveCOTAConfig expects — the shape captured from the portal."""
    return {"deviceType": [device_type], "deviceList": device_ids, "type": cmd_type, **params}


@dataclass
class SendResult:
    calls: int = 0
    sent: int = 0
    failed: int = 0


def pending_calls(conn, job_id: int, canary: int | None = None) -> list[list]:
    """The job's unsent tasks, grouped by call. With `canary`, only the first N devices."""
    rows = conn.execute("""
        SELECT id, seq, imei, device_id, device_type, cmd_type, params, batch_no
        FROM cota_task WHERE job_id = ? AND state = 'planned' ORDER BY seq
    """, (job_id,)).fetchall()
    if canary is not None:
        chosen: list[str] = []
        for row in rows:
            if row["imei"] not in chosen and len(chosen) < canary:
                chosen.append(row["imei"])
        rows = [r for r in rows if r["imei"] in chosen]

    calls: dict[int, list] = {}
    for row in rows:
        calls.setdefault(row["batch_no"], []).append(row)
    return list(calls.values())


def send_job(conn, job_id: int, client, *, canary: int | None = None,
             interval: float = CALL_INTERVAL_SECONDS) -> SendResult:
    """Send the job's planned tasks, one API call per batch, recording each reply as it lands.

    A rejected token stops the job at once (CotaError) with everything sent so far recorded;
    what is left stays `planned`, so running it again carries on where it stopped.
    """
    result = SendResult()
    for number, call in enumerate(pending_calls(conn, job_id, canary)):
        if number and interval:
            time.sleep(interval)
        first = call[0]
        body = payload_for(first["device_type"], [r["device_id"] for r in call],
                           first["cmd_type"], json.loads(first["params"]))
        sent_at = _now()
        error = None
        try:
            status, reply = client.send(body)
        except CotaError as exc:
            if "rejected the token" in str(exc):
                raise
            status, reply, error = None, None, str(exc)
        ok = status is not None and 200 <= status < 300
        if status is not None and not ok:
            error = f"HTTP {status}"
        with conn:
            conn.executemany("""
                UPDATE cota_task SET state = ?, sent_at = ?, http_status = ?, send_reply = ?,
                                     error = ?
                WHERE id = ?
            """, [("sent" if ok else "send_failed", sent_at, status, reply, error, r["id"])
                  for r in call])
        result.calls += 1
        if ok:
            result.sent += len(call)
        else:
            result.failed += len(call)
    return result


# ─── collecting replies ─────────────────────────────────────────────────────

@dataclass
class PollResult:
    devices: int = 0
    failed: int = 0


def poll_job(conn, job_id: int, client, *, interval: float = CALL_INTERVAL_SECONDS,
             now: datetime | None = None) -> PollResult:
    """Ask the cloud what each device of the job received and replied, and keep it raw.

    The window runs from just before the device's first send in this job to now, so a reply that
    arrives late is still inside it on the next poll.
    """
    now = now or datetime.now()
    devices = conn.execute("""
        SELECT device_id, MIN(sent_at) AS first_sent FROM cota_task
        WHERE job_id = ? AND state = 'sent' GROUP BY device_id ORDER BY MIN(seq)
    """, (job_id,)).fetchall()

    result = PollResult()
    for number, row in enumerate(devices):
        if number and interval:
            time.sleep(interval)
        start = int((_parse(row["first_sent"]) - POLL_MARGIN).timestamp())
        end = int((now + POLL_MARGIN).timestamp())
        try:
            status, raw = client.responses(row["device_id"], start, end)
        except CotaError as exc:
            if "rejected the token" in str(exc):
                raise
            status, raw = None, None
        records = None
        if raw:
            try:
                found = sources.find_records(json.loads(raw))
                records = len(found) if found else 0
            except ValueError:
                pass
        with conn:
            conn.execute("""
                INSERT INTO cota_poll (job_id, device_id, polled_at, window_from, window_to,
                                       http_status, records, raw)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (job_id, row["device_id"], _now(), start, end, status, records, raw))
        result.devices += 1
        if status is None or not 200 <= status < 300:
            result.failed += 1
    return result


# ─── reading jobs back ──────────────────────────────────────────────────────

def jobs(conn) -> list[dict]:
    return [dict(r) for r in conn.execute("""
        SELECT j.id, j.name, j.source_file, j.created_at,
               COUNT(t.id)                                   AS tasks,
               COUNT(DISTINCT t.imei)                        AS devices,
               SUM(t.state = 'planned')                      AS planned,
               SUM(t.state = 'sent')                         AS sent,
               SUM(t.state = 'send_failed')                  AS send_failed,
               SUM(t.state = 'unmapped')                     AS unmapped
        FROM cota_job j LEFT JOIN cota_task t ON t.job_id = j.id
        GROUP BY j.id ORDER BY j.id DESC
    """)]


TASK_COLUMNS = [
    ("imei", "IMEI"),
    ("device_id", "Device ID"),
    ("device_type", "Device type"),
    ("cmd_type", "Command"),
    ("params", "Values"),
    ("state", "State"),
    ("sent_at", "Sent at"),
    ("http_status", "HTTP"),
    ("send_reply", "Send reply"),
    ("error", "Error"),
    ("polled_at", "Last polled"),
    ("records", "Entries in reply"),
    ("response_raw", "Device reply (raw)"),
    ("seq", "Sheet row"),
]


def task_rows(conn, job_id: int) -> list[dict]:
    """Every task of a job with the latest poll for its device beside it."""
    return [dict(r) for r in conn.execute("""
        SELECT t.imei, t.device_id, t.device_type, t.cmd_type, t.params, t.state, t.sent_at,
               t.http_status, t.send_reply, t.error, p.polled_at, p.records,
               p.raw AS response_raw, t.seq
        FROM cota_task t
        LEFT JOIN cota_poll p ON p.id = (SELECT MAX(id) FROM cota_poll
                                         WHERE job_id = t.job_id AND device_id = t.device_id)
        WHERE t.job_id = ? ORDER BY t.seq
    """, (job_id,))]


def export_job(conn, job_id: int) -> bytes:
    """The job as a spreadsheet: one row per device × command, replies beside them."""
    rows = task_rows(conn, job_id)
    for row in rows:
        for key in ("send_reply", "response_raw"):
            if row[key] and len(row[key]) > XLSX_CELL_LIMIT:
                row[key] = row[key][:XLSX_CELL_LIMIT] + " …(truncated — full reply in database)"
    return exports.to_xlsx(rows, TASK_COLUMNS, sheet_name=f"COTA job {job_id}")


# ─── helpers ────────────────────────────────────────────────────────────────

TIME_FORMAT = "%Y-%m-%d %H:%M:%S"


def _now() -> str:
    return datetime.now().strftime(TIME_FORMAT)


def _parse(text: str) -> datetime:
    return datetime.strptime(text, TIME_FORMAT)
