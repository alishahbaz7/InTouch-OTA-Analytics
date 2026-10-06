# CLAUDE.md — InTouch OTA Analytics

Guidance for Claude Code sessions working in this repo. Read this first.

## What this project is

An analytics layer on top of an existing **InTouch OTA platform** (internal web platform that
pushes firmware updates to field devices). The platform performs updates well but surfaces
almost no analysis. This repo builds:

1. A local **snapshot warehouse** that ingests the platform's device exports into SQLite.
2. A **dashboard** (FastAPI + server-rendered charts) for fleet and rollout health.
3. **Downloadable reports** (XLSX, later PDF).
4. An **AI insight layer** (single Claude call over pre-computed aggregates) that writes the
   "what changed" narrative.

Local-first. Cloud hosting comes later, and nothing in the design should block it.

## The single most important fact about the data

**The platform export is a point-in-time device inventory, not an OTA event stream.**

The sample export (`Sample data/Devices_35477_15Aug26_1511.xlsx`) has 35,477 rows × 14 columns:
`IMEI, STATUS, QUEUE, Device Name/VIN, Created By, Device Model, FIRMWARE, CONFIGURATION,
SEEN AT, ICCID, hwVer, vin, Groups, First Ping`.

There is **no** campaign id, job id, per-device state transition, failure code, retry count,
duration, or byte counter.

**Confirmed by the platform owner (2026-08-15): the platform itself holds the same data as this
export.** There is no richer event log to connect to later. This is the complete data surface,
so design for it rather than around it:

- Campaign success/failure rates, failure taxonomy, retry analysis, update durations, and
  bandwidth metrics are **not buildable — full stop.** They would require new instrumentation on
  the platform/device side, which is a separate project and not an assumption anything here
  may depend on.
- Therefore the snapshot warehouse is not a stopgap; it is **the** architecture. All analytical
  depth comes from accumulating snapshots and diffing them.
- Everything time-based must come from **diffing consecutive snapshots**. One snapshot gives
  you state; two or more give you movement. This is why ingest is append-only and why
  `snapshot_at` is part of nearly every key.
- The snapshot timestamp is **not a column** — it is in the filename:
  `Devices_<rowcount>_<DDMonYY>_<HHMM>.xlsx` → `Devices_35477_15Aug26_1511.xlsx` = 2026-08-15 15:11.
  Parse it there, and fall back to file mtime only with a loud warning.

See `docs/DATA_PROFILE.md` for the full column profile and every data-quality trap found.

## Column semantics confirmed by the platform owner (2026-08-15)

These two definitions carry most of the project's analytical weight. Do not re-guess them.

**`QUEUE`** — OTA task state, and the only task-level signal that exists:

| Value | Meaning | Stored as |
|---|---|---|
| `-` | no OTA task has **ever** been assigned to this device | `queue = NULL`, `queue_state = 'never_tasked'` |
| `0` | tasks were assigned and completed; nothing pending | `queue = 0`, `queue_state = 'completed'` |
| `1`+ | that many tasks still pending | `queue = n`, `queue_state = 'pending'` |

**`-` and `0` must never be collapsed into one bucket.** "Never targeted" and "targeted and
finished" are different operational facts, and the difference is where update coverage gaps
live. This is why `queue_state` exists as its own column.

Diffing `queue_state` across snapshots yields the closest thing to update outcomes available:
`pending → completed` **with** a firmware change is a successful update; the same transition
**without** one is the strongest failure signal we can produce. Note a rollback also completes
its task with a firmware change — `task_event` and `kind` must be read together.

**`STATUS`** — a 24-hour recency bucket over `SEEN AT`, not an independent liveness signal:

| Value | Meaning |
|---|---|
| `Online` | last ping within 24 hours |
| `Offline` | no ping for more than 24 hours |
| `Inactive` (shown as `-` in the platform UI) | never pinged at all |

Because STATUS is derivable from `SEEN AT`, ingest cross-checks the two and raises a
`status_seen_at_mismatch` quality issue on disagreement (3 devices in the sample, so the rule
holds). Note that `Offline` spans everything from 25 hours to two years — always refine it with
`seen_age_hours` rather than reporting a raw offline count.

## How the platform is actually operated (confirmed 2026-08-15)

Two things about the workflow that change what the numbers mean:

**Tasks are assigned in bulk, including to unreachable devices.** A pending task is parked, not
failed — it completes whenever the device next comes online. So a large pending count is normal
and must never be presented as a backlog or a failure rate. What matters is *why* a device is
pending, and there are exactly two reasons:

1. **Powered off / out of service** — self-resolving, expected, the large majority.
2. **Powered on but unable to reach the OTA platform** — the device pings fine, so it looks
   healthy, but the update cannot be delivered. This is a genuine fault.

The platform shows these identically. **Separating them is the primary job of this project.**
The signal is simple and cheap: a pending device whose `STATUS` is Online is case (2). In the
sample that is 155 devices out of 7,859 pending — the smallest number in the dataset and the
most urgent. Never let it get averaged away into the aggregate.

**Not every device is supposed to be updated.** Some models are end-of-life and already run the
correct firmware, so `queue_state = 'never_tasked'` is the correct state for them, not a gap.
Compliance is therefore measured against a **declared** target version per model
(`firmware_target` table), never against "the most common version" — inferring the target would
mark an in-progress rollout's laggards as compliant and an EOL fleet as failing.

## Fallback — the highest-priority signal in the system

A **fallback** is a device reverting to a firmware version it has **run before** (owner's
example: shipped on 1.0.0, updated to 1.1.0, then 1.2.0, then dropped back to 1.0.0). It is
deliberately distinguished from an ordinary downgrade:

| | Meaning | Column |
|---|---|---|
| downgrade to a version the device previously ran | the device **reverted** — something on it caused this | `is_fallback = 1` |
| downgrade to a version it has never run | it was **sent** an older build — a targeting problem | `is_fallback = 0` |

`fallback_kind` is `original` (back to the earliest version ever recorded for that device — the
most severe case) or `previous` (an intermediate build).

Two rules this depends on:

- **Version direction is compared within a device model, across version families.** An earlier
  build compared families too and classified `1.2.0 → 1.0.0` as "unknown", which silently hid
  every revert of this shape. Only cross-*model* comparison is refused, because the models use
  unrelated schemes.
- **Cause has to be inferred from what the affected devices share** — there is no recorded
  reason. `metrics.fallback_segments()` slices fallbacks by model, version path, hardware
  revision and group; a slice concentrating far above its share of the fleet is a lead. Present
  these as candidate explanations, never as causes.

## Environment facts (verified on this machine, 2026-08-15)

- **This project uses a virtual environment: `.venv` (Python 3.12.10). Use it for everything —
  running, testing, installing.** Four Pythons are installed on this machine (3.12, 3.13, 3.14
  and a WindowsApps shim), and packages installed into one are invisible to the others. That
  mismatch already cost a round trip: the terminal's `python` was 3.12 while VS Code ran 3.14,
  so a suite that passed on one side failed on the other with
  `ImportError: jinja2 must be installed`. The venv removes the ambiguity — one interpreter,
  the same on both sides, pinned in `.vscode/settings.json`.

  ```powershell
  .\.venv\Scripts\python.exe -m pytest -q      # tests
  .\.venv\Scripts\python.exe main.py           # dashboard
  .\.venv\Scripts\python.exe -m pip install -r requirements.txt
  ```

  **Never verify with a bare `python`** — it resolves to 3.12 outside the venv and proves
  nothing about what F5 runs. `main.py` preflights its imports and prints `sys.executable`
  plus the exact pip command if a package is missing.

  To rebuild the venv from scratch:
  `python -m venv .venv; .\.venv\Scripts\python.exe -m pip install -r requirements.txt`
