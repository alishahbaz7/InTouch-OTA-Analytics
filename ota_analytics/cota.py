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

    def __init__(self, token: str, base_url: str = BASE_URL, timeout: float = 30.0,
                 on_auth=None):
        import httpx

        if not token:
            raise CotaError(f"Not signed in to the cloud. Sign in from the dashboard's "
                            f"\"Cloud: …\" chip (or set {ENV_TOKEN}).")
        self.base_url = base_url.rstrip("/")
        # Told on every answer whether the cloud accepted the token, so the sign-in chip can say
        # "session expired" the moment it happens and clear itself when a fresh one works.
        self._on_auth = on_auth
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
            if self._on_auth:
                self._on_auth(False, response.status_code)
            raise CotaError(f"The cloud rejected the token (HTTP {response.status_code}). "
                            "It has most likely expired — sign in again with a fresh token "
                            "from the portal.")
        if self._on_auth and 200 <= response.status_code < 300:
            self._on_auth(True, response.status_code)
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


# ─── one command, typed in ──────────────────────────────────────────────────
# The Devices tab's "Send a command" form: device type, device ids, command type and values,
# exactly the fields of the portal's own request. It becomes a job like any sheet does, so it is
# sent and recorded by the same code and shows up under Jobs with its reply.

MAX_MANUAL_DEVICES = 200       # a typed list; anything bigger belongs in a sheet


def parse_device_ids(text: str) -> tuple[list[int], list[str]]:
    """Device ids separated by commas, spaces, semicolons or new lines. Order kept, repeats
    dropped; anything that is not a whole number is returned rather than silently skipped."""
    import re

    ids: list[int] = []
    bad: list[str] = []
    for part in re.split(r"[\s,;]+", (text or "").strip().strip("[]")):
        if not part:
            continue
        if part.isdigit():
            if int(part) not in ids:
                ids.append(int(part))
        else:
            bad.append(part)
    return ids, bad


def parse_portal_payload(text: str) -> dict:
    """Read a saveCOTAConfig body however it was copied: plain JSON, a JSON string, or the
    Windows "Copy as cURL (cmd)" form where every quote and brace carries a ^ escape:

        ^"^{^\\^"deviceType^\\^":^[124^],...^}^"

    Returns {device_type, device_ids, cmd_type, params}. Raises CotaError with a readable
    reason; nothing here touches the network.
    """
    raw = (text or "").strip()
    if not raw:
        raise CotaError("Paste the payload first.")
    raw = raw.replace("^", "")                       # cmd.exe's escape character
    data = None
    for _ in range(2):                               # a JSON string wrapping the JSON object
        try:
            data = json.loads(raw)
        except ValueError:
            if raw.startswith('"') and raw.endswith('"'):
                raw = raw[1:-1].replace('\\"', '"')
                continue
            raise CotaError("That is not a payload this tool can read — expected JSON like "
                            '{"deviceType":[124],"deviceList":[14906],"type":36,'
                            '"val1":"..."}.') from None
        if isinstance(data, str):
            raw = data
            continue
        break
    if not isinstance(data, dict):
        raise CotaError("The payload should be one JSON object.")

    def ints(value, label):
        values = value if isinstance(value, list) else [value]
        try:
            return [int(v) for v in values]
        except (TypeError, ValueError):
            raise CotaError(f"{label} should be numbers.") from None

    types = ints(data.get("deviceType"), "deviceType")
    if len(types) != 1:
        raise CotaError("The payload should carry exactly one deviceType.")
    if data.get("type") is None:
        raise CotaError("The payload has no command type (\"type\").")
    params = {f"val{n}": str(data[f"val{n}"]) for n in range(1, MAX_VALUES + 1)
              if data.get(f"val{n}") not in (None, "")}
    return {"device_type": types[0],
            "device_ids": ints(data.get("deviceList") or [], "deviceList"),
            "cmd_type": ints(data["type"], "type")[0],
            "params": params}


