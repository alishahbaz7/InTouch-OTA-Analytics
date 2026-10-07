"""FastAPI dashboard.

Pages are rendered server-side; /api/* mirrors each view as JSON for reports and any future
consumer. Filters travel as query params, so every view is linkable and bookmarkable.
"""

from __future__ import annotations

import io
import json
import sqlite3
import tempfile
import threading
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from urllib.parse import quote, urlencode

from fastapi import FastAPI, File, Form, Query, Request, Response, UploadFile
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse, StreamingResponse,
                               RedirectResponse)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool

from . import (auth, bundle, config, cota, cota_campaign, cota_connection, cota_library,
               cota_run, db, errors, exports, identity,
               ingest, metrics, nav, normalize, progress, registry, rollup, scheduler, sources,
               startup)

from . import __version__, build_info      # noqa: E402  (kept beside the app metadata)

# Templates and static files ship inside the bundle in a packaged build, so they are located
# the same way schema.sql is rather than relative to this module.
WEB = config.resource("ota_analytics", "web")
BUILD = build_info()
app = FastAPI(title="InTouch OTA Analytics", version=__version__)
app.mount("/static", StaticFiles(directory=WEB / "static"), name="static")
templates = Jinja2Templates(directory=str(WEB / "templates"))


def get_conn() -> sqlite3.Connection:
    conn = db.connect()
    # The command library's names, loaded once per database so that every page — and an export,
    # and the first page after a restart — names commands the user's way. Saving refreshes it.
    cota_library.ensure_loaded(conn)
    return conn


@app.middleware("http")
async def require_login(request: Request, call_next):
    """Default-deny. Every route needs a session unless it is explicitly public.

    Enforced here rather than per route on purpose: a route added later is protected by
    omission. The alternative — remembering a dependency on each of ~30 handlers — fails the
    first time someone forgets, and the thing left open would be a page listing IMEIs and VINs.
    """
    path = request.url.path

    # With no password configured there is nobody who *can* log in. Refusing everything would
    # brick a local install on upgrade, so the app stays open and says so loudly on every page;
    # the deployment runbook sets the hashes before it is ever reachable from outside.
    if not auth.is_configured() or auth.is_public(path):
        return await call_next(request)

    user = auth.identify(request)
    if user is None:
        if path.startswith("/api/"):
            return JSONResponse({"error": "authentication required"}, status_code=401)
        return RedirectResponse(f"/login?next={quote(request.url.path)}", status_code=303)

    # A viewer reads. Blocking unsafe methods here is what actually enforces it — hiding the
    # buttons in a template is a courtesy, not a permission.
    if request.method not in auth.SAFE_METHODS and not user.is_admin:
        if path.startswith("/api/"):
            return JSONResponse({"error": "read-only account"}, status_code=403)
        return HTMLResponse(
            '<!doctype html><html><head><title>Read-only</title>'
            '<link rel="stylesheet" href="/static/app.css"></head><body><main>'
            '<h1>Read-only account</h1><p>This account can view the dashboard but not change '
            'anything.</p><p><a href="/">Back to the dashboard</a></p>'
            '</main></body></html>', status_code=403)

    request.state.user = user
    return await call_next(request)


# Identifies a running copy as *this* application, so a second launch can tell the difference
# between our own dashboard already serving and some unrelated thing holding the port.
APP_ID = "intouch-ota-analytics"


@app.get("/healthz")
def healthz():
    """Liveness for the service manager and the tunnel. Deliberately says nothing about data."""
    return JSONResponse({"status": "ok", "app": APP_ID, "channel": config.CHANNEL})


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request, next: str = "/", error: str = ""):
    if auth.identify(request) is not None:
        return RedirectResponse(next or "/", status_code=303)
    return templates.TemplateResponse(request, "login.html", {
        "next": next or "/", "error": error, "configured": auth.is_configured(),
        "build": BUILD})


@app.post("/login", response_class=HTMLResponse)
def login_submit(request: Request, password: str = Form(""), next: str = Form("/")):
    user = auth.authenticate(password)
    if user is None:
        # Deliberately vague, and never echoes what was typed back into the page.
        return templates.TemplateResponse(request, "login.html", {
            "next": next or "/", "error": "That password was not accepted.",
            "configured": auth.is_configured(), "build": BUILD}, status_code=401)

    # Only send the redirect somewhere within this site: an open redirect turns the login page
    # into a credential-phishing hop.
    target = next if next.startswith("/") and not next.startswith("//") else "/"
    response = RedirectResponse(target, status_code=303)
    response.set_cookie(
        auth.SESSION_COOKIE, auth.issue(user),
        max_age=auth.SESSION_MAX_AGE, httponly=True, samesite="lax",
        # Set only when the request actually arrived over HTTPS — a Secure cookie on plain
        # http://127.0.0.1 is silently dropped, which locks out local use entirely.
        secure=request.url.scheme == "https")
    return response


@app.post("/logout")
@app.get("/logout")
def logout():
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(auth.SESSION_COOKIE)
    return response


@app.exception_handler(Exception)
def handle_unexpected(request: Request, exc: Exception):
    """Record the failure and show what happened, rather than a bare 'Internal Server Error'."""
    errors.record("web", exc, path=str(request.url.path))
    body = f"""
      <h1>Something went wrong</h1>
      <p>The error has been logged. <a href="/errors">See the error log</a> or
         <a href="{request.url.path}">try again</a>.</p>
      <pre>{type(exc).__name__}: {str(exc)[:300]}</pre>
      <p><a href="/">Back to the dashboard</a></p>
    """
    return HTMLResponse(
        f'<!doctype html><html><head><title>Error</title>'
        f'<link rel="stylesheet" href="/static/app.css"></head>'
        f'<body><main>{body}</main></body></html>', status_code=500)


def _pct(value: float | int | None, total: float | int | None) -> float:
    return (value or 0) / total if total else 0.0


templates.env.filters["pct"] = _pct
templates.env.filters["comma"] = lambda v: f"{v:,}" if isinstance(v, (int, float)) else v
templates.env.filters["from_json"] = lambda v: json.loads(v) if v else []
# One mapping from stored status to the word on screen, so no template invents its own.
templates.env.filters["status"] = normalize.status_label
# The sidebar and the top bar title read one definition (nav.py).
templates.env.globals["nav_modules"] = nav.MODULES
templates.env.globals["nav_state"] = nav.state
templates.env.globals["nav_icons"] = nav.ICONS


def _relative_age(timestamp: str | None) -> str:
    """'4 min ago' — how fresh the data is, which is what people actually want to know."""
    if not timestamp:
        return ""
    try:
        moment = datetime.fromisoformat(timestamp)
    except (TypeError, ValueError):
        return ""
    seconds = (datetime.now() - moment).total_seconds()
    if seconds < 90:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)} min ago"
    if seconds < 172800:
        hours = seconds / 3600
        return f"{hours:.0f} hr ago" if hours < 24 else "yesterday"
    return f"{int(seconds // 86400)} days ago"


def _day_label(moment: datetime | None) -> str:
    """The separator between days in a console thread."""
    if not moment:
        return ""
    today = datetime.now().date()
    if moment.date() == today:
        return "Today"
    if moment.date() == today - timedelta(days=1):
        return "Yesterday"
    return f"{moment:%a %d %b %Y}"


def _printable(text) -> str:
    """A device's answer with its control bytes shown as \\x16 rather than rendered as
    garbage. The live cloud returns them inside otherwise readable text."""
    return cota.visible(text) or ""


templates.env.filters["printable"] = _printable
templates.env.filters["command_name"] = cota.command_name
templates.env.filters["describe_command"] = lambda v: cota.describe_command(v) or ""
templates.env.filters["ago"] = _relative_age
templates.env.filters["day_label"] = _day_label


# A long-running dev copy keeps the Python it started with while Jinja reloads templates from
# disk — so a template can ask for something the running code does not have. That served 500s,
# an empty command table and blank icons before it got a guard: the dev copy now says so.
_CODE_DIR = Path(__file__).resolve().parent


def _code_mtime() -> float:
    try:
        return max(p.stat().st_mtime for p in _CODE_DIR.glob("*.py"))
    except (OSError, ValueError):
        return 0.0


_STARTED_WITH_CODE = _code_mtime()


def code_is_stale() -> bool:
    """True when this dev copy is running older code than is on disk. Never in a release: a
    packaged build's code cannot change underneath it."""
    if config.CHANNEL != "dev" or config.is_frozen():
        return False
    return _code_mtime() > _STARTED_WITH_CODE + 1


def page_context(conn: sqlite3.Connection, request: Request, snapshot: int | None) -> dict:
    """Shared context: which snapshot is being viewed, and what else is available."""
    available = metrics.snapshots(conn)
    snapshot_id = snapshot or (available[0]["id"] if available else None)
    latest = available[0] if available else None
    return {
        "request": request,
        "snapshots": available,
        "snapshot_id": snapshot_id,
        "current": next((s for s in available if s["id"] == snapshot_id), None),
        "latest": latest,
        "updated_at": latest["snapshot_at"] if latest else None,
        "updated_age": _relative_age(latest["snapshot_at"]) if latest else "",
        # True when looking at history rather than the newest data — the header has to say so,
        # or the numbers read as current when they are not.
        "viewing_history": bool(latest and snapshot_id and snapshot_id != latest["id"]),
        "multi_snapshot": len(available) > 1,
        # Carries the selected snapshot across navigation.
        "qs": lambda: f"?snapshot={snapshot_id}" if snapshot_id else "",
        "build": BUILD,
        "code_stale": code_is_stale(),
        # On every page, because "are we even looking at the same data?" is the first question
        # whenever two people compare numbers, and it is not answerable from anything else here.
        "identity": identity.manifest(conn),
        # Spelled for however this copy is running: a packaged build has no
        # `python -m ota_analytics.cli` to offer, and saying otherwise sends someone to install
        # Python to fix a problem they do not have.
        "export_dir": str(config.EXPORT_DIR),
        "ingest_command": config.command_hint("ingest-dir", str(config.EXPORT_DIR)),
        "error_summary": errors.summary(conn),
        "auth": scheduler.auth_status(),
        "agent": scheduler.get_scheduler().state,
        "describe_interval": scheduler.describe_interval,
    }


@app.get("/", response_class=HTMLResponse)
def overview(request: Request, snapshot: int | None = None, window: str = "today",
             model: list[str] = Query(default=[])):
    conn = get_conn()
    ctx = page_context(conn, request, snapshot)
    if not ctx["snapshot_id"]:
        return templates.TemplateResponse(request, "empty.html", ctx)

    # One model selection drives the whole page. Empty means every model, so the default view
    # is the full fleet and narrowing is opt-in.
    selected = [m for m in model if m]
    sid = ctx["snapshot_id"]

    # Change information is the point of the system, so it sits on the front page. It reads
    # from the change log over a chosen period rather than comparing two snapshots, so nothing
    # can slip between a chosen pair of endpoints.
    since, until = registry.window_range(window)
    change = registry.movement_summary(conn, since, until)
    change["window_label"] = registry.window_label(window)

    hourly = metrics.hourly_activity(conn, sid, models=selected)
    ctx.update(
        change=change,
        window=window,
        windows=registry.WINDOWS,
        selected_models=selected,
        all_models=metrics.task_state_by(conn, sid, "model"),
        kpis=metrics.kpis(conn, sid, selected),
        pending=metrics.pending_by_reason(conn, sid, selected),
        by_model=metrics.task_state_by(conn, sid, "model", selected),
        staleness=metrics.staleness_buckets(conn, sid, selected),
        hourly=hourly,
        hourly_total=sum(h["devices"] for h in hourly),
        hourly_peak=max(hourly, key=lambda h: h["devices"]) if hourly else {"devices": 0, "hour": "—"},
        by_model_donut=metrics.model_breakdown(conn, sid),
        status_donut=metrics.status_breakdown(conn, sid, selected),
        task_donut=metrics.task_breakdown(conn, sid, selected),
    )
    return templates.TemplateResponse(request, "overview.html", ctx)


