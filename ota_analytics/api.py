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

from urllib.parse import parse_qs, parse_qsl, quote, urlencode, urlsplit

from fastapi import FastAPI, File, Form, Query, Request, Response, UploadFile
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse, StreamingResponse,
                               RedirectResponse)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool

from . import (auth, bundle, config, cota, cota_campaign, cota_connection, cota_fleet,
               cota_insight, cota_library, cota_run, db, errors, exports, fota_devices,
               identity, ingest, metrics, nav, normalize, progress, registry, rollup, scheduler, sources,
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
    # The day's COTA record lives for the day: earlier days are cleared once per process, here,
    # before any page reads anything. In _cota_context it ran after a page had already computed
    # its figures — the first visit of a new day showed yesterday's jobs as today's.
    cota_campaign.purge_previous_days(conn)
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


def _when(value) -> str:
    """A stored time as the pages write it: DD-MM HH:MM — day first, the platform's order."""
    text = str(value or "")
    if len(text) < 16:
        return text or "—"
    return f"{text[8:10]}-{text[5:7]} {text[11:16]}"


templates.env.filters["when"] = _when
# The sidebar and the top bar title read one definition (nav.py).
templates.env.globals["nav_modules"] = nav.MODULES
templates.env.globals["nav_state"] = nav.state
templates.env.globals["nav_icons"] = nav.ICONS


def static_version(name: str = "app.css") -> str:
    """The stylesheet's version in its URL: the app version plus the file's own time. By app
    version alone it stayed `?v=2.0.1` through a day of style changes, the browser kept the copy
    it had, and new parts of a page came out unstyled — plain text where bars should be."""
    try:
        return f"{__version__}.{int((WEB / 'static' / name).stat().st_mtime)}"
    except OSError:
        return __version__


templates.env.globals["static_version"] = static_version


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
    all_models = metrics.task_state_by(conn, sid, "model")
    ctx.update(
        change=change,
        window=window,
        windows=registry.WINDOWS,
        selected_models=selected,
        all_models=all_models,
        model_choices=_model_choices(all_models, selected),
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


def _model_choices(models: list[dict], selected: list[str]) -> dict:
    """The models as the shared list filter takes them: (value, label, devices)."""
    return {"entries": [(r["label"], r["label"], r["devices"]) for r in models],
            "chosen": selected}


@app.get("/pending", response_class=HTMLResponse)
def pending(request: Request, snapshot: int | None = None):
    conn = get_conn()
    ctx = page_context(conn, request, snapshot)
    ctx.update(
        kpis=metrics.kpis(conn, ctx["snapshot_id"]),
        buckets=metrics.pending_by_reason(conn, ctx["snapshot_id"]),
        # Every one of them: the list scrolls in its own frame, and these are the devices to chase.
        pending_online=metrics.pending_online_devices(conn, ctx["snapshot_id"], limit=5000),
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
    models = metrics.task_state_by(conn, ctx["snapshot_id"], "model")
    ctx.update(
        selected_models=selected,
        models=models,
        model_choices=_model_choices(models, selected),
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
    # Both ends of the window, for every figure on the page. "Yesterday" is the one with an end,
    # and with the start alone its tiles and lists also counted today.
    since, until = registry.window_range(window)

    # The total comes free: movement_summary already counts the moves in this window with the
    # same filter the list uses, so paging needs no second COUNT.
    summary = registry.movement_summary(conn, since, until)
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
        moves=registry.firmware_moves(conn, since, until, limit=size, offset=(page - 1) * size),
        at_base=registry.at_base_firmware(conn),
        segments=registry.fallback_segments(conn),
        registry_summary=registry.summary(conn),
    )

    # Fallbacks page independently of the moves list above — sharing one page number would move
    # both at once — but they follow the same window: with "Today" picked, the tile said 6 and
    # the list showed 271, every fallback ever recorded (09-10-2026). The repeat table beside it
    # stays all-time: "has done it more than once" is a question about history.
    fb_totals = registry.fallback_totals(conn)
    fb_size = fb_size if fb_size in PAGE_SIZES else 25
    fb_sort = fb_sort if fb_sort in registry.FALLBACK_SORTS else registry.DEFAULT_FALLBACK_SORT
    fb_min = 2 if fb_min and int(fb_min) > 1 else 1

    # The filtered total, so the pager counts what is actually on screen rather than everything.
    fb_shown = registry.fallback_count(conn, min_times=fb_min, model=fb_model, since=since, until=until)
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
        fb_models=registry.fallback_models(conn, since, until),
        fb_query="window=" + window + (f"&fb_sort={fb_sort}" if fb_sort != "when" else "")
                 + (f"&fb_min={fb_min}" if fb_min > 1 else "")
                 + (f"&fb_model={quote(fb_model)}" if fb_model else ""),
        fallbacks=registry.fallbacks(conn, limit=fb_size, offset=(fb_page - 1) * fb_size,
                                     sort=fb_sort, min_times=fb_min, model=fb_model,
                                     since=since, until=until),
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
def devices_export(request: Request, snapshot: int | None = None, sort: str = "seen",
                   dir: str = "desc", format: str = "csv"):
    """The filtered device list, as a file. Same rows as the page, without pagination."""
    conn = get_conn()
    snapshot_id = snapshot or metrics.latest_snapshot_id(conn)
    filters = fota_devices.clean_filters(request.query_params)
    rows, _ = fota_devices.rows(conn, snapshot_id, filters, sort=sort,
                                direction="asc" if dir == "asc" else "desc")

    # Spell the tag out in the file: a reader opening this in Excel should not have to know
    # what a 1 in a column called is_fallback means.
    for row in rows:
        row["fallback_tag"] = ""
        if row.get("is_fallback"):
            row["fallback_tag"] = ("FALLBACK — missed target"
                                   if row.get("update_firmware") not in (None, row.get("firmware"))
                                   else "FALLBACK — target is base")

    slug = fota_devices.describe(filters)
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
    # Jobs left running by a restart come back paused. (Earlier days are cleared in get_conn.)
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
                         live=cota_campaign.active(conn), today=cota_campaign.today(conn),
                         hours=cota_insight.by_hour(conn), **extra)


JOB_FORM_FIELDS = ("name", "group_id", "devices", "commands", "batch_size", "rate_per_sec",
                   "time_limit_minutes", "answer_wait_seconds", "in_progress", "device_type",
                   "cmd_type")


def _new_job_context(conn, request: Request, **extra) -> dict:
    """The new-job page: the form, the plan once previewed, and the library to pick from."""
    return _cota_context(conn, request, "jobs", groups=cota_campaign.groups(conn),
                         live=cota_campaign.active(conn), library=cota_library.commands(conn),
                         defaults={"batch": cota_campaign.DEFAULT_BATCH,
                                   "batch_max": cota_campaign.MAX_PER_CALL,
                                   "in_progress": cota_campaign.DEFAULT_IN_PROGRESS,
                                   "rate": cota_campaign.DEFAULT_RATE,
                                   "time_limit": cota_campaign.DEFAULT_TIME_LIMIT_MINUTES,
                                   "answer_wait": cota_campaign.DEFAULT_ANSWER_WAIT_SECONDS,
                                   "wait_choices": ",".join(map(str, cota_run.WAIT_CHOICES)),
                                   "type": cota.DEFAULT_DEVICE_TYPE, "cmd": cota.DEFAULT_CMD_TYPE},
                         **extra)


@app.get("/cota", response_class=HTMLResponse)
def cota_jobs(request: Request):
    return templates.TemplateResponse(request, "cota_jobs.html", _jobs_context(get_conn(), request))


@app.get("/cota/overview", response_class=HTMLResponse)
def cota_overview(request: Request):
    """Today's jobs at a glance: the record's own figures, and Web FOTA's view of the devices
    that never answered. Reads only — no cloud call."""
    conn = get_conn()
    return templates.TemplateResponse(request, "cota_overview.html",
                                      _cota_context(conn, request, "overview",
                                                    o=cota_insight.overview(conn)))


@app.get("/cota/jobs/new", response_class=HTMLResponse)
def cota_job_new(request: Request, source: int = Query(0, alias="from"), which: str = "all",
                 commands: str = ""):
    """A new job — blank, filled from an earlier one (Duplicate, Rerun), or started from a saved
    command (Commands → New job). Declared before /cota/jobs/{job_id}, which would otherwise
    claim "new" and refuse it as not a number."""
    conn = get_conn()
    draft = cota_campaign.draft_from(conn, source, which if which in ("unfinished", "rest") else "all") \
        if source else ({"commands": commands[:5000]} if commands.strip() else None)
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
        waits = form.getlist("waits")
        the_plan = cota_campaign.plan(
            conn, ids, draft["commands"], batch_size=batch, rate_per_sec=rate,
            answer_wait_seconds=_number(draft["answer_wait_seconds"], float, None),
            time_limit_minutes=_number(draft["time_limit_minutes"], float, None),
            in_progress=_number(draft["in_progress"], int, None), waits=waits or None)
        draft["waits"] = ",".join(str(w) for w in the_plan["waits"])
        # What was planned is what starts: a batch over 200 comes back as 200, in the form too.
        draft["batch_size"] = str(the_plan["batch"])
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
                    answer_wait_seconds: str = Form(""), in_progress: str = Form(""),
                    device_type: str = Form(""), cmd_type: str = Form(""), waits: str = Form("")):
    conn = get_conn()
    ids, _, _ = cota_campaign.parse_device_ids(device_ids)
    step_waits = [cota_run.parse_wait(w) for w in waits.split(",")] if waits.strip() else None
    # Refused, the page goes back to the form as it was — not to a list that has lost it.
    draft = {"name": name, "group_id": "", "devices": ", ".join(map(str, ids)),
             "commands": commands, "batch_size": batch_size, "rate_per_sec": rate_per_sec,
             "time_limit_minutes": time_limit_minutes, "answer_wait_seconds": answer_wait_seconds,
             "in_progress": in_progress, "device_type": device_type, "cmd_type": cmd_type,
             "waits": waits}

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
            answer_wait_seconds=_number(answer_wait_seconds, float, None),
            in_progress=_number(in_progress, int, None), waits=step_waits)
    except cota_campaign.CampaignError as exc:
        return refused(str(exc))
    cota_campaign.start(cid)
    return RedirectResponse(f"/cota/jobs/{cid}", status_code=303)