- **No Node.js, no Postgres, no Docker installed.** Do not propose a stack that needs them.
- pip has network access. Already installed: `fastapi 0.136.3`, `uvicorn 0.49.0`, `openpyxl 3.1.5`.
- Platform: Windows 11, PowerShell 7. Paths have spaces — always quote them.
- The OTA platform itself is login-gated and internal; do not attempt to reach it over the network.

## Stack decisions (settled — do not relitigate without a reason)

| Decision | Choice | Why |
|---|---|---|
| Language | Python 3.12 | Only runtime installed; openpyxl already there |
| Store | **SQLite** (`data/ota_analytics.db`) | Zero install, single file, handles 35k×N rows trivially. Postgres only if we go multi-user cloud |
| Excel read | openpyxl `read_only=True` | 22 MB files; streaming avoids loading the whole workbook |
| Aggregation | **SQL in SQLite**, not pandas | Keeps deps minimal and logic inspectable; pandas is not a required dependency |
| Web | FastAPI + Jinja2 templates + vendored Chart.js | Already installed; no Node build step; works offline |
| Charts | **Server-rendered inline SVG**, no JS charting library | Guaranteed to work offline and on a locked-down box; no vendored 200 KB bundle; filters are query params, which makes every view linkable and bookmarkable |
| Reports | openpyxl for XLSX; PDF via print-stylesheet HTML first | Avoids a heavyweight PDF dep until it's actually needed |
| AI | `anthropic` SDK, one call/day over aggregates, structured output | Arithmetic in SQL, judgment in the model. Never point the model at raw rows |
| Scheduling | stdlib thread + APScheduler-style loop in-process | No cron/Task Scheduler dependency for local; swap for real cron on cloud |

## Credentials and the Update Data page

`/update` loads a new export either by upload or by downloading from the platform
(`sources.py`). Rules that must not be relaxed:

- **The platform password lives in the OS credential store or the environment — never in a
  file this repo owns.** On Windows it is Windows Credential Manager via `keyring`, service name
  `InTouchOTA-Analytics`. A headless Linux server has no credential store at all, so
  `save_password()` returns `False` and `load_password()` returns `None` there — the service
  would start and silently never fetch again. `OTA_PLATFORM_PASSWORD` is how a deployed service
  is given the secret, and it wins over `keyring` when set: what the deployment configured is
  what runs, and a stale keyring entry shadowing it would be undebuggable. A blank value counts
  as unset, because that is a misconfigured unit file rather than an empty password.
  Never write it to `connection.json`, the database, a log line, or an HTML value attribute.
  `data/connection.json` holds only URL, username, auth mode and TLS flag.
- **Never echo a submitted password back into the rendered page.** There is a test asserting it
  does not appear in the response.
- **Never put raw exception text from an HTTP call into the UI** — request objects can carry a
  URL with credentials embedded. `fetch_export` reports the exception *type* and a hint.
- **Validate that downloads and uploads are really .xlsx** (zip magic `PK\x03\x04`). The normal
  failure mode is a 200 OK containing the login page, which would otherwise be ingested as a
  corrupt snapshot.
- **Uploaded filenames are untrusted** — `safe_filename()` takes the basename and strips unsafe
  characters so a write cannot escape the export folder.
- The password form is only safe over loopback; the page warns when the request comes from
  another host, since the dashboard serves plain HTTP.
- A duplicate export is detected by SHA-256 and its file is **deleted after ingest**, otherwise
  repeated uploads pile up 22 MB copies that carry no information.

## Who may see the dashboard (`auth.py`)

Every page lists IMEIs, VINs and ICCIDs, and `/update/*` posts to the production OTA admin API.
Access control is therefore not optional decoration.

- **Default-deny in one middleware, not a dependency per route.** A route added later is then
  protected by omission rather than exposed by it. Only `PUBLIC_PATHS` (login, logout, health,
  static) bypass it.
- **Two roles.** `admin` does everything; `viewer` reads and is refused on every non-safe HTTP
  method. Enforce that by method on the server — hiding a button in a template is a courtesy,
  not a permission. Note `viewer` limits *actions, not exposure*: a viewer still reads every
  identifier on the devices page.
- **Sessions are stdlib-signed cookies and passwords are `hashlib.scrypt`** — no new dependency.
  Starlette's `SessionMiddleware` needs `itsdangerous`, which is not installed.
- **With no password configured the app stays open**, so upgrading a local install does not
  brick it. What makes that safe is `main.py:check_exposure()`: binding to anything other than
  loopback without a password configured exits rather than publishing the fleet to the network.
- **`OTA_TRUST_PROXY_AUTH` is off by default.** It makes the app believe an identity header from
  Cloudflare Access; anything that can reach the port could forge one, so it is safe only when
  the port is loopback-only behind the tunnel.

See `docs/DEPLOY.md` for how this is configured on a real host.

## Two installs, one reference point (`identity.py`, `bundle.py`)

A dev machine and a packaged exe, or two colleagues working offline and syncing at different
times, hold different snapshot sets and therefore report different numbers. Nothing else in
the UI makes that visible, so it gets argued about instead of checked.

- **The comparable value is the input set, not the file.** Two databases holding identical
  snapshots are never byte-identical — rowids, `ingested_at`, WAL state and vacuum history all
  differ — so hashing the `.db` would report a mismatch every time and prove nothing. Every
  derived table is rebuildable from the raw snapshots, and a snapshot is identified by
  `file_sha256`, so **same set of file hashes ⇒ same numbers, by construction.**
  `identity.fleet_digest()` hashes exactly that set, sorted, so ingest order cannot affect it.
  It is in the footer of every page and on a `Source` sheet in every XLSX report.
- **A creation date is provenance, not a reconciliation key.** `db_id`, `created_at` and
  `instance_label` answer *whose* numbers these are; only the digest answers *do they match*.
  Do not present `created_at` as evidence of agreement.
- **"Last sync" is two clocks and they must stay apart.** `snapshot_at` is when the platform's
  data was true; `ingested_at` is when this install pulled it. The first decides the numbers.
  Reporting one figure conflates them and hides the case where both sides last fetched at the
  same minute while holding a different number of snapshots.
- **A bundle is replayed, not swapped in.** Shipping the `.db` is a *replace* — the importer
  loses everything the sender lacked — and it breaks across schema versions. `bundle.py`
  exports the first snapshot as a full resolved baseline (tombstones included) and the rest as
  delta rows, and import re-ingests them, so the two snapshot sets union and re-importing is a
  no-op. `device_group` is not shipped; it is rebuilt from `groups_raw`.
- **Inserting a snapshot into the middle of a timeline rewrites it.** `device_state` resolves
  each device to its most recent row at or before a snapshot, so a foreign snapshot landing in
  a gap becomes the answer for every device that did not change locally across it — a value no
  fetch ever observed, reported without error. Import therefore refuses an interleave by
  default. `allow_interleave=True` handles it properly: `retention.densify` makes the
  surrounding snapshots self-sufficient *before* any foreign row lands, the bundle's own chain
  is resolved in staging on its own terms, `retention.renumber_snapshots` restores id order,
  and `retention.compact` squeezes the duplicates back out.
- **`device_state` compares snapshot *ids*, not timestamps.** So id order matching time order
  is a correctness precondition, not tidiness. Ingest maintains it by only ever appending;
  import is the one thing that can break it, which is why it renumbers.