@app.get("/pending", response_class=HTMLResponse)
def pending(request: Request, snapshot: int | None = None):
    conn = get_conn()
    ctx = page_context(conn, request, snapshot)
    ctx.update(
        kpis=metrics.kpis(conn, ctx["snapshot_id"]),
        buckets=metrics.pending_by_reason(conn, ctx["snapshot_id"]),
        pending_online=metrics.pending_online_devices(conn, ctx["snapshot_id"]),
        stalled=registry.stalled_devices(conn) if ctx["multi_snapshot"] else [],
        stall_hours=config.STALL_HOURS,
    )
    return templates.TemplateResponse(request, "pending.html", ctx)


@app.get("/firmware", response_class=HTMLResponse)
def firmware(request: Request, snapshot: int | None = None,
             model: list[str] = Query(default=[])):
    conn = get_conn()
    ctx = page_context(conn, request, snapshot)
    selected = [m for m in model if m]
    mix = metrics.firmware_mix(conn, ctx["snapshot_id"], selected)
    ctx.update(
        selected_models=selected,
        models=metrics.task_state_by(conn, ctx["snapshot_id"], "model"),
        mix=mix,
        mix_total=sum(r["devices"] for r in mix),
        kpis=metrics.kpis(conn, ctx["snapshot_id"]),
        targets=metrics.targets(conn),
        gaps=metrics.coverage_gaps(conn, ctx["snapshot_id"]),
    )
    return templates.TemplateResponse(request, "firmware.html", ctx)


