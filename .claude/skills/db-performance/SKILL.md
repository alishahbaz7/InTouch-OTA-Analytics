---
name: db-performance
description: Rules for touching the snapshot warehouse in InTouch OTA Analytics — reading a snapshot, writing an ingest, adding a query, adding a column or an index, changing retention, or investigating anything described as slow, hanging, locked, or growing. Use before writing SQL against device_snapshot, device_seen, device_state, device_change or device_current, before adding a step to ingest/rollup/quality/registry, and whenever a report says the app is slow, the database is large, or the disk is busy.
---

# Keeping this database fast

Everything here was learned by measuring the live install, twice, a release apart. The failures
have a shape worth recognising: **nothing errors, the numbers stay correct, and the work simply
takes longer every week** until someone calls it a hang. There is no exception in the log,
because nothing went wrong.

Read `ota_analytics/resolve.py` first. It is the short version of most of this.

## The one rule

**Never resolve `device_state` on a path that runs more than once.**

It is a view, and SQLite plans it as a full scan of `device_snapshot` with a correlated subquery
per row. Its cost is proportional to *all history ever stored*, not to the fleet. Measured on
the live database at 272 snapshots / 1,081,179 rows: **4–18 seconds per reference**, growing by
another fetch every hour.

Go through `resolve` instead:

```python
from . import resolve

# One query, snapshot known: swap the table name onto the finished SQL.
rows = conn.execute(resolve.at(conn, snapshot_id, """
    SELECT COUNT(*) FROM device_state WHERE snapshot_id = ? AND status = 'Online'
"""), (snapshot_id,))

# Several queries over one snapshot: resolve once, read many times.
table = resolve.source(conn, snapshot_id)

# Streaming rows straight into something else (a bundle, an export).
conn.execute(resolve.select(("imei", "firmware")), (snapshot_id,))
```

`metrics.at` is the same thing and is fine to use from metrics.

**Keep `WHERE snapshot_id = ?` in every per-snapshot query**, even though the resolved table
holds only that snapshot. It is what makes the whole scheme safe: a caller that somehow gets a
different snapshot's copy matches nothing, which is visible, instead of returning plausible rows
for the wrong moment.

**Leave the view in the schema.** It is the readable definition of what resolving means and it
is what `tests/test_resolve.py` checks the fast path against. An optimization with nothing to
check it against is just an assertion.

## Before you call anything slow, measure where

Wall-clock on this machine swings by 2–3× depending on what else is running — the packaged app
holds an hourly scheduler. Never compare a number taken today against one written down last
week. Either A/B the two versions interleaved, or profile statements inside one process:

```python
prof, state = {}, {"sql": None, "t": None}
def trace(sql):
    t = time.perf_counter()
    if state["sql"] is not None:
        d = prof.setdefault(" ".join(state["sql"].split())[:120], [0, 0.0])
        d[0] += 1; d[1] += t - state["t"]
    state["sql"], state["t"] = sql, t
conn.set_trace_callback(trace)
...
for k, (n, s) in sorted(prof.items(), key=lambda kv: -kv[1][1])[:10]:
    print(f"{s:8.2f}s x{n:<5} {k}")
```

That one snippet found every problem in 1.9.0, including two that were nothing like what was
suspected: a single `UPDATE` at 367 seconds, and a helper called 196 times in one prune.

Work on a **copy** of `dist/InTouchOTA-Analytics-v<version>/data/ota_analytics.db` (`dist/InTouchOTA-Analytics/data` before 2.1.1). It is the user's live
data — 828 MB at 2.1.0 (238 MB after v23), two months of real history, and the only place these
problems are visible. Never profile against the live file. Take the copy with SQLite's backup
API from a `mode=ro` connection — it is consistent with the WAL beside it, and the running
release does not notice:

```python
src = sqlite3.connect(f"file:{live}?mode=ro", uri=True)
dst = sqlite3.connect(copy); src.backup(dst)
```

Three more instruments, each of which found something the statement profile could not:

- **Where the bytes are.** This SQLite has no `dbstat`. Inside a transaction you roll back,
  `DROP INDEX x` (or `DROP TABLE`, views and triggers first) puts exactly that object's pages on
  the freelist: `PRAGMA freelist_count` before and after is its size. That is how 228 MB of the
  828 MB file turned out to be four indexes nothing read.
- **Why rows exist.** For a table storing changes, ask which column changed:
  `LAG(...) OVER (PARTITION BY imei ORDER BY snapshot_id)` and count `a.col IS NOT b.col` per
  column. 86.8% of `device_snapshot` differed from the row before it only in `seen_at`.
- **What a fetch writes.** `PRAGMA wal_autocheckpoint = 0`, then the WAL file's size after each
  statement (from the trace callback) is that statement's disk write. A fetch recording ~3,100
  changes wrote 55 MB, half of it scattered pages of two IMEI-ordered indexes.

And time a page per function, not per statement: wrap the module's functions with a timer. A
route runs in a worker thread, so `cProfile` from the test thread sees nothing.

## Statistics are not optional

`ANALYZE` runs in migration v9 and `db.refresh_statistics` (`PRAGMA optimize`) runs after every
ingest. Without statistics SQLite guessed, and on this schema the guess was catastrophic: the
registry's `prev_firmware` update chose the `field` index over the `imei` one and scanned all
21,097 firmware-change rows once per device — 756 million row visits, **367 seconds in one
statement, on every fetch**. With statistics, 0.21s.