- Import is resumable rather than atomic: renumbering has to toggle `PRAGMA foreign_keys`,
  which SQLite ignores inside a transaction. Re-running the same bundle finishes an interrupted
  merge instead of reporting success over a half-merged database.

## The packaged build (`build.py`, `InTouchOTA-Analytics.spec`)

`python build.py` produces `dist\InTouchOTA-Analytics\` plus a zip to hand over. Everything
here exists because a frozen build breaks assumptions that are invisible from source.

- **Two roots, and conflating them is the whole problem.** `config.ROOT` is where the program
  *writes* (the folder holding the .exe); `config.resource()` is where it *reads* its own files
  from (inside the bundle, read-only). Running from source they are the same directory, which
  is exactly why the difference is easy to miss. Deriving `data/` from `__file__` put the
  database inside PyInstaller's extraction folder — **every launch would have started from an
  empty database.** `schema.sql` and `web/` go through `resource()`; nothing else may.
- **A release is `InTouchOTA-Analytics-v<version>` — the zip and the folder it unpacks to — and
  nothing else.** No platform suffix. That is the user's rule for every release, here and in
  their other projects (it is in the shared `ship` skill). `build.release_name()` is the one
  place it is spelled. The `.exe` keeps its unversioned name so shortcuts survive upgrades, and
  the working folder in `dist\` stays unversioned because this install keeps its database in
  it. A versioned release folder means an upgrade by unzipping starts empty unless the old
  `data` folder is moved across — `READ ME FIRST.txt` says so.
- **One executable, with a console.** There used to be a windowless twin,
  `InTouchOTA-Analytics-silent.exe`, for auto-start to run after a reboot. Auto-start is
  withdrawn, so nothing launched it, and run by hand it showed nothing at all — which reads as a
  program that failed to start. Dropped in 1.9.1. `startup.launch_command()` still prefers a
  windowless build if one is present, so reviving auto-start means adding it back to the spec.
  `-silent.exe` stays in `build.BUILD_OUTPUTS` so one left by an older build is cleared, not
  rescued as the user's file.
- **A windowless build has no usable stdout, and uvicorn will not start without one.** It
  installs a logging handler on `sys.stdout`, so the silent exe exited a few seconds after
  launch, every reboot, recording nothing. `main.attach_log_when_headless()` points both
  streams at `data\app.log`. Kept for when a windowless build comes back, and for `pythonw`.
- **Only one copy runs.** `main.already_serving()` asks `/healthz` whether the port belongs to
  this app and, if so, opens that URL and exits. Without it a second launch quietly moved to
  8001 and ran a second scheduler against the same database, while the copy on 8000 might have
  no window at all — which reads as "I ran it and nothing happened". A port held by anything
  else still falls back to the next one, so this defers only to our own dashboard.
- **"Start with Windows" is withdrawn** (`startup.AUTO_START_AVAILABLE = False`). The mechanism
  works and is still tested; what made it wrong was the result — an invisible process that
  starts itself, holds the port and cannot be seen or stopped from the dashboard. Bringing it
  back means first giving the UI a way to show and stop the hidden copy. `purge_if_withdrawn()`
  takes down an entry armed by an older version, because that entry lives in the Startup folder
  and would otherwise outlive the feature with no toggle left to remove it.
- **Auto-start builds one argv, not `(interpreter, script)`.** A packaged build has no
  `main.py`; the program *is* the executable. `startup.launch_command()` is the single source
  for the Startup-folder entry, the scheduled task and "Test it", so they cannot drift apart.
  Paths are quoted **unconditionally** — whoever unpacks the app chooses where it lives, and an
  unquoted `C:\Program Files\...` breaks at boot with nobody watching.
- **The CLI must be reachable from the .exe.** `main.main()` delegates to `ota_analytics.cli`
  when argv[0] is a known subcommand, derived from the parser via `cli.command_names()` so a
  new command cannot be left unreachable. Otherwise `db-export`, `db-import` and `passwd` would
  be source-only.
- **Hidden imports are not optional.** `uvicorn.loops.*`, `uvicorn.protocols.*` and
  `keyring.backends.Windows` are imported by name at runtime, so analysis cannot see them. Left
  out, the build succeeds and then fails when run — and a missing keyring backend would put the
  platform password in a file instead of the credential store.
- One-folder, not one-file: one-file unpacks to a temp directory on every launch, and a single
  large unsigned binary is what antivirus quarantines hardest. UPX is off for the same reason.

## Release and dev side by side (`config.CHANNEL`)

The user runs the packaged release daily and a source copy while developing, **at the same
time**. `config.CHANNEL` is `release` when frozen and `dev` from source, overridable with
`OTA_CHANNEL`, and everything the two could fight over is keyed on it:

- **Port**: `config.DEFAULT_PORT`, 8000 for release and 8100 for dev. An explicit `--port` still
  wins, and the deploy units pass one.
- **Single instance**: `main.already_serving()` defers only to a copy of the *same* channel, read
  from `/healthz`. Deferring across channels made a dev launch open the release and exit, so a
  code change looked untaken. A copy too old to report a channel is treated as release.
- **Session cookie**: `ota_session` for release (unchanged, so upgrading signs nobody out) and
  `ota_session_dev` for dev. Browsers scope cookies by host, not port, and each copy signs with
  its own key, so a shared name means signing in to one signs you out of the other.
- **Data** was already separate, because `config.ROOT` differs.
- A server deployed **from source** must set `OTA_CHANNEL=release`, or it wears a DEV badge and
  issues the dev cookie. The deploy env templates say so.
- `/healthz` stays free of anything about the fleet. The channel describes the program, not
  the data.

## The sidebar (`nav.py`) and the module boundary

The UI is three modules in one sidebar, modelled on the CAN utility's workbench
(`D:\shahbaz\Miscellenious\Code\CAN utlity\web`): **Web FOTA** (everything that existed
before 2.0), **Web COTA** (listed and marked *Soon* — not built) and **Intouch COTA** (Jobs,
Devices, Sign in).

- **`nav.MODULES` is the only definition.** The sidebar, the top bar's title and description,
  and the tests all read it. A page goes into the right module by its URL alone (`nav.state`),
  so a route added later does not need to pass anything.
- **FOTA's chips belong to FOTA.** The fetch/agent chips and *Update data* render only when
  `module == 'fota'`. On a COTA page they would claim a freshness unrelated to what is on
  screen. The fleet digest stays in every footer anyway — `test_share_pages` holds that.
- **A module's way to its connection page is in the top bar**, not the rail: FOTA's filled
  *Update data* button; COTA's "Cloud: …" status chip, which is the only way in — a separate
  *Cloud sign-in* button beside it was redundant and removed at the user's request.
  `Item(in_rail=False)` keeps such a page titled and inside its module without a rail entry.
- Collapsed state is `data-nav="rail"` on `<html>`, set by the inline `<head>` script for the
  same reason the theme is: applied later, every page would open wide and snap shut.
- **COTA credentials follow the FOTA rules** (`cota_connection.py`): token and password in the
  credential store or the environment, never in `cota_connection.json` or a value attribute.
  The COTA password uses the account `cota-password:<user>`, so it can never overwrite FOTA's
  password for the same username. The InTouch cloud's login is **known and locked in its preset**
  (`cota_connection.PRESETS`): `POST IntouchAdminApi/user/login`, multipart, `username` + MD5 of
  the password, captured from the portal 2026-10-06. A posted login URL cannot override a known
  cloud's; only "Other cloud" takes one, and it is never guessed.
- **A rejected token is remembered** (`cota_connection.record_auth`, fed by `cota.Client`'s
  `on_auth`) and the chip reads **"Cloud: session expired"** — the user's wording. With a
  remembered password, `renew()` signs in again by itself and the step is repeated once: a check
  just asks again; a refused send (which never reached the device) is sent once more, with the
  refusal left on the record. Never over a token from `OTA_COTA_TOKEN`.
- **Configure (`/cota/console`) is the first COTA page:** one device, one command, and its
  stages. A device's console is a job keyed `source_file = 'console:<id>'`, so sends and checks
  are ordinary tasks and polls. Send answers with a **303 redirect** — a refresh must never
  resend a command to a device.
- **`getGPRSCommand` returns command records, not replies** — one per command sent to the
  device from anywhere. They live in `cota_command` (v11), matched to our sends on device, type
  and time (the cloud rewrites `val1`, so values cannot be compared), and paired **in time
  order** — closest-first alone crossed two quick sends over. Ticks: ✓ accepted → white ✓✓
  record seen (`status` 0) → green ✓✓ the record's `response` filled (`status` 1). The IMEI is
  learned from records into the map, never over a person's entry.
- **The console's range is `DD-MM-YYYY HH:MM`, 24-hour, capped at 15 days** (`cota.parse_range`,
  `RANGE_FORMAT`, `MAX_RANGE_DAYS`). Not `<input type="datetime-local">`: it follows the PC's
  locale. After a send, `range_after_send` keeps the start and moves the end to today 23:59.
- **Raw request/response are rendered into `<template id="raw-<key>">` with the thread**, so the
  side panel needs no extra call and a check's redraw refreshes them.
- **A device's answer can contain secrets** — the live one carried an FTP password. It is stored
  in `cota_command` and shown to anyone who can open the console, viewers included. Never copy
  a real one into a test, a doc or a commit; the tests use a sanitized copy.
- **Sequences (`cota_run.py`) are one command at a time, by rules agreed with the user** —
  the next command 2 s after an answer; the guard band (30 s, 60 s for an unrecognised command) before a
  resend of the same command and before moving on after one that gave up; checks every 10 s
  (one device heartbeat), 30 s to appear in the cloud, 2 min per attempt, up to 3 attempts then
  the next command. **The never-duplicate
  rule**: a resend is safe when the earlier attempt provably never reached the device (refused,
  or never listed); after that `DA` GET, `DB` SET and `DD` CLR may go again — CLR by the user's
  decision, knowing a resend could clear a *new* SOS — but never an unrecognised command. A late
  answer to an earlier attempt counts. Pauses (session, three failed calls), cancels and restart
  recovery all leave state in `cota_run` / `cota_run_step`; a crashed runner pauses its run and
  logs to the Errors page. While a run is live, hand-sends to that device are refused.
- **Runner tests drive a simulated device with simulated time** (`tests/test_cota_run.py`):
  every failure case, the user's five commands, real-length waits in about a second. Never call
  `cota_run.start` from a test — patch it — because a real run uses the real cloud client.
- **Tests cannot reach the network or the real credential store** (`conftest.py`,
  `no_real_network_or_credentials`). It exists because a runner test once started a real run
  and most likely sent one live GET to the desk device.
- **"Command sent at …" in `response` is the cloud's delivery note, not an answer**
  (`cota.is_delivery_note`) — stage `delivered`, white ✓✓, never green.
- **In Jinja, never name a dict key `items`, `keys` or `values`.** `thread.items` resolves to
  the dict's own method before the key, and the template loops over a function. The console's
  list is `entries` for that reason.
- Device-map uploads do **not** go through `sources._validate_csv`, which demands an `IMEI`
  header. A map headed "Device Unique No" is valid, and `cota.import_device_map` names any
  missing column itself.
- **Jobs (`cota_campaign.py`) are many devices × one sequence**, by the sequence rules above,
  driven by one scheduler thread per job that ticks every 10 s. Devices on the same step go in
  one `saveCOTAConfig` call (batch size), calls are paced to the job's rate, a sleeping device
  is waited for rather than resent (validity, then `expired`), a canary goes first above 20
  devices, and the job pauses itself on three failed calls or more than 10% failed. A refused
  call is never an attempt. One job at a time; a device in a live job or sequence is refused
  everywhere else. Never call `cota_campaign.start` from a test — patch it, as `no_job_threads`
  does.
- **`getGPRSCommand` reads one device per call** — a comma list got HTTP 400 on the live cloud
  (2026-10-06). So a big job is bounded by *checks*: 30,000 × 5 is 162 sends and ~152,000
  checks, ~8½ h at the default 5 calls/s. Do not "optimize" sends; the rate is the lever, and
  what the cloud tolerates is still unknown.
- **The COTA record lives for a day** (the user's rule). `purge_previous_days` runs once per
  process, on the first COTA page: earlier days' jobs, sequences, sends and cloud records go;
  groups and the device map stay. A session that runs past midnight keeps its day.
- **Device lists upload as the cloud's own format, `id,trackingCode`** — id required,
  trackingCode (IMEI) optional, a header row optional; `/cota/groups/template.csv` is the
  template both upload forms link. Tracking codes are learned into the device map.
- **Scale is a test, not a guess:** `OTA_SCALE_DEVICES=30000` runs the full-fleet simulation
  in `tests/test_cota_campaign.py` (~4 min; 1,000 by default). Run it after touching the
  scheduler's queries — an unindexed lookup made 5,000 devices take 303 s instead of 48.
- **A user-supplied string never goes inside an inline script.** HTML-escaping does not protect
  it there: the attribute is decoded before the script runs. Put it in a `data-` attribute and
  read `this.dataset` (the group delete confirm does).

## Two upload routes, and the one thing they must not do

`/update/import`, `/update/bundle-import`, `/cota/devices/import`, `/cota/jobs/preview` and
`/cota/groups` are the only `async def` handlers in `api.py` —
they have to be, to `await file.read()`. **An `async` handler runs on the event loop, so calling
a job that takes tens of seconds from one stops the server answering anything at all** — every
page, and `/healthz` with it. It presented as "the import hung" when in fact the whole dashboard
had, with the browser spinning on a request that would never return. Both now hand the blocking
work to `run_in_threadpool`, and the sqlite connection is opened *inside* that function because
sqlite3 objects belong to the thread that created them. Every other route is a plain `def`,
which Starlette already runs in a threadpool — so if a new route does slow work, leave it `def`.

## Loading a `.csv` as well as an `.xlsx`

Both formats go through `ingest._table_rows`, then the same header mapping and the same
normalization, so a CSV cannot bypass a rule the spreadsheet obeys — `tests/test_ingest_csv.py`
checks that by loading identical rows through both and comparing every column.

- CSV gives every cell as **text**. `normalize.clean`, `parse_queue` and `parse_dt` already
  handle that (`parse_dt` accepts the platform's day-first form *and* ISO), which is why this
  works without a second set of rules. Do not add string-specific branches.
- Read with `utf-8-sig`: Excel writes a byte-order mark and so does our own CSV export, and left
  in place it becomes part of the first header name and stops `IMEI` matching.
- A CSV holding a **report this dashboard produced** is loadable but second-hand, and
  `looks_like_dashboard_report` flags it as `loaded_from_report`: its values have been
  normalized once already, so `device_model_raw` holds a canonical name rather than the
  platform's spelling, and columns a report never carried are absent rather than empty. The
  snapshot time is when the report was written. Prefer the platform's export or a bundle. This
  is recorded because none of it is visible from the numbers afterwards.

## Progress for long jobs (`progress.py`)

A merge takes minutes and a fetch takes tens of seconds. Both used to hold the POST open with
nothing to show, so the only feedback was a page that never finished loading — which is how a
working merge came to be reported as "no action, only loading".

- **The POST starts a job and returns immediately.** `api.run_job` puts the work on a plain
  thread; the page redirects and polls `/api/progress`. Holding the request open is what made
  the work indistinguishable from a hang, and it also meant the answer could never arrive,
  because it would have come back on the same response.
- **The bar is determinate and derived from work done** — snapshots folded, devices read — never
  from elapsed time. A bar that advances on a timer teaches people to ignore it exactly when it
  matters. Every step declares its total before it starts.
- **Steps are weighted by measurement, not evenly.** Replaying the change log is ~60% of a
  merge; even weights would leave the bar apparently stalled through it. Weights live next to
  the code that does the work: `bundle.MERGE_STEPS`, `ingest.INGEST_STEPS`,
  `scheduler.FETCH_STEPS`.
- **Progress lives on the server.** Closing the tab does not stop the job, and reopening
  `/update` finds it mid-flight — the panel is rendered from `progress.snapshot()` on load as
  well as polled. A spinner drawn in JavaScript cannot do that, and a two-minute merge invites
  someone to close the tab.
- **One job at a time.** They all write to the database, so two would queue on the write lock
  anyway — but silently, with two bars both claiming to move. A second start raises
  `progress.Busy`.
- The bar may under-report and then complete in a jump: appending folds the registry and rolls
  up metrics in one pass, so both phases finish together. Under-reporting then completing is the
  safe direction; claiming progress that has not happened is not.

## Never let a build touch data (`build.py`)

`dist/` is deleted to start a build clean, and **this install keeps its data there by choice** —
the database in `dist/InTouchOTA-Analytics/data`, bundles beside it in `dist/`, reports below it.
A build used to delete all of it: 48 snapshots of live fleet history, more than once. A printed
warning was tried first and was not enough, because it scrolled past.

So the rule is **inverted**: `build.BUILD_OUTPUTS` declares what the build itself produced, and
`user_files()` treats everything else in `dist/` as the user's. It is moved aside before the
delete and put back after the zip is written, so a handover file cannot carry a database either.
Listing what to *delete* rather than what to keep is the point — the first version rescued a
hard-coded `data` folder, so the database survived and the bundles and reports beside it did
not. Now a kind of file nobody anticipated is preserved by omission instead of destroyed by it,
for the same reason `auth.py` denies by default. An interrupted rescue refuses rather than
overwriting the parked copy, and on a name collision the rescued copy wins — a fresh build ships
an empty `data` folder, and theirs is the only real one.

## One vocabulary, one palette

Every task state has exactly one name and exactly one colour, everywhere it appears — tile,
donut, table header, download filename, CLI summary.

| State | Colour | Meaning |
|---|---|---|
| `Task completed` | green `#35c48a` | tasked, nothing outstanding |
| `Task pending — Online` | **yellow** `#e8c33c` | reachable and still not updated — the actionable one |
| `Task pending — Offline` | orange `#e8893c` | parked until the device is switched on |
| `No pending task` | grey `--muted` | never targeted, which for an EOL model is correct |