def manual_plan(conn, device_type: int, device_ids: list[int], cmd_type: int,
                params: dict[str, str], max_per_call: int = MAX_DEVICES_PER_CALL) -> Plan:
    """A typed command as a plan: one task per device id, batched into calls.

    The cloud's device id is used as given — it is the cloud's own key, so a device the local
    map does not know is still reachable. The map only supplies the IMEI for the record.
    """
    if not device_ids:
        raise CotaError("Enter at least one device id.")
    if len(device_ids) > MAX_MANUAL_DEVICES:
        raise CotaError(f"{len(device_ids)} devices typed in; the form takes up to "
                        f"{MAX_MANUAL_DEVICES}. Use a job sheet for more.")
    if not params.get("val1"):
        raise CotaError("Enter the command value (val1).")
    known = {r["device_id"]: r["imei"] for r in conn.execute(
        f"SELECT device_id, imei FROM cota_device WHERE device_id IN "
        f"({','.join('?' * len(device_ids))})", device_ids)}
    tasks = []
    for seq, device_id in enumerate(device_ids, start=1):
        tasks.append(PlannedTask(seq=seq, imei=known.get(device_id, ""), cmd_type=cmd_type,
                                 params=dict(params), device_id=device_id,
                                 device_type=device_type,
                                 batch_no=(seq - 1) // max_per_call + 1))
    return Plan(tasks=tasks, problems=[])


def plan_payloads(the_plan: Plan) -> list[dict]:
    """The exact body of every call the plan will make, in order — what Preview shows."""
    calls: dict[int, list[PlannedTask]] = {}
    for task in the_plan.mapped:
        calls.setdefault(task.batch_no, []).append(task)
    return [payload_for(c[0].device_type, [t.device_id for t in c], c[0].cmd_type, c[0].params)
            for c in calls.values()]


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
        ok = _send_call(conn, client, call)
        result.calls += 1
        if ok:
            result.sent += len(call)
        else:
            result.failed += len(call)
    return result


def _send_call(conn, client, call: list, *, stop_on_rejected_token: bool = True) -> bool:
    """Make one saveCOTAConfig call for tasks that share it, and record the outcome on each.

    A rejected token is raised by default, so a job stops instead of failing every remaining
    call the same way. The console records it on the task instead: one command, one answer.
    """
    first = call[0]
    body = payload_for(first["device_type"], [r["device_id"] for r in call],
                       first["cmd_type"], json.loads(first["params"]))
    sent_at = _now()
    error = None
    try:
        status, reply = client.send(body)
    except CotaError as exc:
        if stop_on_rejected_token and "rejected the token" in str(exc):
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
    return ok


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
        status, _ = poll_device(conn, job_id, row["device_id"], client, start, end)
        result.devices += 1
        if status is None or not 200 <= status < 300:
            result.failed += 1
    return result


def poll_device(conn, job_id: int, device_id: int, client, start: int,
                end: int) -> tuple[int | None, str | None]:
    """One getGPRSCommand call for one device over [start, end], kept raw in cota_poll.

    A rejected token is raised; any other failure is recorded as a poll with no status, so the
    record shows that a check was attempted and did not get an answer.
    """
    try:
        status, raw = client.responses(device_id, start, end)
    except CotaError as exc:
        if "rejected the token" in str(exc):
            raise
        status, raw = None, None
    found = reply_records(raw)
    with conn:
        conn.execute("""
            INSERT INTO cota_poll (job_id, device_id, polled_at, window_from, window_to,
                                   http_status, records, raw)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (job_id, device_id, _now(), start, end, status,
              None if found is None else len(found), raw))
    return status, raw


# ─── the console: one device, one command at a time ─────────────────────────
# The Configure page is a conversation with one device. Commands go out as tasks of that
# device's own console job; every check is a cota_poll row of the same job; and every command
# record the cloud returns is kept in cota_command, matched to the task that caused it.

CONSOLE_SOURCE = "console:{device_id}"
# After a send the page checks on its own every CONSOLE_AUTO_EVERY seconds, CONSOLE_AUTO_TRIES
# times, then leaves it to the refresh button. The live device answered within a minute.
CONSOLE_AUTO_EVERY = 30
CONSOLE_AUTO_TRIES = 3
CONSOLE_WATCH_SECONDS = CONSOLE_AUTO_EVERY * CONSOLE_AUTO_TRIES + 10

# The time range a console shows and asks the cloud for. Day-first and 24-hour, like every date
# in this tool; the browser's own date-time picker would follow the PC's locale instead and
# could show 12:00 AM. The cloud's limit is unknown, so the span is capped.
RANGE_FORMAT = "%d-%m-%Y %H:%M"
MAX_RANGE_DAYS = 15

# Locked defaults for this first step, set by the user: model (deviceType) 124 and command
# type 36. Shown pre-filled and locked, with an Edit to unlock — a default, not a hard-code.
DEFAULT_DEVICE_TYPE = 124
DEFAULT_CMD_TYPE = 36

# A cloud record is the command our send created when it is for the same device and type and
# its "timestamp" is within this many seconds of our send. The cloud rewrites val1, so the value
# cannot be compared; time and type are what both sides agree on.
MATCH_SECONDS = 120

# The three stages a command moves through, in order — the console's ticks. All three are read
# from two calls: the send's own reply (✓), and getGPRSCommand's record of the command, which
# appears with status 0 and response null (white ✓✓) and later carries the device's answer in
# `response` with status 1 (green ✓✓). Confirmed on the live cloud, 2026-10-06.
STAGES = {
    "failed": "Not accepted by the cloud",
    "accepted": "Command via API — the cloud accepted the send",
    "api": "Response via API — the cloud holds the command for the device",
    "delivered": "Response via API — the cloud delivered it; the device has not answered",
    "device": "Response via device — the device answered",
}

# The cloud sets status 1 and writes this into `response` when it has *delivered* a command —
# not when the device answered. Seen on 2 of the first 7 live commands (2026-10-06), and shown
# as answered until this was spotted. It is the cloud's note, never the device's words.
import re as _re
_DELIVERY_NOTE = _re.compile(r"^\s*Command sent at \d{4}-\d{2}-\d{2} \d{2}:\d{2}(:\d{2})?\s*$")


def is_delivery_note(text) -> bool:
    return bool(text) and bool(_DELIVERY_NOTE.match(str(text)))

# getGPRSCommand's `status`, as the portal's own COTA screen reads it: its "Pending Count"
# counts status 0, and status 1 rows carry the Response. Anything else is shown as given.
RECORD_STATUS = {0: "waiting for the device", 1: "device answered"}

# Command type names, as the portal's COTA screen shows them. Add one when it is seen there.
COMMAND_NAMES = {36: "Zenithra Command"}


# A command (val1) reads as <operation byte> D7 <parameter> [D5 values…]. Operations and the
# parameters named so far, confirmed by the user on the desk device (2026-10-06). Add a name
# here when one is confirmed; an unnamed parameter shows its code.
COMMAND_OPERATIONS = {"DA": "GET", "DB": "SET", "DD": "CLR"}
COMMAND_PARAMETERS = {"6F4B": "FTP_SETTINGS", "6D66": "SOS"}


# The user's own names, from the command library (cota_library.refresh fills it): a saved
# command's name for that exact command, a parameter's name for every command on it.
LIBRARY: dict[str, dict[str, str]] = {"parameters": {}, "commands": {}}


def parameter_name(code: str) -> str:
    """The user's name for a parameter, else the built-in one, else its code."""
    code = code.upper()
    return LIBRARY["parameters"].get(code) or COMMAND_PARAMETERS.get(code, code)


def describe_command(val1) -> str | None:
    """A saved command's name; else 'GET FTP_SETTINGS', 'CLR SOS', 'SET 6C0A' — or None when it
    is not shaped like a command."""
    value = "".join(str(val1 or "").split()).upper()
    saved = LIBRARY["commands"].get(value)
    if saved:
        return saved
    if len(value) < 8 or value[2:4] != "D7" or value[:2] not in COMMAND_OPERATIONS:
        return None
    return f"{COMMAND_OPERATIONS[value[:2]]} {parameter_name(value[4:8])}"


def command_name(cmd_type) -> str:
    """'Zenithra Command' for a known type, 'Command 12' for any other."""
    return COMMAND_NAMES.get(cmd_type) or f"Command {cmd_type}"

# How the device's own words read, for when its answer arrives. A reading, labelled as one in
# the UI — not a verdict.
_FAILED_WORDS = ("FAIL", "FAILED", "ERROR", "INVALID", "NACK", "REJECT", "REJECTED")
_OK_WORDS = ("OK", "SUCCESS", "SUCCESSFUL", "DONE", "ACK", "ACCEPTED")


def console_job(conn, device_id: int, *, create: bool = True) -> int | None:
    """The job that holds this device's console history, made on first use."""
    source = CONSOLE_SOURCE.format(device_id=device_id)
    row = conn.execute("SELECT id FROM cota_job WHERE source_file = ? ORDER BY id LIMIT 1",
                       (source,)).fetchone()
    if row or not create:
        return row["id"] if row else None
    with conn:
        return conn.execute(
            "INSERT INTO cota_job (name, source_file, created_at) VALUES (?, ?, ?)",
            (f"Console · device {device_id}", source, _now())).lastrowid


def resolve_device(conn, text: str) -> tuple[int, int | None, str | None]:
    """(device_id, device_type, imei) from what was typed: a cloud device id, or an IMEI the
    device map knows. An IMEI is never turned into a device id by guessing."""
    text = (text or "").strip()
    if not text.isdigit():
        raise CotaError("Enter a device id (e.g. 14906) or an IMEI from the device map.")
    if len(text) >= 14:                                   # IMEIs are 15 digits; ids are short
        row = conn.execute("SELECT device_id, device_type FROM cota_device WHERE imei = ?",
                           (text,)).fetchone()
        if not row:
            raise CotaError(f"IMEI {text} is not in the device map, so its cloud device id is "
                            "not known. Enter the device id, or load the map on Devices.")
        return row["device_id"], row["device_type"], text
    device_id = int(text)
    return device_id, device_type_for(conn, device_id), imei_for(conn, device_id)


def device_type_for(conn, device_id: int) -> int | None:
    """The type this device was last sent with, else the device map's."""
    row = conn.execute("SELECT device_type FROM cota_task WHERE device_id = ? AND device_type "
                       "IS NOT NULL ORDER BY id DESC LIMIT 1", (device_id,)).fetchone()
    if row:
        return row["device_type"]
    row = conn.execute("SELECT device_type FROM cota_device WHERE device_id = ? LIMIT 1",
                       (device_id,)).fetchone()
    return row["device_type"] if row else None


def imei_for(conn, device_id: int) -> str | None:
    """From the device map — which the console also fills itself, from the cloud's records."""
    row = conn.execute("SELECT imei FROM cota_device WHERE device_id = ? LIMIT 1",
                       (device_id,)).fetchone()
    return row["imei"] if row else None


def console_send(conn, client, device_id: int, device_type: int, cmd_type: int,
                 params: dict[str, str]) -> dict:
    """Send one command to one device and record it. Returns the task as stored.

    Unlike a job, a rejected token is recorded on the task rather than raised: the console
    shows each command's own outcome, and nothing is left `planned` to go out by surprise with
    the next send.
    """
    if not params.get("val1"):
        raise CotaError("Enter the command value (val1).")
    job_id = console_job(conn, device_id)
    with conn:
        seq = conn.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM cota_task WHERE job_id = ?",
                           (job_id,)).fetchone()[0]
        task_id = conn.execute("""
            INSERT INTO cota_task (job_id, seq, imei, device_id, device_type, cmd_type, params,
                                   batch_no, state)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'planned')
        """, (job_id, seq, imei_for(conn, device_id) or "", device_id, device_type, cmd_type,
              json.dumps(params), seq)).lastrowid
    row = conn.execute("SELECT * FROM cota_task WHERE id = ?", (task_id,)).fetchone()
    _send_call(conn, client, [row], stop_on_rejected_token=False)
    return dict(conn.execute("SELECT * FROM cota_task WHERE id = ?", (task_id,)).fetchone())


def day_range(day: datetime | None = None) -> tuple[datetime, datetime]:
    """00:00 to 23:59:59 of a day — the default view, and what the portal asks the cloud for."""
    day = (day or datetime.now()).replace(hour=0, minute=0, second=0, microsecond=0)
    return day, day + timedelta(days=1, seconds=-1)


def _read_moment(text: str, *, end: bool) -> datetime | None:
    text = (text or "").strip()
    if not text:
        return None
    for fmt in (RANGE_FORMAT, "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%d-%m-%Y", "%Y-%m-%d"):
        try:
            moment = datetime.strptime(text, fmt)
        except ValueError:
            continue
        if "%H" not in fmt:                         # a date alone means the whole day
            return moment + timedelta(days=1, seconds=-1) if end else moment
        # A minute typed as the end includes all of it: 23:59 means up to 23:59:59.
        return moment + timedelta(seconds=59) if end else moment
    raise CotaError(f"Could not read '{text}' as a time — use DD-MM-YYYY HH:MM, "
                    f"e.g. {datetime.now():%d-%m-%Y} 00:00.")


def parse_range(start_text: str = "", end_text: str = "", *,
                now: datetime | None = None) -> tuple[datetime, datetime, str | None]:
    """The console's time range from what was typed, with a note when it had to be adjusted.

    Blank means today, 00:00 to 23:59. An end before the start is refused; a span over
    MAX_RANGE_DAYS keeps the start and is cut to the limit, because the cloud's own limit is
    unknown and the first sign of it would be a failed check.
    """
    today_start, today_end = day_range(now)
    start = _read_moment(start_text, end=False) or today_start
    end = _read_moment(end_text, end=True) or (
        day_range(start)[1] if start_text and not end_text else today_end)
    if end <= start:
        raise CotaError("The end of the range is before its start.")
    note = None
    if end - start > timedelta(days=MAX_RANGE_DAYS):
        end = start + timedelta(days=MAX_RANGE_DAYS, seconds=-1)
        note = (f"Ranges are limited to {MAX_RANGE_DAYS} days — showing "
                f"{start:{RANGE_FORMAT}} to {end:{RANGE_FORMAT}}.")
    return start, end, note


def range_after_send(start: datetime, now: datetime | None = None) -> tuple[datetime, datetime]:
    """After a send the view must include it: the start stays, the end moves to the end of
    today — unless that would exceed the limit, in which case the start moves up instead."""
    end = day_range(now)[1]
    if start > end:
        start = day_range(now)[0]
    if end - start > timedelta(days=MAX_RANGE_DAYS):
        start = end - timedelta(days=MAX_RANGE_DAYS, seconds=-1)
    return start, end


def console_check(conn, client, device_id: int, start: datetime | None = None,
                  end: datetime | None = None) -> dict:
    """Ask the cloud for this device's command records over the range (today by default),
    keep them, and match them to sends."""
    job_id = console_job(conn, device_id)
    if start is None or end is None:
        start, end = day_range()
    window = (int(start.timestamp()), int(end.timestamp()))
    status, raw = poll_device(conn, job_id, device_id, client, *window)
    ok = status is not None and 200 <= status < 300
    records = reply_records(raw) if ok else None
    stored = store_cloud_records(conn, device_id, records, window=window) \
        if records is not None else {"records": 0}
    return {"status": status, "ok": ok, **stored}


def reply_records(raw: str | None) -> list[dict] | None:
    """The list of records in a getGPRSCommand body, or None when it holds no list at all.

    An empty `data` list is an answer too — the period holds nothing — and is returned as [],
    not None: only a body that says nothing either way may leave stored records alone.
    """
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except ValueError:
        return None
    found = sources.find_records(payload)
    if found is not None:
        return found
    if isinstance(payload, list) and not payload:
        return []
    if isinstance(payload, dict) and isinstance(payload.get("data"), list) and not payload["data"]:
        return []
    return None


def _field(record: dict, name: str):
    """A record's field by name, whatever its capitalisation."""
    lowered = {str(k).lower(): v for k, v in record.items()}
    return lowered.get(name.lower())


def _epoch(value) -> int | None:
    """Epoch seconds from seconds or milliseconds, as a number or digits."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, str):
        value = value.strip()
        if not value.isdigit():
            return None
        value = int(value)
    if isinstance(value, (int, float)):
        return int(value / 1000 if value > 1e11 else value)
    return None


def _text(value) -> str | None:
    if value is None or value == "":
        return None
    return json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value)


def _answered_at(value, now: str) -> str:
    """When the device answered: the record's responseTime if it carries one this can read,
    else now — the moment this install first saw the answer. The live cloud sends
    responseTime null even after answering, so in practice it is the second."""
    epoch = _epoch(value)
    if epoch:
        return datetime.fromtimestamp(epoch).strftime(TIME_FORMAT)
    if isinstance(value, str):
        for fmt in (TIME_FORMAT, "%Y-%m-%dT%H:%M:%S", "%d-%m-%Y %H:%M:%S"):
            try:
                return datetime.strptime(value.strip()[:19], fmt).strftime(TIME_FORMAT)
            except ValueError:
                continue
    return now


def store_cloud_records(conn, device_id: int, records: list[dict],
                        window: tuple[int, int] | None = None) -> dict:
    """Keep the cloud's command records, match each to the send that caused it, and learn the
    device's IMEI from them.

    Upserted by the cloud's id, so asking again never duplicates a command. `first_seen_at` is
    kept from the first time — when the command reached white ✓✓ — and the device's answer,
    once a record carries one, is kept from the first time too: a later record can update it
    but never take it away, and its time does not move.
    """
    now = _now()
    learned = None
    seen_ids = []
    with conn:
        for record in records:
            rec_device = _epoch(_field(record, "deviceId")) or device_id
            if rec_device != device_id:
                continue                                  # not this device's record
            cmd_type = _epoch(_field(record, "type"))
            sent_epoch = _epoch(_field(record, "timestamp"))
            cloud_id = _text(_field(record, "id")) or f"{device_id}:{sent_epoch}:{cmd_type}"
            seen_ids.append(cloud_id)
            imei = _text(_field(record, "imei"))
            if imei and imei.isdigit():
                learned = learned or imei
            answer = _text(_field(record, "response"))
            if is_delivery_note(answer):
                answer = None               # the cloud's delivery note, not the device's answer
            conn.execute("""
                INSERT INTO cota_command (cloud_id, device_id, imei, cmd_type, sent_epoch, val1,
                                          status, api_response, api_response_time,
                                          first_seen_at, last_seen_at, raw,
                                          device_response, device_response_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(cloud_id) DO UPDATE SET
                    imei = excluded.imei, val1 = excluded.val1, status = excluded.status,
                    api_response = excluded.api_response,
                    api_response_time = excluded.api_response_time,
                    last_seen_at = excluded.last_seen_at, raw = excluded.raw,
                    device_response = COALESCE(excluded.device_response,
                                               cota_command.device_response),
                    device_response_at = COALESCE(cota_command.device_response_at,
                                                  excluded.device_response_at)
            """, (cloud_id, device_id, imei, cmd_type, sent_epoch,
                  _text(_field(record, "val1")), _field(record, "status"),
                  _text(_field(record, "response")), _text(_field(record, "responseTime")),
                  now, now, json.dumps(record, ensure_ascii=False),
                  answer, _answered_at(_field(record, "responseTime"), now) if answer else None))
        # The answer is the cloud's whole list for the window: a record stored from an earlier
        # check that is in the window but not in this answer has left the cloud. Marked, not
        # deleted — it was real, and the record says when it went.
        if window is not None:
            marks = ",".join("?" * len(seen_ids))
            not_seen = f"AND cloud_id NOT IN ({marks})" if seen_ids else ""
            gone = conn.execute(f"""
                UPDATE cota_command SET missing_since = ?
                WHERE device_id = ? AND sent_epoch BETWEEN ? AND ? AND missing_since IS NULL
                  {not_seen}
            """, (now, device_id, *window, *seen_ids)).rowcount
        else:
            gone = 0
        if seen_ids:
            conn.execute(f"UPDATE cota_command SET missing_since = NULL "
                         f"WHERE cloud_id IN ({','.join('?' * len(seen_ids))})", seen_ids)
    matched = _match_records(conn, device_id)
    if learned:
        _learn_imei(conn, device_id, learned)
    return {"records": len(records), "matched": matched, "imei": learned, "gone": gone}


def _match_records(conn, device_id: int) -> int:
    """Pair unmatched sends with unmatched cloud records: same type, within MATCH_SECONDS, one to
    one, in the order they were sent. Closest pairs decide *which* sends and records belong
    together; time order decides which goes with which."""
    tasks = conn.execute("""
        SELECT t.id, t.cmd_type, t.sent_at FROM cota_task t
        WHERE t.device_id = ? AND t.state = 'sent' AND t.sent_at IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM cota_command c WHERE c.task_id = t.id)
    """, (device_id,)).fetchall()
    records = conn.execute("""
        SELECT cloud_id, cmd_type, sent_epoch FROM cota_command
        WHERE device_id = ? AND task_id IS NULL AND sent_epoch IS NOT NULL
    """, (device_id,)).fetchall()
    sent_at = {t["id"]: _parse(t["sent_at"]).timestamp() for t in tasks}
    epoch = {r["cloud_id"]: r["sent_epoch"] for r in records}
    pairs = []
    for t in tasks:
        for r in records:
            if r["cmd_type"] is not None and r["cmd_type"] != t["cmd_type"]:
                continue
            gap = abs(r["sent_epoch"] - sent_at[t["id"]])
            if gap <= MATCH_SECONDS:
                pairs.append((gap, t["id"], r["cloud_id"]))
    chosen, used_tasks, used_records = [], set(), set()
    for _, task_id, cloud_id in sorted(pairs):
        if task_id not in used_tasks and cloud_id not in used_records:
            chosen.append((task_id, cloud_id))
            used_tasks.add(task_id)
            used_records.add(cloud_id)

    # Closest-first can cross two quick sends over — the first send taking the second record —
    # and the cloud keeps commands in the order they were sent. So the chosen sends and records
    # are lined up in time order. Uncrossing never widens the largest gap, so every pair stays
    # inside MATCH_SECONDS; it is checked anyway, and the closest-first pairs kept if not.
    by_time = sorted((t for t, _ in chosen), key=lambda t: (sent_at[t], t))
    in_order = sorted((c for _, c in chosen), key=lambda c: (epoch[c], c))
    ordered = list(zip(by_time, in_order))
    if all(abs(epoch[c] - sent_at[t]) <= MATCH_SECONDS for t, c in ordered):
        chosen = ordered
    with conn:
        for task_id, cloud_id in chosen:
            conn.execute("UPDATE cota_command SET task_id = ? WHERE cloud_id = ?",
                         (task_id, cloud_id))
    return len(chosen)


def _learn_imei(conn, device_id: int, imei: str) -> None:
    """Put the IMEI the cloud reported into the device map — unless the map already says
    something about this device or this IMEI, which a person put there and which wins."""
    taken = conn.execute("SELECT 1 FROM cota_device WHERE device_id = ? OR imei = ?",
                         (device_id, imei)).fetchone()
    with conn:
        if not taken:
            conn.execute("""
                INSERT INTO cota_device (imei, device_id, device_type, source, updated_at)
                VALUES (?, ?, ?, 'cloud record', ?)
            """, (imei, device_id, device_type_for(conn, device_id) or DEFAULT_DEVICE_TYPE,
                  _now()))
        # Commands sent before the IMEI was known get it too, so exports carry it.
        conn.execute("UPDATE cota_task SET imei = ? WHERE device_id = ? AND imei = ''",
                     (imei_for(conn, device_id) or imei, device_id))


def reading(text: str | None) -> str | None:
    """'failed' / 'ok' when a device's answer says so in its own words, else None. Failure wins
    a tie: "SET TIMERS:KEEP,FAIL,KEEP FAILED" must not read as fine because it also says KEEP."""
    if not text:
        return None
    import re

    words = set(re.findall(r"[A-Z]+", str(text).upper()))
    if words & set(_FAILED_WORDS):
        return "failed"
    if words & set(_OK_WORDS):
        return "ok"
    return None


def _cloud_view(row) -> dict | None:
    """The cloud's side of a command, for the bubble that says the API responded."""
    if not row or row["cloud_id"] is None:
        return None
    answer = None if is_delivery_note(row["device_response"]) else row["device_response"]
    delivered = is_delivery_note(row["api_response"]) or is_delivery_note(row["device_response"])
    try:
        record = json.loads(row["raw"])
    except (TypeError, ValueError):
        record = {}
    return {"id": row["cloud_id"], "status": row["status"],
            "missing_since": _parse(row["missing_since"]) if row["missing_since"] else None,
            "status_text": RECORD_STATUS.get(row["status"]), "val1": row["val1"],
            "seen_at": _parse(row["first_seen_at"]),
            "api_response": row["api_response"],
            "device_response": answer,
            "delivered": delivered and not answer,
            "device_response_at": _parse(row["device_response_at"])
                                  if answer and row["device_response_at"] else None,
            "reading": reading(answer),
            # ASCII-escaped: a device's answer carries control bytes, and  is both
            # visible and still valid JSON when copied.
            "pretty": json.dumps(record, indent=2),
            "compact": json.dumps(record)}


def thread(conn, device_id: int, start: datetime | None = None,
           end: datetime | None = None) -> dict:
    """One device's commands in the range (all of them without one), oldest first, each with
    the stage it has reached.

    A command is either one this tool sent (a task, matched to the cloud's record once one is
    seen) or one the cloud holds that this tool did not send — from the portal or another
    install. Both appear, so the thread is the device's whole history, not only ours.
    """
    commands = []
    cloud_cols = """c.cloud_id, c.status, c.val1, c.first_seen_at, c.api_response, c.raw,
                    c.device_response, c.device_response_at, c.missing_since"""
    for t in conn.execute(f"""
        SELECT t.id, t.cmd_type, t.device_type, t.params, t.state, t.sent_at, t.http_status,
               t.error, t.send_reply, j.source_file, {cloud_cols}
        FROM cota_task t JOIN cota_job j ON j.id = t.job_id
        LEFT JOIN cota_command c ON c.task_id = t.id
        WHERE t.device_id = ? AND t.state IN ('sent', 'send_failed')
    """, (device_id,)):
        cloud = _cloud_view(t)
        params = json.loads(t["params"])
        stage = ("failed" if t["state"] == "send_failed" else
                 "device" if cloud and cloud["device_response"] else
                 "delivered" if cloud and cloud["delivered"] else
                 "api" if cloud else "accepted")
        payload = payload_for(t["device_type"], [device_id], t["cmd_type"], params)
        commands.append({
            "key": f"t{t['id']}",
            "origin": "console" if (t["source_file"] or "").startswith("console:") else "job",
            "at": _parse(t["sent_at"]), "cmd_type": t["cmd_type"], "params": params,
            "stage": stage, "http_status": t["http_status"], "error": t["error"],
            "send_reply": t["send_reply"], "cloud": cloud,
            "payload": json.dumps(payload),
            "payload_pretty": json.dumps(payload, indent=2, ensure_ascii=False),
        })
    for c in conn.execute(f"""
        SELECT c.cmd_type, c.sent_epoch, {cloud_cols} FROM cota_command c
        WHERE c.device_id = ? AND c.task_id IS NULL
    """, (device_id,)):
        cloud = _cloud_view(c)
        at = datetime.fromtimestamp(c["sent_epoch"]) if c["sent_epoch"] else cloud["seen_at"]
        commands.append({
            "key": f"c{c['cloud_id']}",
            "origin": "elsewhere", "at": at, "cmd_type": c["cmd_type"],
            "params": {"val1": c["val1"]} if c["val1"] else {},
            "stage": "device" if cloud["device_response"] else
                     "delivered" if cloud["delivered"] else "api",
            "http_status": None, "error": None, "send_reply": None, "cloud": cloud,
            "payload": None, "payload_pretty": None,
        })
    if start is not None and end is not None:
        commands = [c for c in commands if start <= c["at"] <= end]
    commands.sort(key=lambda c: c["at"])

    latest = conn.execute("""
        SELECT polled_at, http_status, records, raw FROM cota_poll WHERE device_id = ?
        ORDER BY id DESC LIMIT 1
    """, (device_id,)).fetchone()
    unparsed_body = None
    if latest and latest["http_status"] and 200 <= latest["http_status"] < 300 \
            and latest["raw"] and reply_records(latest["raw"]) is None:
        unparsed_body = latest["raw"][:2000]
    ours = [c for c in commands if c["origin"] != "elsewhere"]
    # The portal's "Total Count / Pending Count", plus what it leaves implicit.
    counts = {"total": len(commands),
              "waiting": sum(1 for c in commands if c["stage"] in ("api", "delivered")),
              "answered": sum(1 for c in commands if c["stage"] == "device"),
              "not_seen": sum(1 for c in commands if c["stage"] == "accepted"),
              "failed": sum(1 for c in commands if c["stage"] == "failed"),
              "gone": sum(1 for c in commands if c["cloud"] and c["cloud"]["missing_since"])}
    return {
        "counts": counts,
        # Not "items": in a template, thread.items is the dict's own method, not this list.
        "entries": commands,
        "last_check": {k: latest[k] for k in ("polled_at", "http_status", "records")}
                      if latest else None,
        "last_sent": max((c["at"] for c in ours), default=None),
        "latest_stage": ours[-1]["stage"] if ours else None,
        "latest_at": ours[-1]["at"] if ours else None,
        "unparsed_body": unparsed_body,
    }


# What a download carries, in order. Sent from, Command, Type, Stage, Send HTTP, Send reply and
# Cloud id were dropped at the user's request (2026-10-06); console_export_rows still fills
# them, so putting one back is a line here.
CONSOLE_EXPORT_COLUMNS = [
    ("imei", "IMEI"),
    ("device_id", "Device ID"),
    ("sent_at", "Sent at"),
    ("value", "Value sent"),
    ("cloud_val1", "Cloud val1"),
    ("status", "Cloud status"),
    ("cloud_seen_at", "Response via API seen"),
    ("answer", "Response via device"),
    ("answered_at", "Answer seen"),
]

_STAGE_WORDS = {"failed": "Not accepted", "accepted": "Command via API",
                "api": "Response via API", "delivered": "Delivered, no answer",
                "device": "Response via device"}


def visible(text) -> str | None:
    """Control bytes as \\x16 — how the console shows them, so a file says what the screen
    says. Left in, the exporter would strip them and the two would quietly disagree."""
    if text is None:
        return None
    return "".join(ch if ch in "\n\t" or (ord(ch) >= 32 and not 0x7f <= ord(ch) <= 0x9f)
                   else f"\\x{ord(ch):02x}" for ch in str(text))


def console_export_rows(th: dict, device_id: int, imei: str | None) -> list[dict]:
    """The thread as rows — one per command, what was sent beside what came back."""
    when = lambda d: d.strftime(TIME_FORMAT) if d else None           # noqa: E731
    rows = []
    for c in th["entries"]:
        cloud = c["cloud"] or {}
        rows.append({
            "imei": imei, "device_id": device_id, "sent_at": when(c["at"]),
            "origin": {"console": "this tool", "job": "this tool (job)",
                       "elsewhere": "portal / elsewhere"}[c["origin"]],
            "command": command_name(c["cmd_type"]), "cmd_type": c["cmd_type"],
            "value": " · ".join(str(v) for v in c["params"].values()) if c["origin"] != "elsewhere"
                     else None,
            "stage": _STAGE_WORDS.get(c["stage"], c["stage"]),
            "http_status": c["http_status"], "send_reply": c["send_reply"] or c["error"],
            "cloud_id": cloud.get("id"), "cloud_val1": cloud.get("val1"),
            "status": cloud.get("status"), "cloud_seen_at": when(cloud.get("seen_at")),
            "answer": visible(cloud.get("device_response")),
            "answered_at": when(cloud.get("device_response_at")),
        })
    return rows


def period_loaded(conn, device_id: int, start: datetime, end: datetime,
                  fresh_seconds: int = 60) -> dict | None:
    """The latest successful check that covered this whole period — what the page can say it
    has from the cloud — and whether it is recent enough not to ask again on opening."""
    covered_to = min(int(end.timestamp()), int(datetime.now().timestamp()))
    row = conn.execute("""
        SELECT polled_at, records FROM cota_poll
        WHERE device_id = ? AND http_status BETWEEN 200 AND 299
          AND window_from <= ? AND window_to >= ?
        ORDER BY id DESC LIMIT 1
    """, (device_id, int(start.timestamp()), covered_to)).fetchone()
    if not row:
        return None
    age = (datetime.now() - _parse(row["polled_at"])).total_seconds()
    return {"at": row["polled_at"], "records": row["records"] or 0, "fresh": age <= fresh_seconds}


def conversations(conn, limit: int = 50) -> list[dict]:
    """Devices with a console or any sent command, most recent first, with the last command."""
    # A UNION rather than FULL OUTER JOIN, which needs SQLite 3.39 — newer than some servers.
    rows = conn.execute("""
        WITH devices AS (
            SELECT device_id, MAX(id) AS last_task FROM cota_task
            WHERE device_id IS NOT NULL AND state IN ('sent', 'send_failed')
            GROUP BY device_id
        ), consoles AS (
            SELECT CAST(SUBSTR(source_file, 9) AS INTEGER) AS device_id,
                   MIN(created_at) AS created_at
            FROM cota_job WHERE source_file LIKE 'console:%' GROUP BY 1
        )
        SELECT d.device_id, t.cmd_type, t.params, t.state, t.sent_at AS at,
               (SELECT 1 FROM cota_command c WHERE c.task_id = t.id) AS in_cloud
        FROM devices d JOIN cota_task t ON t.id = d.last_task
        UNION ALL
        SELECT c.device_id, NULL, NULL, NULL, c.created_at, NULL
        FROM consoles c WHERE c.device_id NOT IN (SELECT device_id FROM devices)
        ORDER BY at DESC LIMIT ?
    """, (limit,)).fetchall()
    imeis = {r["device_id"]: r["imei"] for r in conn.execute(
        "SELECT device_id, imei FROM cota_device")}
    return [{**dict(r), "imei": imeis.get(r["device_id"]),
             "params": json.loads(r["params"]) if r["params"] else {}} for r in rows]


def recent_commands(conn, device_id: int, limit: int = 6) -> list[dict]:
    """The device's last few distinct commands, newest first — one click to send again."""
    seen, out = set(), []
    for r in conn.execute("""
        SELECT cmd_type, params FROM cota_task
        WHERE device_id = ? AND state IN ('sent', 'send_failed') ORDER BY id DESC LIMIT 60
    """, (device_id,)):
        key = (r["cmd_type"], r["params"])
        if key not in seen:
            seen.add(key)
            out.append({"cmd_type": r["cmd_type"], "params": json.loads(r["params"])})
        if len(out) == limit:
            break
    return out


def fleet_context(conn, imei: str | None) -> dict | None:
    """What Web FOTA's warehouse knows about this device — the registry's single row, so no
    snapshot is resolved. It says whether a reply is even possible: an offline device holds the
    command until it next connects."""
    if not imei:
        return None
    row = conn.execute("""
        SELECT imei, device_model, firmware, configuration, status, seen_at, hw_ver, vin,
               last_checked_at
        FROM device WHERE imei = ?
    """, (imei,)).fetchone()
    return dict(row) if row else None


# ─── reading jobs back ──────────────────────────────────────────────────────

def jobs(conn) -> list[dict]:
    return [dict(r) for r in conn.execute("""
        SELECT j.id, j.name, j.source_file, j.created_at,
               COUNT(t.id)                                   AS tasks,
               COUNT(DISTINCT COALESCE(t.device_id, t.imei)) AS devices,   -- ids typed in carry no IMEI
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