JOB_DEVICE_FILTERS = ("", "waiting", "ready", "queued", "done", "failed", "unreachable", "expired",
                      "not_started", "cancelled")


def _open_devices(text: str) -> list[int]:
    """Devices whose conversation is unfolded on the page — kept by the page, at most a page."""
    return [int(x) for x in (text or "").split(",") if x.strip().isdigit()][:50]


def _job_context(conn, request: Request, job_id: int, state: str = "", q: str = "",
                 page: int = 1, open_: str = "", sort: str = "serial", direction: str = "asc",
                 **extra) -> dict | None:
    job = cota_campaign.summary(conn, job_id)
    if not job:
        return None
    state = state if state in JOB_DEVICE_FILTERS else ""
    sort = sort if sort in cota_campaign.DEVICE_SORTS else "serial"
    direction = "desc" if direction == "desc" else "asc"
    rows, total = cota_campaign.device_rows(conn, job_id, state=state, search=q, page=page,
                                            sort=sort, direction=direction)
    shown = {r["device_id"] for r in rows}
    opened = {d: cota_campaign.device_conversation(conn, job_id, d)
              for d in _open_devices(open_) if d in shown}
    return _cota_context(conn, request, "jobs", job=job, grid=cota_campaign.grid(conn, job_id),
                         times=cota_campaign.timing(conn, job),
                         charts=cota_campaign.job_charts(conn, job_id, job),
                         devices=rows, devices_total=total, state=state, q=q,
                         sort=sort, direction=direction,
                         view_query=urlencode({"state": state, "q": q, "sort": sort, "dir": direction}),
                         page=max(1, page), pages=max(1, -(-total // 50)), opened=opened,
                         now_epoch=round(cota._parse(cota._now()).timestamp()),
                         stages=cota.STAGES, words=cota_campaign.RESULT_WORDS, **extra)


@app.get("/cota/jobs/{job_id}", response_class=HTMLResponse)
def cota_job(request: Request, job_id: int, state: str = "", q: str = "", page: int = 1,
             open_: str = Query("", alias="open"), sort: str = "serial", dir: str = "asc"):
    conn = get_conn()
    ctx = _job_context(conn, request, job_id, state, q, page, open_, sort, dir)
    if ctx is None:
        return RedirectResponse("/cota", status_code=303)
    return templates.TemplateResponse(request, "cota_job.html", ctx)


@app.get("/cota/jobs/{job_id}/status")
def cota_job_status(request: Request, job_id: int, state: str = "", q: str = "", page: int = 1,
                    open_: str = Query("", alias="open"), sort: str = "serial", dir: str = "asc"):
    """What the job page redraws every few seconds — this install's record only, no cloud call."""
    conn = get_conn()
    ctx = _job_context(conn, request, job_id, state, q, page, open_, sort, dir)
    if ctx is None:
        return JSONResponse({"ok": False})
    job = ctx["job"]
    return JSONResponse({"ok": True, "state": job["state"], "percent": job["percent"],
                         "name": job["name"], "answered": job["results"].get("done", 0),
                         "failed": job["results"].get("failed", 0), "total": job["total"],
                         "progress": templates.get_template("_cota_job_progress.html").render(ctx),
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


# ── groups, on the Devices page: one group per device (cota_fleet) ──

def _selection(conn, ids: str, scope: str, query: str) -> list[int]:
    """The devices an action is for: the ticked ones, or every device the page's filters match
    ("Select all N matching")."""
    if scope == "all":
        return cota_fleet.matching_ids(conn, cota_fleet.clean_filters(parse_qs(query)))
    return cota_campaign.parse_device_ids(ids)[0]


def _group_source(conn, source: str, devices: str, tag: str, ids: str, scope: str,
                  query: str) -> list[int]:
    """The devices a new group is made of: typed ids, every device with a tag, or the page's
    selection."""
    if source == "tag":
        if not tag.strip():
            raise cota_fleet.FleetError("Choose a tag.")
        return cota_fleet.tag_devices(conn, tag)
    if source == "selection":
        return _selection(conn, ids, scope, query)
    found, problems, _ = cota_campaign.parse_device_ids(devices)
    if problems:
        raise cota_fleet.FleetError("; ".join(problems[:5]) + (" …" if len(problems) > 5 else ""))
    return found


@app.post("/cota/groups/check")
def cota_groups_check(source: str = Form("ids"), devices: str = Form(""), tag: str = Form(""),
                      ids: str = Form(""), scope: str = Form(""), query: str = Form(""),
                      group_id: str = Form("")):
    """What a new group, or a move, would do — the dialog shows it before anything changes."""
    conn = get_conn()
    try:
        members = _group_source(conn, source, devices, tag, ids, scope, query)
    except cota_fleet.FleetError as exc:
        return JSONResponse({"ok": False, "message": str(exc)})
    if not members:
        return JSONResponse({"ok": False, "message": "No devices." if source != "tag"
                             else "No device has that tag."})
    out = cota_fleet.check_group(conn, members, int(group_id) if group_id.isdigit() else None)
    return JSONResponse({"ok": True, **out})


@app.post("/cota/groups", response_class=HTMLResponse)
def cota_groups_create(request: Request, name: str = Form(""), source: str = Form("ids"),
                       devices: str = Form(""), tag: str = Form(""), ids: str = Form(""),
                       scope: str = Form(""), query: str = Form(""), skip: str = Form(""),
                       transfer: bool = Form(False)):
    conn = get_conn()
    try:
        members = _group_source(conn, source, devices, tag, ids, scope, query)
        out = cota_fleet.create_group(conn, name, members,
                                      set(cota_campaign.parse_device_ids(skip)[0]), transfer)
    except cota_fleet.FleetError as exc:
        draft = {"name": name, "source": source, "devices": devices, "tag": tag}
        return templates.TemplateResponse(request, "cota_devices.html", _cota_devices_context(
            conn, request, result={"level": "error", "message": str(exc)}, group_draft=draft))
    return RedirectResponse(f"/cota/devices?group={out['group_id']}&note=grouped:{out['joined']}:"
                            f"{out['left']}:{out['elsewhere']}:{int(out['transfer'])}", status_code=303)


@app.post("/cota/groups/{group_id}/rename", response_class=HTMLResponse)
def cota_groups_rename(request: Request, group_id: int, name: str = Form("")):
    conn = get_conn()
    try:
        cota_fleet.rename_group(conn, group_id, name)
    except cota_fleet.FleetError as exc:
        return templates.TemplateResponse(request, "cota_devices.html", _cota_devices_context(
            conn, request, {"group": [str(group_id)]}, result={"level": "error", "message": str(exc)}))
    return RedirectResponse(f"/cota/devices?group={group_id}&note=renamed", status_code=303)


@app.post("/cota/groups/{group_id}/edit", response_class=HTMLResponse)
def cota_groups_edit(request: Request, group_id: int, name: str = Form(""), source: str = Form("ids"),
                     devices: str = Form(""), tag: str = Form(""), ids: str = Form(""),
                     scope: str = Form(""), query: str = Form(""), skip: str = Form(""),
                     transfer: bool = Form(False)):
    """Edit group (the user, 10-10-2026): a new name, and devices added in bulk — 1,000+ pasted,
    a tag, or the ticked rows — when picking them on the page is not practical. Either or both."""
    conn = get_conn()
    row = conn.execute("SELECT name FROM cota_group WHERE id = ?", (group_id,)).fetchone()
    if not row:
        return RedirectResponse("/cota/devices?note=none", status_code=303)
    try:
        wanted = bool(devices.strip()) if source == "ids" else (bool(tag.strip()) if source == "tag" else True)
        members = _group_source(conn, source, devices, tag, ids, scope, query) if wanted else []
        renamed = int(name.strip() != row["name"])
        if renamed:
            cota_fleet.rename_group(conn, group_id, name)
        joining = [d for d in members if d not in set(cota_campaign.parse_device_ids(skip)[0])]
        out = cota_fleet.add_to_group(conn, group_id, joining, transfer) if joining else \
            {"devices": 0, "elsewhere": 0, "transfer": transfer}
    except cota_fleet.FleetError as exc:
        draft = {"edit_id": group_id, "name": name, "source": source, "devices": devices, "tag": tag}
        return templates.TemplateResponse(request, "cota_devices.html", _cota_devices_context(
            conn, request, {"group": [str(group_id)]}, result={"level": "error", "message": str(exc)},
            group_draft=draft))
    return RedirectResponse(f"/cota/devices?group={group_id}&note=edited:{out['devices']}:"
                            f"{out['elsewhere']}:{int(out['transfer'])}:{renamed}", status_code=303)


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
    return RedirectResponse("/cota/devices?note=groupdeleted", status_code=303)


# ─── Intouch COTA: the command library ──────────────────────────────────────

LIBRARY_PAGE_SIZES = (5, 20, 50)


def _paged(rows: list, page: int, size: int) -> tuple[list, int, int, int]:
    """One page of a list, the page and size actually used, and the page count."""
    size = size if size in LIBRARY_PAGE_SIZES else 20
    pages = max(1, -(-len(rows) // size))
    page = min(max(1, page), pages)
    return rows[(page - 1) * size: page * size], page, size, pages


SAVED_SORTS = ("name", "parameter", "type", "tags")


def _sort_saved(rows: list[dict], key: str, direction: str) -> list[dict]:
    """Saved commands in a header's order; a row with nothing in that column goes last either way.
    "type" is the library's own order — GET, SET, CLR, then by name."""
    if key == "type":
        return sorted(rows, key=lambda r: (cota_library._KIND_ORDER.get(r["kind"], 9), r["name"].lower()),
                      reverse=direction == "desc")
    value = {"name": lambda r: r["name"],
             "parameter": lambda r: r["parameter_name"] or r["parameter"],
             "tags": lambda r: r["tags"]}[key]
    filled = sorted((r for r in rows if value(r)), key=lambda r: value(r).lower(),
                    reverse=direction == "desc")
    return filled + [r for r in rows if not value(r)]


def _commands_context(conn, request: Request, q: str = "", tag: str = "", edit: int = 0,
                      ppage: int = 1, psize: int = 20, cpage: int = 1, csize: int = 20,
                      csort: str = "type", cdir: str = "asc", **extra) -> dict:
    all_saved = cota_library.commands(conn)
    editing = next((c for c in all_saved if c["id"] == edit), None) if edit else None
    params = cota_library.parameters(conn, q=q)
    csort = csort if csort in SAVED_SORTS else "type"
    cdir = "desc" if cdir == "desc" else "asc"
    saved = _sort_saved(cota_library.commands(conn, q=q, tag=tag), csort, cdir)
    param_rows, ppage, psize, ppages = _paged(params, ppage, psize)
    saved_rows, cpage, csize, cpages = _paged(saved, cpage, csize)
    # Each pager carries the search and the other pager's place, so moving one keeps the rest.
    keep = {"q": q, "tag": tag, "csort": csort, "cdir": cdir}
    p_base = urlencode({**keep, "cpage": cpage, "csize": csize, "names": 1})
    c_base = urlencode({**keep, "ppage": ppage, "psize": psize})
    # Parameter names are folded away; they open when asked for, after working on one, or when a
    # search finds one.
    extra.setdefault("names_open", bool(q and params) or bool(extra.get("param_draft")))
    extra.setdefault("wait_choices", cota_run.WAIT_CHOICES)
    return _cota_context(conn, request, "commands",
                         parameters=param_rows, params_total=len(params),
                         params_all=len(cota_library.parameters(conn)),
                         ppage=ppage, psize=psize, ppages=ppages, p_base=p_base,
                         p_first=(ppage - 1) * psize,
                         saved=saved_rows, saved_total=len(saved), saved_all=len(all_saved),
                         cpage=cpage, csize=csize, cpages=cpages, c_base=c_base,
                         c_first=(cpage - 1) * csize, sizes=LIBRARY_PAGE_SIZES,
                         tags=cota_library.all_tags(conn), q=q, tag=tag, editing=editing,
                         csort=csort, cdir=cdir,
                         **extra)


@app.get("/cota/commands", response_class=HTMLResponse)
def cota_commands(request: Request, q: str = "", tag: str = "", edit: int = 0, ppage: int = 1,
                  psize: int = 20, cpage: int = 1, csize: int = 20, saved: int = 0,
                  imported: str = "", names: int = 0, csort: str = "type", cdir: str = "asc"):
    conn = get_conn()
    result = None
    if saved:
        # After a save: say so, and say if the same command is also saved under another name —
        # two names for one command can be on purpose, so it is said, not refused.
        row = conn.execute("SELECT name, val1 FROM cota_saved_command WHERE id = ?", (saved,)).fetchone()
        if row:
            twins = cota_library.same_command(conn, row["val1"], saved)
            result = {"level": "warn" if twins else "ok",
                      "message": f"Saved “{row['name']}”." + (
                          f" The same command is also saved as “{'”, “'.join(twins)}”." if twins else "")}
    elif imported:
        counts = dict(zip(("new", "updated", "same", "error"),
                          (int(x) if x.isdigit() else 0 for x in (imported.split(",") + ["0"] * 4)[:4])))
        result = {"level": "ok", "message": f"Imported: {counts['new']} new, {counts['updated']} "
                  f"updated, {counts['same']} already the same" + (
                      f", {counts['error']} left out with a problem." if counts["error"] else ".")}
    ctx = _commands_context(conn, request, q, tag, edit, ppage, psize, cpage, csize, csort, cdir,
                            result=result)
    ctx["names_open"] = ctx["names_open"] or bool(names)
    return templates.TemplateResponse(request, "cota_commands.html", ctx)


@app.post("/cota/commands/parameters", response_class=HTMLResponse)
def cota_commands_parameter(request: Request, code: str = Form(""), name: str = Form("")):
    conn = get_conn()
    try:
        saved = cota_library.save_parameter(conn, code, name)
        result = {"level": "ok", "message": f"Parameter {saved} is “{name.strip()}”."}
        draft = None
    except cota_library.LibraryError as exc:
        result = {"level": "error", "message": str(exc)}
        draft = {"code": code, "name": name}
    return templates.TemplateResponse(request, "cota_commands.html",
                                      _commands_context(conn, request, result=result,
                                                        param_draft=draft, names_open=True))


@app.post("/cota/commands/parameters/{code}/delete")
def cota_commands_parameter_delete(code: str):
    cota_library.delete_parameter(get_conn(), code)
    return RedirectResponse("/cota/commands?names=1#parameters", status_code=303)


@app.post("/cota/commands/saved", response_class=HTMLResponse)
def cota_commands_save(request: Request, command_id: str = Form(""), name: str = Form(""),
                       val1: str = Form(""), tags: str = Form(""), note: str = Form(""),
                       parameter_name: str = Form(""), wait_after: str = Form("")):
    """A saved command — and, in the same step, a name for its parameter when it has none yet
    (the user, 10-10-2026: one dialog, not two forms)."""
    conn = get_conn()
    editing = int(command_id) if command_id.isdigit() else None
    try:
        saved = cota_library.save_command(conn, name=name, val1=val1, tags=tags, note=note,
                                          command_id=editing,
                                          wait_after_seconds=cota_run.parse_wait(wait_after))
        code = cota_library.parameter_of("".join(val1.split()))
        if code and parameter_name.strip():
            cota_library.save_parameter(conn, code, parameter_name)
        return RedirectResponse(f"/cota/commands?saved={saved}#saved", status_code=303)
    except cota_library.LibraryError as exc:
        draft = {"id": editing, "name": name, "val1": val1, "tags": tags, "note": note,
                 "parameter_name": parameter_name, "wait_after_seconds": cota_run.parse_wait(wait_after)}
        return templates.TemplateResponse(request, "cota_commands.html", _commands_context(
            conn, request, result={"level": "error", "message": str(exc)}, draft=draft))


@app.post("/cota/commands/saved/{command_id}/delete")
def cota_commands_delete(command_id: int):
    cota_library.delete_command(get_conn(), command_id)
    return RedirectResponse("/cota/commands#saved", status_code=303)


@app.get("/cota/commands/export.csv")
def cota_commands_export():
    """The whole library as one CSV — to share with colleagues, and load into their install."""
    body = "﻿" + cota_library.export_csv(get_conn())
    return Response(content=body.encode("utf-8"), media_type=EXPORT_MEDIA["csv"], headers={
        "Content-Disposition": f'attachment; filename="{exports.timestamped("cota_commands", "csv")}"'})


@app.get("/cota/commands/template.csv")
def cota_commands_template():
    return Response(content=("﻿" + cota_library.TEMPLATE_CSV).encode("utf-8"),
                    media_type=EXPORT_MEDIA["csv"],
                    headers={"Content-Disposition": 'attachment; filename="cota_commands_template.csv"'})


@app.post("/cota/commands/import", response_class=HTMLResponse)
async def cota_commands_import(request: Request):
    """Read a shared file and show what it would change. Nothing is written until Apply."""
    form = await request.form()
    upload = form.get("file")
    content = await upload.read() if upload is not None and getattr(upload, "filename", "") else b""

    def build():
        conn = get_conn()
        if not content:
            return _commands_context(conn, request, result={
                "level": "error", "message": "Choose a file to import — export one from Commands, "
                                             "or start from the template."})
        plan = cota_library.plan_import(conn, content)
        if plan["error"]:
            return _commands_context(conn, request, result={"level": "error", "message": plan["error"]})
        return _commands_context(conn, request, import_plan=plan,
                                 import_content=content.decode("utf-8-sig", errors="replace"))

    ctx = await run_in_threadpool(build)
    return templates.TemplateResponse(request, "cota_commands.html", ctx)


@app.post("/cota/commands/import/apply")
def cota_commands_import_apply(content: str = Form("")):
    counts = cota_library.apply_import(get_conn(), content.encode("utf-8"))
    done = ",".join(str(counts.get(k, 0)) for k in ("new", "updated", "same", "error"))
    return RedirectResponse(f"/cota/commands?imported={done}", status_code=303)


def _plural(n: int, word: str) -> str:
    return f"{n:,} {word}{'' if n == 1 else 's'}"


def _devices_note(note: str) -> dict | None:
    """The notice after an action — the action redirects here, so a refresh repeats nothing."""
    kind, _, rest = note.partition(":")
    n = [int(x) if x.isdigit() else 0 for x in rest.split(":")] + [0, 0, 0]
    if kind == "added":
        text = f"{_plural(n[0], 'device')} added"
        text += f" · {n[1]:,} already listed" if n[1] else ""
        text += f" · {_plural(n[2], 'line')} left out with a problem" if n[2] else ""
        return {"level": "warn" if n[2] else "ok", "message": text + "."}
    messages = {
        "grouped": f"Group created with {_plural(n[0], 'device')}"
                   + (f" · {n[1]:,} left out" if n[1] else "")
                   + ((f" · {n[2]:,} moved from another group" if n[3] else
                       f" · {n[2]:,} also in another group") if n[2] else "") + ".",
        "joined": f"{_plural(n[0], 'device')} in the group"
                 + ((f" · {n[1]:,} moved from another group" if n[2] else
                     f" · {n[1]:,} also in another group") if n[1] else "") + ".",
        "ungrouped": f"{_plural(n[0], 'device')} taken out of the group.",
        "tagged": f"Tag added to {_plural(n[0], 'device')}.",
        "untagged": f"Tag removed from {_plural(n[0], 'device')}.",
        "deleted": f"{_plural(n[0], 'device')} removed from the list.",
        "renamed": "Group renamed.",
        "edited": ("Group renamed" if n[3] else "Group saved")
                  + (f" · {_plural(n[0], 'device')} added" if n[0] else "")
                  + ((f" · {n[1]:,} moved from another group" if n[2] else
                      f" · {n[1]:,} also in another group") if n[1] else "") + ".",
        "groupdeleted": "Group deleted. Its devices are now in no group.",
        "none": "No devices were selected.",
    }
    return {"level": "warn" if kind == "none" else "ok", "message": messages[kind]} if kind in messages else None


def _cota_devices_context(conn, request: Request, filters: dict | None = None, sort: str = "id",
                          direction: str = "asc", page: int = 1, size: int = 0, **extra) -> dict:
    filters = filters or {}
    sort = sort if sort in cota_fleet.SORTS else "id"
    direction = "desc" if direction == "desc" else "asc"
    listing = cota_fleet.list_devices(conn, filters, sort=sort, direction=direction, page=page,
                                      size=size or cota_fleet.DEFAULT_PAGE_SIZE)
    picked = filters.get("group") or []
    group = conn.execute("SELECT id, name FROM cota_group WHERE id = ?", (int(picked[0]),)).fetchone() \
        if len(picked) == 1 and picked[0].isdigit() else None
    base = {**filters, "sort": sort, "dir": direction, "size": listing["size"]}

    def link(extra: dict | None = None, path: str = "/cota/devices") -> str:
        """This view's URL with `extra` changed — repeated keys for a filter's several values."""
        return f"{path}?{urlencode({**base, **(extra or {})}, doseq=True)}"

    return _cota_context(
        conn, request, "devices", listing=listing, filters=filters, sort=sort, direction=direction,
        opts=cota_fleet.options(conn),
        group=dict(group) if group else None, sizes=cota_fleet.PAGE_SIZES, link=link,
        filter_query=urlencode(filters, doseq=True), **extra)


@app.get("/cota/devices", response_class=HTMLResponse)
def cota_devices(request: Request, sort: str = "id", dir: str = "asc", page: int = 1,
                 size: int = 0, note: str = ""):
    conn = get_conn()
    filters = cota_fleet.clean_filters(request.query_params)
    ctx = _cota_devices_context(conn, request, filters, sort, dir, page, size,
                                result=_devices_note(note) if note else None)
    return templates.TemplateResponse(request, "cota_devices.html", ctx)


@app.post("/cota/devices/add", response_class=HTMLResponse)
async def cota_devices_add(request: Request):
    """Devices by id — typed, pasted, or a CSV. Async only to await the file; the work goes to
    the threadpool (see "Two upload routes" in CLAUDE.md)."""
    form = await request.form()
    upload = form.get("file")
    content = await upload.read() if upload is not None and getattr(upload, "filename", "") else b""
    text, tags = str(form.get("devices", "")), str(form.get("tags", ""))

    def run():
        conn = get_conn()
        try:
            if content:
                out = cota_fleet.import_devices(conn, content)
                if tags.strip():
                    cota_fleet.add_tags(conn, [r[0] for r in cota_fleet.read_device_file(content)[0]], tags)
            else:
                out = cota_fleet.add_devices(conn, text, tags)
        except cota_fleet.FleetError as exc:
            return _cota_devices_context(conn, request, result={"level": "error", "message": str(exc)},
                                         add_draft={"devices": text, "tags": tags})
        return f"added:{out['added']}:{out['existing']}:{len(out.get('problems', []))}"

    out = await run_in_threadpool(run)
    if isinstance(out, str):
        return RedirectResponse(f"/cota/devices?sort=added&dir=desc&note={out}", status_code=303)
    return templates.TemplateResponse(request, "cota_devices.html", out)


def _back_to_devices(back: str, note: str) -> RedirectResponse:
    """Back to the page as it was — filters, sort and page — with the action's notice. Only ever
    to the Devices page: `back` comes from the form."""
    parts = urlsplit(back or "")
    if parts.path != "/cota/devices" or parts.netloc:
        parts = urlsplit("/cota/devices")
    query = [(k, v) for k, v in parse_qsl(parts.query) if k != "note"] + [("note", note)]
    return RedirectResponse(f"/cota/devices?{urlencode(query)}", status_code=303)


@app.post("/cota/devices/action", response_class=HTMLResponse)
def cota_devices_action(request: Request, action: str = Form(""), ids: str = Form(""),
                        scope: str = Form(""), query: str = Form(""), group_id: str = Form(""),
                        tag: str = Form(""), back: str = Form(""), transfer: bool = Form(False),
                        from_group: str = Form("")):
    """What the selection bar does to the ticked devices — or to every device the filters match."""
    conn = get_conn()
    selected = _selection(conn, ids, scope, query)
    if not selected:
        return _back_to_devices(back, "none")
    try:
        if action in ("add", "move") and group_id.isdigit():
            out = cota_fleet.add_to_group(conn, int(group_id), selected, transfer or action == "move")
            return _back_to_devices(back, f"joined:{out['devices']}:{out['elsewhere']}:{int(out['transfer'])}")
        if action == "ungroup":
            gone = cota_fleet.remove_from_groups(conn, selected,
                                                 int(from_group) if from_group.isdigit() else None)
            return _back_to_devices(back, f"ungrouped:{gone}")
        if action == "tag":
            return _back_to_devices(back, f"tagged:{cota_fleet.add_tags(conn, selected, tag)}")
        if action == "untag":
            return _back_to_devices(back, f"untagged:{cota_fleet.remove_tags(conn, selected, tag)}")
        if action == "delete":
            return _back_to_devices(back, f"deleted:{cota_fleet.delete_devices(conn, selected)}")
        if action == "newjob":
            return templates.TemplateResponse(request, "cota_job_new.html", _new_job_context(
                conn, request, draft={"devices": ", ".join(str(d) for d in selected)}))
        if action == "export":
            wanted = set(selected)
            rows = [r for r in cota_fleet.export_rows(conn, {}) if r["device_id"] in wanted]
            return _devices_file(rows, "csv")
    except cota_fleet.FleetError as exc:
        return templates.TemplateResponse(request, "cota_devices.html", _cota_devices_context(
            conn, request, result={"level": "error", "message": str(exc)}))
    return _back_to_devices(back, "none")


def _devices_file(rows: list[dict], format: str) -> Response:
    stem = "cota_devices"
    if format == "xlsx":
        return Response(content=exports.to_xlsx(rows, cota_fleet.EXPORT_COLUMNS, sheet_name="Devices"),
                        media_type=EXPORT_MEDIA["xlsx"], headers={
                            "Content-Disposition": f'attachment; filename="{exports.timestamped(stem, "xlsx")}"'})
    body = "\ufeff" + exports.to_csv(rows, cota_fleet.EXPORT_COLUMNS)
    return Response(content=body.encode("utf-8"), media_type=EXPORT_MEDIA["csv"], headers={
        "Content-Disposition": f'attachment; filename="{exports.timestamped(stem, "csv")}"'})


@app.get("/cota/devices/export")
def cota_devices_export(request: Request, format: str = "csv", sort: str = "id", dir: str = "asc"):
    """Every device the filters match, sorted as on screen."""
    conn = get_conn()
    filters = cota_fleet.clean_filters(request.query_params)
    return _devices_file(cota_fleet.export_rows(conn, filters, sort=sort, direction=dir), format)


@app.get("/cota/devices/template.csv")
def cota_devices_template():
    """Add device's file format: the cloud's device list, with tags."""
    return Response(content=("\ufeff" + cota_fleet.TEMPLATE_CSV).encode("utf-8"),
                    media_type=EXPORT_MEDIA["csv"],
                    headers={"Content-Disposition": 'attachment; filename="cota_add_devices_template.csv"'})


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
    conn = cota_connection.load()
    cota_connection.sign_out(conn)
    kept = bool(conn.username and cota_connection.load_password(conn.username))
    return templates.TemplateResponse(request, "cota_signin.html", _cota_signin_context(
        request, result={"level": "ok", "message": "Signed out. Your password stays saved — press "
                                                   "Sign in to sign in again." if kept else "Signed out."}))


@app.post("/cota/signin/forget", response_class=HTMLResponse)
def cota_forget_password(request: Request):
    """Remove the remembered password from the credential store — sign-out keeps it."""
    conn = cota_connection.load()
    cota_connection.forget_password(conn.username)
    return templates.TemplateResponse(request, "cota_signin.html", _cota_signin_context(
        request, result={"level": "ok", "message": "The saved password was removed."}))


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
               notice=f"Cleared {removed} recorded error{'' if removed == 1 else 's'}.")
    return templates.TemplateResponse(request, "errors.html", ctx)


@app.get("/quality", response_class=HTMLResponse)
def quality(request: Request, snapshot: int | None = None):
    conn = get_conn()
    ctx = page_context(conn, request, snapshot)
    ctx.update(issues=metrics.quality_issues(conn, ctx["snapshot_id"]))
    return templates.TemplateResponse(request, "quality.html", ctx)


@app.get("/devices", response_class=HTMLResponse)
def devices(request: Request, snapshot: int | None = None, sort: str = "seen", dir: str = "desc",
            page: int = 1, size: int = 0, page_size: int = 0):
    """Every device of the snapshot — the Intouch COTA Devices list's design (10-10-2026): each
    column filtered as in Excel and sorted from its header, the view kept in the URL."""
    conn = get_conn()
    ctx = page_context(conn, request, snapshot)
    sid = ctx["snapshot_id"]
    filters = fota_devices.clean_filters(request.query_params)
    sort = sort if sort in fota_devices.SORTABLE else fota_devices.DEFAULT_SORT
    direction = "asc" if dir == "asc" else "desc"
    size = size or page_size
    size = size if size in fota_devices.PAGE_SIZES else fota_devices.DEFAULT_PAGE_SIZE
    page = max(1, page)
    rows, total = fota_devices.rows(conn, sid, filters, sort=sort, direction=direction,
                                    limit=size, offset=(page - 1) * size)
    pages = max(1, -(-total // size))
    if page > pages:                     # past the end: the last page, not an empty one
        page = pages
        rows, total = fota_devices.rows(conn, sid, filters, sort=sort, direction=direction,
                                        limit=size, offset=(page - 1) * size)

    # A snapshot in the URL pins every link to it, so it is carried only while viewing history.
    base = {**({"snapshot": sid} if ctx["viewing_history"] else {}), **filters,
            "sort": sort, "dir": direction, "size": size}

    def link(extra: dict | None = None, path: str = "/devices") -> str:
        """This view's URL with `extra` changed — repeated keys for a filter's several values."""
        merged = {k: v for k, v in {**base, **(extra or {})}.items() if v not in (None, "", [])}
        return f"{path}?{urlencode(merged, doseq=True)}"

    keep = {k: v for k, v in base.items() if k not in filters}
    ctx.update(rows=rows, total=total, page=page, pages=pages, size=size,
               clear_link=f"/devices?{urlencode(keep, doseq=True)}", keep=keep,
               first=(page - 1) * size, sizes=fota_devices.PAGE_SIZES, filters=filters,
               sort=sort, direction=direction, link=link,
               opts=fota_devices.options(conn, sid) if sid else {"choices": {}, "all": 0},
               fallbacks=fota_devices.FALLBACKS, task_labels=fota_devices.TASK_LABELS)
    return templates.TemplateResponse(request, "devices.html", ctx)


# Rows per page. Offered rather than fixed because the useful number depends on the job:
# 25 to read carefully, 100 to scan for a pattern.
PAGE_SIZES = (25, 50, 100)
DEFAULT_PAGE_SIZE = 50


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


