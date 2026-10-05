# Release notes

Newest first. The in-app version history (`ota_analytics/__init__.py`, shown at `/api/version`)
carries one line per release; this file explains the reasoning.

---

## 2.0.0 — in development

### IntouchCOTA, a separate module

COTA (configuration over the air) is a different surface from everything before it. The rest
of the app reads the OTA platform's device inventory and works out what happened by comparing
snapshots. COTA *sends* configuration commands through the cloud's own API
(ctvms IntouchAdminApi) and records each request and reply. It has its own tables (schema v10),
its own device map from IMEI to the cloud's internal device id, and its own credential, and it
shares nothing with the snapshot warehouse. Bundles do not carry it.

This is a major version because it is the first time the app writes to a production system
rather than only reading from one. The schema also moves to v10.

---

## 1.9.1 — 2026-10-05

### One release, named for its version

The release is now `InTouchOTA-Analytics-v1.9.1.zip`, and it unpacks to a folder called
`InTouchOTA-Analytics-v1.9.1`. Previously the zip was `InTouchOTA-Analytics-v1.9.0-win64.zip`
and unpacked to an unversioned folder, so two releases side by side were indistinguishable once
unpacked. This is the naming for every release from here on, in this project and the user's
others — the shared `ship` skill now says so.

Because each release unpacks to its own folder, upgrading by unzipping now means moving the old
`data` folder across before the first start — otherwise the new copy opens on an empty database.
`READ ME FIRST.txt` says so, since it is the only instruction a recipient of the zip gets. The
working folder in `dist\` is deliberately left unversioned: this install runs from there and keeps
its database inside it, and a folder renamed every release would leave the history behind.

The executable itself keeps its unversioned name, so shortcuts and pinned icons survive upgrades.

### The silent executable is gone

`InTouchOTA-Analytics-silent.exe` existed for one reason: auto-start ran it after a reboot so no
console window was left on the desktop. Auto-start was withdrawn in an earlier release, so nothing
launched it any more — and anyone who did run it by hand saw nothing at all, which reads as a
program that failed to start. The release now carries one executable.

`startup.launch_command()` still prefers a windowless build when one is present, so bringing
auto-start back means adding it to the spec again and nothing else. A silent exe left in `dist\`
by an older build is cleared as build output rather than "rescued" as the user's file.

---

## 1.9.0 — 2026-10-05

Built on 15 September and released three weeks later, during which the install kept running
1.8.0 and got worse in exactly the way measured here. On 5 October, opened after five days
offline, a single fetch sat in "Updating the device registry" for more than eight minutes. Re-
measured that day on a copy of the live database (362 snapshots, 760 MB): the one statement it
was stuck in took **360 seconds**, and the post-gap fetch had stored 22,564 changed rows against
~3,500 for an ordinary hourly one — so a gap multiplies every per-change cost on top of a
resolution that had grown to 18s. The same scenario on 1.9.0, with an even larger gap fetch
(34,995 rows): **26.9 seconds**, most of it the first round of retention catching up.

### A fetch took three minutes; it takes about ten seconds

Reported from the live install after a month of hourly fetching: "fetching and ingesting data
taking very long time, sometimes application get hanged, I have to restart the application",
and "I try to export the data this is not working".

Measured on a copy of the live database — 574 MB, 272 snapshots, 1,081,179 change rows, 35,848
devices — one fetch took **194 seconds**, and 453 on a second run. The dashboard's own
`snapshot.duration_ms` shows the slide: 2.7s in August, 27–53s by September, and that column
stops counting before the slowest step even starts.

It was four separate problems.

| Step of one fetch | Before | After |
|---|---|---|
| Storing what changed | 9.0s | **1.6s** |
| Checking data quality | 25.9s | **1.0s** |
| Updating the device registry | 150.6s | **4.7s** |
| Rebuilding metrics | 8.7s | **0.5s** |
| **Total** | **194s** | **~8s** |

**SQLite had no query statistics, and guessed catastrophically.** `ANALYZE` had never run on
this database. Without it the planner chose `ix_change_field` over `ix_change_imei` for the
registry's `prev_firmware` update, so for each of 35,848 devices it scanned all 21,097 change
rows with `field = 'firmware'` — 756 million row visits, **367 seconds in a single statement**,
on every fetch. `ANALYZE` takes 1.2 seconds and makes the same statement take **0.21s**. It runs
once in migration v9, and `PRAGMA optimize` keeps it current after every ingest.

This was the cheapest fix in the project's history and it hid for a month, because the wrong
plan still produces the right answer.

**`device_state` was being resolved eighteen times per fetch.** It is a view, and SQLite plans
it as a full scan of `device_snapshot` with a correlated subquery per row, so one reference
costs time proportional to *all history stored* — 4 to 18 seconds here, growing by another fetch
every hour. The quality rules resolved it once per rule; the delta write twice; rollup twice
more.

The new `resolve.py` does the same resolution as a grouped join over `ix_ds_imei_snap`:
**17.5s → 1.3s** for all 27 columns of all 35,848 devices, byte-identical output on nine
snapshots including the first and the last. It cannot be a view — the snapshot id has to bound
the inner `GROUP BY` and SQLite has no LATERAL — and two other formulations turned out slower
than the view rather than faster (`ROW_NUMBER() OVER (PARTITION BY imei)` at 17.4s, restricting
the view's join at 12.5s).

The view stays in the schema. It is the readable statement of what resolving a snapshot *means*
and it is what `tests/test_resolve.py` checks the fast path against.

**Pages are unaffected**, and that was verified rather than assumed: 1.8.0 and 1.9.0 were run
side by side against identical copies of the live database, interleaved so machine load hit both
equally. Every page and every download came out within noise of each other. (Wall-clock on this
machine swings 2–3× depending on what else is running, which is exactly why the comparison had
to be interleaved.)

### One fetch at a time

`database is locked` appears twice in the live error log, both times at the very first INSERT of
a scheduled fetch. A timed fetch ran outside `progress` entirely, so "one job at a time" did not
cover the one job that runs by itself — pressing "Fetch now" during a scheduled fetch started a
second concurrent writer, and one of them waited out the 30-second busy timeout and died.

The timed fetch now takes a job like everything else, so the two cannot overlap in either
direction. A tick that finds something already running **skips** rather than queueing: the next
tick is along shortly and its data is fresher. A skip is recorded and explained but not counted
as a failure — an agent that appears to have silently stopped fetching is the thing that gets
debugged for an afternoon. A job nobody started also clears itself when it finishes, so Update
Data does not open on a stale panel every hour.

### Retention could never fire, so the database grew for ever

Retention thins by age, and also refused to prune any snapshot that recorded a change. That
sounds careful. At the hourly cadence the tool actually runs at, a fetch of 35,848 devices
always contains *some* change — so **268 of 272 snapshots were exempt** and retention removed
nothing, on every run, while the database grew about 45 MB a day.

The rule is gone, because it protected against a loss that cannot happen: `device_change` has no
foreign key to `snapshot`, nothing outside `registry.apply_snapshot` reads its `snapshot_id`, and
every reader works from `changed_at`. Pruning now moves the change-log rows onto the survivor
along with the device rows, so the log stays consistent and the time a firmware move actually
happened is untouched.

On the live data this thins **177 of 273 snapshots and 587,211 of 1,081,179 device rows**, and
every surviving snapshot was verified to resolve byte-identically before and after.

An automatic prune is capped at 20 snapshots (`retention.AUTO_PRUNE_LIMIT`) because it holds the
write lock inside a fetch: the first run after upgrading has a month of backlog, which in one go
is another minute of the app apparently hanging. The rest is taken by the next few fetches. The
CLI's `prune` stays unbounded, because somebody is watching it.

### "Export is not working" — the bundle

Nothing raised, and nothing was ever logged, which is why this was hard to place. A full history
is ~960,000 rows of JSON and the better part of a minute to write, and it was assembled whole in
memory before a single byte reached the browser. As a plain download that is a request which
does not come back: no progress bar, no download indicator, nothing to tell it apart from a
hang.

**Building the bundle is now a job.** The POST returns in milliseconds, a determinate bar counts
the rows as they are written, and the finished job offers a link — collecting the file is then
instant, because it already exists. `GET /update/bundle` still builds one inline for scripts and
short histories, through the same builder so the two cannot drift.

**Nothing large is assembled in memory any more.** The bundle streams to a scratch file (deleted
after sending, and on the error path too). The resolved baseline inside it, which was most of
the export's time, went from 49s to **0.74s** on the fix above.

`exports.to_xlsx` now uses openpyxl's write-only mode: a full device export went from **190 MB
of Python objects to 3 MB** for the same 3.1 MB file, at the same speed. The trade is that
nothing can be revisited after it is written, so column widths, the frozen header and the filter
range are computed up front and the IMEI text format goes on each cell as it is created — all
four are now covered by tests, because all four would have failed silently.

### So it does not happen again

The pattern behind every one of these is the same, and it is worth naming: **nothing errors, the
numbers stay correct, and the work just takes longer every week.** There is no exception to find.

- `tests/test_db_performance.py` traces the SQL actually executed and fails if an ingest,
  rollup, registry fold, quality run or page metric resolves the view. It watches execution
  rather than grepping source, because naming `device_state` in a SQL literal is correct —
  `resolve.at` swaps the table name onto the finished statement.
- `tests/test_resolve.py` holds the fast resolver against the view on every snapshot, including
  a device that leaves and returns and a snapshot that stores no rows of its own.
- `tests/test_retention.py` asserts that pruning never changes what a surviving snapshot
  resolves to.
- Two project skills, `.claude/skills/db-performance` and `.claude/skills/long-jobs-ux`, carry
  the rules and the measurements into the next session. Both of these mistakes had by then been
  made in three separate places each, which is what a rule written only in prose gets you.

### Also

- `python -m ota_analytics.cli vacuum` reclaims file space and refreshes statistics, replacing
  the `python -c "...VACUUM"` line the docs used to carry.
- The scheduler's `last_status` gains `skipped`.

---

## 1.8.0 — 2026-09-08

### Switching pages no longer takes half a minute

Reported from the live fleet at 245 snapshots: moving between Dashboard, Changes and Devices
"creates huge delay in loading the data, which is not acceptable". Measured before the fix, and
it was not one problem but four.

| Page | Before | After |
|---|---|---|
| Overview | 18.4s | **0.84s** |
| Devices | 41.5s | **0.25s** |
| Pending | 76.8s | **0.45s** |
| Firmware | 7.0s | **0.28s** |
| Changes | 1.4s | **0.64s** |

**The newest snapshot is now kept resolved.** `device_state` is a view, and every reference
re-resolves each device's most recent row across the whole fleet — 4.5s at 245 snapshots, and
growing by another fetch every 15 minutes. It was already materialized once per request into a
temp table, but a request opens its own connection, so that 10.9s rebuild was paid on *every
page load*. It now lives in `device_current`, advanced when a fetch lands: ~150 upserts rather
than re-resolving 35,848 devices. `device_current_meta` records which snapshot the rows resolve,
and readers use them only when that matches what they asked for — anything else falls back to
the view, which is slower and always right.

**Three reads bypassed all of that.** They returned correct answers, so nothing failed; they
just each cost a full fleet resolution. `rollup.fragmentation` is called by `kpis()`, so every
page carrying the headline numbers paid it, and *both* queries behind the devices table paid it
again — which is where 41 seconds came from.

**"Pending across several fetches" was counting snapshots.** Three consecutive pending snapshots
meant three days at the original daily export; at the 15-minute cadence it means 45 minutes, so
26,481 devices — three quarters of the fleet — qualified, and the query grouped `device_state`
across all 245 snapshots to work it out (118s). It now reads the change log, which already
records when each device became pending: **0.043s**, and the threshold is `STALL_HOURS = 24`,
because an hour is an hour whatever the cadence.

Two attempts were measured and thrown away: a `valid_to` temporal column made the isolated query
15x faster and the pages no better, and a first rewrite of the stalled query came out *slower*
than what it replaced (SQLite preferred an index that made every pending device scan all 51,178
queue-state rows).

### Fallbacks: how many times, not just when

A chronological list hides the devices that do this repeatedly — on the live fleet, 186
fallbacks across 148 devices, but **23 of them had done it more than once** and one had done it
six times, none of which is visible reading events in date order.

- A **Times** column on every occurrence, and a **Falling back repeatedly** table, worst first.
- The list **pages at 25/50/100** — it previously rendered a hard-sliced 100 rows with no control
  and no indication there was more.
- **Filter** by repeats-only or by model, and **sort** by IMEI, times, model or date. Every view
  is a plain link, so a filtered list is a URL that can be handed to a colleague.
- The download carries **Fallbacks (times)** too — a fallback read away from the dashboard gives
  no hint that it is the device's sixth.

One definition of "fallback" backs the list, the counts, the totals and the file, so they cannot
drift apart.

### A field the source never sent is no longer treated as empty

The platform API carries no group information at all. Treating that absence as NULL wiped the
groups of 29,384 devices on the first API fetch and every one after it — and group is one of the
few dimensions available for explaining why a set of devices reverted. It also made every device
look changed at the moment the source switched, writing 35,475 rows to record nothing.

### Fixes

- The stylesheet is versioned (`app.css?v=…`). Without it the browser caches the file under a URL
  that never changes, so a restyled page keeps rendering with the old rules — which is
  indistinguishable from the new CSS being broken, and is exactly how a set of filter controls
  shipped looking like raw browser links.
- A test now parses every template and fails if a class has no rule in the stylesheet. It would
  have caught that, and the `.num` that should have been `.n`.
- The sortable-column header is a shared macro, so tables cannot each invent their own.

---

## 1.6.0 — 2026-08-19

### Devices page filters

- **Firmware is a checkbox list, not a single choice.** Looking at the two versions a rollout is
  moving between used to take one page load each. The list shows version numbers only — the
  device counts were the widest thing in it and are already on the Firmware page. **All** is a
  real toggle: ticking it selects every version, clearing it deselects every one, and it shows
  the indeterminate dash on a partial selection rather than reading as unchecked while half the
  list is ticked.
- **The Group text box is gone** — an exact-match field nobody could type from memory. The
  capability stays: the Groups page still links here with `?group=…`, carried in a hidden field
  so it survives paging.
- **Find IMEI** takes any part of a number. Digits are extracted rather than required, so a value
  pasted from the platform — quotes, trailing comma — works without tidying, and the last few
  digits off a label are enough.

### Firmware moves is paged

25 / 50 / 100 per page, with the count reporting the **whole** result rather than the rows on
screen, so a filtered view cannot be mistaken for the full one. The pager is a shared macro, so
the remaining long lists can adopt it without a fourth copy drifting.

Ordering is `changed_at DESC, id DESC`. The tie-break is not cosmetic: a snapshot stamps every
change it observes with the same time, so ordering by the timestamp alone lets one row appear on
two pages and another on none. Verified on the live fleet — 1,414 moves walked across 57 pages,
zero duplicates, zero losses.

### Task state by model

Restructured to read like the Firmware table, and now **adds up two ways**:

```
Completed + Pending + No task  = Devices     what the platform was asked to do
Online + Offline + Act-Pending = Devices     whether the device can be reached
```

`Activation-Pending` is a column here and a marker on the Firmware table. It is zero on five of
six rows, so the objection to a mostly-empty column applies — but without it the `(unknown)` row
reads 0 online and 36 offline out of 513 and loses 477 devices with nothing to explain them. Six
rows can afford it; a hundred cannot.

There is deliberately no Task figure under Devices: that number is Pending, and printing it twice
under two headings invites the reader to look for a difference that cannot exist.

---

## 1.5.0 — 2026-08-19

### Page switching was slow. It was measured, not guessed at

`device_state` is a view: every reference re-resolves each device's most recent row across the
whole fleet. At 48 snapshots that is **1.0s per reference**, against 0.01s for a plain count off
the physical table — and a page calls six to ten metrics, each of which referenced it.
`metrics.snapshot_source()` now resolves the snapshot once per request into a temp table.

| Page | Before | After |
|---|---|---|
| Overview | 12.71s | **3.31s** |
| Firmware | 5.16s | 2.52s |
| Devices | 4.33s | 4.42s* |
| Reachability | 3.04s | 1.42s |

\* Devices did not improve; its cost is the row query, not the metrics.

Safety comes from what was *not* changed: every metric keeps its own `WHERE snapshot_id = ?`, so
a caller asking for a snapshot other than the one held gets nothing rather than the wrong rows,
and a metric still reading the view is merely slow. The temp table lives in SQLite's temp schema,
so building it takes no write lock — a read path stays a read path.

Pending is still the slowest page. Its cost is `registry.stalled_devices`, which spans **all**
snapshots and so cannot use the materialized copy.

### Progress for long jobs

A merge takes minutes and a fetch tens of seconds. Both used to hold the POST open with nothing
to show, so the only feedback was a page that never finished loading — indistinguishable from a
hang, and reported as exactly that. Now the POST starts a job, returns, and the page polls.

- The bar is **determinate and derived from work done** — snapshots folded, devices read — never
  from elapsed time. A bar that advances on a timer teaches people to ignore it precisely when it
  matters.
- Steps are **weighted by measurement**: replaying the change log is ~60% of a merge, so even
  weights would leave the bar apparently stalled right through it.
- Progress lives on the **server**, so closing the tab does not stop the job and reopening the
  page finds it mid-flight.
- One job at a time; they all write to the database.

### A build can no longer delete data

`dist/` is cleared to start clean, and an install that lives there keeps its database, bundles and
reports in it. That cost 48 snapshots of live fleet history, more than once. Warning first was
tried and was not enough — the warning scrolled past and the data went anyway.

`build.py` now declares the four files it produces and treats **everything else in `dist/` as the
user's**, moving it aside and putting it back after the zip is written, so the handover file
cannot carry a database either. Listing what to *delete* rather than what to keep is the point: a
kind of file nobody anticipated is preserved by omission instead of destroyed by it.

### One vocabulary, one palette

Nothing is called "stuck" or "failed" any more. The platform assigns tasks in bulk to devices that
are switched off, so a pending task is *parked*; the word carried a judgement the data does not
support, and the page coloured it red like a fault.

| State | Colour |
|---|---|
| Task completed | green |
| **Task pending — Online** | **yellow** — reachable and still not updated, the actionable one |
| Task pending — Offline | orange — waiting by design |
| No pending task | grey |

`Activation-Pending` replaces the platform's `Inactive`: those devices carry an IMEI and nothing
else — no VIN, no ICCID, no first ping, never tasked — so they are waiting to be activated, not
inactive in the sense of having gone quiet. On the live fleet the two descriptions select an
identical 645 devices, so it is a rename of a defined state rather than a new heuristic. **The
stored value stays `Inactive`**, because that is what the export said; only the reading changes.

### Smaller

- **One freshness chip** instead of two: `Updated 09:06 · 3 hr ago · auto 1 hour`. They answered
  the same question and side by side read as two unrelated clocks.
- **Theme switch** — auto / light / dark, applied by an inline script before first paint so there
  is no flash of the wrong theme. "Auto" is a real third state, not the absence of a choice.
- **Pinned table headers**, with the fleet total moved from the foot of 103 rows into the header.
  `top: 0` alone was not enough: the page header is itself sticky at top 0, so the column names
  slid underneath it and vanished exactly when they became useful.
- **XLSX downloads work again.** 128 devices carry an ICCID with an embedded backspace, and a
  `.xlsx` may not contain those characters at all, so openpyxl refused the whole workbook over one
  cell — which is why only CSV appeared to work. A quality rule now reports the affected devices
  rather than the corruption being quietly cleaned away.
- **`.csv` loads as well as `.xlsx`**, columns matched by name. A CSV that turns out to be a report
  this dashboard produced still loads but is marked second-hand, because its values have already
  been normalized once and it drops columns the platform sends.
- **Launching the app twice** opens the copy already running instead of quietly starting a second
  one on another port with a second scheduler behind it.

---

## 1.4.0 — 2026-08-19

Devices-per-firmware gains online, offline and task-pending columns under a grouped header that
names each percentage's denominator. Three columns headed "Share (%)" would have put one word on
three different meanings in adjacent columns.

## 1.3.x — 2026-08-17

CSV ingest; uploads and merges no longer freeze the dashboard; the interleave option asks the
question it means; the connection form says when a password is already saved.

## 1.2.x — 2026-08-16/17

Packaged Windows application — portable, data beside the .exe, CLI reachable from the executable.
"Start with Windows" withdrawn: it produced a copy of the dashboard with no window that could not
be seen or stopped.

## 1.1.0 — 2026-08-16

Fleet digest and database identity on every page and report; snapshot bundles so two installs can
merge history and prove they hold the same data.

## 1.0.0 — 2026-08-15

Device registry with last-checked/last-changed, change log, fallback detection, auto-fetch,
error log.