**`Activation-Pending`** is the never-pinged state. The platform calls it `Inactive`, which reads
like a device that stopped working; these have never started. On the real fleet the 645 devices
with `status = 'Inactive'` are exactly the 645 with `seen_at IS NULL`, and they carry an IMEI and
nothing else — no VIN, no ICCID, no first ping, never tasked. They are onboarded and waiting to be
activated, and the fleet-health question is whether that number is falling.

- **The stored value stays `Inactive`.** That is the word the export used, and re-labelling stored
  data would make the database and the platform disagree. Only the reading changes, through
  `normalize.STATUS_LABELS` and the `status` Jinja filter — one mapping, so no template invents
  its own.
- **Downloads carry the same word as the screen** (`exports._label_status`). A file saying
  "Inactive" where the dashboard says "Activation-Pending" turns one source of truth into two.
- An unrecognised status is shown as it is, not hidden behind a fallback.
- Note this is *not* the same set as the `(unknown)` model/firmware rows: 674 devices report
  neither, of which 645 have never pinged and 29 have pinged but the platform sends no model or
  firmware for them. The second group is a data gap, not activation-pending.

- **Do not write "stuck", "stalled" or "failed" about a pending task.** The platform assigns
  tasks in bulk to devices that are switched off, so a pending task is *parked*, and the word
  carries a judgement the data does not support. The page previously said "Stuck while
  reachable" and coloured it red like a fault; it is `Task pending — Online` in yellow. What
  matters is reachability, not blame.
