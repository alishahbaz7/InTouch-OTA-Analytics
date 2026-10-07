# COTA — Configuration Over The Air

Working notes for the COTA feature of InTouch OTA Analytics. Keep this file current as the
feature grows: what the cloud API is known to do, what has been built, and what is still open.
`CLAUDE.md` holds the rules for the whole repo; this file holds the ones specific to COTA.

## Why this exists

The cloud portal (`ctvms.mappls.com`, IntouchAdminApi) can already configure a TCU over the
air, but only one command at a time:

- one command can go to several devices, but their replies can only be read **one device at a
  time** on a separate screen;
- **several commands to several devices** cannot be sent in one go;
- nothing can be exported, and there is no analysis over what was sent and what came back.

This utility uses the cloud as the gateway, the same way the portal does, and adds:

1. **Many devices × many commands** as one job
2. **Bulk configuration** from a spreadsheet
3. **Export** of every command and reply
4. **Analytics** over jobs, replies and failures (planned, see the roadmap)

The cloud already does the hard part, which is the secure link to every device. What is missing
is the layer on top: running many commands as one job, tracking each result, keeping a record,
and showing patterns. Fleet-ops teams usually end up building exactly this.

## How it works

```
 ┌──────────────┐     ┌────────────────────────────┐     ┌─────────┐     ┌──────┐
 │  UI / CLI    │ ──► │  COTA orchestrator         │ ──► │  Cloud  │ ──► │ TCUs │
 │ (jobs, CSV,  │     │  • job engine (devices ×   │ ◄── │  API    │ ◄── │      │
 │  templates)  │ ◄── │    commands → tasks)       │     └─────────┘     └──────┘
 └──────────────┘     │  • rate-limited dispatcher │
                      │  • reply collector (poll)  │
                      │  • DB: every cmd + reply   │
                      │  • export + analytics      │
                      └────────────────────────────┘
```

The core idea is a **job**: a set of devices × a set of commands. The tool expands a job into
**tasks**, one per device + command, and each task moves through its own lifecycle. Every other
feature (multi-command runs, bulk configuration, export, analytics) reads from these tasks.

Full task lifecycle, as intended:

```
planned → sent → (acked) → reply received → verified
              ↘ send_failed        ↘ no_reply / timeout        ↘ failed
unmapped   (IMEI not in the device map; never sent)
```

Only `planned`, `sent`, `send_failed` and `unmapped` exist in the code today. The rest need the
reply format (see "Open questions").

**MVP direction (agreed):** a Python tool inside this app. It imports IMEIs and commands from
Excel or CSV, sends them through the cloud API, collects the replies, and exports commands and
replies to Excel. Analytics and the dashboard page come after that works.

## The cloud API — what is known

Captured from the portal's own network traffic (DevTools, 2026-10-05). Only the
`Authorization` header matters; the cookies are analytics trackers and are not sent.

### Send a command — `POST /IntouchAdminApi/api/saveCOTAConfig/0`

```json
{ "deviceType": [124], "deviceList": [14906], "type": 36, "val1": "DBD76B82D531" }
```

| Field | Meaning |
|---|---|
| `deviceType` | Device type/model id, as a list. One call is assumed to be one type. |
| `deviceList` | **Cloud internal device ids**, not IMEIs. A list, so one call reaches many devices. |
| `type` | Command / parameter code. `36` takes a 12-hex value (looks like a MAC). |
| `val1` | Command value. Further values (`val2`…) are assumed by analogy, **not yet seen**. |
| `/0` | Path parameter, meaning unknown. |

Because `deviceList` is a list, the cloud already supports **one command → many devices** in a
single call. To send **many commands**, the tool makes one call per command (per device type)
and runs those calls together as a single job.

Headers in the captured request: `authorization: Bearer …`, `content-type: application/json`,
and browser headers (`origin`, `referer: https://ctvms.mappls.com/adminnextgen/`, `sec-ch-*`,
`user-agent`). The tool sends only `Authorization`, `Content-Type` and `Accept`. If the server
ever turns out to check `origin` or `referer`, add them; don't copy the cookies.

### Read replies — `GET /IntouchAdminApi/api/getGPRSCommand`

`?deviceId=14906&startTime=1791138600&endTime=1791225000`

- **One device per call — confirmed 2026-10-06:** `deviceId=14906,786` gets **HTTP 400**.
  `deviceId` is the same internal id as `deviceList`. Whether the call answers for the whole
  fleet without a `deviceId` was not tried (a fleet-wide read was held back for the user to
  decide).
