# InTouch OTA Analytics

An analytics layer over the InTouch OTA platform, which pushes firmware updates to field devices
but surfaces almost no analysis of what happened. This adds the analysis: a local warehouse of
device snapshots, a dashboard over them, and downloadable reports.

Local-first, single file database, no Node build step, works offline.

## One vocabulary

Every task state has one name and one colour, everywhere — tile, chart, table, download,
CLI:

| State | Colour |
|---|---|
| Task completed | green |
| **Task pending — Online** | **yellow** — reachable and still not updated, the actionable one |
| Task pending — Offline | orange — parked until the device is switched on |
| No pending task | grey |
| **Activation-Pending** | grey — onboarded, IMEI only, has never pinged |

Nothing here is called "stuck" or "failed". The platform assigns tasks in bulk to devices that
are switched off, so a pending task is *parked*; the word would carry a judgement the data does
not support. `Activation-Pending` is the platform's `Inactive` renamed for what it is — on the
real fleet those 645 devices are exactly the ones with no last-ping, no VIN, no ICCID and no
task ever assigned. The stored value stays `Inactive`, because that is what the export said.

## The question it exists to answer

The platform assigns update tasks in bulk, including to devices that are switched off. A pending
task is therefore *parked*, not failed — so a large pending count is normal and means nothing on
its own. There are exactly two reasons a device is pending:

1. **Powered off or out of service.** Self-resolving, expected, the large majority.
2. **Powered on but unable to reach the OTA platform.** The device pings fine, so it looks
   healthy, but the update cannot be delivered. This is a real fault.

The platform shows these identically. **Separating them is the point of this project.** In the
sample export that second group is 155 devices out of 7,859 pending — the smallest number in the
dataset and the only one worth acting on. The dashboard leads with it, and the live count moves
as the fleet does.

## Quick start

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe main.py
```

That creates the database, ingests any exports in `Sample data\`, rebuilds metrics, and opens the
dashboard. In VS Code, press **F5**. From source that is <http://127.0.0.1:8100>; the
packaged release uses <http://127.0.0.1:8000>.

Use `.\.venv\Scripts\python.exe` rather than a bare `python` — several Pythons are usually
installed and only the venv has the dependencies.

```powershell
.\.venv\Scripts\python.exe main.py --no-ingest      # serve what is already loaded
.\.venv\Scripts\python.exe main.py --port 8080
.\.venv\Scripts\python.exe -m pytest -q             # 899 tests
```

**Running the release and a development copy together.** The packaged release serves on
<http://127.0.0.1:8000>. A copy run from source serves on <http://127.0.0.1:8100>, wears a
**DEV** badge, and keeps its own database (`data\` in the repo) and its own login cookie. Start
both and neither defers to the other. `OTA_CHANNEL=release` or `dev` overrides the guess.

## How the data works

The platform export is a **point-in-time inventory, not an event stream**: 35,477 rows describing
what every device looks like right now. There is no campaign id, failure code, retry count or
duration anywhere, and the platform owner has confirmed no richer log exists.

So all movement comes from **diffing snapshots**. One export gives you state; two or more give
you change. That single fact drives the whole design — ingest is append-only, and the snapshot
timestamp comes from the filename (`Devices_35477_15Aug26_1511.xlsx` → 2026-08-15 15:11), because
it is not a column.

**Storage is per change, not per fetch.** A fetch of a fixed fleet is nearly identical to the one
before it — measured on real data, consecutive fetches differed in about 150 of 35,475 devices.
Writing a full copy each time cost ~23 MB per fetch, or ~2.2 GB/day at a 15-minute cadence. Now a
row is written only when a device actually changes, and the `device_state` view reconstructs any
snapshot from those rows. Same numbers, 88.5% fewer rows, and a database that stays nearly flat.

**Both `.xlsx` and `.csv` load**, with columns matched by name rather than position, so a
colleague's file works whatever order its columns are in. Keep the original filename either way —
the snapshot time is read from it, because no column carries it. A CSV that turns out to be a
report this dashboard produced still loads, but is marked second-hand on the quality page: its
values have already been normalized once and it drops columns the platform sends.

**Always resolve a snapshot, never read `device_snapshot` raw.** The physical table holds only
what changed in each fetch, so querying it directly returns a partial fleet — a query that looks
correct and is silently wrong. `device_state` is the definition of resolving; `resolve.py` is how
code actually does it, because reading the view scans all of history.

## Two installs, one reference point

A dev machine and a packaged build, or two people working offline and syncing at different times,
hold different snapshot sets and therefore report different numbers. That gets argued about
instead of checked, so the footer of every page carries a **fleet digest**:

```
Shahbaz · data 8b7ca8fd · 27 snapshots
```

Same digest, same numbers — guaranteed, because every derived table is rebuildable from the raw
snapshots and the digest is a hash of the exports loaded. Compare eight characters before
comparing anything else. It is also on a `Source` sheet in every XLSX report, so a file that has
been emailed around still says which dataset produced it.

Hashing the `.db` file would not work: two copies holding identical data are never byte-identical
(rowids, ingest timestamps, WAL state, vacuum history), so it would report a mismatch every time.

When the digests differ, swap the missing fetches:

```powershell
# on the machine that is ahead
python -m ota_analytics.cli db-export --out share.otabundle