- **Neither pending state is coloured like an error.** Red is for a genuine fault. Yellow says
  "worth chasing", orange says "waiting by design".
- `metrics.pending_online_devices` is the function behind the actionable list; downloads are
  named `pending_online.*`. If a name needs changing, change all of them — a donut and a tile
  showing the same number under different words is how people stop trusting both.
- `seg-yellow` / `seg-orange` reuse the tile hex values exactly, so a chart and a tile for the
  same state cannot drift apart.

## Reading a snapshot: resolve the view once, not once per metric

`device_state` is a view. Every reference re-resolves each device's most recent row at or before
the snapshot, across the whole fleet. Measured on the real 48-snapshot database: **1.0s per
reference**, against 0.01s for a plain count off the physical table — and a page calls six to ten
metrics. The overview took 12.7s and switching pages felt broken.

`metrics.snapshot_source()` materializes the requested snapshot once into a temp table `_snap`
and every per-snapshot metric reads that instead. Result: overview 12.7s → **1.05s**, pending
9.3s → 2.5s, firmware 5.2s → 0.9s, devices 4.3s → 1.5s.

**That was not enough on its own, because the cost grows with snapshot count and `_snap` does
not outlive a request.** By 245 snapshots a single reference was 4.5s and building `_snap` was
10.9s — and `get_conn()` opens a new connection per request, so a temp table is rebuilt on every
page load. The overview was back to 18s, devices 42s, pending 77s.

So the newest snapshot — which is what almost every read asks for, and which only changes when a
fetch lands — is resolved once *when that happens* and kept in `device_current` (`current.py`).
`snapshot_source()` returns it whenever `device_current_meta` holds exactly the snapshot asked
for. Advancing it is incremental: a fetch of a fixed fleet changes ~150 devices, so it costs
~150 upserts rather than re-resolving 35,848.

- **Anything that writes `device_snapshot` directly must refresh the materialization**
  (`current.refresh_latest`, or `invalidate` to fall back to the view). Ingest, `retention.prune`
  and `bundle.import_bundle` all do. The guard is snapshot *identity*, not content: a copy that
  holds snapshot N is trusted for snapshot N, so mutating N's rows underneath it is the one way
  to get a stale answer — and the append-only rule is what normally makes that impossible.
  A fixture doing surgery on stored rows is the realistic case, and `test_fallback_tag` shows it.
- **Empty is safe.** With nothing materialized, readers fall back to the view: slower, never
  wrong. That is what makes it correct to invalidate whenever anything is uncertain.