- `startTime` / `endTime` are **Unix epoch seconds**. The example is 05-Oct-2026 00:00 IST to
  06-Oct-2026 00:00 IST, which is the portal's "today" view.
- Replies are **polled**: the cloud does not push them.
- **Response body (captured 2026-10-06):** a list of **command records**, one per command sent
  to the device from anywhere, not a list of replies:

  ```json
  {"id": "865510083360422_6AC48E16", "deviceId": 14906, "timestamp": 1791266326, "type": 36,
   "val1": "245A454ED731323334D7DAD76F4BD76AC48E16D73542D1", "val2": null,
   "movementState": null, "status": 0, "commandType": null, "commandValue": null,
   "response": null, "responseTime": null, "inputVal1": null, "inputVal2": null,
   "commandId": null, "imei": "865510083360422", "commandExcecuteType": 0}
  ```

  | Field | Reading |
  |---|---|
  | `id` | the cloud's id for the command: `<imei>_<8 hex>` |
  | `timestamp` | when the cloud took the command, epoch **seconds** (11:28:46, one second after the send) |
  | `val1` | **rewritten by the cloud.** It appears to wrap the typed value: `245A454E`·`D7`·`31323334`·`D7`·`DAD76F4B`·`D7`·`6AC48E16`·`D7`·`3542D1` — "$ZEN", "1234", the value sent (`DAD76F4B`), the id's hex, a trailer. One sample only; not relied on. |
  | `status` | **0 = waiting for the device, 1 = the device answered** (confirmed on the live cloud; the portal's *Pending Count* counts 0) |
  | `response` | **the device's answer**, filled when `status` becomes 1, e.g. `(GET FTP_SETTINGS:…;Source:…)*EF`. It can contain control bytes, and secrets: the live one carried an FTP password. |
  | `responseTime` | still null after the device answered — not usable |
  | `imei` | the device's IMEI — learned into the device map |

  The device's answer arrives in this same record — there is no separate API for it. The
  portal's COTA screen ("Search Device Command") lists exactly these records, with columns
  Device Id, Status, Delete (offered only while status is 0), Date & Time (`timestamp`), Type
  (36 = "Zenithra Command"), Val 1, Val 2, Response. Deleting a pending command is a call this
  tool does not know yet.

What polling means for the design:

- The tool sends a command, then calls `getGPRSCommand` for each device on an interval until a
  reply arrives or a timeout passes.
- **One call per device per poll round.** A job of 500 devices is 500 calls per round, so the
  poller is rate-limited to avoid loading the server.
- **Matching a reply to its command.** If the call returns *every* command for the device in the
  window, match on command type + timestamp, or on an id if the reply carries one. The response
  body decides which.

### Device replies are unstructured — and that is fine

Each command answers in its own format. The reply is treated as two layers:

| Layer | Example | Structured? |
|---|---|---|
| **Outer API JSON** (the cloud's wrapper) | `{ "deviceId": …, "command": …, "response": …, "sentTime": …, "status": … }` *(shape assumed)* | Yes: the cloud builds it, so the field names are the same for every command |
| **Device reply text** inside it | `"SET BLEMAC:DBD76B82D531 OK"`, `"VER:2.1.4,…"` *(illustrative)* | No: it changes per command |

How the tool handles it:

1. **Store the reply as-is.** Every reply goes into the database and the export unchanged.
2. **Classify with simple rules.** Per command type, look for patterns such as `OK` /
   `SUCCESS` / `ERROR` / `INVALID`, and mark the task **Success**, **Failed** or **Unknown**.
   The rules live in an editable config file, not in code.
3. **Add parsers only where needed.** For commands whose reply should feed analytics (firmware
   version, APN, …), add a small parser that pulls out those fields. Every other command still
   works from the raw text.

So the first version does not need to know every reply format. It only needs the **outer
wrapper's field names**, which is why one real `getGPRSCommand` response is the top open item.
Masked IMEIs and values are fine, as long as the field names are kept.

### Two identifiers per device

| Name | Example | Used by |
|---|---|---|
| Device ID (cloud internal) | `14906` | both API calls |
| Device Unique No (IMEI) | `865510083360422` | people, sheets, customers |

Confirmed pair: Device ID `14906` = IMEI `865510083360422`, Device Type `124`.

The send call also needs the **Device Type**, so the tool keeps a map of
**IMEI → Device ID + Device Type** (`cota_device`). There are two ways to fill it, and the tool is
meant to support both:

1. **Lookup API (preferred).** The portal shows devices in its device list, search box and COTA
   device picker, so there is an API behind them. Captured, it lets the tool resolve IMEIs
   automatically and stay current.
2. **Mapping file (fallback, what exists today).** A device-list export with Device ID, IMEI and
   Device Type. It works, but goes stale as devices are added.

Either way, **IMEIs that are not found are flagged before anything is sent**, so nothing reaches
the wrong device.

### Authentication

`Authorization: Bearer <uuid>`, copied from the portal. The token expires when the session ends,
and no login call has been captured yet. Until one is, the token is pasted in by hand.

> **Security note (2026-10-05):** a live token was pasted into the design conversation while the
> requests were being captured. Log out of the portal to invalidate it, and blank the token in
> any request shared from now on.

## Current state of the code

| Piece | Where | State |
|---|---|---|
| Module | `ota_analytics/cota.py` | **In the repo** (came in with commit `7ce78fb`): token, `Client`, device map, plan, job, send, poll, export |
| Tables (schema v10) | `cota_device`, `cota_job`, `cota_task`, `cota_poll` in `ota_analytics/schema.sql` | **In the repo** |
| CLI | `cota …` subcommands in `ota_analytics/cli.py` | To do |
| Tests | `tests/test_cota.py`, using a fake cloud that never touches the network | To do |
| Dashboard | `/cota/console` (Configure: one device, send and see the reply), `/cota` (Jobs), `/cota/devices` (send one command, device map), `/cota/signin` | **Built in 2.0.0**: manual send goes through `manual_plan` → `create_job` → `send_job`, so it is recorded like a sheet job |

Known cleanup in `cota.py`: a rejected token is detected by matching the message text
(`"rejected the token" in str(exc)`). Give it its own exception class (`TokenRejected(CotaError)`)
and catch that instead.

### Data model

```
cota_device  imei → device_id, device_type                (the map)
cota_job     one row per job (name, source sheet, created_at)
cota_task    one row per device × command
             state: planned → sent | send_failed;   unmapped (IMEI not in the map)
             keeps: payload values, batch_no, sent_at, HTTP status, raw send reply, error
cota_poll    every getGPRSCommand reply per device, raw, with the epoch window asked for
```

**How tasks are batched:** one API call carries one command to many devices of one type. Tasks
that share *(device type, command, values)* go into one call, with at most
`MAX_DEVICES_PER_CALL` (50) devices per call. Calls run in the order of the first sheet row they
contain, so a device given two commands receives them in sheet order.

**Poll window:** from 5 minutes before the device's first send in the job, to 5 minutes after
now. A reply that arrives late is still inside the window on the next poll.

### Usage (the intended CLI, not wired up yet)

```powershell
# 1. Token: prompted with hidden input and stored in Windows Credential Manager.
#    OTA_COTA_TOKEN in the environment overrides it.
.\.venv\Scripts\python.exe -m ota_analytics.cli cota token set

# 2. Device map: a sheet with IMEI (or "Device Unique No"), Device ID, Device Type
.\.venv\Scripts\python.exe -m ota_analytics.cli cota map-import "devices_map.xlsx"

# 3. Command sheet: IMEI, Command, Val1 [, Val2 … Val9]. Check it first (no network):
.\.venv\Scripts\python.exe -m ota_analytics.cli cota plan "commands.xlsx"

# 4. Create the job. Nothing is sent yet.
.\.venv\Scripts\python.exe -m ota_analytics.cli cota new "commands.xlsx" --name "BLE MAC batch 1"

# 5. Send a few devices first, then the rest. Asks you to type SEND to confirm.
.\.venv\Scripts\python.exe -m ota_analytics.cli cota send 1 --canary 5
.\.venv\Scripts\python.exe -m ota_analytics.cli cota send 1

# 6. Collect replies (repeat as devices answer), list jobs, export
.\.venv\Scripts\python.exe -m ota_analytics.cli cota poll 1
.\.venv\Scripts\python.exe -m ota_analytics.cli cota jobs
.\.venv\Scripts\python.exe -m ota_analytics.cli cota export 1        # → reports\cota_job_1_….xlsx
```

Command sheet example:

| IMEI | Command | Val1 |
|---|---|---|
| 865510083360422 | 36 | DBD76B82D531 |
| 865510083360430 | 36 | DBD76B82D531 |

Columns are matched by name, without regard to case or spacing. `.csv` works as well as `.xlsx`.
Values are kept as text exactly as written, so `00A1` stays `00A1`.

## Rules for this feature

These are deliberate. Change one only for a reason, and record it here.

- **Nothing is sent unless asked for explicitly.** `plan` and `new` never touch the network.
  `send` shows the counts and the host, then asks for `SEND`. The first send of a real job
  should be a `--canary`.
- **The token is a secret.** It is kept in the environment or the OS credential store only, and
  never in a file, the database, a log, or an error message. HTTP errors report the exception
  *type*, never its text. A test checks that the token does not reach the database.
- **A rejected token (401/403) stops the job immediately.** Everything sent so far is recorded,
  and the rest stays `planned`, so running `send` again resumes the job.
- **An IMEI that is not in the map is never guessed.** It is stored as `unmapped`, so the export
  still accounts for every row of the sheet.
- **Replies are stored raw, always.** Parsing is added per command later, on top of the raw
  reply, never in place of it.
- **Rate limit:** 0.5 s between calls (`CALL_INTERVAL_SECONDS`) until the cloud's real limit is
  known.
- Follow the repo's existing conventions: SQLite only, plain dicts across module boundaries, a
  test for every behaviour, and slow work as a job when it reaches the dashboard (see the
  `long-jobs-ux` skill).

## The Configure console (2.0.0)

`/cota/console` is a conversation with one device. Each device's console is a job with
`source_file = 'console:<device_id>'`: sends are its tasks (`console_send`, through the same
`_send_call` a job uses) and reply checks are its `cota_poll` rows (`console_check`, through
`poll_device`). The thread shows every task that reached the device from any job, plus the
entries of the latest successful check. Each check asks for the whole window, from today 00:00
or the first send, whichever is earlier, capped at a week, so the latest holds everything.

Each record a check returns is kept in `cota_command` (schema v11), upserted by the cloud's id,
and matched to the send that caused it by `_match_records`: same device and type, timestamp
within `MATCH_SECONDS` (120), closest pairs first, one to one. Unmatched records were sent
elsewhere and are shown as such. The first record that carries an IMEI fills the device map
(`_learn_imei`) unless a person already mapped that device or IMEI.

A command's stage is the console's ticks: ✓ `accepted` (send call 2xx) → white ✓✓ `api` (its
cloud record has been seen, status 0) → green ✓✓ `device` (the record's `response` is filled,
status 1, copied into `device_response` and kept even if a later record omits it). `failed` is a refused send. Model 124 and type 36 are
`DEFAULT_DEVICE_TYPE` / `DEFAULT_CMD_TYPE`, locked in the UI with an Edit.

## Sequences (2.0.0)

One device, several commands, one at a time — `ota_analytics/cota_run.py`, rules in its
docstring. Kinds are read from the first byte: `DA` GET, `DB` SET, `DD` CLR, anything else
unknown. Names come from `cota.COMMAND_OPERATIONS` and `COMMAND_PARAMETERS` (`6F4B`
FTP_SETTINGS, `6D66` SOS so far). CLR is retried like GET and SET — the user's decision, knowing
a resend minutes later could clear a new SOS raised in between. Only an unrecognised command is
not resent once it may have reached the device. Commands from the user's test list:

| Command | Reading |
|---|---|
| `DAD76F4B` | GET `6F4B` — answers `GET FTP_SETTINGS` (confirmed) |
| `DAD76C0A` | GET `6C0A` |
| `DDD76D66` | **CLR SOS** — `DD` is CLR (clear), `6D66` is SOS (confirmed by the user) |
| `DBD76C0A D5 31 D9 31 D9 322E35 …` | SET `6C0A` = 1, 1, 2.5, 3.5, 3.5, 1, 10, 3 (ASCII, `D9`-separated) |
| `DBD76B38 D5 30303030303030303030 …` | SET `6B38` = five fields of `0000000000` |

Read-back verification (SET `6C0A`, then GET `6C0A`, compare) is the natural next step; it needs
the GET's answer format for each parameter.

## Jobs (2.0.0)

Many devices, one sequence each: `ota_analytics/cota_campaign.py`, with the rules in its
docstring. The pages are `/cota` (new job → plan → start, and today's jobs), `/cota/jobs/<id>`
(progress, the command × outcome grid, devices, pause/resume/cancel, export) and the Groups card
on `/cota/devices`.

| Setting | Default | Why |
|---|---|---|
| Devices per send call | 50 | raise during live runs as the cloud allows (open question 8) |
| Calls per second | 5 | sends and checks together; checks dominate (below) |
| Wait for an answer, per attempt | 30 s | the user's rule (2026-10-07); then sent again, 3 attempts, then the next command |
| Time limit for the whole job | 60 min | at the limit every device still in progress stops; replaced waiting 12 h for held commands |
| Canary | 1% of jobs over 20 devices, 1–20 | the rest go only if the canary stays within the stop |
| Automatic stop | >10% of finished commands failed, or 3 failed calls in a row | judged after 20 |

**Upload format:** the cloud's device list, `id,trackingCode`. `id` is required and
`trackingCode` (IMEI) is optional; a header row is optional too. The fleet list
`DeviceList06Oct26.csv` (23,103 rows) parses with no problems and no duplicates.

**Cost at fleet scale (simulated, every wait its real length):**

| Devices × commands | Send calls | Checks | Scheduler time |
|---|---|---|---|
| 1,000 × 5 | 15 | 10,010 | seconds |
| 5,000 × 5 | 37 | 35,696 | 48 s (303 s before `ix_cota_task_device`) |
| 30,000 × 5 | 162 | 152,072 | 236 s for 15 simulated minutes |

The checks are the bill: about one per device per command, because a check reads one device. At
5 calls/s, 30,000 × 5 is about 8½ hours. At 20 calls/s it would be about 2 hours, if the cloud
allows it.

**Live test, 06-10-2026 21:26 (job #1, `DAD76F4B` to 14906 and 786):** both sends were
accepted, and each call carried both devices. Neither device answered, so after 3 attempts each
was marked failed at 21:34. The finding: **"Command sent at <ts>" is written in the same second
as the send, even to a device that is switched off.** 14906 answered normally until 20:06 and
then stopped. Every later command, and every command to 786, got that note immediately and no
answer. So the note does not prove delivery, and the job's rule "delivered without an answer →
resend" turns a switched-off device into 3 resends and a failure, not a wait. **Open:** does the
cloud hold commands for a switched-off device and deliver them when it wakes (then wait, as for
`status` 0), or drop them (then resend once the device is back — FOTA's last ping per IMEI could
tell when)? To find out, switch 14906 on and read its records from 06-10-2026 20:00. Late
answers mean the cloud holds commands.

**Second live test, 07-10-2026 (jobs #1–#3, 14906 and 786):** 786 answered about **5¾ min after
the first attempt** of every command — 5 min 50 s, 5 min 39 s, 5 min 50 s — each time on attempt
3's record, 10–20 s after attempt 3 was sent. Attempt 1 of GET 6C0A went 12 s after 786 had just
answered and was still not answered. Not specific to several devices per call: single-device
sends in the same job behaved the same. Readings still open: the device takes ~5 min to wake for
each command (the leading one), or the cloud holds the newest command and hands it over when the
device connects. 2.0.1 records which attempt carried each answer and the time from the first
attempt (job page, export), and makes the answer wait a job setting. The same evening the user
set the rule in the table above: 30 s per attempt, 3 attempts, held = no answer, and a time
limit per job.

**The command library (2.0.1):** the Commands page names parameters (`6C0A` → your name, for
every GET/SET/CLR of it) and saves whole commands under a name with tags, picked in Configure and
Jobs. Kept across days, like groups and the device map.

**Retention:** the COTA record lives for the day. The first COTA page opened on a later day
clears the earlier days' jobs, sequences, sends and cloud records. Groups and the device map are
kept.

## Open questions — capture these next

In DevTools, open the Network tab and select **Fetch/XHR**. Do the action in the portal, then use
**Copy as cURL** and copy the **Response** body. Blank out the token before sharing.

| # | What | Why it matters |
|---|---|---|
| 1 | ~~Response body of `getGPRSCommand`~~ — **captured 2026-10-06** (a pending record). Still wanted: one record **after** the device answered, and the meaning of `status` values | Decides whether `status` gives a "delivered" stage between the ticks |
| 2 | Response body of `saveCOTAConfig` | Does it return a request id? If yes, matching becomes exact. |
| 3 | Device search/list API (IMEI → Device ID + Type) | Replaces the hand-loaded device map. |
| 4 | Command-type list (the portal's dropdown) | All `type` codes, their names, and which `valN` each one needs. Enables validation before sending. |
| 5 | ~~Login API~~ — **captured 2026-10-06**: `POST https://ctvms.mappls.com/IntouchAdminApi/user/login`, multipart, `username` + `password` as MD5 hex. Still unseen: the **reply** (which field holds the token); `sources.find_token` looks for the usual names and the Sign in page lists the fields if none match | Lets the tool get a fresh token by itself, as `sources.py` already does for the OTA platform. |
| 6 | A command that uses `val2`+ | Confirms the multi-value payload shape. |
| 7 | Meaning of `/0` in `saveCOTAConfig/0` | |
| 8 | Max devices per send call, and the call rate the cloud tolerates | Sets a job's batch size and calls/s. Checks are one device per call (HTTP 400 on a list), so the rate decides how long a fleet job takes. |
| 9 | ~~Scale~~ — **known 2026-10-06**: 23,103 devices, up to 10% more; a job may be the whole fleet | Sized for 30,000 and simulated at that size. |
| 10 | Who uses it: engineers (CLI) or an ops team (web page) | Decides how soon the dashboard page is needed and who gets the admin role. |

A quick way to capture items 1–6: with the **Fetch/XHR** filter on, search a device, open the
COTA screen, send a command and view its reply. Then right-click each request, choose
**Copy → Copy as cURL (bash)**, and save it together with its **Response** body.

## Roadmap

1. **Send in bulk, poll, export raw.** The module and tables exist; the CLI and tests are still to do
2. **Match replies to commands.** Once #1 above is known: per-task `responded_at`,
   `reply_text`, and the states `answered` / `no_reply` / `timeout`
3. **Verdict rules.** Per command type: patterns for success and failure, kept in an editable
   file rather than in code. Each task gets `success` / `failed` / `unknown`
4. **Device lookup and the command catalogue via the API** (#3, #4), with values validated
   before sending
5. **Dashboard page** `/cota`: upload a sheet, review the plan, canary, then send, with a live
   progress bar and the job table. Admin role only, because it changes devices in the field
6. **Read-before-write and verify:** read the current value, push the new one, read it back,
   and keep before and after for rollback
7. **Analytics:** success, failure and timeout rate per job, command, model and firmware;
   reply-time distribution; failure-reason clusters; devices that rarely answer; config drift
   against a declared profile
8. **Templates and profiles:** a named set of commands applied to a list of IMEIs
9. **Offline handling and scheduled retries.** A device that is offline keeps its task queued and
   is retried later, not marked failed. This matches how the OTA platform parks tasks (see
   `CLAUDE.md`).
10. **Command validation** before sending: syntax and allowed ranges per command, from the
    catalogue (#4 above)
11. **Audit trail:** who sent what, to which devices, and when. This builds on the existing
    `auth.py` roles

Analytics wanted, in full: success / failure / timeout rate per job, firmware version, device
model and region; reply time (how long devices take to answer); groups of common failure
reasons; **config drift**, meaning devices that no longer match a standard profile; and devices
that often fail to answer, which usually points to a hardware or SIM problem.

The biggest operational risk is **one bad config pushed in bulk to thousands of devices**.
Canary-first sending, validation and read-before-write exist to prevent that, so treat them as
requirements, not extras.

## Decision log

| Date | Decision |
|---|---|
| 2026-10-05 | Build COTA inside InTouch OTA Analytics as a separate module (`cota.py`, own tables, own token). It shares nothing with the snapshot warehouse, and bundles do not carry it |
| 2026-10-05 | Use the cloud as the gateway: reuse the portal's two calls (`saveCOTAConfig`, `getGPRSCommand`) and add no device-side change |
| 2026-10-05 | People work in IMEIs; the tool maps them to Device ID + Device Type. Unknown IMEIs are recorded as `unmapped`, never guessed |
| 2026-10-05 | Store every reply raw; add classification rules and parsers on top, per command |
| 2026-10-05 | Batch by (device type, command, values) to use the API's device list; up to 50 devices per call and 0.5 s between calls until the real limits are known |
| 2026-10-05 | Nothing is sent without an explicit send step, and the first send of a job is a canary |
| 2026-10-05 | Version 2.0.0, a major version, because it is the first time the app writes to a production system. Schema v10 |
| 2026-10-05 | `cota.py` and the schema v10 tables were committed with v1.9.1 (`7ce78fb`, pushed to `main`) by accident, ahead of the CLI and tests. They are inert until wired up: the tables are empty and nothing calls the module |

## Developing

```powershell
.\.venv\Scripts\python.exe -m pytest -q        # everything
```

The cloud is reached only through `cota.Client`. Tests pass a fake with the same two methods
(`send`, `responses`), so new behaviour can be built and tested without a token or a network
connection. When a captured response arrives, add it as a fixture under `tests/` with IMEIs and
values masked, and build the parser against it.
