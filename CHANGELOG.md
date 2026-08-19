# Release notes

Newest first. The in-app version history (`ota_analytics/__init__.py`, shown at `/api/version`)
carries one line per release; this file explains the reasoning.

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