- **A per-snapshot read that does not go through `metrics.at()` is a bug that only shows up as
  slowness.** It returns the right answer, so nothing fails; it just costs a full fleet
  resolution. Three were found this way, all outside metrics.py and all on the request path:
  `rollup.fragmentation` (called by `kpis`, so every page with headline numbers), and the count
  and the select inside `api._device_rows` — two resolutions per load, which is why `/devices`
  took 41s. Grep for `device_state` outside metrics.py before assuming a page is slow for an
  interesting reason.

Measured on the 245-snapshot database, before → after: `/` 18.36s → **0.84s**, `/devices`
41.54s → **0.25s**, `/pending` 76.79s → **0.45s**, `/firmware` 6.95s → **0.28s**.

- **Every metric keeps its own `WHERE snapshot_id = ?`.** A caller asking for a different
  snapshot than the one held gets *nothing* rather than the wrong rows, and a metric that still
  reads the view is merely slow. Both failure modes are safe; neither is silent wrongness.
- **The temp table is in SQLite's temp schema, not the main database**, so building it takes no
  write lock on `ota_analytics.db`. A read path stays a read path.
- **Which snapshot is held is read back out of `_snap` itself.** `sqlite3.Connection` does not
  accept attributes and a dict keyed on the connection would outlive it; the rows carry
  `snapshot_id` anyway.
- `metrics.at(conn, snapshot_id, sql)` swaps the table name on the finished SQL rather than
  interpolating into the literal. That is deliberate: the query text in the file is exactly what
  runs. Editing inside the literals corrupted them twice while this was being written — once
  turning SQL's `'Online'` into `'Onlinef'`, once rewriting the helper's own query into a
  reference to itself.
- **Anything that spans *all* snapshots is the thing to be suspicious of.** It grows by another
  fetch every 15 minutes. `registry.stalled_devices` grouped `device_state` across every
  snapshot — 245 × 35,848 resolved rows to answer a question about the present — and took 118s.
  It now reads the registry and the change log instead, which already record when each device
  became pending and when its firmware last moved.
- **A threshold counted in snapshots is a threshold in disguise.** That same function called a
  device stalled after 3 consecutive pending snapshots. At the original daily export that meant
  three days; at the 15-minute cadence it means 45 minutes, so 26,481 devices qualified and the
  list stopped meaning anything. `config.STALL_HOURS` is in hours for that reason.
- If you touch metrics.py: Python 3.12 tokenizes f-strings as `FSTRING_START/MIDDLE/END`, not
  `STRING` (PEP 701), and adjacent literals are one implicit concatenation. Any script that
  rewrites SQL here has to handle both.

## Resolving a snapshot: the view states the meaning, `resolve.py` does the work

The page work above fixed the *reads*. It left the writes, and by 272 snapshots a single fetch
took **194 seconds** — sometimes 450 — while the hourly timer kept firing at it. Same shape as
before: nothing errors, the numbers stay right, it just takes longer every week.

`device_state` expresses resolution with a correlated subquery per row, and SQLite plans that as
a full scan of `device_snapshot`. Its cost is proportional to *all history*, not to fleet size.
The same answer comes out of a grouped join on `ix_ds_imei_snap` — **17.5s → 1.3s for all 27
columns of all 35,848 devices**, byte-identical on nine snapshots including the first and last.

- **It cannot be a view.** The snapshot id has to bound the inner `GROUP BY`, and SQLite has no
  LATERAL. Two other formulations were tried and are *slower* than the view: `ROW_NUMBER() OVER
  (PARTITION BY imei)` at 17.4s, and adding `d.snapshot_id <= s.id` to the view's join at 12.5s.
- **The view stays in the schema regardless.** It is the readable statement of what resolving
  means and it is what `tests/test_resolve.py` holds the fast path against. An optimization with
  nothing to check it against is just an assertion.
- **Three ways in, cheapest first**: `resolve.source()` returns the table to read a snapshot
  from — free when `device_current` already holds it; `resolve.materialize()` builds a named
  temp table; `resolve.select()` is the bare SELECT, for streaming. `metrics.at` delegates here,
  so nothing that already used it had to change.
- **Every per-snapshot query keeps its own `WHERE snapshot_id = ?`.** A caller handed a copy of
  the wrong snapshot then matches nothing, which is visible, rather than returning plausible
  rows for the wrong moment.
- **The tripwire is at runtime, not in the source.** `tests/test_db_performance.py` traces the
  SQL actually executed and fails if an ingest, rollup, registry fold, quality run or page
  metric resolves the view. Grepping the source would be wrong: naming `device_state` in a
  literal is correct, because `resolve.at` swaps the name onto the finished statement so the
  query in the file is what it appears to be.

Where the 194 seconds went, and where it went to:

| Step | Before | After |
|---|---|---|
| Storing what changed | 9.0s | **1.6s** |
| Checking data quality | 25.9s | **1.0s** |
| Updating the device registry | 150.6s | **4.7s** |
| Rebuilding metrics | 8.7s | **0.5s** |

## Statistics, and the 367-second statement

**`ANALYZE` had never run on this database.** Without statistics SQLite guesses, and here it
guessed that `ix_change_field` was the way into `device_change` — so the registry's
`prev_firmware` update scanned all 21,097 rows with `field = 'firmware'` once per device. 756
million row visits. **367 seconds, in one statement, on every fetch.** With statistics it picks
`ix_change_imei` and takes **0.21s**.

Migration v9 runs `ANALYZE` once (1.2s on the live database) and `db.refresh_statistics`
(`PRAGMA optimize`) runs after every ingest, because statistics that are never refreshed go
stale as the database grows. `PRAGMA optimize` writes, so it may never run on a read path.

This is the cheapest fix in the project's history and it was invisible for a month: the wrong
plan produces the right answer.

## Retention has to be able to fire

Retention thins by age — everything for 2 days, then hourly, daily, weekly — and it also refused
to prune any snapshot that recorded a change. That sounds careful. At the hourly cadence the
tool actually runs at, a fetch of 35,848 devices always contains *some* change, so **268 of 272
snapshots were exempt**: retention removed nothing on any run, ever, while the database grew
~45 MB a day. On the live data the rule now thins 177 of 273 snapshots and 587,211 of 1,081,179
device rows.

- **If you add a protection rule, work out what fraction of real snapshots it exempts.** Above a
  few percent it is not a safeguard, it is an off switch.
- **Thinning costs time resolution, never facts.** A pruned snapshot's device rows *and* its
  change-log rows move onto the next survivor; `changed_at` keeps the real time of the move. The
  test asserts every surviving snapshot resolves identically before and after — keep that true.
- **An automatic prune is bounded** (`retention.AUTO_PRUNE_LIMIT`, 20 snapshots). It holds the
  write lock inside a fetch, and the first run after upgrading had 177 of backlog — 61 seconds
  of the app apparently hanging. The rest is taken by the next few fetches; the policy describes
  the shape history should have, not a sequence that must complete. The CLI's `prune` is
  unbounded, because someone is watching it.

## One fetch at a time

A scheduled fetch used to run `_fetch_and_ingest` directly with a silent job, invisible to
`progress` — so "one job at a time" did not cover the one job that runs by itself. Pressing
"Fetch now" during a scheduled fetch started a second concurrent writer; one waited out the 30s
busy timeout and died with `database is locked`. Twice in the live error log, both at the very
first INSERT of the run.

The timed fetch now takes a job like everything else. A tick that finds one already running
**skips** rather than queueing — the next tick is along shortly and its data is fresher — and
records `last_status = "skipped"`, which is not counted as a failure. A job nobody asked for
also clears itself when it finishes, or every visit to Update Data would open on a stale panel.

## Building a file is a job; downloading it is a download