If you add a table or an index that a hot query depends on, check the plan
(`EXPLAIN QUERY PLAN`) with statistics present, not on an empty test database where every plan
looks the same. A migration that creates or reshapes a big table runs `ANALYZE` on it (v23 does).

## A connection starts cold

Every request opens its own connection (`api.get_conn`), and SQLite's default page cache is
2 MB — smaller than the registry (11 MB) and the change log (35 MB with its indexes). Each
query on a page then re-read the same pages through the OS. `db.CACHE_KIB` (32 MB) gave
Changes 5.15s → 0.55s, Overview 0.94s → 0.44s, Devices 1.44s → 0.77s, with no query touched.
The same query measured alone, on a warm connection, ran in 110 ms — which is why it looked
fine in isolation. **Measure a page through the app, not a query in a REPL.** `mmap_size` helped
as much but was not used: on Windows a mapped file can block the truncation VACUUM needs.

## Writing

- **Ingest is append-only and idempotent.** Re-ingesting the same bytes is a no-op. Never UPDATE
  or DELETE snapshot rows outside `retention`.
- **`device_snapshot` stores changes, not fetches.** A row is written only when a device
  actually differs, plus a `present = 0` tombstone when the platform stops listing it.
- **A column that moves on most fetches does not belong in `device_snapshot`.** Every change
  copies the whole row (~300 bytes plus its index entries). `seen_at` moved for ~3,000 devices
  every hour, and was 86.8% of all rows until v23 gave it `device_seen` (~45 bytes a move):
  828 MB → 238 MB, every snapshot resolving identically. Before adding a column, ask how often
  it changes; if the answer is "most fetches", it gets a narrow table of its own.
- **`device_seen` moves with `device_snapshot` everywhere.** Retention carries it forward,
  densify writes it (an explicit NULL for "never pinged" included), compact squeezes it,
  renumbering remaps it, a bundle rebuilds one whole row per change from both. They resolve
  independently: a device's ping is its newest `device_seen` row at or before the snapshot,
  whichever `device_snapshot` row its other values come from.
- **Never store anything derived from the snapshot time.** `seen_age_hours` is computed at read
  time for exactly this reason: stored, it differs on every row of every fetch and defeats delta
  storage entirely (measured: 6.7% compaction with it, 87.2% without).
- **An index must name the query it serves.** `device_snapshot` carries its primary key and
  `ix_ds_imei_snap`, nothing else. Four more were dropped in v23 — 228 MB — because nothing had
  filtered the physical table by status, task or model since reads went through `resolve`. On a
  `WITHOUT ROWID` table a secondary index carries the primary key, so `(imei)` there *was*
  `(imei, snapshot_id)`, a byte-for-byte duplicate. An index ordered by IMEI also costs a
  scattered page per changed device on every fetch.
- **Anything that writes `device_snapshot` or `device_seen` directly must refresh the
  materialization** — `current.refresh_latest`, or `current.invalidate` to fall back to the view.
  Empty is always safe: slower, never wrong.
- **In a fetch, advance `device_current` before anything reads the new snapshot.** Quality and
  the registry used to run first and each resolved the snapshot from scratch (0.8–2.5s per
  fetch) for the answer advancing produces from ~3,000 changed rows. `ingest._fold_in` holds
  the order.
- **Refresh statistics after bulk writes** (`db.refresh_statistics`). It writes, so it may never
  run on a read path.

## Holding the write lock

One writer at a time, and everything else waits out `busy_timeout` (30s) before failing with
`database is locked`. Two of those are in the live error log.

- **Long writes belong to a job.** `progress.start` refuses a second one. The scheduled fetch
  registers as a job for exactly this reason — before 1.9.0 it did not, so "Fetch now" during a
  scheduled fetch started a second concurrent writer.
- **Bound the work a single run may do.** `retention.AUTO_PRUNE_LIMIT` caps an automatic prune
  at 20 snapshots because the first run after upgrading had 177 of backlog — 61 seconds of held
  lock inside a fetch. A backlog that clears over several runs is fine; policy here describes
  the shape history should have, not a sequence that must complete.
- **Build temp tables in the temp schema** (`CREATE TEMP TABLE`), so materializing a snapshot on
  a read path takes no write lock on the database.

## Retention has to be able to fire

The policy thins by age: everything for 2 days, then hourly, daily, weekly. It once also refused
to prune any snapshot that recorded a change — which sounds careful and silently disabled
retention altogether, because at hourly cadence a fetch of 35,848 devices always contains *some*
change. 268 of 272 snapshots were exempt; the database grew ~45 MB a day for a month.

If you add a protection rule, **work out what fraction of real snapshots it exempts** before
committing it. Anything above a few percent is not a safeguard, it is an off switch.

What thinning costs is time resolution, never facts: a pruned snapshot's device rows *and* its
change-log rows move onto the next survivor, and `changed_at` keeps the real time of the move.
`tests/test_retention.py` asserts that every surviving snapshot resolves identically before and
after — keep that true.

## The tripwires

`tests/test_db_performance.py` watches the executed SQL at runtime and fails if an ingest,
rollup, registry fold, quality run or page metric resolves the view. It watches execution rather
than grepping source, because naming `device_state` in a SQL literal is correct — `resolve.at`
swaps the name onto the finished statement so the query in the file stays readable.

If one of those tests fails, the fix is to route the new query through `resolve`, not to add it
to an exemption list.