@app.get("/changes", response_class=HTMLResponse)
def changes(request: Request, window: str = "today", page: int = 1, size: int = 50,
            fb_page: int = 1, fb_size: int = 25, fb_sort: str = "when",
            fb_min: int = 1, fb_model: str | None = None):
    """Changes over a time window, read from the per-device change log.

    No snapshot pair to choose: the log records every move individually, so a device that
    changed and changed back is still visible — which comparing two endpoints could never
    guarantee, however carefully the pair was picked.
    """
    conn = get_conn()
    ctx = page_context(conn, request, None)
    since = registry.window_since(window)

    # The total comes free: movement_summary already counts the moves in this window with the
    # same filter the list uses, so paging needs no second COUNT.
    summary = registry.movement_summary(conn, since)
    size = size if size in PAGE_SIZES else DEFAULT_PAGE_SIZE
    pages = max(1, -(-(summary["moves"] or 0) // size))
    page = min(max(1, page), pages)

    ctx.update(
        window=window,
        windows=registry.WINDOWS,
        since=since,
        summary=summary,
        page=page,
        pages=pages,
        size=size,
        page_sizes=PAGE_SIZES,
        moves=registry.firmware_moves(conn, since, limit=size, offset=(page - 1) * size),
        at_base=registry.at_base_firmware(conn),
        segments=registry.fallback_segments(conn),
        registry_summary=registry.summary(conn),
    )

    # Fallbacks page independently of the moves list above. They are different questions asked
    # on the same screen — "what moved in this window" and "what has ever gone backwards" — and
    # sharing one page number would move both at once.
    fb_totals = registry.fallback_totals(conn)
    fb_size = fb_size if fb_size in PAGE_SIZES else 25
    fb_sort = fb_sort if fb_sort in registry.FALLBACK_SORTS else registry.DEFAULT_FALLBACK_SORT
    fb_min = 2 if fb_min and int(fb_min) > 1 else 1

    # The filtered total, so the pager counts what is actually on screen rather than everything.
    fb_shown = registry.fallback_count(conn, min_times=fb_min, model=fb_model)
    fb_pages = max(1, -(-fb_shown // fb_size))
    fb_page = min(max(1, fb_page), fb_pages)
    ctx.update(
        fb_totals=fb_totals,
        fb_shown=fb_shown,
        fb_page=fb_page,
        fb_pages=fb_pages,
        fb_size=fb_size,
        fb_sort=fb_sort,
        fb_min=fb_min,
        fb_model=fb_model,
        fb_models=registry.fallback_models(conn),
        fb_query="window=" + window + (f"&fb_sort={fb_sort}" if fb_sort != "when" else "")
                 + (f"&fb_min={fb_min}" if fb_min > 1 else "")
                 + (f"&fb_model={quote(fb_model)}" if fb_model else ""),
        fallbacks=registry.fallbacks(conn, limit=fb_size, offset=(fb_page - 1) * fb_size,
                                     sort=fb_sort, min_times=fb_min, model=fb_model),
        # The counter: devices that have done this more than once, worst first. A chronological
        # list cannot show this — a device reverting every few days is scattered down the page
        # as unrelated rows.
        fb_repeats=registry.fallback_repeats(conn, limit=25),
    )
    return templates.TemplateResponse(request, "changes.html", ctx)


@app.get("/reachability", response_class=HTMLResponse)
def reachability(request: Request, snapshot: int | None = None, min_devices: int = 20):
    conn = get_conn()
    ctx = page_context(conn, request, snapshot)
    ctx.update(
        rows=metrics.reachability_by_firmware(conn, ctx["snapshot_id"], min_devices),
        staleness=metrics.staleness_buckets(conn, ctx["snapshot_id"]),
        min_devices=min_devices,
    )
    return templates.TemplateResponse(request, "reachability.html", ctx)


@app.get("/groups", response_class=HTMLResponse)
def groups(request: Request, snapshot: int | None = None):
    conn = get_conn()
    ctx = page_context(conn, request, snapshot)
    ctx.update(rows=metrics.groups(conn, ctx["snapshot_id"]))
    return templates.TemplateResponse(request, "groups.html", ctx)


# ─── downloads ──────────────────────────────────────────────────────────────

EXPORT_MEDIA = {
    "csv": "text/csv; charset=utf-8",
    "txt": "text/plain; charset=utf-8",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}


def _download(payload, fmt: str, stem: str):
    """Wrap exported content with the headers that make a browser save it.

    CSV gets a byte-order mark so Excel reads it as UTF-8 instead of mangling accents. The IMEI
    list must NOT: it is pasted straight into the platform, and a leading invisible character
    would corrupt the first identifier in the list.
    """
    filename = exports.timestamped(stem, fmt)
    if isinstance(payload, bytes):
        body = payload
    elif fmt == "csv":
        body = payload.encode("utf-8-sig")
    else:
        body = payload.encode("utf-8")
    return Response(content=body, media_type=EXPORT_MEDIA.get(fmt, "text/plain"),
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@app.get("/devices/export")
def devices_export(
    snapshot: int | None = None,
    model: str | None = None,
    firmware: list[str] = Query(default=[]),
    status: str | None = None,
    queue_state: str | None = None,
    group: str | None = None,
    changed: str | None = None,
    fallback: str | None = None,
    q: str = "",
    sort: str = "seen",
    dir: str = "desc",
    format: str = "csv",
):
    """The filtered device list, as a file. Same rows as the page, without pagination."""
    conn = get_conn()
    snapshot_id = snapshot or metrics.latest_snapshot_id(conn)
    rows, _ = _device_rows(conn, snapshot_id, model=model, firmware=firmware, status=status,
                           queue_state=queue_state, group=group, changed=changed,
                           fallback=fallback, search=q, sort=sort, dir=dir)

    # Spell the tag out in the file: a reader opening this in Excel should not have to know
    # what a 1 in a column called is_fallback means.
    for row in rows:
        row["fallback_tag"] = ""
        if row.get("is_fallback"):
            row["fallback_tag"] = ("FALLBACK — missed target"
                                   if row.get("update_firmware") not in (None, row.get("firmware"))
                                   else "FALLBACK — target is base")

    slug = exports.describe({"m": model, "f": "-".join(firmware or []), "s": status,
                             "q": queue_state, "g": group, "c": changed,
                             "fb": fallback, "imei": q})
    stem = f"devices_{slug}" if slug else "devices"

    if format == "txt":
        return _download(exports.to_imei_list(rows), "txt", f"{stem}_imei")
    if format == "xlsx":
        return _download(exports.to_xlsx(rows, exports.DEVICE_COLUMNS, "Devices",
                                         identity.manifest(conn)), "xlsx", stem)
    return _download(exports.to_csv(rows, exports.DEVICE_COLUMNS), "csv", stem)


@app.get("/changes/export")
def changes_export(window: str = "today", format: str = "csv", only: str = "all"):
    """Firmware moves in a period — all of them, or just the fallbacks."""
    conn = get_conn()
    since, until = registry.window_range(window)
    moves = registry.firmware_moves(conn, since, until, limit=100_000)

    if only == "fallbacks":
        moves = [m for m in moves if m["is_fallback"]]
    elif only == "unplanned":
        moves = [m for m in moves if m["direction"] == "downgrade" and not m["matched_target"]]

    for move in moves:            # spell out the verdict rather than leaving raw flags
        if move["is_fallback"]:
            move["verdict"] = "fallback to base"
        elif move["direction"] == "downgrade":
            move["verdict"] = "planned rollback" if move["matched_target"] else "unplanned"
        else:
            move["verdict"] = "upgrade"

    stem = f"changes_{window}" + (f"_{only}" if only != "all" else "")
    if format == "txt":
        return _download(exports.to_imei_list(moves), "txt", f"{stem}_imei")
    if format == "xlsx":
        return _download(exports.to_xlsx(moves, exports.CHANGE_COLUMNS, "Changes",
                                         identity.manifest(conn)), "xlsx", stem)
    return _download(exports.to_csv(moves, exports.CHANGE_COLUMNS), "csv", stem)


@app.get("/pending/export")
def pending_export(snapshot: int | None = None, format: str = "csv"):
    """Devices with a task pending while online — the cohort most likely to need action
    today, because they are reachable and still have not taken the update."""
    conn = get_conn()
    snapshot_id = snapshot or metrics.latest_snapshot_id(conn)
    rows = metrics.pending_online_devices(conn, snapshot_id, limit=100_000)

    if format == "txt":
        return _download(exports.to_imei_list(rows), "txt", "pending_online_imei")
    if format == "xlsx":
        return _download(exports.to_xlsx(rows, exports.DEVICE_COLUMNS, "Task pending - Online",
                                         identity.manifest(conn)), "xlsx", "pending_online")
    return _download(exports.to_csv(rows, exports.DEVICE_COLUMNS), "csv", "pending_online")


# ─── the other modules in the rail ──────────────────────────────────────────
# Web FOTA is every page above. The two COTA modules share nothing with the snapshot
# warehouse; they only borrow the page frame, so the header and footer hide FOTA's chips there.

@app.get("/web-cota", response_class=HTMLResponse)
def web_cota(request: Request):
    """Listed in the rail before it exists, and says so, rather than linking nowhere."""
    ctx = page_context(get_conn(), request, None)
    return templates.TemplateResponse(request, "web_cota.html", ctx)


def _cota_context(conn: sqlite3.Connection, request: Request, tab: str, **extra) -> dict:
    """What every Intouch COTA tab needs: the page frame, the tab, and the sign-in chip."""
    # The day's COTA record lives for the day: a session started on a later day clears the
    # earlier ones (once per process). Jobs left running by a restart come back paused.
    cota_campaign.purge_previous_days(conn)
    cota_campaign.recover(conn)
    ctx = page_context(conn, request, None)
    ctx.update(tab=tab, cota_status=cota_connection.status(), **extra)
    return ctx


# ─── Intouch COTA: jobs — many devices, a sequence each ─────────────────────

def _jobs_context(conn, request: Request, **extra) -> dict:
    """The jobs list. One-off sends from the Devices page are listed too — that page says "see
    Jobs". A job's own calls and a console's are not: they have their own pages."""
    sends = [j for j in cota.jobs(conn) if j["source_file"] != "campaign"
             and not (j["source_file"] or "").startswith("console:")]
    return _cota_context(conn, request, "jobs", jobs=cota_campaign.campaigns(conn), sends=sends,
                         live=cota_campaign.active(conn), today=cota_campaign.today(conn), **extra)


JOB_FORM_FIELDS = ("name", "group_id", "devices", "commands", "batch_size", "rate_per_sec",
                   "time_limit_minutes", "answer_wait_seconds", "device_type", "cmd_type")


def _new_job_context(conn, request: Request, **extra) -> dict:
    """The new-job page: the form, the plan once previewed, and the library to pick from."""
    return _cota_context(conn, request, "jobs", groups=cota_campaign.groups(conn),
                         live=cota_campaign.active(conn), library=cota_library.commands(conn),
                         defaults={"batch": cota_campaign.DEFAULT_BATCH,
                                   "rate": cota_campaign.DEFAULT_RATE,
                                   "time_limit": cota_campaign.DEFAULT_TIME_LIMIT_MINUTES,
                                   "answer_wait": cota_campaign.DEFAULT_ANSWER_WAIT_SECONDS,
                                   "type": cota.DEFAULT_DEVICE_TYPE, "cmd": cota.DEFAULT_CMD_TYPE},
                         **extra)


@app.get("/cota", response_class=HTMLResponse)
def cota_jobs(request: Request):
    return templates.TemplateResponse(request, "cota_jobs.html", _jobs_context(get_conn(), request))


@app.get("/cota/jobs/new", response_class=HTMLResponse)
def cota_job_new(request: Request, source: int = Query(0, alias="from"), which: str = "all"):
    """A new job — blank, or filled from an earlier one (Duplicate, Rerun). Declared before
    /cota/jobs/{job_id}, which would otherwise claim "new" and refuse it as not a number."""
    conn = get_conn()
    draft = cota_campaign.draft_from(conn, source, which if which == "unfinished" else "all") \
        if source else None
    return templates.TemplateResponse(request, "cota_job_new.html",
                                      _new_job_context(conn, request, draft=draft))


def _job_form_devices(conn, group_id: str, device_text: str) -> tuple[list[int], list[str]]:
    if group_id.isdigit():
        return cota_campaign.group_devices(conn, int(group_id)), []
    ids, problems, _ = cota_campaign.parse_device_ids(device_text)
    return ids, problems


@app.post("/cota/jobs/preview", response_class=HTMLResponse)
async def cota_jobs_preview(request: Request):
    """The plan, before anything is sent. A CSV may come with it, hence async to read it."""
    form = await request.form()
    upload = form.get("file")
    content = await upload.read() if upload is not None and getattr(upload, "filename", "") else b""

    def build():
        conn = get_conn()
        draft = {k: str(form.get(k, "")) for k in JOB_FORM_FIELDS}
        if content:
            rows, _ = cota_campaign.read_csv_devices(content)
            draft["devices"] = "\n".join(d for d, _ in rows)
            draft["group_id"] = ""
            cota_campaign.learn_tracking_codes(conn, rows)
        ids, problems = _job_form_devices(conn, draft["group_id"], draft["devices"])
        try:
            batch = max(1, int(draft["batch_size"] or cota_campaign.DEFAULT_BATCH))
            rate = max(0.1, float(draft["rate_per_sec"] or cota_campaign.DEFAULT_RATE))
        except ValueError:
            batch, rate = cota_campaign.DEFAULT_BATCH, cota_campaign.DEFAULT_RATE
        the_plan = cota_campaign.plan(
            conn, ids, draft["commands"], batch_size=batch, rate_per_sec=rate,
            answer_wait_seconds=_number(draft["answer_wait_seconds"], float, None),
            time_limit_minutes=_number(draft["time_limit_minutes"], float, None))
        the_plan["problems"] = problems
        the_plan["device_ids"] = ",".join(map(str, ids))
        return _new_job_context(conn, request, draft=draft, preview=the_plan)

    ctx = await run_in_threadpool(build)
    return templates.TemplateResponse(request, "cota_job_new.html", ctx)


def _number(text: str, cast, default):
    try:
        return cast(text) if str(text).strip() else default
    except ValueError:
        return default


@app.post("/cota/jobs/start", response_class=HTMLResponse)
def cota_jobs_start(request: Request, name: str = Form(""), device_ids: str = Form(""),
                    commands: str = Form(""), batch_size: str = Form(""),
                    rate_per_sec: str = Form(""), time_limit_minutes: str = Form(""),
                    answer_wait_seconds: str = Form(""), device_type: str = Form(""),
                    cmd_type: str = Form("")):
    conn = get_conn()
    ids, _, _ = cota_campaign.parse_device_ids(device_ids)
    # Refused, the page goes back to the form as it was — not to a list that has lost it.
    draft = {"name": name, "group_id": "", "devices": ", ".join(map(str, ids)),
             "commands": commands, "batch_size": batch_size, "rate_per_sec": rate_per_sec,
             "time_limit_minutes": time_limit_minutes, "answer_wait_seconds": answer_wait_seconds,
             "device_type": device_type, "cmd_type": cmd_type}

    def refused(message: str):
        return templates.TemplateResponse(request, "cota_job_new.html", _new_job_context(
            conn, request, draft=draft, result={"level": "error", "message": message}))

    if not cota.load_token() and not cota_connection.renew():
        return refused("Not signed in to the cloud — sign in, then start.")
    try:
        cid = cota_campaign.create(
            conn, name=name, device_ids=ids, commands_text=commands,
            device_type=_number(device_type, int, cota.DEFAULT_DEVICE_TYPE),
            cmd_type=_number(cmd_type, int, cota.DEFAULT_CMD_TYPE),
            batch_size=_number(batch_size, int, cota_campaign.DEFAULT_BATCH),
            rate_per_sec=_number(rate_per_sec, float, cota_campaign.DEFAULT_RATE),
            time_limit_minutes=_number(time_limit_minutes, float, None),
            answer_wait_seconds=_number(answer_wait_seconds, float, None))
    except cota_campaign.CampaignError as exc:
        return refused(str(exc))
    cota_campaign.start(cid)
    return RedirectResponse(f"/cota/jobs/{cid}", status_code=303)


JOB_DEVICE_FILTERS = ("", "waiting", "ready", "done", "failed", "expired", "cancelled")


def _open_devices(text: str) -> list[int]:
    """Devices whose conversation is unfolded on the page — kept by the page, at most a page."""
    return [int(x) for x in (text or "").split(",") if x.strip().isdigit()][:50]


def _job_context(conn, request: Request, job_id: int, state: str = "", q: str = "",
                 page: int = 1, open_: str = "", **extra) -> dict | None:
    job = cota_campaign.summary(conn, job_id)
    if not job:
        return None
    state = state if state in JOB_DEVICE_FILTERS else ""
    rows, total = cota_campaign.device_rows(conn, job_id, state=state, search=q, page=page)
    shown = {r["device_id"] for r in rows}
    opened = {d: cota_campaign.device_conversation(conn, job_id, d)
              for d in _open_devices(open_) if d in shown}
    return _cota_context(conn, request, "jobs", job=job, grid=cota_campaign.grid(conn, job_id),
                         charts=cota_campaign.job_charts(conn, job_id, job),
                         devices=rows, devices_total=total, state=state, q=q,
                         page=max(1, page), pages=max(1, -(-total // 50)), opened=opened,
                         now_epoch=round(cota._parse(cota._now()).timestamp()),
                         stages=cota.STAGES, words=cota_campaign.RESULT_WORDS, **extra)


@app.get("/cota/jobs/{job_id}", response_class=HTMLResponse)
def cota_job(request: Request, job_id: int, state: str = "", q: str = "", page: int = 1,
             open_: str = Query("", alias="open")):
    conn = get_conn()
    ctx = _job_context(conn, request, job_id, state, q, page, open_)
    if ctx is None:
        return RedirectResponse("/cota", status_code=303)
    return templates.TemplateResponse(request, "cota_job.html", ctx)


@app.get("/cota/jobs/{job_id}/status")
def cota_job_status(request: Request, job_id: int, state: str = "", q: str = "", page: int = 1,
                    open_: str = Query("", alias="open")):
    """What the job page redraws every few seconds — this install's record only, no cloud call."""
    conn = get_conn()
    ctx = _job_context(conn, request, job_id, state, q, page, open_)
    if ctx is None:
        return JSONResponse({"ok": False})
    job = ctx["job"]
    return JSONResponse({"ok": True, "state": job["state"], "percent": job["percent"],
                         "html": templates.get_template("_cota_job_live.html").render(ctx)})


@app.post("/cota/jobs/{job_id}/control", response_class=HTMLResponse)
def cota_job_control(job_id: int, action: str = Form("")):
    conn = get_conn()
    if action in ("pause", "resume", "cancel"):
        cota_campaign.request(conn, job_id, action)
    return RedirectResponse(f"/cota/jobs/{job_id}", status_code=303)


@app.post("/cota/commands/describe")
def cota_commands_describe(text: str = Form("")):
    """Each typed line's name, as the page shows it under the box. Local only."""
    cota_library.refresh(get_conn())
    return JSONResponse({"lines": cota_library.describe_lines(text[:200_000])})


@app.get("/cota/jobs/{job_id}/export")
def cota_job_export(job_id: int, format: str = "csv"):
    """Every device × command. CSV streams, so 150,000 rows never sit in memory; Excel is offered
    up to its practical size, and the page says so beyond it."""
    conn = get_conn()
    job = cota_campaign.summary(conn, job_id)
    if not job:
        return JSONResponse({"error": "no such job"}, status_code=404)
    stem = f"cota_job_{job_id}"
    if format == "xlsx":
        if job["total"] > 200_000:
            return JSONResponse({"error": "Too many rows for Excel — download the CSV."},
                                status_code=413)
        rows = list(cota_campaign.export_rows(conn, job_id))
        source = [("Job", f"#{job_id} {job['name']}"), ("Devices", job["devices"]),
                  ("Commands", " → ".join(job["names"])), ("Answered", job["results"].get("done", 0)),
                  ("Failed", job["results"].get("failed", 0)),
                  ("Expired", job["results"].get("expired", 0)),
                  ("Send calls", job["send_calls"]), ("Checks", job["poll_calls"]),
                  ("Exported", f"{datetime.now():{cota.RANGE_FORMAT}}")]
        return _download(exports.to_xlsx(rows, cota_campaign.EXPORT_COLUMNS, sheet_name="Job",
                                         source=source), "xlsx", stem)

    def stream():
        import csv as _csv
        yield "\ufeff"
        buffer = io.StringIO()
        writer = _csv.writer(buffer, lineterminator="\n")
        writer.writerow([h for _, h in cota_campaign.EXPORT_COLUMNS])
        for row in cota_campaign.export_rows(get_conn(), job_id):
            writer.writerow([exports._clean(row.get(k)) for k, _ in cota_campaign.EXPORT_COLUMNS])
            if buffer.tell() > 64_000:
                yield buffer.getvalue()
                buffer.seek(0)
                buffer.truncate()
        yield buffer.getvalue()

    return StreamingResponse(stream(), media_type=EXPORT_MEDIA["csv"], headers={
        "Content-Disposition": f'attachment; filename="{exports.timestamped(stem, "csv")}"'})


# ── groups, on the Devices page ──

@app.post("/cota/groups", response_class=HTMLResponse)
async def cota_groups_save(request: Request):
    form = await request.form()
    upload = form.get("file")
    content = await upload.read() if upload is not None and getattr(upload, "filename", "") else b""
    name = str(form.get("name", ""))

    def save():
        conn = get_conn()
        rows = []
        if content:
            rows, _ = cota_campaign.read_csv_devices(content)
            text = "\n".join(d for d, _ in rows)
        else:
            text = str(form.get("devices", ""))
        ids, problems, dupes = cota_campaign.parse_device_ids(text)
        if problems:
            return {"level": "error", "message": "Not saved — " + "; ".join(problems[:5])
                    + (" …" if len(problems) > 5 else "")}
        try:
            cota_campaign.save_group(conn, name, ids)
        except cota_campaign.CampaignError as exc:
            return {"level": "error", "message": str(exc)}
        learned = cota_campaign.learn_tracking_codes(conn, rows)
        note = f" ({dupes} duplicate{'' if dupes == 1 else 's'} dropped)" if dupes else ""
        mapped = (f" {learned:,} tracking code{'' if learned == 1 else 's'} added to the device "
                  "map." if learned else "")
        return {"level": "ok", "message": f"Group “{name.strip()}” saved with {len(ids):,} "
                                          f"device{'' if len(ids) == 1 else 's'}{note}.{mapped}"}

    result = await run_in_threadpool(save)
    ctx = await run_in_threadpool(lambda: _cota_devices_context(get_conn(), request, result=result))
    return templates.TemplateResponse(request, "cota_devices.html", ctx)


@app.get("/cota/groups/template.csv")
def cota_groups_template():
    """The upload format — the cloud's own device list: id (required), trackingCode (optional)."""
    return Response(content=("\ufeff" + cota_campaign.TEMPLATE_CSV).encode("utf-8"),
                    media_type=EXPORT_MEDIA["csv"],
                    headers={"Content-Disposition": 'attachment; filename="cota_devices_template.csv"'})


@app.post("/cota/groups/{group_id}/delete", response_class=HTMLResponse)
def cota_groups_delete(request: Request, group_id: int):
    conn = get_conn()
    cota_campaign.delete_group(conn, group_id)
    return RedirectResponse("/cota/devices#groups", status_code=303)


# ─── Intouch COTA: the command library ──────────────────────────────────────

def _commands_context(conn, request: Request, q: str = "", tag: str = "", edit: int = 0,
                      **extra) -> dict:
    editing = next((c for c in cota_library.commands(conn) if c["id"] == edit), None) if edit else None
    return _cota_context(conn, request, "commands", parameters=cota_library.parameters(conn),
                         saved=cota_library.commands(conn, q=q, tag=tag),
                         saved_total=conn.execute("SELECT COUNT(*) FROM cota_saved_command").fetchone()[0],
                         tags=cota_library.all_tags(conn), q=q, tag=tag, editing=editing, **extra)


@app.get("/cota/commands", response_class=HTMLResponse)
def cota_commands(request: Request, q: str = "", tag: str = "", edit: int = 0):
    return templates.TemplateResponse(request, "cota_commands.html",
                                      _commands_context(get_conn(), request, q, tag, edit))


@app.post("/cota/commands/parameters", response_class=HTMLResponse)
def cota_commands_parameter(request: Request, code: str = Form(""), name: str = Form("")):
    conn = get_conn()
    try:
        saved = cota_library.save_parameter(conn, code, name)
        result = {"level": "ok", "message": f"{saved} is now “{name.strip()}” — every GET, SET "
                                            "and CLR of it reads by that name."}
    except cota_library.LibraryError as exc:
        result = {"level": "error", "message": str(exc)}
    return templates.TemplateResponse(request, "cota_commands.html",
                                      _commands_context(conn, request, result=result))


@app.post("/cota/commands/parameters/{code}/delete")
def cota_commands_parameter_delete(code: str):
    cota_library.delete_parameter(get_conn(), code)
    return RedirectResponse("/cota/commands#parameters", status_code=303)


@app.post("/cota/commands/saved", response_class=HTMLResponse)
def cota_commands_save(request: Request, command_id: str = Form(""), name: str = Form(""),
                       val1: str = Form(""), tags: str = Form(""), note: str = Form("")):
    conn = get_conn()
    editing = int(command_id) if command_id.isdigit() else None
    try:
        cota_library.save_command(conn, name=name, val1=val1, tags=tags, note=note,
                                  command_id=editing)
        return RedirectResponse("/cota/commands#saved", status_code=303)
    except cota_library.LibraryError as exc:
        draft = {"id": editing, "name": name, "val1": val1, "tags": tags, "note": note}
        return templates.TemplateResponse(request, "cota_commands.html", _commands_context(
            conn, request, result={"level": "error", "message": str(exc)}, draft=draft))


@app.post("/cota/commands/saved/{command_id}/delete")
def cota_commands_delete(command_id: int):
    cota_library.delete_command(get_conn(), command_id)
    return RedirectResponse("/cota/commands#saved", status_code=303)


COTA_DEVICE_PAGE_SIZES = (25, 50, 100)


def _cota_devices_context(conn, request: Request, q: str = "", page: int = 1, size: int = 50,
                          **extra) -> dict:
    size = size if size in COTA_DEVICE_PAGE_SIZES else 50
    q = q.strip()
    # Any part of an IMEI, or an exact cloud device id — the two numbers people hold.
    where, params = ("WHERE imei LIKE ? OR CAST(device_id AS TEXT) = ?",
                     (f"%{q}%", q)) if q else ("", ())
    total = conn.execute(f"SELECT COUNT(*) FROM cota_device {where}", params).fetchone()[0]
    pages = max(1, -(-total // size))
    page = min(max(1, page), pages)
    rows = [dict(r) for r in conn.execute(
        f"SELECT imei, device_id, device_type, source, updated_at FROM cota_device {where} "
        "ORDER BY imei LIMIT ? OFFSET ?", (*params, size, (page - 1) * size))]
    by_type = [dict(r) for r in conn.execute(
        "SELECT device_type, COUNT(*) AS devices FROM cota_device "
        "GROUP BY device_type ORDER BY devices DESC")]
    mapped = sum(r["devices"] for r in by_type)
    extra.setdefault("groups", cota_campaign.groups(conn))
    return _cota_context(conn, request, "devices", rows=rows, total=total, q=q, page=page,
                         pages=pages, size=size, sizes=COTA_DEVICE_PAGE_SIZES,
                         by_type=by_type, mapped=mapped,
                         max_manual=cota.MAX_MANUAL_DEVICES, **extra)


@app.get("/cota/devices", response_class=HTMLResponse)
def cota_devices(request: Request, q: str = "", page: int = 1, size: int = 50):
    ctx = _cota_devices_context(get_conn(), request, q, page, size)
    return templates.TemplateResponse(request, "cota_devices.html", ctx)


def _import_device_map(filename: str, content: bytes) -> dict:
    """Runs in the threadpool. The connection is opened here because sqlite3 objects belong
    to the thread that created them."""
    # Only "is this really a sheet" is checked here. sources' CSV check insists on an IMEI
    # column, which suits a snapshot but not a map headed "Device Unique No"; the COTA parser
    # names any missing column itself.
    is_csv = sources.looks_like_csv(filename, content)
    head = content[:400].decode("utf-8", errors="replace").lower()
    if not content.strip():
        return {"level": "error", "message": "The uploaded file is empty."}
    if "<html" in head or "<!doctype" in head:
        return {"level": "error", "message": "The uploaded file is a web page, not a sheet."}
    if not is_csv and not content.startswith(sources.XLSX_MAGIC):
        return {"level": "error",
                "message": "The uploaded file is not a spreadsheet or a CSV."}

    # A scratch file, not the export folder: a device map is not a snapshot, and left where
    # ingest-dir looks it would be ingested as one.
    with tempfile.TemporaryDirectory() as scratch:
        path = Path(scratch) / sources.safe_filename(filename, fallback_stem="device_map",
                                                     suffix=".csv" if is_csv else ".xlsx")
        path.write_bytes(content)
        try:
            result = cota.import_device_map(db.connect(), path)
        except cota.CotaError as exc:
            return {"level": "error", "message": str(exc)}

    message = f"{result.added:,} added, {result.updated:,} updated"
    if result.skipped:
        shown = ", ".join(result.skipped[:5]) + (" …" if len(result.skipped) > 5 else "")
        message += (f"; {len(result.skipped):,} skipped for a missing IMEI, device id or "
                    f"type ({shown})")
    return {"level": "warn" if result.skipped else "ok", "message": message + "."}


@app.post("/cota/devices/import", response_class=HTMLResponse)
async def cota_devices_import(request: Request, file: UploadFile = File(...)):
    # async only to await the read; the import goes to the threadpool so it cannot stall the
    # event loop (see "Two upload routes" in CLAUDE.md).
    content = await file.read()
    result = await run_in_threadpool(_import_device_map, file.filename or "", content)
    ctx = await run_in_threadpool(lambda: _cota_devices_context(get_conn(), request,
                                                                result=result))
    return templates.TemplateResponse(request, "cota_devices.html", ctx)


def _payload_digest(payloads: list[dict]) -> str:
    """Ties a Send to the Preview it confirms: the same form must produce the same bodies."""
    import hashlib

    return hashlib.sha256(json.dumps(payloads, sort_keys=True).encode()).hexdigest()[:16]


@app.post("/cota/devices/send", response_class=HTMLResponse)
def cota_devices_send(
    request: Request,
    action: str = Form("preview"),         # read | preview | send
    payload: str = Form(""),
    device_type: str = Form(""),
    device_ids: str = Form(""),
    cmd_type: str = Form(""),
    val1: str = Form(""), val2: str = Form(""), val3: str = Form(""),
    val4: str = Form(""), val5: str = Form(""), val6: str = Form(""),
    val7: str = Form(""), val8: str = Form(""), val9: str = Form(""),
    previewed: str = Form(""),
    confirm: bool = Form(False),
):
    """Send one command to devices typed in by id. Three explicit steps, because this writes
    to real devices: read a pasted payload (no network), preview the exact bodies (no network),
    then send — only with the box ticked, and only if the form still matches the preview."""
    conn = get_conn()
    values = [val1, val2, val3, val4, val5, val6, val7, val8, val9]
    cmd = {"payload": payload, "device_type": device_type.strip(),
           "device_ids": device_ids.strip(), "cmd_type": cmd_type.strip(),
           "values": [v.strip() for v in values]}
    extra: dict = {"cmd": cmd}

    def render(**more):
        extra.update(more)
        return templates.TemplateResponse(
            request, "cota_devices.html", _cota_devices_context(conn, request, **extra))

    try:
        if action == "read":
            read = cota.parse_portal_payload(payload)
            cmd.update(device_type=str(read["device_type"]),
                       device_ids=", ".join(str(i) for i in read["device_ids"]),
                       cmd_type=str(read["cmd_type"]),
                       values=[read["params"].get(f"val{n}", "") for n in range(1, 10)])
            return render(result={"level": "ok", "message": "Payload read into the form. "
                                  "Check it, then Preview."})

        ids, bad = cota.parse_device_ids(cmd["device_ids"])
        if bad:
            raise cota.CotaError(f"Not a device id: {', '.join(bad[:5])}. Device ids are the "
                                 "cloud's numbers (e.g. 14906), not IMEIs.")
        if not cmd["device_type"].isdigit() or not cmd["cmd_type"].isdigit():
            raise cota.CotaError("Device model (type) and command type must be numbers.")
        params = {f"val{n}": v for n, v in enumerate(cmd["values"], start=1) if v}
        the_plan = cota.manual_plan(conn, int(cmd["device_type"]), ids, int(cmd["cmd_type"]),
                                    params)
        payloads = cota.plan_payloads(the_plan)
        digest = _payload_digest(payloads)
        preview = {"payloads": [json.dumps(p) for p in payloads], "digest": digest,
                   "devices": [{"device_id": t.device_id, "imei": t.imei}
                               for t in the_plan.tasks],
                   "count": len(the_plan.tasks),
                   "unknown": sum(1 for t in the_plan.tasks if not t.imei)}

        if action != "send":
            return render(preview=preview)
        if previewed != digest:
            return render(preview=preview, result={
                "level": "warn", "message": "The form changed since the preview. Check the "
                                            "new preview below, then send again."})
        if not confirm:
            return render(preview=preview, result={
                "level": "warn", "message": "Tick the box to confirm before sending."})
        if not cota.load_token():
            return render(preview=preview, result={
                "level": "error", "message": "Not signed in to the cloud — use Sign in, top "
                                             "right, then send again."})

        job_id = cota.create_job(conn, the_plan, source_file="typed in",
                                 name=f"Command {cmd['cmd_type']} → "
                                      f"{len(the_plan.tasks)} device"
                                      f"{'' if len(the_plan.tasks) == 1 else 's'}")
        client = cota_connection.client()
        try:
            sent = cota.send_job(conn, job_id, client)
            stopped = None
        except cota.CotaError as exc:
            sent, stopped = None, str(exc)
        finally:
            client.close()
        calls = [dict(r) for r in conn.execute("""
            SELECT batch_no, COUNT(*) AS devices, MAX(state) AS state,
                   MAX(http_status) AS http_status, MAX(send_reply) AS send_reply,
                   MAX(error) AS error
            FROM cota_task WHERE job_id = ? GROUP BY batch_no ORDER BY batch_no
        """, (job_id,))]
        if stopped:
            result = {"level": "error", "message": stopped}
        elif sent.failed:
            result = {"level": "error",
                      "message": f"{sent.failed} of {sent.sent + sent.failed} device(s) were "
                                 f"not accepted by the cloud — see the reply below."}
        else:
            result = {"level": "ok",
                      "message": f"Sent to {sent.sent} device(s) in {sent.calls} call(s). "
                                 "The cloud accepted it; the device's own reply comes later."}
        return render(result=result, sent={"job_id": job_id, "calls": calls},
                      cmd={**cmd, "device_ids": ""})
    except cota.CotaError as exc:
        return render(result={"level": "error", "message": str(exc)})


# ─── Intouch COTA: Configure — one device, one command, and what it said ────

def _console_device(conn, device_text: str, type_text: str) -> dict:
    """The device a console page is about, with what Web FOTA knows of it."""
    device_id, device_type, imei = cota.resolve_device(conn, device_text)
    if type_text.strip().isdigit():
        device_type = int(type_text.strip())
    fleet = cota.fleet_context(conn, imei)
    return {"id": device_id, "type": device_type or cota.DEFAULT_DEVICE_TYPE, "imei": imei,
            "fleet": fleet,
            # "ID: 14906 | IMEI: 865510083360422" — the IMEI once the map, or a cloud record,
            # has told us it.
            "label": f"ID: {device_id}" + (f" | IMEI: {imei}" if imei else ""),
            "seen_age": _relative_age(fleet["seen_at"]) if fleet and fleet["seen_at"] else ""}


def _console_range(from_text: str, to_text: str) -> tuple[datetime, datetime, dict | None]:
    """The page's time range, and a notice when it had to be refused or adjusted."""
    try:
        start, end, note = cota.parse_range(from_text, to_text)
    except cota.CotaError as exc:
        start, end = cota.day_range()
        return start, end, {"level": "error", "message": f"{exc} Showing today instead."}
    return start, end, {"level": "warn", "message": note} if note else None


def _console_query(device_id, device_type, start: datetime, end: datetime, **more) -> str:
    """The query string that reopens this exact view — device, model, range, and the command
    type when it is not the default."""
    if more.get("cmd") in (None, "", cota.DEFAULT_CMD_TYPE, str(cota.DEFAULT_CMD_TYPE)):
        more.pop("cmd", None)
    return urlencode({"device": device_id, "type": device_type,
                      "from": f"{start:{cota.RANGE_FORMAT}}", "to": f"{end:{cota.RANGE_FORMAT}}",
                      **more})


def _console_thread(conn, device: dict, start: datetime, end: datetime) -> dict:
    """The thread in the range, plus when the page should stop checking on its own."""
    th = cota.thread(conn, device["id"], start, end)
    watch_until = None
    if th["last_sent"]:
        until = th["last_sent"] + timedelta(seconds=cota.CONSOLE_WATCH_SECONDS)
        if until > datetime.now():
            watch_until = int(until.timestamp() * 1000)
    th["watch_until"] = watch_until
    th["last_sent_ms"] = int(th["last_sent"].timestamp() * 1000) if th["last_sent"] else None
    # What the page holds of this period from the cloud — and whether it must ask on opening.
    th["loaded"] = cota.period_loaded(conn, device["id"], start, end)
    return th


def _range_presets(device: dict) -> list[dict]:
    today = cota.day_range()
    yesterday = cota.day_range(today[0] - timedelta(days=1))
    week = (today[0] - timedelta(days=6), today[1])
    return [{"label": label, "query": _console_query(device["id"], device["type"], *span)}
            for label, span in (("Today", today), ("Yesterday", yesterday),
                                ("Last 7 days", week))]


def _console_context(conn, request: Request, device_text: str = "", type_text: str = "",
                     from_text: str = "", to_text: str = "", **extra) -> dict:
    start, end, range_note = _console_range(from_text, to_text)
    if range_note:
        extra.setdefault("result", range_note)
    device = th = run = None
    cota_run.recover(conn)                 # writes only when a run was left behind by a restart
    if device_text.strip():
        try:
            device = _console_device(conn, device_text, type_text)
            th = _console_thread(conn, device, start, end)
            run = _visible_run(conn, device["id"])
        except cota.CotaError as exc:
            extra.setdefault("result", {"level": "error", "message": str(exc)})
    return _cota_context(
        conn, request, "console", device=device, thread=th,
        conversations=cota.conversations(conn),
        recent=cota.recent_commands(conn, device["id"]) if device else [],
        library=cota_library.commands(conn),
        stages=cota.STAGES, default_type=cota.DEFAULT_DEVICE_TYPE,
        command_names={str(k): v for k, v in cota.COMMAND_NAMES.items()},
        default_cmd=cota.DEFAULT_CMD_TYPE,
        range_from=f"{start:{cota.RANGE_FORMAT}}", range_to=f"{end:{cota.RANGE_FORMAT}}",
        range_label=f"{start:%d %b %H:%M} – {end:%d %b %H:%M}",
        range_query=_console_query(device["id"], device["type"], start, end) if device else "",
        presets=_range_presets(device) if device else [],
        max_range_days=cota.MAX_RANGE_DAYS,
        auto_every=cota.CONSOLE_AUTO_EVERY, auto_tries=cota.CONSOLE_AUTO_TRIES,
        run=run, outcomes=cota_run.OUTCOME_WORDS,
        run_rules={"after_answer": cota_run.GAP_AFTER_ANSWER_SECONDS,
                   "guard": cota_run.GUARD_SECONDS["get"],
                   "tries": cota_run.MAX_ATTEMPTS, "wait": cota_run.ANSWER_WAIT_SECONDS},
        start_device=device_text, start_type=type_text, **extra)


def _visible_run(conn, device_id: int) -> dict | None:
    """The run the page shows: a live one, or the last one if it ended within the hour."""
    run = cota_run.latest_run(conn, device_id)
    if not run:
        return None
    if run["state"] in ("running", "paused"):
        return run
    ended = cota._parse(run["finished_at"]) if run["finished_at"] else None
    return run if ended and datetime.now() - ended < timedelta(hours=1) else None


@app.get("/cota/console", response_class=HTMLResponse)
def cota_console(request: Request, device: str = "", type: str = "",
                 range_from: str = Query("", alias="from"), to: str = "", cmd: str = ""):
    draft = {"cmd_type": cmd} if cmd.isdigit() else None
    ctx = _console_context(get_conn(), request, device, type, range_from, to, draft=draft)
    return templates.TemplateResponse(request, "cota_console.html", ctx)


@app.post("/cota/console/send", response_class=HTMLResponse)
def cota_console_send(
    request: Request,
    device_id: str = Form(""),
    device_type: str = Form(""),
    cmd_type: str = Form(""),
    range_from: str = Form("", alias="from"),
    val1: str = Form(""), val2: str = Form(""), val3: str = Form(""),
    val4: str = Form(""), val5: str = Form(""), val6: str = Form(""),
    val7: str = Form(""), val8: str = Form(""), val9: str = Form(""),
):
    """Send one command to one device. Answers with a redirect, so refreshing the page that
    follows can never send the command a second time — and the view it lands on keeps its
    start but reaches to the end of today, so the new command is always in it."""
    conn = get_conn()
    values = [v.strip() for v in (val1, val2, val3, val4, val5, val6, val7, val8, val9)]
    draft = {"cmd_type": cmd_type.strip(), "values": values}

    def again(message: str):
        ctx = _console_context(conn, request, device_id, device_type, range_from, "",
                               draft=draft, result={"level": "error", "message": message})
        return templates.TemplateResponse(request, "cota_console.html", ctx)

    if not device_id.strip().isdigit():
        return again("Open a device first.")
    if cota_run.active_run(conn, int(device_id)):
        return again("A sequence is running for this device — pause it and let it finish, or "
                     "cancel it, before sending by hand. One command at a time.")
    if cota_campaign.busy_devices(conn, [int(device_id)]):
        return again("This device is in a running job — it finishes, or the job is cancelled, "
                     "before anything is sent by hand. One command at a time.")
    if not device_type.strip().isdigit():
        return again("The device model (deviceType) is a number (e.g. 124).")
    if not cmd_type.strip().isdigit():
        return again("The command type is a number (e.g. 36).")
    # A token already known to be rejected is renewed first, when a saved password allows it.
    if cota_connection.status()["rejected"]:
        cota_connection.renew()
    if not cota.load_token() and not cota_connection.renew():
        return again("Not signed in to the cloud — use Sign in, top right, then send again.")
    params = {f"val{n}": v for n, v in enumerate(values, start=1) if v}
    try:
        client = cota_connection.client()
        try:
            task = cota.console_send(conn, client, int(device_id), int(device_type),
                                     int(cmd_type), params)
        finally:
            client.close()
        # Rejected on the way: the cloud refused the send, so nothing reached the device. With a
        # saved password, sign in again and send it once more — the refused attempt stays in
        # the record, so the thread shows exactly what happened.
        if task["state"] == "send_failed" and "rejected the token" in (task["error"] or "") \
                and cota_connection.renew():
            client = cota_connection.client()
            try:
                cota.console_send(conn, client, int(device_id), int(device_type),
                                  int(cmd_type), params)
            finally:
                client.close()
    except cota.CotaError as exc:
        return again(str(exc))
    try:
        start = cota.parse_range(range_from, "")[0]
    except cota.CotaError:
        start = cota.day_range()[0]
    start, end = cota.range_after_send(start)
    return RedirectResponse(
        f"/cota/console?{_console_query(int(device_id), int(device_type), start, end, cmd=cmd_type.strip())}#latest",
        status_code=303)


def _cloud_chip() -> dict:
    """The sign-in chip's state, for the page to redraw it without a reload."""
    st = cota_connection.status()
    return {"level": st["level"], "label": st["label"], "title": st["title"]}


@app.post("/cota/console/run", response_class=HTMLResponse)
def cota_console_run(request: Request, device_id: str = Form(""), device_type: str = Form(""),
                     cmd_type: str = Form(""), commands: str = Form(""),
                     range_from: str = Form("", alias="from")):
    """Start a sequence: one device, the commands in order, one at a time, in the background."""
    conn = get_conn()

    def again(message: str):
        ctx = _console_context(conn, request, device_id, device_type, range_from, "",
                               draft={"cmd_type": cmd_type, "commands": commands},
                               result={"level": "error", "message": message}, mode="sequence")
        return templates.TemplateResponse(request, "cota_console.html", ctx)

    if not (device_id.isdigit() and device_type.strip().isdigit() and cmd_type.strip().isdigit()):
        return again("Open a device first; model and type are numbers.")
    if not cota.load_token() and not cota_connection.renew():
        return again("Not signed in to the cloud — sign in, then start the sequence.")
    try:
        run_id = cota_run.create_run(conn, int(device_id), int(device_type), int(cmd_type), commands)
    except cota_run.RunError as exc:
        return again(str(exc))
    cota_run.start(run_id)
    try:
        start = cota.parse_range(range_from, "")[0]
    except cota.CotaError:
        start = cota.day_range()[0]
    start, end = cota.range_after_send(start)
    return RedirectResponse(
        f"/cota/console?{_console_query(int(device_id), int(device_type), start, end, cmd=cmd_type)}#run",
        status_code=303)


@app.post("/cota/console/run/control", response_class=HTMLResponse)
def cota_console_run_control(request: Request, run_id: int = Form(...), action: str = Form(""),
                             back: str = Form("/cota/console")):
    conn = get_conn()
    if action in ("pause", "resume", "cancel"):
        cota_run.request(conn, run_id, action)
    # Only ever back to the console — the field is the page's own query string, not a URL.
    target = back if back.startswith("/cota/console") else "/cota/console"
    return RedirectResponse(target, status_code=303)


@app.get("/cota/console/run-status")
def cota_console_run_status(device: str = "", type: str = "",
                            range_from: str = Query("", alias="from"), to: str = ""):
    """What a page watching a sequence redraws every few seconds. Reads only this install's
    record — the runner is the one asking the cloud — so polling it costs the cloud nothing."""
    conn = get_conn()
    try:
        info = _console_device(conn, device, type)
        start, end, _ = cota.parse_range(range_from, to)
    except cota.CotaError as exc:
        return JSONResponse({"ok": False, "message": str(exc)})
    run = _visible_run(conn, info["id"])
    th = _console_thread(conn, info, start, end)
    return JSONResponse({
        "ok": True, "state": run["state"] if run else None,
        "run_html": templates.get_template("_cota_run.html").render(
            run=run, outcomes=cota_run.OUTCOME_WORDS),
        "html": templates.get_template("_cota_thread.html").render(
            thread=th, device=info, stages=cota.STAGES),
        "counts_html": templates.get_template("_cota_counts.html").render(thread=th),
        "cloud": _cloud_chip()})


@app.post("/cota/console/check")
def cota_console_check(request: Request, device: str = Form(""), type: str = Form(""),
                       range_from: str = Form("", alias="from"), to: str = Form("")):
    """Ask the cloud for this device's records over the page's range, and hand back the redrawn
    thread. Called by the page after a send and by the refresh button."""
    conn = get_conn()
    try:
        info = _console_device(conn, device, type)
        start, end, _ = cota.parse_range(range_from, to)
    except cota.CotaError as exc:
        return JSONResponse({"ok": False, "message": str(exc)})
    if not cota.load_token() and not cota_connection.renew():
        return JSONResponse({"ok": False, "message": "Not signed in to the cloud."})
    renewed = False
    for attempt in (1, 2):
        try:
            client = cota_connection.client()
            try:
                outcome = cota.console_check(conn, client, info["id"], start, end)
            finally:
                client.close()
            break
        except cota.CotaError as exc:
            # An expired token, and a saved password to get a new one with: sign in again by
            # itself and ask once more. A check only reads, so repeating it is harmless.
            if attempt == 1 and "rejected the token" in str(exc) and cota_connection.renew():
                renewed = True
                continue
            return JSONResponse({"ok": False, "message": str(exc), "cloud": _cloud_chip()})
    info = _console_device(conn, device, type)            # the IMEI may have just been learned
    th = _console_thread(conn, info, start, end)
    html = templates.get_template("_cota_thread.html").render(
        thread=th, device=info, stages=cota.STAGES)
    counts_html = templates.get_template("_cota_counts.html").render(thread=th)
    if outcome["ok"]:
        n = outcome.get("records", 0)
        message = f"Checked {datetime.now():%H:%M:%S}"
        if renewed:
            message += " · the token had expired; signed in again"
        if outcome.get("gone"):
            message += f" · {outcome['gone']} no longer in the cloud"
    else:
        message = (f"The cloud answered HTTP {outcome['status']}" if outcome["status"]
                   else "The cloud could not be reached")
    return JSONResponse({"ok": outcome["ok"], "message": message, "html": html,
                         "counts_html": counts_html, "cloud": _cloud_chip(),
                         "label": info["label"], "latest_stage": th["latest_stage"],
                         "watch_until": th["watch_until"]})


@app.get("/cota/console/export")
def cota_console_export(device: str = "", type: str = "",
                        range_from: str = Query("", alias="from"), to: str = "",
                        format: str = "xlsx"):
    """The conversation in the range: one row per command, what was sent beside what came back.
    Small by construction (one device, at most 15 days), so it is built inline."""
    conn = get_conn()
    try:
        info = _console_device(conn, device, type)
        start, end, _ = cota.parse_range(range_from, to)
    except cota.CotaError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    th = cota.thread(conn, info["id"], start, end)
    rows = cota.console_export_rows(th, info["id"], info["imei"])
    stem = f"cota_{info['id']}_{start:%Y%m%d}-{end:%Y%m%d}"
    if format == "csv":
        return _download(exports.to_csv(rows, cota.CONSOLE_EXPORT_COLUMNS), "csv", stem)
    source = [("Device", info["label"]), ("Model (deviceType)", info["type"]),
              ("From", f"{start:{cota.RANGE_FORMAT}}"), ("To", f"{end:{cota.RANGE_FORMAT}}"),
              ("Commands", len(rows)),
              ("Answered by the device", th["counts"]["answered"]),
              ("Waiting for the device", th["counts"]["waiting"]),
              ("Exported", f"{datetime.now():{cota.RANGE_FORMAT}}"),
              ("Produced by", f"InTouch Utility v{BUILD['version']} ({BUILD['channel']})"),
              ("Note", "Device answers are as the cloud returned them; control bytes are shown "
                       "as \\xNN. They can contain credentials — handle the file accordingly.")]
    return _download(exports.to_xlsx(rows, cota.CONSOLE_EXPORT_COLUMNS, sheet_name="Commands",
                                     source=source), "xlsx", stem)


def _cota_signin_context(request: Request, **extra) -> dict:
    return _cota_context(get_conn(), request, "signin", connection=cota_connection.load(),
                         clouds=cota_connection.CLOUDS, is_local=_is_local(request),
                         portal_url=cota_connection.PORTAL_URLS.get(
                             cota_connection.load().cloud),
                         credential_store=sources.credential_store_name(),
                         token_env=cota.ENV_TOKEN, **extra)


@app.get("/cota/signin", response_class=HTMLResponse)
def cota_signin(request: Request):
    return templates.TemplateResponse(request, "cota_signin.html",
                                      _cota_signin_context(request))


@app.post("/cota/signin", response_class=HTMLResponse)
def cota_signin_submit(
    request: Request,
    cloud: str = Form(cota_connection.DEFAULT_CLOUD),
    base_url: str = Form(""),
    method: str = Form("token"),
    username: str = Form(""),
    password: str = Form(""),
    token: str = Form(""),
    remember: bool = Form(False),
    login_url: str = Form(""),
    login_encoding: str = Form("json"),
    password_hash: str = Form("none"),
    user_field: str = Form("username"),
    pass_field: str = Form("password"),
):
    previous = cota_connection.load()
    conn = cota_connection.apply_cloud(cota_connection.CotaConnection(
        cloud=cloud, base_url=base_url, method=method, username=username.strip(),
        login_url=login_url.strip(),
        login_encoding=login_encoding if login_encoding in ("json", "multipart", "form")
        else "json",
        password_hash=password_hash if password_hash in ("none", "md5") else "none",
        user_field=user_field.strip() or "username", pass_field=pass_field.strip() or "password",
        signed_in_at=previous.signed_in_at,
    ))
    try:
        if conn.method == "token":
            cota_connection.use_token(conn, token)
            result = {"level": "ok", "message": "Token saved. It lasts as long as the portal "
                                                "session it was copied from."}
        else:
            # An empty box with a saved password means "use the saved one".
            secret = password or (cota_connection.load_password(conn.username) or "")
            cota_connection.sign_in(conn, secret)
            if remember and password:
                if not cota_connection.save_password(conn.username, password):
                    raise cota_connection.CotaSignInError(
                        "Signed in, but the password could not be saved to the OS credential "
                        "store.")
            elif not remember:
                cota_connection.forget_password(conn.username)
            result = {"level": "ok", "message": f"Signed in as {conn.username}."}
    except cota_connection.CotaSignInError as exc:
        # Keep what was chosen, so a failed attempt does not reset the form.
        cota_connection.save(conn)
        result = {"level": "error", "message": str(exc)}
    return templates.TemplateResponse(request, "cota_signin.html",
                                      _cota_signin_context(request, result=result))


@app.post("/cota/signout", response_class=HTMLResponse)
def cota_signout(request: Request):
    cota_connection.sign_out(cota_connection.load())
    return templates.TemplateResponse(request, "cota_signin.html", _cota_signin_context(
        request, result={"level": "ok", "message": "Signed out — the token and any saved COTA "
                                                   "password were removed."}))


@app.get("/errors", response_class=HTMLResponse)
def error_log(request: Request):
    conn = get_conn()
    ctx = page_context(conn, request, None)
    ctx.update(entries=errors.recent(conn, limit=200), log_path=str(errors.LOG_PATH))
    return templates.TemplateResponse(request, "errors.html", ctx)


@app.post("/errors/clear", response_class=HTMLResponse)
def error_log_clear(request: Request):
    conn = get_conn()
    removed = errors.clear(conn)
    ctx = page_context(conn, request, None)
    ctx.update(entries=[], log_path=str(errors.LOG_PATH),
               notice=f"Cleared {removed} recorded error(s). The text log is untouched.")
    return templates.TemplateResponse(request, "errors.html", ctx)


@app.get("/quality", response_class=HTMLResponse)
def quality(request: Request, snapshot: int | None = None):
    conn = get_conn()
    ctx = page_context(conn, request, snapshot)
    ctx.update(issues=metrics.quality_issues(conn, ctx["snapshot_id"]))
    return templates.TemplateResponse(request, "quality.html", ctx)


@app.get("/devices", response_class=HTMLResponse)
def devices(
    request: Request,
    snapshot: int | None = None,
    model: str | None = None,
    firmware: list[str] = Query(default=[]),
    status: str | None = None,
    queue_state: str | None = None,
    group: str | None = None,
    changed: str | None = None,
    fallback: str | None = None,
    q: str = "",
    # Literals, not the constants below: default arguments are evaluated when the function is
    # defined, and those are declared further down the module.
    sort: str = "seen",
    dir: str = "desc",
    page: int = 1,
    page_size: int = 100,
):
    conn = get_conn()
    ctx = page_context(conn, request, snapshot)
    rows, total = _device_rows(
        conn, ctx["snapshot_id"], model=model, firmware=firmware, status=status,
        queue_state=queue_state, group=group, changed=changed, fallback=fallback, search=q,
        sort=sort, dir=dir, limit=page_size, offset=(page - 1) * page_size)

    selected_firmware = [v for v in (firmware or []) if v]
    active_filters = {"model": model, "firmware": selected_firmware, "status": status,
                      "queue_state": queue_state, "group": group, "changed": changed,
                      "fallback": fallback, "q": q}
    # urlencode with doseq, because a multi-select repeats its key: firmware=a&firmware=b. Hand
    # rolling that produced firmware=['a',%20'b'] and silently matched nothing.
    pairs = [(k, v) for k, v in active_filters.items() if v]
    if ctx["snapshot_id"]:
        pairs.insert(0, ("snapshot", ctx["snapshot_id"]))
    query = urlencode(pairs, doseq=True)

    ctx.update(
        rows=rows, total=total, page=page, page_size=page_size,
        pages=max(1, -(-total // page_size)),
        filters=active_filters,
        change_windows=CHANGE_WINDOWS,
        sort=sort if sort in SORTABLE else DEFAULT_SORT,
        dir="asc" if dir == "asc" else "desc",
        base_query=query,
        models=metrics.task_state_by(conn, ctx["snapshot_id"], "model"),
        # Firmware values present in this snapshot, so the filter is a pick-list rather than
        # something to type exactly right.
        firmwares=[dict(r) for r in conn.execute(metrics.at(conn, ctx["snapshot_id"], """
            SELECT firmware AS label, COUNT(*) AS devices FROM device_state
            WHERE snapshot_id = ? AND firmware IS NOT NULL
            GROUP BY firmware ORDER BY devices DESC
        """), (ctx["snapshot_id"],))],
    )
    return templates.TemplateResponse(request, "devices.html", ctx)


# Sortable columns, whitelisted so a query param can never reach the ORDER BY as raw SQL.
SORTABLE = {
    "imei": "d.imei",
    "model": "d.device_model",
    "firmware": "d.firmware",
    "changed": "r.last_fw_change_at",
    "target": "d.update_firmware",
    "config": "d.configuration",
    "hw": "d.hw_ver",
    "status": "d.status",
    "task": "d.queue_state",
    "seen": "d.seen_at",
    "checked": "r.last_checked_at",
}
# Most recently seen first: the devices that just reported are the ones worth looking at.
DEFAULT_SORT, DEFAULT_DIR = "seen", "desc"


def _order_by(sort: str, direction: str) -> str:
    column = SORTABLE.get(sort, SORTABLE[DEFAULT_SORT])
    descending = direction != "asc"
    # Missing values sort last either way — a device that has never reported should not head
    # the list just because its timestamp is NULL.
    return f"{column} IS NULL, {column} {'DESC' if descending else 'ASC'}, d.imei"


# Rows per page. Offered rather than fixed because the useful number depends on the job:
# 25 to read carefully, 100 to scan for a pattern.
PAGE_SIZES = (25, 50, 100)
DEFAULT_PAGE_SIZE = 50


# Filter presets for "last update". Values are SQL fragments over the registry row.
CHANGE_WINDOWS = [
    ("1h", "changed in the last hour"),
    ("24h", "changed in the last 24 hours"),
    ("7d", "changed in the last 7 days"),
    ("30d", "changed in the last 30 days"),
    ("never", "never changed"),
]


def _device_rows(conn, snapshot_id, *, model=None, firmware=None, status=None,
                 queue_state=None, group=None, changed=None, fallback=None, search="",
                 sort="seen", dir="desc", limit=None, offset=0):
    """The device list behind both the page and its export.

    Shared deliberately: an export that returned a different set from the table above it would
    send someone to act on the wrong devices.
    """
    where, params = _device_filters(snapshot_id, model, firmware, status, queue_state,
                                    group, search)
    join = ("JOIN device_group g ON g.snapshot_id = d.snapshot_id AND g.imei = d.imei"
            if group else "")
    join += " LEFT JOIN device r ON r.imei = d.imei"
    if changed:
        where += " AND " + _changed_clause(changed)

    # The fallback tag, from the single shared definition: task completed, sitting on base.
    rule = metrics.fallback_rule("d")
    tag_sql = f"CASE WHEN {rule} THEN 1 ELSE 0 END AS is_fallback"

    if fallback == "yes":
        where += f" AND ({rule})"
    elif fallback == "missed":
        where += f" AND ({rule}) AND {metrics.missed_target_rule('d')}"

    # Both statements go through metrics.at, like every other per-snapshot read. Left pointing
    # at the view they cost a full fleet resolution each, and this function runs two of them per
    # page load — which is why /devices took 41s on the 245-snapshot database while pages that
    # used the resolved copy took under a second.
    total = conn.execute(metrics.at(
        conn, snapshot_id,
        f"SELECT COUNT(*) FROM device_state d {join} WHERE {where}"), params).fetchone()[0]

    sql = metrics.at(conn, snapshot_id, f"""
        SELECT d.imei, d.device_model, d.firmware, d.hw_ver, d.status, d.queue_state, d.queue,
               d.seen_at, d.seen_age_hours, d.configuration, d.groups_raw, d.vin, d.iccid,
               d.update_firmware, d.base_firmware,
               {tag_sql},
               r.prev_firmware, r.last_changed_at, r.last_fw_change_at, r.last_checked_at,
               r.changes AS change_count
        FROM device_state d {join} WHERE {where}
        ORDER BY {_order_by(sort, dir)}
    """)
    if limit is not None:
        sql += " LIMIT ? OFFSET ?"
        params = [*params, limit, offset]
    return [dict(r) for r in conn.execute(sql, params)], total


def _changed_clause(window: str) -> str:
    if window == "never":
        return "r.last_changed_at IS NULL"
    hours = {"1h": 1, "24h": 24, "7d": 168, "30d": 720}.get(window)
    if not hours:
        return "1 = 1"
    return (f"r.last_changed_at >= datetime('now', 'localtime', '-{hours} hours')")


def _device_filters(snapshot_id, model, firmware, status, queue_state, group, search=""):
    """WHERE fragment and parameters for the device list.

    `firmware` is a list: several versions are usually interesting together — the ones a rollout
    is moving between — and picking them one at a time meant reloading the page per version.
    """
    clauses = ["d.snapshot_id = ?"]
    params: list = [snapshot_id]

    for column, value in (("d.device_model", model), ("d.status", status),
                          ("d.queue_state", queue_state), ("g.group_name", group)):
        if value:
            clauses.append(f"{column} = ?")
            params.append(value)

    versions = [v for v in (firmware or []) if v]
    if versions:
        clauses.append(f"d.firmware IN ({','.join('?' * len(versions))})")
        params.extend(versions)

    # Digits only: an IMEI is a number, and the platform is pasted from lists that carry commas,
    # quotes and stray spaces. Stripping them means a paste works without being tidied first.
    digits = "".join(c for c in str(search or "") if c.isdigit())
    if digits:
        # Substring rather than prefix: the last few digits are what people read off a label,
        # and LIKE cannot use the index either way on a middle match.
        clauses.append("d.imei LIKE ?")
        params.append(f"%{digits}%")

    return " AND ".join(clauses), params


# ─── updating the data ──────────────────────────────────────────────────────

def _is_local(request: Request) -> bool:
    """Whether this request came from the machine itself.

    Matters because the update form takes a platform password: over a plain-HTTP connection
    from another machine that password crosses the network in the clear.
    """
    host = (request.client.host if request.client else "") or ""
    return host in {"127.0.0.1", "::1", "localhost"}


def _update_context(request: Request, **extra) -> dict:
    conn = get_conn()
    ctx = page_context(conn, request, None)
    ctx.update(
        connection=sources.load_connection(),
        presets=sources.PRESETS,
        credential_store=sources.credential_store_name(),
        export_dir=str(config.EXPORT_DIR),
        is_local=_is_local(request),
        startup=startup.status(),
        startup_available=startup.AUTO_START_AVAILABLE,
        # Rendered on load as well as polled, so a job already running when the page is opened
        # is visible immediately rather than only after the first poll — and so it survives the
        # tab being closed and reopened, which a two-minute merge invites.
        job=progress.snapshot(),
        **extra,
    )
    return ctx


def _ingest_path(path, job=None) -> dict:
    """Ingest a newly-acquired file and rebuild everything that depends on it.

    A duplicate is discarded rather than kept: uploading the same export twice would otherwise
    leave a second 22 MB copy in the folder forever, and it carries no information.
    """
    conn = get_conn()
    result = ingest.ingest_file(conn, path, job=job)
    if result.status == "already_ingested":
        try:
            path.unlink()
        except OSError:
            pass
        return {"level": "warn",
                "message": f"That export is identical to one already loaded "
                           f"(snapshot {result.snapshot_id}). Nothing changed, and the "
                           f"duplicate copy was discarded."}

    if job:
        job.begin("Rebuilding metrics")
    rollup.rollup_snapshot(conn, result.snapshot_id)
    transitions = None
    detail = (f"{result.rows:,} devices loaded as snapshot {result.snapshot_id}, "
              f"dated {result.snapshot_at}.")
    if transitions:
        detail += f" {transitions:,} device changes computed against the previous snapshot."
    else:
        detail += " This is the first snapshot, so there is nothing to compare against yet."
    if result.ts_source != "filename":
        detail += (" Warning: the snapshot time came from the file date, not the filename, "
                   "so trend spacing may be inaccurate.")
    return {"level": "ok", "message": detail}


def _ingest_records(records: list[dict], source_url: str) -> dict:
    """Load device records pulled straight from the API — no spreadsheet involved."""
    conn = get_conn()
    result = ingest.ingest_records(conn, records, source_name=f"API {source_url}")
    if result.status == "already_ingested":
        return {"level": "warn",
                "message": f"The API returned data identical to snapshot "
                           f"{result.snapshot_id}. Nothing has changed on the platform since "
                           f"that pull, so no new snapshot was created."}

    rollup.rollup_snapshot(conn, result.snapshot_id)
    transitions = None
    message = (f"Pulled {result.rows:,} devices from the API as snapshot "
               f"{result.snapshot_id}.")
    if transitions:
        message += f" {transitions:,} device changes computed against the previous snapshot."
    if result.unknown_columns:
        message += (" Unmapped fields ignored: "
                    + ", ".join(result.unknown_columns[:8]) + ".")
    return {"level": "ok", "message": message}


@app.get("/update", response_class=HTMLResponse)
def update_page(request: Request):
    return templates.TemplateResponse(request, "update.html", _update_context(request))


def run_job(job: progress.Job, work) -> None:
    """Run `work(job)` on a background thread, so the POST can answer straight away.

    Holding the request open for the length of the job is what made a working merge look like a
    hang: the browser sat on a request that would not return for two minutes, and there was no
    way to ask how it was going, because the answer would have arrived on the same response.

    A plain thread rather than the threadpool, because nothing awaits this — the page polls
    /api/progress instead, and the job outlives the request that started it.
    """
    def body() -> None:
        try:
            work(job)
        except Exception as exc:                      # noqa: BLE001 — recorded, not swallowed
            errors.record("web", exc, path=f"job:{job.kind}")
            job.fail(exc)

    threading.Thread(target=body, name=f"job-{job.kind}", daemon=True).start()


def _store_and_ingest(filename: str, content: bytes, job=None) -> dict:
    """Save an upload and load it. Blocking, and deliberately off the event loop.

    Opens its own connection by way of `_ingest_path`: sqlite3 objects are bound to the thread
    that created them, so a connection made in the request handler cannot be used here.
    """
    if job:
        job.begin("Saving the file")
    path = sources.store_upload(filename, content)
    return _ingest_path(path, job=job)


@app.post("/update/import", response_class=HTMLResponse)
async def update_import(request: Request, file: UploadFile = File(...)):
    """Load an uploaded export.

    The work is handed to a worker thread rather than run here. This handler has to be `async`
    to read the upload, and an `async` handler runs *on the event loop* — so calling a job that
    takes tens of seconds directly would stop the server answering anything at all, including
    the page the browser is waiting on and /healthz. It looked like the import had hung when in
    fact the whole dashboard had. Every other route is a plain `def`, which Starlette already
    runs in a threadpool; these two upload routes were the only ones that could freeze it.
    """
    content = await file.read()
    name = file.filename or ""
    try:
        job = progress.start("upload", f"Loading {name or 'export'}", ingest.INGEST_STEPS)
    except progress.Busy as exc:
        return templates.TemplateResponse(request, "update.html", _update_context(
            request, tab="import", result={"level": "warn", "message": str(exc)}))

    def work(job: progress.Job) -> None:
        try:
            outcome = _store_and_ingest(name, content, job=job)
        except (sources.SourceError, ingest.IngestError) as exc:
            job.fail(exc)
            return
        job.finish(outcome["message"])

    run_job(job, work)
    return RedirectResponse("/update?tab=import", status_code=303)


@app.post("/update/online", response_class=HTMLResponse)
def update_online(
    request: Request,
    preset: str = Form(sources.DEFAULT_PRESET),
    url: str = Form(""),
    username: str = Form(""),
    password: str = Form(""),
    auth_mode: str = Form("token"),
    login_url: str = Form(""),
    user_field: str = Form("username"),
    pass_field: str = Form("password"),
    login_encoding: str = Form("multipart"),
    password_hash: str = Form("md5"),
    verify_tls: bool = Form(False),
    remember: bool = Form(False),
):
    connection = sources.Connection(
        preset=preset if preset in sources.PRESETS else sources.DEFAULT_PRESET,
        url=url.strip(), username=username.strip(), auth_mode=auth_mode,
        login_url=login_url.strip(), verify_tls=verify_tls,
        user_field=user_field.strip() or "username",
        pass_field=pass_field.strip() or "password",
        login_encoding=login_encoding, password_hash=password_hash,
    )
    # A known platform supplies its own endpoints, so nothing typed here can misconfigure it.
    connection = sources.apply_preset(connection)

    # An empty password field with a stored credential means "use the saved one" — so the
    # password does not have to be retyped on every refresh.
    secret = password or (sources.load_password(connection.username) or "")

    try:
        if not secret and auth_mode != "none":
            raise sources.SourceError("No password given, and none is saved for this user.")

        sources.save_connection(connection)
        if remember and secret:
            if not sources.save_password(connection.username, secret):
                raise sources.SourceError(
                    "Could not save the password to the OS credential store. The download was "
                    "not attempted — re-run without 'remember me' to continue without saving.")
        elif not remember:
            sources.forget_password(connection.username)

        fetched = sources.fetch_export(connection, secret)
        if fetched.records is not None:
            result = _ingest_records(fetched.records, connection.url)
        else:
            result = _ingest_path(fetched.path)
            result["message"] = f"Downloaded {fetched.path.name}. " + result["message"]
    except (sources.SourceError, ingest.IngestError) as exc:
        result = {"level": "error", "message": str(exc)}

    return templates.TemplateResponse(request, "update.html",
                                      _update_context(request, result=result, tab="online"))


@app.post("/update/schedule", response_class=HTMLResponse)
def update_schedule(request: Request,
                    enabled: bool = Form(False),
                    interval_value: int = Form(1),
                    interval_unit: str = Form("hours")):
    multiplier = {"minutes": 60, "hours": 3600}.get(interval_unit, 3600)
    seconds = scheduler.clamp_interval(interval_value * multiplier)
    agent = scheduler.get_scheduler()
    state = agent.configure(enabled=enabled, interval_seconds=seconds)

    if enabled:
        message = (f"Auto-fetch on — every {scheduler.describe_interval(state.interval_seconds)}. "
                   f"Next run at {state.next_run}.")
        level = "ok"
        if not _update_context(request)["auth"]["can_automate"]:
            message += (" Note: the saved credentials cannot be renewed automatically, so this "
                        "will stop working once the token expires.")
            level = "warn"
    else:
        message, level = "Auto-fetch off.", "ok"

    return templates.TemplateResponse(request, "update.html", _update_context(
        request, tab="agent", result={"level": level, "message": message}))


@app.post("/update/startup", response_class=HTMLResponse)
def update_startup(request: Request, enabled: bool = Form(False),
                   startup_delay: int = Form(startup.DEFAULT_DELAY_MINUTES)):
    """Turn 'start with Windows' on or off.

    Still reachable while the feature is withdrawn, because the route outlives the button: a
    bookmark or an old page left open in a tab would otherwise re-arm the very thing being
    removed. Turning it *off* is always allowed.
    """
    if enabled and not startup.AUTO_START_AVAILABLE:
        return templates.TemplateResponse(request, "update.html", _update_context(
            request, tab="agent", result={
                "level": "warn",
                "message": "Start with Windows has been removed. It ran a copy of the "
                           "dashboard with no window, which held the port and could not be "
                           "seen or stopped. Auto-fetch still runs on its schedule whenever "
                           "the app is open."}))

    state = startup.enable(startup_delay) if enabled else startup.disable()

    if not state.supported:
        result = {"level": "warn", "message": "Starting with the system is only available "
                                              "on Windows."}
    elif state.enabled:
        when = "after the machine boots" if state.starts_at_boot else "after you sign in"
        how = ("a scheduled task — it runs even if nobody signs in"
               if state.starts_at_boot else
               "a Startup-folder entry; this machine refused to register a scheduled task, so "
               "it needs someone signed in")
        message = (f"The dashboard will start with Windows, {state.delay_minutes} minutes "
                   f"{when}, and resume fetching on its schedule. Using {how}.")
        result = {"level": "warn" if state.warning else "ok",
                  "message": message + (f" {state.warning}" if state.warning else "")}
    else:
        result = {"level": "ok", "message": "The dashboard will no longer start with Windows."}

    return templates.TemplateResponse(request, "update.html",
                                      _update_context(request, tab="agent", result=result))


@app.post("/update/startup-test", response_class=HTMLResponse)
def update_startup_test(request: Request):
    """Launch exactly what auto-start launches, without waiting for a reboot.

    The whole point of this feature is that it works when nobody is watching, so being able to
    prove it before trusting it matters more than usual.
    """
    started = startup.run_now()
    port = request.url.port or config.DEFAULT_PORT
    result = ({"level": "ok",
               "message": f"Launched the same command the startup entry uses. If it is working "
                          f"you now have a second copy running — check http://127.0.0.1:{port} "
                          f"in a moment, then close the extra one."}
              if started else
              {"level": "error", "message": "Could not launch it. See the error log."})
    return templates.TemplateResponse(request, "update.html",
                                      _update_context(request, tab="agent", result=result))


@app.post("/update/run-now", response_class=HTMLResponse)
def update_run_now(request: Request):
    try:
        job = progress.start("fetch", "Fetching from the platform", scheduler.FETCH_STEPS)
    except progress.Busy as exc:
        return templates.TemplateResponse(request, "update.html", _update_context(
            request, tab="agent", result={"level": "warn", "message": str(exc)}))

    def work(job: progress.Job) -> None:
        state = scheduler.get_scheduler().run_now(job=job)
        job.finish(state.last_message)

    run_job(job, work)
    return RedirectResponse("/update?tab=agent", status_code=303)


@app.get("/api/agent")
def api_agent():
    """Status for the header bar; polled so the countdown stays honest."""
    agent = scheduler.get_scheduler()
    return JSONResponse({**agent.state.to_dict(),
                         "interval_label": scheduler.describe_interval(
                             agent.state.interval_seconds),
                         "auth": scheduler.auth_status()})


# ─── sharing the database ───────────────────────────────────────────────────

# The bundle most recently built by a job, waiting to be collected: (path, filename).
#
# One slot, like one job. A bundle is a point-in-time copy, so keeping a history of them would
# accumulate stale files that look current — building a new one replaces the old.
_built_bundle: tuple[Path, str] | None = None
_bundle_lock = threading.Lock()


def _build_bundle(since: str | None, job=None) -> tuple[Path, str]:
    """Write a bundle to a scratch file and return it with the name to serve it under.

    Opens its own connection: this runs on a worker thread, and sqlite3 objects belong to the
    thread that created them.
    """
    conn = db.connect()
    scratch = Path(tempfile.gettempdir()) / f"ota-bundle-{uuid.uuid4().hex}.zip"
    try:
        bundle.export_bundle(conn, scratch, since=since or None, job=job)
    except Exception:
        scratch.unlink(missing_ok=True)
        raise
    return scratch, bundle.suggested_filename(conn)


@app.post("/update/bundle")
def update_bundle_build(request: Request, since: str = Form("")):
    """Build the bundle as a job, and offer it for download when it is ready.

    A full history is ~960,000 rows of JSON and the better part of a minute. Held open as a
    plain download that is a request which does not come back — the browser shows nothing at
    all until the last byte, so there is no download to watch and no bar to read. It was
    reported, accurately, as "export is not working".

    So it follows the same rule as every other long job here: the POST starts the work and
    returns at once, the page polls `/api/progress`, and the finished job carries the link to
    collect the file. `GET /update/bundle` still builds one inline for scripts and small
    histories — same builder, so the two cannot drift.
    """
    global _built_bundle

    try:
        job = progress.start("bundle", "Building the bundle", bundle.EXPORT_STEPS)
    except progress.Busy as exc:
        return templates.TemplateResponse(request, "update.html", _update_context(
            request, tab="share", result={"level": "warn", "message": str(exc)}))

    def work(job: progress.Job) -> None:
        global _built_bundle
        path, filename = _build_bundle(since, job=job)
        with _bundle_lock:
            previous, _built_bundle = _built_bundle, (path, filename)
        if previous:
            previous[0].unlink(missing_ok=True)      # only the newest is worth keeping
        job.download = "/update/bundle/download"
        job.finish(f"{filename} is ready — {path.stat().st_size / 1e6:.1f} MB.")

    run_job(job, work)
    return RedirectResponse("/update?tab=share", status_code=303)


@app.get("/update/bundle/download")
def update_bundle_collect():
    """Hand over the bundle the last job built. Kept until the next one replaces it."""
    with _bundle_lock:
        ready = _built_bundle
    if not ready or not ready[0].exists():
        return RedirectResponse("/update?tab=share", status_code=303)
    return FileResponse(ready[0], media_type="application/zip", filename=ready[1])


@app.get("/update/bundle")
def update_bundle_export(since: str | None = None):
    """Build a bundle and return it in the response. For scripts, and for short histories.

    Written to a scratch file and streamed from there rather than assembled in memory: the
    bundle used to be built whole in a `BytesIO`, so 33 MB compressed — and every row of it as
    Python objects on the way in — sat in the process before a single byte reached the browser.

    `BackgroundTask` deletes the file after the last byte is sent, which is why the path is
    handed over rather than opened in a context manager here.
    """
    scratch, filename = _build_bundle(since)
    return FileResponse(
        scratch, media_type="application/zip", filename=filename,
        background=BackgroundTask(scratch.unlink, missing_ok=True))


def _merge_bundle(content: bytes, allow_interleave: bool, job=None) -> bundle.ImportResult:
    """Merge a bundle. Blocking for minutes on a large history, so it runs off the event loop.

    The connection is opened here, in the worker thread that uses it: sqlite3 objects cannot be
    shared across threads.
    """
    return bundle.import_bundle(db.connect(), content, allow_interleave=allow_interleave,
                                job=job)


@app.post("/update/bundle-import", response_class=HTMLResponse)
async def update_bundle_import(request: Request, file: UploadFile = File(...),
                               allow_interleave: bool = Form(False)):
    """Merge an uploaded bundle. See `update_import` for why the work leaves the event loop."""
    content = await file.read()
    try:
        job = progress.start("import", "Merging bundle", bundle.MERGE_STEPS)
    except progress.Busy as exc:
        return templates.TemplateResponse(request, "update.html", _update_context(
            request, tab="share", result={"level": "warn", "message": str(exc)}))

    def work(job: progress.Job) -> None:
        try:
            outcome = _merge_bundle(content, allow_interleave, job=job)
        except bundle.BundleError as exc:
            job.fail(exc)
            return
        job.finish(outcome.message)
        # "Already loaded" and "refused" are answers, not failures — carried so the page can
        # colour the result without re-deriving it.
        job.kind = "import"
        job.error = "" if outcome.status in ("imported", "already_present") else "advice"

    run_job(job, work)
    return RedirectResponse("/update?tab=share", status_code=303)


@app.post("/update/label", response_class=HTMLResponse)
def update_label(request: Request, label: str = Form("")):
    """Name this install, so a shared report says whose numbers it is."""
    conn = get_conn()
    name = identity.set_instance_label(conn, label)
    return templates.TemplateResponse(request, "update.html", _update_context(
        request, tab="share",
        result={"level": "ok", "message": f"This install is now called {name!r}. It appears on "
                                          f"every bundle and report it produces."}))


@app.get("/api/progress")
def api_progress():
    """What the running job is doing. Always answers, so the poller needs no special cases."""
    return JSONResponse(progress.snapshot())


@app.post("/update/progress/dismiss", response_class=HTMLResponse)
def update_progress_dismiss(request: Request):
    """Acknowledge a finished job so the panel stops showing it."""
    job = progress.current()
    if job is not None and job.status != "running":
        progress.clear()
    return RedirectResponse("/update", status_code=303)


@app.get("/api/identity")
def api_identity():
    """What this database is and exactly what it holds — the reconciliation endpoint."""
    return JSONResponse(identity.manifest(get_conn()))


@app.post("/update/forget", response_class=HTMLResponse)
def update_forget(request: Request, username: str = Form("")):
    sources.forget_password(username)
    return templates.TemplateResponse(request, "update.html", _update_context(
        request, tab="online",
        result={"level": "ok", "message": f"Saved password for {username!r} deleted from the "
                                          "OS credential store."}))


# ─── JSON API ───────────────────────────────────────────────────────────────

@app.get("/api/version")
def api_version():
    """Identify exactly what is running — for bug reports and deployment checks."""
    from . import VERSION_HISTORY

    conn = get_conn()
    latest = metrics.snapshots(conn)
    return JSONResponse({
        **BUILD,
        "snapshots": len(latest),
        "latest_snapshot": latest[0]["snapshot_at"] if latest else None,
        "history": [{"version": v, "released": d, "summary": s} for v, d, s in VERSION_HISTORY],
    })


@app.get("/api/kpis")
def api_kpis(snapshot: int | None = None):
    conn = get_conn()
    snapshot_id = snapshot or metrics.latest_snapshot_id(conn)
    return JSONResponse(metrics.kpis(conn, snapshot_id))


@app.get("/api/pending")
def api_pending(snapshot: int | None = None):
    conn = get_conn()
    snapshot_id = snapshot or metrics.latest_snapshot_id(conn)
    return JSONResponse({"buckets": metrics.pending_by_reason(conn, snapshot_id),
                         "pending_online": metrics.pending_online_devices(conn, snapshot_id)})


@app.get("/api/firmware-mix")
def api_firmware_mix(snapshot: int | None = None, model: str | None = None):
    conn = get_conn()
    snapshot_id = snapshot or metrics.latest_snapshot_id(conn)
    return JSONResponse(metrics.firmware_mix(conn, snapshot_id, model))


@app.get("/api/reachability")
def api_reachability(snapshot: int | None = None, min_devices: int = 20):
    conn = get_conn()
    snapshot_id = snapshot or metrics.latest_snapshot_id(conn)
    return JSONResponse(metrics.reachability_by_firmware(conn, snapshot_id, min_devices))


@app.get("/api/quality")
def api_quality(snapshot: int | None = None):
    conn = get_conn()
    snapshot_id = snapshot or metrics.latest_snapshot_id(conn)
    return JSONResponse(metrics.quality_issues(conn, snapshot_id))


@app.get("/api/changes")
def api_changes(window: str = "today"):
    """Movement over a period, from the change log — replaces the old transition endpoint."""
    conn = get_conn()
    since, until = registry.window_range(window)
    return JSONResponse({
        "window": window,
        "summary": registry.movement_summary(conn, since, until),
        "moves": registry.firmware_moves(conn, since, until, limit=200),
        "fallbacks": registry.fallbacks(conn, limit=100),
    })