Reported as "export is not working": the bundle. Nothing raised and nothing was logged — a full
history is ~960,000 rows of JSON and the better part of a minute, and it was assembled whole in
a `BytesIO` before a single byte reached the browser. As a plain download that is a request
which does not come back: no bar, no download indicator, nothing to distinguish it from a hang.

- **`POST /update/bundle` builds it as a job** with a determinate bar drawn from rows written,
  then the finished job carries `download` and the page renders a link. Collecting is instant,
  because the file already exists. `GET /update/bundle` still builds one inline for scripts and
  short histories — same builder, so the two cannot drift.
- **Nothing large is assembled in memory any more.** The bundle streams to a scratch file;
  `exports.to_xlsx` uses openpyxl's `write_only=True`, which took a full device export from
  **190 MB of Python objects to 3 MB** for the same 3.1 MB file, at the same speed. The cost is
  that nothing can be revisited after it is written, so column widths, the freeze and the filter
  range are all set up front and the IMEI text format goes on the cell as it is created.
- **Clean up on the error path too** — that is the one nobody checks.

## Two skills carry these rules

`.claude/skills/db-performance` and `.claude/skills/long-jobs-ux`. They exist because the same
two mistakes have now been made in three different places each, and a rule written only in prose
gets read once. Load them before touching the warehouse or adding a slow route.

UI and UX follow the user-level skill **`utility-ui`** (`~/.claude/skills/utility-ui`), shared
with the CAN utility: sidebar workbench, readiness pills, per-module chips, credential forms,
release/dev side by side. Load it before changing any page, nav item, form or style.

## Theme: three states, and the CSS order that makes them work

- **Never style a bare `header` (or `nav`, `aside`, `section`).** `header { position: sticky }`
  made the console's own heading stick and paint over the top bar on scroll. The top bar is
  `header.topbar`; `test_no_bare_header_rule_can_make_other_headings_sticky` holds it.
- **The dev copy flags stale code** (`api.code_is_stale`): Python on disk newer than the running
  process shows "Restart to load new code". If a page looks wrong after an edit — blank icons,
  missing data, a 500 — check for that chip before debugging.

- **Every theme state declares `color-scheme`.** Without it the browser draws its own controls
  — scrollbars, dropdowns, date pickers — light, whatever the page looks like: a white scrollbar
  on the dark theme, which the user rightly called unacceptable. Scrollbars are also styled
  thin in the theme's own colours, globally, and a test holds both.

`:root` carries the dark palette. `@media (prefers-color-scheme: light)` applies the light one
**only** through `:root:not([data-theme="dark"])`, and each explicit choice is then restated in
`:root[data-theme="light"]` and `:root[data-theme="dark"]`.

- A light palette defined *only* inside the media query could never be switched on by someone
  whose system is dark — an explicit choice has to win in both directions.
- The chosen theme is applied by a tiny **inline script in `<head>`**, before the stylesheet
  paints. An external file arrives too late and the reader gets a flash of the wrong theme on
  every page load.
- "Auto" is a real third state, not the absence of a choice: it removes the attribute so a
  machine that switches at dusk switches with it. `localStorage` is wrapped in try/catch because
  private browsing blocks it, and the system theme must still work there.

## Hard rules

- **Ingest is append-only and idempotent.** Re-ingesting the same file (matched by SHA-256)
  must be a no-op, not a duplicate. Never UPDATE or DELETE snapshot rows.
- **`device_snapshot` stores changes, not fetches — always read `device_state` instead.**
  A fetch of a fixed fleet is nearly identical to the one before it: measured on the real
  database, consecutive fetches differed in ~150 of 35,475 devices. Writing a full copy each
  time cost ~23 MB per fetch, which at a 15-minute cadence is ~2.2 GB/day of duplicates. So a
  row is written only when a device actually changes, plus a `present = 0` tombstone when the
  platform stops listing it.

  `device_state` is a view that reconstructs any snapshot from those rows (each device's most
  recent row at or before it) and carries its own `snapshot_id`, so `WHERE snapshot_id = ?`
  works exactly as it did against the old full-copy table. **Every read resolves a snapshot** —
  querying `device_snapshot` directly returns only the devices that changed in that fetch,
  which looks like a working query and is silently wrong. Only ingest and retention touch the
  physical table. **Resolve through `resolve.py`, not the view itself**: the view is the
  definition, and reading it costs a scan of all history (see the section above). Verified on the real database: 19/19 snapshots resolve identically, rows fell
  88.5% and the file went 305 MB → 52 MB.
- **Never store a value derived from the snapshot time.** `seen_age_hours` is `snapshot_at`
  minus `seen_at`, so storing it made *every row of every fetch* differ and defeated the scheme
  entirely — compaction removed 6.7% of rows with it included and 87.2% without. `device_state`
  computes it. The same trap applies to anything else measured relative to "now".
- **Retention carries state forward; it never just drops rows.** A stored row is the
  authoritative value for that device in every later snapshot until the next change, and
  snapshots cascade-delete their device rows. Pruning one without moving its rows onto the next
  surviving snapshot would not thin history, it would rewrite it.
- **Keep raw and normalized side by side.** Store `device_model_raw` *and* canonical
  `device_model`. The source data has `AX1_SCAN` / `AX1_sCAN` / `sCAN_AX1` as three spellings of
  one model — normalize for analysis, but never lose the original.
- **`-` means null.** The export uses the literal string `-` as its null marker across many
  columns (681 device models, 674 firmwares, 6,093 group values, 1,251 first pings).
  Convert to SQL NULL on ingest; never let `-` become a chart category.
- **Dates are `DD-MM-YY HH:MM:SS`** — day first, 2-digit year. `15-08-26 15:11:17` is
  15 Aug 2026. Parse explicitly; do not let any library guess month-first.
- **A field the source did not send is unknown, not empty.** Ingest carries such columns forward
  instead of writing NULL, and leaves them out of the change comparison. The platform API sends
  no group information at all, and treating that as NULL wiped the groups of 29,384 devices on
  every API fetch — while also making every device look changed at the source boundary.
  `ingest.provided_columns()` decides this, and it must include *derived* columns (`firmware`
  from `firmware_raw`, and so on) or a real change gets written next to a stale canonical value.
