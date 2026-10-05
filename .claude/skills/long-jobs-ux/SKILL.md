---
name: long-jobs-ux
description: How anything slow must behave in the InTouch OTA Analytics dashboard — fetches, imports, merges, exports and downloads. Use when adding or changing a route that can take more than a second or two, when building a file for the user to download, when adding a progress bar or a status message, and whenever something working is reported as hung, stuck, or "not doing anything".
---

# Anything slow has to look like it is working

Every UX bug this project has had is the same bug. The work was fine; there was no way to tell
it was happening. The reports read:

- "no action, only loading" — a merge that was running correctly, for two minutes
- "the import hung" — an `async` handler blocking the event loop, so the whole dashboard stopped
- "export is not working" — a bundle being built in memory for a minute before the first byte
- "I ran it and nothing happened" — a second copy that had moved to another port
- "the application gets hanged, I have to restart it" — a fetch holding the write lock

None of them raised. Nothing was in the error log. **Silence is the defect.**

## The rules

**A request that can exceed a couple of seconds does not hold the browser.** Start a job, return
`303`, and let the page poll `/api/progress`:

```python
try:
    job = progress.start("kind", "What it is doing", MODULE.STEPS)
except progress.Busy as exc:
    return templates.TemplateResponse(request, "update.html", _update_context(
        request, result={"level": "warn", "message": str(exc)}))

def work(job: progress.Job) -> None:
    ...
    job.finish("What happened.")

run_job(job, work)
return RedirectResponse("/update?tab=...", status_code=303)
```

**Progress lives on the server, not in the tab.** Closing the page does not stop the work, and
reopening it finds the job mid-flight — `/update` renders `progress.snapshot()` on load as well
as polling. A spinner drawn in JavaScript cannot do that, and a two-minute job invites someone
to close the tab.

**The bar is determinate and derived from work done** — rows written, snapshots folded, devices
read. Never from elapsed time. Every step declares its total before it starts. A bar that
advances on a timer teaches people to ignore it exactly when it matters.

**Weight the steps by measurement, and keep the weights next to the code that does the work.**
`bundle.MERGE_STEPS`, `bundle.EXPORT_STEPS`, `ingest.INGEST_STEPS`, `scheduler.FETCH_STEPS`.
Writing a bundle's change rows is 45 of its 50 units because it is most of the minute; even
weights would leave the bar apparently stalled through the only part that takes time.

**Under-reporting then completing in a jump is fine. Claiming progress that has not happened is
not.**

**One job at a time.** They all write to the database, so two would queue on the write lock
anyway — but silently, with two bars both claiming to move. This includes work nobody started
by hand: the scheduled fetch takes a job too, so "Fetch now" during one is refused with a
message instead of becoming a second concurrent writer.

**A job nobody asked for tidies itself away.** The scheduled fetch clears its own finished job
(`progress.clear(own_job)`), or every visit to Update Data would open on a stale panel needing
dismissal. A job someone started stays until they dismiss it.

## Building files

**Build first, download second.** A download that takes a minute to produce shows the browser
nothing at all until the last byte — no bar, no download indicator, nothing to distinguish it
from a dead button. Run the build as a job, set `job.download` to a collection URL, and the
page renders a link when it finishes. Collecting it is then instant, because the file exists.

**Never assemble a large file in memory.** Write it to a scratch file and hand the path to
`FileResponse`. The bundle held 33 MB plus every row as Python objects before sending anything;
the device spreadsheet held 190 MB of `Cell` objects to produce 3 MB. Use `openpyxl`'s
`write_only=True` for sheets of any size (see `exports.to_xlsx` for what that costs you: nothing
can be revisited after it is written, so widths, freeze panes and the filter range are all set
up front).

**Clean up on the error path too.** `BackgroundTask(path.unlink, missing_ok=True)` for a
one-shot download; delete the scratch file in an `except` before re-raising. That is the path
nobody checks.

## Handlers

**Every route is a plain `def`.** Starlette runs those in a threadpool. An `async def` runs on
the event loop, so slow work inside one stops the server answering *anything*, `/healthz`
included — which presents as the whole dashboard hanging.

Only `/update/import` and `/update/bundle-import` are `async`, because they must `await
file.read()`, and both immediately hand the blocking work to `run_in_threadpool`. Open the
sqlite connection *inside* that function: sqlite3 objects belong to the thread that created
them.

## Words

**Say what happened, in the same words the screen uses elsewhere.** One vocabulary and one
colour per state — see the table in CLAUDE.md. A donut and a tile showing the same number under
different words is how people stop trusting both.

**Do not call a working thing stuck, stalled or failed.** A pending OTA task is *parked*: the
platform assigns tasks in bulk to devices that are switched off. Yellow means worth chasing,
orange means waiting by design, red is for a genuine fault.

**A skipped run is not a failure.** Record it and say why, rather than passing over it in
silence — an agent that appears to have stopped fetching, with no explanation anywhere, is the
thing that gets debugged for an afternoon.

**Never put raw exception text from an HTTP call into the page.** Request objects can carry a
URL with credentials in it. Report the exception *type* and a hint.