# on the machine that is behind
python -m ota_analytics.cli db-import share.otabundle
```

The bundle carries the snapshots, not the database, so the two histories **merge** rather than one
replacing the other — and re-importing changes nothing. On real data a 26-snapshot bundle is
3.5 MB against a 63 MB database; catching up on a few fetches takes seconds. The same thing is on
the **Update data** page under *Share this data*.

Importing snapshots dated *inside* history you already hold is refused unless you ask for it:
inserting a fetch into the middle of a timeline changes what every unchanged device appears to
have been doing. `--allow-interleave` does it properly, but rebuilds everything and takes minutes.

## Building the application

```powershell
.\.venv\Scripts\python.exe build.py
```

Produces `dist\InTouchOTA-Analytics-v<version>.zip` (~27 MB), which unpacks to a folder of the
same name, e.g. `InTouchOTA-Analytics-v1.9.1\`. The recipient unpacks it anywhere and runs
`InTouchOTA-Analytics.exe` — no Python, no install. It is the only executable in the release.

**The app is portable: its database lives in a `data` folder beside the .exe.** Copy the folder
to move or back up the history; delete it and you start empty. `OTA_DATA_DIR` still overrides.

**Upgrading:** each release unpacks to its own versioned folder, so close the old copy and move
its `data` folder into the new one before the first start. The database is upgraded on start.

The CLI is the same executable: `InTouchOTA-Analytics.exe db-info`, `... db-export --out
share.otabundle`, `... passwd --role admin`, `... vacuum`.

Launching the app twice does not start a second copy: it detects the one already running and
opens that, rather than quietly serving on another port with a second scheduler behind it.

**"Start with Windows" was withdrawn in 1.2.1.** It worked, but what it produced was a copy of
the dashboard running with no window — holding the port, fetching on its own schedule, and
impossible to see or stop from the dashboard itself. Auto-fetch still runs on its schedule
whenever the app is open. An entry left armed by an earlier version is removed on next start.

**A build cannot delete your data.** `dist/` is cleared to start clean, and an install that lives
there keeps its database, bundles and reports in it. `build.py` declares the four files it
produces and moves everything else aside, putting it back after the zip is written — so the
handover file cannot carry a database either. Warning about it first was tried and was not
enough: the warning scrolled past and the data went anyway.

## While it works

A merge takes minutes and a fetch takes tens of seconds, so both report a **determinate progress
bar** — the width comes from work completed (snapshots folded, devices read), never from elapsed
time. The job runs on the server, so closing the tab does not stop it and reopening **Update
data** finds it mid-flight. One job at a time; they all write to the database — including the
scheduled fetch, so "Fetch now" can no longer collide with it.

**Building a bundle is a job too.** A full history is most of a minute to write; the page shows the
bar, then offers the file, and collecting it is instant.

The header carries one freshness chip — `Updated 09:06 · 3 hr ago · auto 1 hour` — and a theme
switch cycling **auto / light / dark**, applied before first paint so there is no flash of the
wrong theme.

## Speed

The slowdowns here all have one shape: nothing errors, the numbers stay right, and the work takes
longer every week as history accumulates. Measured on the live install, not guessed:

**A fetch** went from 194s (and 8+ minutes after a few days offline) to about 10s in 1.9.0.
SQLite had never been given query statistics and read the change log by the wrong index — one
statement took **360 seconds**; with `ANALYZE` it takes 0.74s. And `device_state`, a view whose
cost grows with every fetch ever taken (18s per reference at 362 snapshots), was being resolved
eighteen times per fetch. `resolve.py` gives the same rows from a grouped join instead.

**Pages** read the newest snapshot from `device_current`, kept resolved as each fetch lands, so
the overview, devices and pending pages answer in about a second whatever the history length.

**Retention actually thins now.** It used to refuse to prune any snapshot that recorded a change,
which at an hourly cadence was 99% of them. On the live data that is 777 MB → 431 MB, with the
change log intact.

Every per-snapshot query keeps its own `WHERE snapshot_id = ?`, so a mismatched snapshot returns
nothing rather than the wrong rows. `tests/test_db_performance.py` fails if a fetch or a page
starts resolving the view again.

## Intouch COTA — configuration over the air (2.0.0, in progress)

A second module, beside the analytics, that **sends** configuration commands to devices through
the InTouch cloud's COTA API (ctvms IntouchAdminApi) and records what each device answers. It
shares nothing with the snapshot warehouse. It has its own tables, its own device map
(cloud device id ↔ IMEI) and its own sign-in, kept in the credential store like the platform
password.

- **Configure**: a conversation with one device. Send a command, see it accepted (✓), listed by
  the cloud (white ✓✓) and answered by the device (green ✓✓). Or run a **sequence**, several
  commands one at a time — 30 s for an answer, up to 3 attempts, then the next command.
- **Jobs**: the same sequence for many devices, up to the whole fleet. The page opens on today's
  dashboard — tiles, outcomes per job, answers per hour — and today's jobs; *New job* chooses the devices by group, by typed ids, or from a CSV of `id,trackingCode`
  (*Download template* gives the format), names each command as it is typed, and previews the
  plan before anything is sent. The job page shows progress as tiles and graphs (outcome split, time to
  answer, answered on which attempt), a command summary, and every device — each unfolds to its own conversation — with pause, resume,
  cancel, duplicate, rerun of the unanswered devices, and export.
- **Commands**: your names for parameters (`6C0A` → a name) and for whole commands, with tags —
  every page and export reads them, and Configure and Jobs pick saved commands from a list.
- **Devices**: send one command to a list, load the device map, save groups.

The COTA record lives for the day: the first COTA page opened on a later day clears the earlier
days. Groups and the device map are kept. See [COTA.md](COTA.md) for the cloud API, the rules
agreed for sending, the scale figures and the open questions.

## Hosting it

Runs on anything: peak memory for a full 35,475-device fetch is **25 MB**. At an hourly cadence the
database settles at a few hundred MB once retention has thinned old history — run
`python -m ota_analytics.cli vacuum` (app stopped) to hand freed space back to the disk.

- **On your own machine, for the office network** — free, and the device data never leaves the
  building. Requires a dashboard password; the app refuses to bind to the network without one.
- **On a small cloud VM behind a Cloudflare Tunnel** — no inbound ports, per-person email login.

Both paths are written up in [docs/DEPLOY.md](docs/DEPLOY.md).

Access control: `admin` does everything, `viewer` reads everything and is refused on every write.
Set the passwords with `python -m ota_analytics.cli passwd --role admin`.

## Documentation

| | |
|---|---|
| [CLAUDE.md](CLAUDE.md) | Start here. Design decisions, data semantics, and the rules that must not be broken |
| [docs/DATA_PROFILE.md](docs/DATA_PROFILE.md) | Every column, and every data-quality trap found in the real export |
| [docs/DEPLOY.md](docs/DEPLOY.md) | Running it for a team, locally or in the cloud |
| [docs/PROJECT_PLAN.md](docs/PROJECT_PLAN.md) | Phases and scope |
| [docs/IMPLEMENTATION.md](docs/IMPLEMENTATION.md) | Module-level detail |
| [COTA.md](COTA.md) | Intouch COTA: the cloud API, sending rules, scale, open questions |

## Status

Working: ingest (`.xlsx` and `.csv`, columns matched by name), snapshot diffing, the dashboard,
data-quality rules, scheduled fetching, retention, XLSX reports, authentication, database
identity plus snapshot bundles for keeping two installs in sync, progress reporting for long
jobs, and a packaged Windows build. Intouch COTA (2.0.0) sends configuration commands — one
device, a sequence, or a fleet job — and is being tested live; how a job treats a switched-off
device is the open question (COTA.md). AI-written insight summaries are the next phase.

Next up: consistent table behaviour across every list — page size, pinned headers, per-column
filter and sort, and search — starting with **Firmware moves** on the Changes page.

Data note: the exports contain IMEIs, VINs and ICCIDs of customer vehicles. `data/`,
`Sample data/` and `*.otabundle` are gitignored, and they should stay that way — a bundle holds
every identifier in the fleet, so treat it exactly like an export.