- **`device_state` is a view, so referring to it twice costs twice.** Each reference re-resolves
  every device's most recent row across the whole fleet. `registry.apply_snapshot` referred to
  it fifteen times — once per tracked field, plus the upsert and two counts — which cost ~14s
  per snapshot and was paid on *every fetch*, not just on a rebuild. It now materializes the
  snapshot once into an indexed temp table: replaying 37 snapshots went from 533s to 66s with
  byte-identical output. When a function needs the same snapshot more than twice, resolve it
  once. (Note the `WHERE 1` before `ON CONFLICT` in that upsert: without a WHERE clause SQLite
  cannot tell `ON CONFLICT` from a join's `ON` and rejects the statement.)
- **Merging only has to materialize what actually lost its neighbour.** `bundle.plan_densify`
  compares each snapshot's predecessor before and after the merge; only those that differ need
  their inherited rows written out. Densifying everything is also correct and is what the first
  version did — it turned a 37-snapshot merge into 1.3M staged rows that never finished. The
  bundle's baseline is dense by construction and is never in the plan.
- **A staging table needs the same indexes as the table it stands in for.** `stage_bundle`'s
  primary key is `(seq, imei)`, but resolving a chain looks a device up *across* sequences, so
  it also needs `(imei, seq)` — mirroring `ix_ds_imei_snap`. Without it every snapshot scanned
  the whole staging table.
- **Never open a write transaction on a read path.** `db.connect()` migrates once per process,
  not once per request: re-applying the schema on every page load cost 0.25s and, because it
  writes, serialized every concurrent reader behind it. `busy_timeout` is 30s because a whole
  ingest is one transaction lasting up to 10.6s.
- **Never query the platform's production DB from a request path** (applies from Phase 6 on).
- No secrets in the repo. `ANTHROPIC_API_KEY` comes from the environment or `.env` (gitignored).

## Working rules for this repo

- **Branch per version, and `main` only on request.** Work happens on a branch named exactly the
  app version (`1.7.0`), and the version in `ota_analytics/__init__.py` matches the branch it is
  on. Once something is pushed to `main`, the next change starts on a new branch — never carry on
  committing to `main`.
- **Three shipping steps, each containing the one before it** — the `ship` skill has the detail:

  | Say | Means |
  |---|---|
  | **push** | commit → update the literature → push the version branch |
  | **land** | push, then merge into `main` and start the next version branch |
  | **release** | land, then build the exe and tag `v<version>` |

  Note "push" is deliberately *not* called "commit": in git that word means the local step, and
  giving it a second meaning here would collide with every terminal command.
- **One version number for the whole application.** `ota_analytics/__init__.py:__version__` is
  the only declaration; `build.py` imports it and generates the .exe's version resource from it,
  and the UI reads it through `build_info()`. Never add a second — a UI version and a build
  version drift, and the one that lags makes a bug report point at the wrong code.
  `tests/test_version_hygiene.py` fails if a second declaration appears, if the changelog has no
  section for the current version, or if the branch name and the version disagree.
- **The version is set once, when the branch starts** — not on every push. Later pushes go to the
  branch already named for it.
- **A release is not done until the running copy says so.** A long-lived process holds the old
  code in memory and keeps reporting the old version after a build. Ask it (`/api/version`) and
  say plainly if it needs restarting. A release was once reported as finished while every screen
  the user looked at still showed the previous version: the filename was right and the running
  app was wrong, and only one of those is visible.
- **Never build unless asked.** `python build.py` is not part of finishing a change. It happens
  at a release.
- **Never leave scratch scripts in the tree.** Patch helpers written to make an edit are deleted
  in the same step, not committed.
- **`dist/` holds live data on this machine** — the database, bundles and reports. Treat anything
  in it as the user's; see the build-safety section.

## Commands

`main.py` at the repo root is the one-click entry point: it creates the database, ingests any
new exports, rebuilds metrics, and serves the dashboard. In VS Code, press F5 or use the ▷ Run
button. `.vscode/launch.json` also has configs for ingest, status, quality and tests.

```powershell
python main.py                 # load new exports + serve + open browser
python main.py --no-ingest     # serve only
python main.py --port 8080 --no-browser
```

The `ota_analytics.cli` commands below do the same work in smaller pieces:

```powershell
# install deps
python -m pip install -r requirements.txt

# ingest one export (idempotent)
python -m ota_analytics.cli ingest "Sample data\Devices_35477_15Aug26_1511.xlsx"

# ingest every new export in a folder
python -m ota_analytics.cli ingest-dir "Sample data"

# rebuild derived facts from raw snapshots
python -m ota_analytics.cli rollup

# run the dashboard
python -m ota_analytics.cli serve          # http://127.0.0.1:8000

# generate a report
python -m ota_analytics.cli report --format xlsx --out reports/

# hash a dashboard password (prompts — never pass it as an argument, it would land in
# shell history and in `ps` output). Prints the environment line to deploy.
python -m ota_analytics.cli passwd --role admin
python -m ota_analytics.cli passwd --role viewer

# who am I, and exactly what data do I hold (compare this before comparing numbers)
python -m ota_analytics.cli db-info
python -m ota_analytics.cli db-info --label shahbaz-laptop
python -m ota_analytics.cli db-info --compare theirs.otabundle

# share snapshot history between two installs
python -m ota_analytics.cli db-export --out share.otabundle
python -m ota_analytics.cli db-export --since 2026-08-15 --out gap.otabundle
python -m ota_analytics.cli db-import theirs.otabundle --inspect
python -m ota_analytics.cli db-import theirs.otabundle
python -m ota_analytics.cli db-import theirs.otabundle --allow-interleave   # slow, rebuilds

# reclaim file space and refresh query statistics (stop the app first)
python -m ota_analytics.cli vacuum
python -m ota_analytics.cli vacuum --no-analyze

# apply the retention policy by hand — unbounded, unlike the one a fetch runs
python -m ota_analytics.cli prune --dry-run
python -m ota_analytics.cli prune

# tests
python -m pytest -q
```

## Layout

```
main.py          one-click entry point: ingest + serve (VS Code F5 target); check_exposure()
ota_analytics/
  cli.py         entry point for every command above
  config.py      paths, settings, env
  db.py          sqlite connection, migrate-once, versioned migrations
  schema.sql     DDL, versioned, plus the device_state view
  resolve.py     resolving one snapshot fast — what device_state means, without the scan
  identity.py    db_id, instance label, fleet digest — "are we looking at the same data?"
  bundle.py      export/import snapshot history between installs (merge, not replace)
  ingest.py      export -> change rows (streaming, idempotent, delta writes)
  normalize.py   model/firmware canonicalization, date parsing, '-' handling
  registry.py    current device state + the change log
  rollup.py      snapshot tables -> fact tables
  metrics.py     one function per dashboard metric, returns plain dicts
  quality.py     data-quality rules
  retention.py   thinning with carry-forward; densify/compact/renumber for merging
  auth.py        roles, scrypt passwords, signed session cookies
  sources.py     platform connection + credentials (keyring / env)
  nav.py         the sidebar: modules, pages, icons — the one definition
  cota.py        Intouch COTA: cloud client, sends, the cloud's command records, the console
  cota_connection.py  COTA sign-in: cloud presets, token/password (keyring / env), renewal
  cota_run.py    sequences: one device, commands one at a time
  cota_campaign.py    jobs: many devices × a sequence, the scheduler, groups, day retention
  scheduler.py   periodic fetch and rollup
  errors.py      failure log shown at /errors
  exports.py     XLSX report generation
  api.py         FastAPI app, routes, auth middleware, templates
  web/           templates/ + static/
deploy/          systemd unit + env template; windows/ launcher for local hosting
docs/            PROJECT_PLAN.md, IMPLEMENTATION.md, DATA_PROFILE.md, DEPLOY.md
data/            ota_analytics.db, secret.key (gitignored)
Sample data/     platform exports (gitignored except .gitkeep)
tests/
```

## Working conventions

- **Tests never touch this machine's data.** `tests/conftest.py` redirects every path under
  `data/` to a temp folder for every test (`isolated_data`), and `never_the_live_database` fails
  any test that opens `data/ota_analytics.db` or the release's copy in `dist/`. Seven tests had
  been opening the real database before this existed. A new module-level path under
  `config.DATA_DIR` must be added to `isolated_data`.

- Metric functions in `metrics.py` return plain Python dicts/lists — no ORM objects, no
  DataFrames crossing module boundaries. Makes them trivially testable and JSON-serializable.
- Every fact table is rebuildable from the snapshot tables. If a rollup is wrong, fix the code
  and re-run `rollup`; never hand-patch fact rows.
- New metrics need a matching test in `tests/` with a small fixture, not the 22 MB file.
- Prefer one SQL statement over a Python loop over rows.
