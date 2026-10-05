---
name: db-performance
description: Rules for touching the snapshot warehouse in InTouch OTA Analytics — reading a snapshot, writing an ingest, adding a query, changing retention, or investigating anything described as slow, hanging, locked, or growing. Use before writing SQL against device_snapshot, device_state, device_change or device_current, before adding a step to ingest/rollup/quality/registry, and whenever a report says the app is slow or the database is large.
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

Work on a **copy** of `dist/InTouchOTA-Analytics/data/ota_analytics.db`. It is the user's live
data — 574 MB, a month of real history, and the only place these problems are visible. Never
profile against the live file.

## Statistics are not optional

`ANALYZE` runs in migration v9 and `db.refresh_statistics` (`PRAGMA optimize`) runs after every
ingest. Without statistics SQLite guessed, and on this schema the guess was catastrophic: the
registry's `prev_firmware` update chose the `field` index over the `imei` one and scanned all
21,097 firmware-change rows once per device — 756 million row visits, **367 seconds in one
statement, on every fetch**. With statistics, 0.21s.

If you add a table or an index that a hot query depends on, check the plan
(`EXPLAIN QUERY PLAN`) with statistics present, not on an empty test database where every plan
looks the same.

## Writing

- **Ingest is append-only and idempotent.** Re-ingesting the same bytes is a no-op. Never UPDATE
  or DELETE snapshot rows outside `retention`.
- **`device_snapshot` stores changes, not fetches.** A row is written only when a device
  actually differs, plus a `present = 0` tombstone when the platform stops listing it.
- **Never store anything derived from the snapshot time.** `seen_age_hours` is computed at read
  time for exactly this reason: stored, it differs on every row of every fetch and defeats delta
  storage entirely (measured: 6.7% compaction with it, 87.2% without).
- **Anything that writes `device_snapshot` directly must refresh the materialization** —
  `current.refresh_latest`, or `current.invalidate` to fall back to the view. Empty is always
  safe: slower, never wrong.
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
